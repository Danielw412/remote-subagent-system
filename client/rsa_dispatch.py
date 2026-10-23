"""rsa dispatch: user-authorized Full Access workers for orchestrator agents.

T3 Code never lets a thread start another thread with broader modes than its own, so an
orchestrator (Codex, Claude Code) in Auto mode cannot start an unattended worker. This
dispatcher is a separate T3 client that the *user* authorizes:

- It signs in through T3's outside-agent MCP connection (`/mcp`, OAuth with PKCE). The user
  approves it on T3's own page and picks the access ceiling there ("Full access").
- The user approves profiles that name a repository (a T3 project), the sandbox provider,
  the models, the runtime mode and limits. Within a profile, `rsa dispatch submit` starts an
  ordinary top-level T3 thread on the sandboxed OpenCode provider, in a new git worktree,
  with no further approvals. Anything else is refused, or needs the user's click in a
  native dialog. Rules are enforced here, not in prompts.

Trust model (see README "Full Access dispatch"):
- The T3 credential exists only in this daemon's memory and in a DPAPI-protected vault.
  No command or endpoint returns it; the CLI that orchestrators run never holds it.
- policy.json and jobs.json are HMAC-signed with a key from the vault; a changed file is
  refused, which denies everything (fail closed).
- Granting more (profiles, enabling, one-off exceptions) needs a click in a Windows dialog
  shown by the daemon. Tightening (remove, disable, deauthorize) takes effect at once.
- Threads only run on a provider instance served by the rsa bridge (the Linux sandbox);
  host providers are refused whatever the policy says. Only threads this dispatcher
  started can be messaged or interrupted through it.
- All of this runs as the user's own Windows account. A process of that account that
  deliberately decrypts the vault (or uses T3's own CLI) is outside what this can stop.
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import itertools
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

core = None    # rsa.py: RsaError, CFG, read_prompt
bridge = None  # rsa_bridge.py: bridge_url(), running_status(), T3_SETTINGS, spawn/autostart helpers

HOME = Path(os.environ.get("RSA_DISPATCH_HOME") or Path.home() / ".rsa" / "dispatch")
VAULT = HOME / "vault.bin"
POLICY = HOME / "policy.json"
JOBS = HOME / "jobs.json"
CLIENT_KEY = HOME / "client.key"
LOG_FILE = HOME / "dispatch.log"
PID_FILE = HOME / "dispatch.pid"
T3_RUNTIME = Path.home() / ".t3" / "userdata" / "server-runtime.json"

CLIENT_NAME = "rsa dispatch (Linux sandbox workers)"
REDIRECT_PATH = "/rsa-dispatch/callback"
DPAPI_ENTROPY = b"rsa-dispatch-vault-v1"
MCP_PROTOCOL = "2025-06-18"
# The only T3 tools the daemon ever calls. There is no generic pass-through.
T3_TOOLS = {"t3_project_list", "t3_thread_launch", "t3_thread_read", "t3_thread_list", "t3_thread_send",
            "t3_thread_interrupt", "t3_thread_wait"}

MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
ACTIVE_T3 = {"preparing", "queued", "starting", "running", "waiting"}
RUNNING = {"launching", "active"}
JOB_RE = re.compile(r"^d\d{8}-\d{6}-[0-9a-f]{4}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@#+-]{1,200}$")
PATTERN_RE = re.compile(r"^[A-Za-z0-9._:/@#+*?-]{1,200}$")
REF_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/@+-]{1,200}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PROMPT = 100_000  # T3 accepts 120000 characters per message
LIMITS = {  # profile field: (default, min, max)
    "max_concurrent": (1, 1, 8),
    "max_runtime_minutes": (60, 1, 1440),
    "max_followups": (10, 0, 100),
    "max_jobs_per_day": (20, 1, 500),
}
DEFAULT_POLICY = {"version": 1, "enabled": True, "max_concurrent": 2, "profiles": {}}
DIALOG_TIMEOUT = 300
EXIT_DENIED = 4
PROMPT_HEADER = ("[rsa dispatch job {id}: you run unattended in a Linux sandbox. Nobody can answer questions or "
                 "approve anything during this run, so do not ask; make reasonable assumptions and say which. "
                 "Work only in this checkout. End with a short summary of what you changed and how you "
                 "verified it.]\n\n")

_log_lock = threading.Lock()


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{threading.current_thread().name}] {msg}"
    with _log_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if sys.stderr and sys.stderr.isatty():
            print(line, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age(iso):
    """Seconds since an ISO-8601 UTC timestamp (ours or T3's, with or without milliseconds)."""
    try:
        t = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return time.time() - t.timestamp()


def norm(p):
    return os.path.normcase(os.path.realpath(p)) if p else ""


class DispatchError(Exception):
    def __init__(self, message, code="error", status=400, **extra):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


def denied(message, violations=()):
    return DispatchError(message, "denied", 403, violations=list(violations))


class T3Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Tampered(Exception):
    pass


# ------------------------------------------------------------------ Windows: DPAPI and the trusted dialog

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi(data, protect):
    """Encrypt/decrypt with the user's DPAPI key: unreadable to other accounts and offline copies."""
    if os.name != "nt":
        raise DispatchError("rsa dispatch keeps its T3 credential with Windows DPAPI and runs on Windows only")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    keep = []

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        keep.append(buf)
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    src, ent, out = blob(data), blob(DPAPI_ENTROPY), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise DispatchError(f"DPAPI {'protect' if protect else 'unprotect'} failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


_dialog_lock = threading.Lock()


def confirm(title, text, timeout=DIALOG_TIMEOUT):
    """Ask the user in a native dialog shown by the daemon. Only OK approves; Cancel, closing it
    and the timeout deny. Nothing in the dispatch API can answer it."""
    if os.name != "nt":
        return False
    if not _dialog_lock.acquire(blocking=False):
        raise DispatchError("another rsa dispatch confirmation is already waiting for the user", "busy", 409)
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        caption = f"rsa dispatch: {title} [{secrets.token_hex(3)}]"
        box = {}
        # OK/Cancel, Cancel is the default button, system-modal, topmost, brought to the foreground
        flags = 0x1 | 0x30 | 0x100 | 0x1000 | 0x10000 | 0x40000

        def show():
            box["answer"] = user32.MessageBoxW(None, text, caption, flags)

        log(f"asking the user: {title}")
        t = threading.Thread(target=show, name="confirm", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            hwnd = user32.FindWindowW(None, caption)
            if hwnd:
                user32.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE: counts as Cancel
            t.join(10)
            log(f"no answer within {timeout}s, denied: {title}")
            return False
        ok = box.get("answer") == 1  # IDOK
        log(f"user {'approved' if ok else 'denied'}: {title}")
        return ok
    finally:
        _dialog_lock.release()


def open_browser(url):
    if os.name == "nt":
        os.startfile(url)
    else:
        import webbrowser
        webbrowser.open(url)


# ------------------------------------------------------------------ protected state

class Vault:
    """DPAPI-protected secrets: the T3 credential and the key that signs policy and jobs."""

    def __init__(self):
        self.data = {"mac_key": secrets.token_hex(32), "t3": None}
        if VAULT.exists():
            try:
                self.data = json.loads(dpapi(VAULT.read_bytes(), protect=False))
            except (DispatchError, ValueError) as e:
                raise DispatchError(f"cannot read {VAULT} ({e}). If it is damaged, delete it, then authorize "
                                    "again and re-add profiles.")
        else:
            self.save()

    @property
    def key(self):
        return bytes.fromhex(self.data["mac_key"])

    @property
    def t3(self):
        return self.data.get("t3")

    def set_t3(self, creds):
        self.data["t3"] = creds
        self.save()

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = VAULT.with_suffix(".tmp")
        tmp.write_bytes(dpapi(json.dumps(self.data).encode(), protect=True))
        os.replace(tmp, VAULT)


def mac(obj, key):
    body = json.dumps({k: v for k, v in obj.items() if k != "mac"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def write_signed(path, obj, key):
    data = {**{k: v for k, v in obj.items() if k != "mac"}, "mac": mac(obj, key)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_signed(path, key):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        raise Tampered(f"{path.name} is not valid JSON")
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("mac", "")), mac(data, key)):
        raise Tampered(f"{path.name} was changed outside rsa dispatch")
    data.pop("mac", None)
    return data


# ------------------------------------------------------------------ T3 Code: outside-agent MCP + OAuth

def t3_origin():
    """Origin of the running T3 server, from T3's own runtime file. Loopback only: the bearer
    credential is never sent anywhere else."""
    try:
        origin = json.loads(T3_RUNTIME.read_text(encoding="utf-8"))["origin"]
    except (OSError, ValueError, KeyError, TypeError):
        raise T3Error("t3_down", "T3 Code is not running (no server-runtime.json)")
    u = urllib.parse.urlsplit(str(origin))
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1") or u.path not in ("", "/"):
        raise T3Error("t3_unreachable", f"refusing a non-loopback T3 origin: {origin!r}")
    return f"http://{u.netloc}"


def http_json(url, method="GET", body=None, headers=None, timeout=30):
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
    except ConnectionRefusedError:
        raise T3Error("t3_down", f"T3 Code is not running (nothing listens on {u.netloc})")
    except (OSError, http.client.HTTPException) as e:
        raise T3Error("t3_unreachable", f"cannot reach T3 Code at {u.netloc}: {e}")
    finally:
        conn.close()
    try:
        return r.status, json.loads(data or b"{}")
    except ValueError:
        return r.status, {"error": data[:300].decode("utf-8", "replace")}


def t3_environment(origin):
    status, env = http_json(origin + "/.well-known/t3/environment")
    if status != 200 or not env.get("environmentId"):
        raise T3Error("t3_unreachable", f"{origin} does not look like a T3 Code server")
    return env


class _SessionLost(Exception):
    pass


class T3:
    """MCP client (streamable HTTP) for T3's `/mcp` endpoint, using the user-approved credential."""

    def __init__(self, vault):
        self.vault = vault
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.session, self.connected_to = None, None

    def creds(self):
        c = self.vault.t3
        if not c:
            raise T3Error("unauthorized", "rsa dispatch is not authorized in T3 Code yet (the user runs "
                                          "`rsa dispatch authorize`)")
        if age(c["expires_at"]) > 0:
            raise T3Error("unauthorized", f"the dispatcher's T3 authorization expired on {c['expires_at']}; "
                                          "the user renews it with `rsa dispatch authorize`")
        return c

    def reset(self):
        with self.lock:
            self.session, self.connected_to = None, None

    def _post(self, origin, token, msg, timeout):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + token, "MCP-Protocol-Version": MCP_PROTOCOL}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        u = urllib.parse.urlsplit(origin)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        try:
            conn.request("POST", "/mcp", body=json.dumps(msg).encode(), headers=headers)
            r = conn.getresponse()
            if r.getheader("mcp-session-id"):
                self.session = r.getheader("mcp-session-id")
            if r.status == 401:
                r.read()
                raise T3Error("unauthorized", "T3 Code rejected the dispatcher's credential (expired or revoked); "
                                              "the user renews it with `rsa dispatch authorize`")
            if r.status in (400, 404) and "Mcp-Session-Id" in headers:
                body = r.read()
                if r.status == 404 or b"session" in body.lower():
                    raise _SessionLost()
                raise T3Error("t3_error", f"T3 MCP endpoint answered HTTP 400: {body[:300].decode('utf-8', 'replace')}")
            if r.status in (202, 204):
                r.read()
                return []
            if r.status >= 400:
                raise T3Error("t3_error", f"T3 MCP endpoint answered HTTP {r.status}: "
                                          f"{r.read()[:300].decode('utf-8', 'replace')}")
            if "text/event-stream" not in (r.getheader("content-type") or ""):
                data = r.read()
                obj = json.loads(data) if data.strip() else []
                return obj if isinstance(obj, list) else [obj]
            out, data = [], []
            while True:  # SSE: stop at the reply to this request instead of waiting for the stream to close
                line = r.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    out.append(json.loads(b"\n".join(data)))
                    data = []
                    if any(isinstance(m, dict) and m.get("id") == msg.get("id") for m in out):
                        break
            if data:
                out.append(json.loads(b"\n".join(data)))
            return out
        except ConnectionRefusedError:  # nothing was sent
            raise T3Error("t3_down", "T3 Code is not running (connection refused)")
        except (OSError, http.client.HTTPException) as e:  # the request may have been processed
            raise T3Error("t3_unreachable", f"lost the connection to T3 Code: {e}")
        except ValueError:
            raise T3Error("t3_error", "T3 sent an unreadable MCP response")
        finally:
            conn.close()

    def _connect(self, creds, origin):
        if t3_environment(origin)["environmentId"] != creds["environment_id"]:
            raise T3Error("t3_mismatch", f"the T3 server at {origin} is not the one the dispatcher was authorized "
                                         "for; the user runs `rsa dispatch authorize` again")
        self.session = None
        mid = next(self.ids)
        reply = self._post(origin, creds["token"], {
            "jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "rsa-dispatch", "version": core.VERSION}}}, 30)
        init = next((m for m in reply if isinstance(m, dict) and m.get("id") == mid), None)
        if not init or "result" not in init:
            raise T3Error("t3_error", f"T3 MCP initialize failed: {(init or {}).get('error')}")
        self._post(origin, creds["token"], {"jsonrpc": "2.0", "method": "notifications/initialized"}, 30)
        self.connected_to = origin

    def call(self, tool, args, timeout=60):
        if tool not in T3_TOOLS:
            raise T3Error("t3_error", f"tool {tool} is not used by rsa dispatch")
        creds = self.creds()
        for _ in range(2):
            origin = t3_origin()
            with self.lock:
                if self.connected_to != origin:
                    self._connect(creds, origin)
                session = self.session
            mid = next(self.ids)
            try:
                msgs = self._post(origin, creds["token"], {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                                                           "params": {"name": tool, "arguments": args}}, timeout)
            except _SessionLost:  # T3 restarted: start a new MCP session
                with self.lock:
                    if self.session == session:
                        self.session, self.connected_to = None, None
                continue
            resp = next((m for m in msgs if isinstance(m, dict) and m.get("id") == mid), None)
            if resp is None:
                raise T3Error("t3_error", f"no reply from T3 to {tool}")
            if "error" in resp:
                raise T3Error("t3_error", str((resp["error"] or {}).get("message") or resp["error"]))
            res = resp.get("result") or {}
            text = next((c.get("text") for c in res.get("content") or []
                         if isinstance(c, dict) and c.get("text")), "")
            if res.get("isError"):
                try:
                    f = json.loads(text)
                except ValueError:
                    f = None
                if isinstance(f, dict):
                    raise T3Error(str(f.get("code") or "t3_error"), str(f.get("message") or text))
                raise T3Error("t3_error", text or f"{tool} failed")
            if isinstance(res.get("structuredContent"), dict):
                return res["structuredContent"]
            try:
                return json.loads(text)
            except ValueError:
                return {"text": text}
        raise T3Error("t3_error", "could not re-establish the MCP session with T3")


class Authorization:
    """One OAuth sign-in: register, send the user to T3's approval page, receive the code on a
    loopback redirect, exchange it. The token goes straight into the vault."""

    def __init__(self, dispatcher):
        self.d = dispatcher
        self.state, self.error, self.url, self.access = "pending", None, None, None
        self.lock = threading.Lock()
        origin = t3_origin()
        self.origin, self.env = origin, t3_environment(origin)
        status, meta = http_json(origin + "/.well-known/oauth-authorization-server")
        endpoints = [str(meta.get(k, "")) for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")]
        if status != 200 or not all(e.startswith(origin + "/") for e in endpoints):
            raise T3Error("t3_error", "T3 Code did not publish its MCP sign-in endpoints")
        self.authorize_url, self.token_url, register_url = endpoints
        # T3 matches loopback redirects by host and path, so any local port works later (RFC 8252).
        status, reg = http_json(register_url, "POST", json.dumps({
            "client_name": CLIENT_NAME, "redirect_uris": [f"http://127.0.0.1{REDIRECT_PATH}"],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
            "response_types": ["code"]}).encode(), {"Content-Type": "application/json"})
        if status not in (200, 201) or not reg.get("client_id"):
            raise T3Error("t3_error", f"T3 refused to register the dispatcher: {reg}")
        self.client_id = reg["client_id"]
        self.verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        self.nonce = secrets.token_urlsafe(24)
        self.server = LoopbackServer(("127.0.0.1", 0), _CallbackHandler)
        self.server.flow = self
        self.redirect = f"http://127.0.0.1:{self.server.server_address[1]}{REDIRECT_PATH}"
        self.url = self.authorize_url + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": self.nonce,
            "resource": origin + "/mcp"})
        self.expires = time.time() + 900
        threading.Thread(target=self.serve, name="oauth-callback", daemon=True).start()

    def serve(self):
        self.server.timeout = 5
        while self.state == "pending" and time.time() < self.expires and not self.d.stopping.is_set():
            self.server.handle_request()
        if self.state == "pending":
            self.state, self.error = "failed", "the sign-in was not completed within 15 minutes"
        self.server.server_close()

    def finish(self, params):
        with self.lock:
            if self.state != "pending":
                return False, "This sign-in has already finished."
            if not hmac.compare_digest(params.get("state", ""), self.nonce):
                return False, "Unexpected sign-in response (state mismatch)."
            if params.get("iss") and params["iss"] != self.origin:
                self.state, self.error = "failed", "the sign-in response came from another issuer"
                return False, self.error
            if params.get("error"):
                self.state = "failed"
                self.error = f"T3 did not approve the dispatcher: {params.get('error_description') or params['error']}"
                return False, self.error
            try:
                self.exchange(params.get("code", ""))
            except (T3Error, DispatchError) as e:
                self.state, self.error = "failed", str(e)
                return False, self.error
            self.state = "done"
            return True, f"rsa dispatch is connected to T3 Code with {self.access} access. You can close this tab."

    def exchange(self, code):
        status, tok = http_json(self.token_url, "POST", urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect,
            "client_id": self.client_id, "code_verifier": self.verifier, "resource": self.origin + "/mcp"}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if status != 200 or not tok.get("access_token"):
            raise T3Error("t3_error", f"token exchange failed: {tok.get('error_description') or tok}")
        scopes = set(str(tok.get("scope", "")).split())
        # T3 grants settings:write only with "full-access"; operate alone means a lower mode ceiling.
        self.access = ("full-access" if "settings:write" in scopes else
                       "limited (below full-access)" if "orchestration:operate" in scopes else "read-only")
        expires = datetime.fromtimestamp(time.time() + int(tok.get("expires_in") or 0), timezone.utc)
        self.d.vault.set_t3({
            "token": tok["access_token"], "scopes": sorted(scopes), "access": self.access,
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), "authorized_at": now_iso(),
            "origin": self.origin, "environment_id": self.env["environmentId"],
            "environment_label": self.env.get("label"), "client_id": self.client_id})
        self.d.t3.reset()
        log(f"authorized by the user in T3 ({self.access}, expires {expires:%Y-%m-%d})")

    def view(self):
        return {"state": self.state, "url": self.url if self.state == "pending" else None,
                "error": self.error, "access": self.access}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path != REDIRECT_PATH:
            self.send_error(404)
            return
        ok, text = self.server.flow.finish(dict(urllib.parse.parse_qsl(query)))
        body = (f"<!doctype html><meta charset=utf-8><title>rsa dispatch</title>"
                f"<p style='font:16px sans-serif;margin:40px'>{'&#10003; ' if ok else ''}"
                f"{text.replace('&', '&amp;').replace('<', '&lt;')}</p>").encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ the dispatcher

def git_out(repo, *args):
    p = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", repo, *args], capture_output=True,
                       timeout=30, **bridge.WIN_FLAGS)
    return p.stdout.decode("utf-8", "replace").strip() if p.returncode == 0 else ""


def text_field(req, name, limit, required=False):
    v = req.get(name)
    if v is None or v == "":
        if required:
            raise DispatchError(f"{name} is required", "invalid")
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise DispatchError(f"{name} must be a string of at most {limit} characters", "invalid")
    return v


def matched(value, regex, name):
    if value is not None and not regex.match(value):
        raise DispatchError(f"invalid {name}: {value!r}", "invalid")
    return value


class Dispatcher:
    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.started = time.time()
        self.vault = Vault()
        self.t3 = T3(self.vault)
        self.flow, self.port = None, None
        self.launching = set()  # job ids with a launch call in flight (in this process)
        if not CLIENT_KEY.exists():
            CLIENT_KEY.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        self.client_key = CLIENT_KEY.read_text(encoding="utf-8").strip()
        self.policy, self.policy_error = dict(DEFAULT_POLICY, profiles={}), None
        try:
            self.policy = read_signed(POLICY, self.vault.key) or self.policy
        except Tampered as e:
            self.policy_error = (f"the policy failed its integrity check ({e}); every request is denied until the "
                                 "user approves a policy change (`rsa dispatch profile add ...`), which replaces it")
            log(f"POLICY REJECTED: {e}")
        try:
            self.jobs = (read_signed(JOBS, self.vault.key) or {}).get("jobs", {})
        except Tampered as e:
            aside = JOBS.with_name(f"jobs.rejected-{int(time.time())}.json")
            os.replace(JOBS, aside)
            log(f"JOB REGISTRY REJECTED: {e}; moved to {aside.name}. Those threads stay in T3 but are no longer "
                "managed by rsa dispatch.")
            self.jobs = {}

    # -- persistence
    def save_policy(self):
        with self.lock:
            self.policy["version"] = 1
            write_signed(POLICY, self.policy, self.vault.key)

    def save_jobs(self):
        with self.lock:
            keep = sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)
            keep = [j for i, j in enumerate(keep) if j["state"] in RUNNING or (i < 500 and age(j["created_at"]) < 30 * 86400)]
            self.jobs = {j["id"]: j for j in keep}
            write_signed(JOBS, {"version": 1, "jobs": self.jobs}, self.vault.key)

    # -- hard rules (no profile, no dialog can override these)
    def sandbox_provider_error(self, provider):
        """Only provider instances served by the rsa bridge run dispatched work: their tools execute in
        the Linux sandbox. A host provider in Full Access would be arbitrary command execution here."""
        try:
            settings = json.loads(bridge.T3_SETTINGS.read_text(encoding="utf-8"))
            inst = (settings.get("providerInstances") or {}).get(provider) or {}
        except (OSError, ValueError, AttributeError):
            inst = {}
        url = bridge.bridge_url()
        if (inst.get("driver") != "opencode" or not inst.get("enabled", True)
                or str((inst.get("config") or {}).get("serverUrl", "")).rstrip("/") != url):
            return (f"provider instance {provider!r} is not served by the rsa bridge at {url}; dispatched workers "
                    "only run in the Linux sandbox")
        return None

    def hard_errors(self, provider, mode):
        errs = []
        if self.policy_error:
            errs.append(self.policy_error)
        if not self.policy.get("enabled", True):
            errs.append("dispatching is disabled by the user")
        try:
            creds = self.t3.creds()
            if mode == "full-access" and creds.get("access") != "full-access":
                errs.append(f"the user approved this dispatcher in T3 with {creds.get('access')} access; Full "
                            "Access workers need `rsa dispatch authorize` again, choosing Full access")
        except T3Error as e:
            errs.append(str(e))
        err = self.sandbox_provider_error(provider)
        if err:
            errs.append(err)
        st = bridge.running_status()
        if not (st and st.get("upstream") == "connected"):
            errs.append("the rsa bridge is not connected to the sandbox server (the user runs `rsa bridge start`; "
                        "see `rsa bridge status`)")
        return errs

    # -- profiles
    def resolve_profile(self, name, repo):
        profiles = self.policy["profiles"]
        if name:
            p = profiles.get(name)
            if not p:
                raise denied(f"there is no approved profile named {name!r}", [f"unknown profile {name}"])
            if repo and norm(repo) != norm(p["repo"]):
                raise denied(f"profile {name!r} covers {p['repo']}, not {repo}", [f"repository {repo}"])
            return name, p
        if not repo:
            raise DispatchError("pass --profile, or run inside (or pass --repo) an approved repository", "invalid")
        hits = [(n, p) for n, p in sorted(profiles.items()) if norm(p["repo"]) == norm(repo)]
        if not hits:
            raise denied(f"no approved profile covers {repo}; only the user can add one "
                         "(`rsa dispatch profile add`)", [f"repository {repo} has no profile"])
        if len(hits) > 1:
            raise DispatchError(f"several profiles cover {repo} ({', '.join(n for n, _ in hits)}); pass --profile",
                                "invalid")
        return hits[0]

    def running_jobs(self, exclude=None):
        return [j for j in self.jobs.values() if j["state"] in RUNNING and j["id"] != exclude]

    def limit_violations(self, name, p, exclude=None, starting=True):
        v, active = [], self.running_jobs(exclude)
        if starting and len(active) >= int(self.policy.get("max_concurrent", 2)):
            v.append(f"{len(active)} dispatched job(s) are running (global limit {self.policy['max_concurrent']})")
        mine = [j for j in active if j["profile"] == name]
        if starting and len(mine) >= p["max_concurrent"]:
            v.append(f"{len(mine)} job(s) of profile {name} are running (profile limit {p['max_concurrent']})")
        return v

    def build_profile(self, req):
        name = matched(text_field(req, "name", 40, True), PROFILE_RE, "profile name")
        repo = text_field(req, "repo", 400, True)
        repo = os.path.realpath(repo)
        if not os.path.isdir(repo) or norm(git_out(repo, "rev-parse", "--show-toplevel")) != norm(repo):
            raise DispatchError(f"{repo} is not the top-level directory of a git checkout", "invalid")
        projects, cursor = [], 0
        while cursor is not None and len(projects) < 2000:
            page = self.t3.call("t3_project_list", {"cursor": cursor, "limit": 100})
            projects += page.get("projects") or []
            cursor = page.get("nextCursor")
        project = next((pr for pr in projects if norm(pr.get("workspaceRoot")) == norm(repo)), None)
        if not project:
            raise DispatchError(f"{repo} is not a T3 project; add it in T3 Code first (or `t3 project add {repo}`)",
                                "invalid")
        provider = matched(req.get("provider") or bridge.T3_INSTANCE_ID, PROVIDER_RE, "provider")
        err = self.sandbox_provider_error(provider)
        if err:
            raise DispatchError(err, "invalid")
        mode = req.get("runtime_mode") or "full-access"
        if mode not in MODES:
            raise DispatchError(f"runtime mode must be one of {', '.join(MODES)}", "invalid")
        models = req.get("models") or []
        if not isinstance(models, list) or not models or len(models) > 20:
            raise DispatchError("give 1 to 20 allowed models (`--model provider/model`, globs allowed)", "invalid")
        for m in models:
            matched(m, PATTERN_RE, "model pattern")
        default = req.get("default_model") or next((m for m in models if not any(c in m for c in "*?")), None)
        if not default or not MODEL_RE.match(default) or not any(fnmatchcase(default, m) for m in models):
            raise DispatchError("pass --default-model, a concrete model that the allowed models match", "invalid")
        profile = {"repo": repo, "project_id": project["id"], "project_title": project.get("title"),
                   "provider": provider, "models": models, "default_model": default, "runtime_mode": mode,
                   "approved_at": now_iso()}
        for key, (dflt, lo, hi) in LIMITS.items():
            val = req.get(key, dflt)
            if not isinstance(val, int) or isinstance(val, bool) or not lo <= val <= hi:
                raise DispatchError(f"{key} must be an integer from {lo} to {hi}", "invalid")
            profile[key] = val
        return name, profile

    def change_policy(self, req):
        op = req.get("op")
        if op in ("profile-remove", "disable"):  # tightening: no confirmation needed
            with self.lock:
                if op == "disable":
                    self.policy["enabled"] = False
                elif self.policy["profiles"].pop(str(req.get("name")), None) is None:
                    raise DispatchError(f"no profile named {req.get('name')!r}", "not_found", 404)
                if not self.policy_error:
                    self.save_policy()
            log(f"policy: {op} {req.get('name') or ''}")
            return {"changed": op, "confirmed_by_user": False}
        if op == "profile-add":
            name, p = self.build_profile(req)
            replacing = name in self.policy["profiles"]
            text = (f"Allow orchestrator agents to start unattended worker threads WITHOUT asking you, "
                    f"within this profile?\n\nProfile: {name}{' (replaces the existing one)' if replacing else ''}\n"
                    f"Repository: {p['repo']}\nT3 project: {p['project_title']}\n"
                    f"Worker: {p['provider']} (rsa bridge -> sandbox container on the Linux host)\n"
                    f"Mode: {p['runtime_mode']}"
                    + (": shell commands, package installs, edits and internet access inside the sandbox, "
                       "with no approvals" if p["runtime_mode"] == "full-access" else "") +
                    f"\nModels: {', '.join(p['models'])} (default {p['default_model']})\n"
                    f"Limits: {p['max_concurrent']} running job(s), {p['max_runtime_minutes']} min per turn, "
                    f"{p['max_followups']} follow-ups per job, {p['max_jobs_per_day']} jobs per day\n\n"
                    "Each job gets a new git worktree of this repository (branch rsa-dispatch/<job>). "
                    "Nothing is merged automatically. Jobs share the sandbox container with other "
                    f"{p['provider']} threads.")
            desc = f"profile {name}"
        elif op == "enable":
            text, desc = "Re-enable rsa dispatch, so orchestrators can start workers within your profiles?", "enable"
        elif op == "max-concurrent":
            n = req.get("value")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 16:
                raise DispatchError("max_concurrent must be an integer from 1 to 16", "invalid")
            if n <= int(self.policy.get("max_concurrent", 2)) and not self.policy_error:
                with self.lock:
                    self.policy["max_concurrent"] = n
                    self.save_policy()
                return {"changed": op, "confirmed_by_user": False}
            text, desc = f"Allow up to {n} dispatched jobs to run at the same time?", f"max_concurrent {n}"
        else:
            raise DispatchError(f"unknown policy operation {op!r}", "invalid")
        if self.policy_error:
            text += "\n\nNOTE: the saved policy failed its integrity check. Approving replaces it with only this change."
        if not confirm(f"approve {desc}", text + "\n\nOK = approve.   Cancel = deny."):
            raise denied(f"the user did not approve {desc}")
        with self.lock:
            if self.policy_error:
                self.policy, self.policy_error = dict(DEFAULT_POLICY, profiles={}), None
            if op == "profile-add":
                self.policy["profiles"][name] = p
            elif op == "enable":
                self.policy["enabled"] = True
            else:
                self.policy["max_concurrent"] = req["value"]
            self.save_policy()
        log(f"policy: {desc} approved by the user")
        return {"changed": op, "confirmed_by_user": True, **({"profile": {name: p}} if op == "profile-add" else {})}

    # -- jobs
    def default_base(self, p, req):
        cwd_repo, cwd_branch = req.get("cwd_repo"), req.get("cwd_branch")
        if cwd_branch and cwd_repo and norm(cwd_repo) == norm(p["repo"]) and REF_RE.match(cwd_branch):
            return cwd_branch
        head = git_out(p["repo"], "rev-parse", "--abbrev-ref", "HEAD")
        if head and head != "HEAD" and REF_RE.match(head):
            return head
        return git_out(p["repo"], "rev-parse", "HEAD") or "HEAD"

    def submit(self, req):
        prompt = text_field(req, "prompt", MAX_PROMPT, True)
        if not prompt.strip():
            raise DispatchError("prompt is empty", "invalid")
        title = " ".join((text_field(req, "title", 200) or prompt.strip().splitlines()[0]).split())[:80]
        model = matched(text_field(req, "model", 200), MODEL_RE, "model")
        base = matched(text_field(req, "base", 200), REF_RE, "base ref")
        key = matched(text_field(req, "key", 100), KEY_RE, "key")
        requester = " ".join((text_field(req, "requester", 80) or "").split()) or None
        repo = text_field(req, "repo", 400) or (None if req.get("profile") else text_field(req, "cwd_repo", 400))
        name = matched(text_field(req, "profile", 40), PROFILE_RE, "profile name")
        self.refresh_running()
        with self.lock:
            hard = [self.policy_error] if self.policy_error else []
            if hard:
                raise denied("rsa dispatch refuses all requests", hard)
            name, p = self.resolve_profile(name, repo)
            model = model or p["default_model"]
            digest = hashlib.sha256(json.dumps([name, model, base, title, prompt]).encode()).hexdigest()
            for j in self.jobs.values():
                if key and j.get("key") == key:
                    if j["request_hash"] != digest:
                        raise DispatchError(f"key {key!r} was already used for a different request (job {j['id']})",
                                            "conflict", 409)
                    return {**self.view(j), "duplicate": True}
                if not key and j["request_hash"] == digest and j["state"] in RUNNING:
                    return {**self.view(j), "duplicate": True}
            hard = self.hard_errors(p["provider"], p["runtime_mode"])
            if hard:
                raise denied("rsa dispatch cannot start this job", hard)
            soft = []
            if not any(fnmatchcase(model, m) for m in p["models"]):
                soft.append(f"model {model} is not allowed by profile {name} ({', '.join(p['models'])})")
            soft += self.limit_violations(name, p)
            today = [j for j in self.jobs.values() if j["profile"] == name and age(j["created_at"]) < 86400]
            if len(today) >= p["max_jobs_per_day"]:
                soft.append(f"profile {name} already started {len(today)} job(s) in 24 hours "
                            f"(limit {p['max_jobs_per_day']})")
            if soft and not req.get("ask"):
                raise denied("this request is outside the user's approved profile and nothing was started. Ask the "
                             "user to widen the profile, or retry with --ask to request a one-time approval",
                             soft)
            job = None if soft else self.new_job(name, p, model, base or self.default_base(p, req), title, key,
                                                 requester, digest, False)
        if job is None:
            text = (f"An orchestrator asks to start an unattended worker OUTSIDE your profile {name!r}:\n\n"
                    + "\n".join(f" - {s}" for s in soft) +
                    f"\n\nRepository: {p['repo']}\nWorker: {p['provider']} / {model} in {p['runtime_mode']} mode\n"
                    f"Title: {title}\nRequested by: {requester or 'unknown'}\n\nTask (start):\n"
                    + "\n".join("    " + line[:120] for line in prompt.strip().splitlines()[:8]) +
                    "\n\nOK = allow this one job.   Cancel = deny.")
            if not confirm("approve one job", text):
                raise denied("the user did not approve this job", soft)
            with self.lock:
                hard = self.hard_errors(p["provider"], p["runtime_mode"])
                if hard:
                    raise denied("rsa dispatch cannot start this job", hard)
                job = self.new_job(name, p, model, base or self.default_base(p, req), title, key, requester,
                                   digest, True)
        self.launch(job, prompt)
        return self.view(job)

    def new_job(self, name, p, model, base, title, key, requester, digest, approved):
        jid = "d" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        job = {"id": jid, "state": "launching", "profile": name, "repo": p["repo"], "project_id": p["project_id"],
               "provider": p["provider"], "model": model, "runtime_mode": p["runtime_mode"], "title": title,
               "base": base, "branch": f"rsa-dispatch/{jid}", "worktree": None, "thread_id": None,
               "key": key, "request_hash": digest, "requester": requester, "approved_by_user": approved,
               "created_at": now_iso(), "finished_at": None, "followups": 0, "send_keys": [],
               "max_runtime_minutes": p["max_runtime_minutes"], "t3_status": None, "error": None, "note": None}
        self.jobs[jid] = job
        self.launching.add(jid)
        self.save_jobs()
        log(f"job {jid}: profile {name}, {p['provider']}/{model}, {p['runtime_mode']}, base {base}"
            + (" (one-off approval by the user)" if approved else "") + f", requested by {requester or '?'}")
        return job

    def launch(self, job, prompt):
        jid = job["id"]
        args = {"projectId": job["project_id"], "title": f"{job['title']} · {jid}",
                "modelSelection": {"instanceId": job["provider"], "model": job["model"]},
                "runtimeMode": job["runtime_mode"], "interactionMode": "default",
                "workspaceStrategy": {"type": "worktree", "baseRef": job["base"], "branch": job["branch"],
                                      "startFromOrigin": False},
                "message": PROMPT_HEADER.format(id=jid) + prompt}
        try:
            res = self.t3.call("t3_thread_launch", args, timeout=300)
            with self.lock:
                job["thread_id"] = res.get("threadId")
                job["state"] = "active"
        except T3Error as e:
            if e.code == "t3_unreachable":  # the request may or may not have reached T3
                job["note"] = f"launch outcome unknown ({e}); rsa dispatch keeps checking T3"
            else:
                with self.lock:
                    job["state"], job["error"], job["finished_at"] = "failed", f"launch failed: {e}", now_iso()
                log(f"job {jid}: launch failed: {e}")
        finally:
            self.launching.discard(jid)
            self.save_jobs()
        if job["state"] == "launching":
            self.reconcile(job)
        if not job.get("thread_id"):
            return
        log(f"job {jid}: thread {job['thread_id']}")
        self.refresh(job)
        if job.get("mismatch"):  # T3 started it somewhere else than asked: never let that run
            self.interrupt(job, "rsa dispatch: thread does not match the approved profile")
            log(f"job {jid}: INTERRUPTED, {job['error']}")
        elif job.get("thread_runtime_mode") and job["thread_runtime_mode"] != job["runtime_mode"]:
            job["note"] = f"T3 runs this thread in {job['thread_runtime_mode']} mode"
        if job.get("cancel_requested"):
            self.interrupt(job, "cancelled via rsa dispatch")
        self.save_jobs()

    def reconcile(self, job):
        """A launch whose reply was lost (or a daemon restart mid-launch): find the thread by the job id
        in its title instead of launching twice."""
        if job["id"] in self.launching:
            return
        try:
            r = self.t3.call("t3_thread_list", {"projectId": job["project_id"], "titleContains": job["id"],
                                                "limit": 5})
        except T3Error as e:
            job["note"] = f"cannot check T3 for the launch yet: {e}"
            return
        hit = next((t for t in r.get("threads") or [] if job["id"] in str(t.get("title"))), None)
        with self.lock:
            if hit:
                job["thread_id"], job["state"], job["note"] = hit["threadId"], "active", None
                log(f"job {job['id']}: found its thread {hit['threadId']} after an interrupted launch")
            elif age(job["created_at"]) > 600:
                job["state"], job["finished_at"] = "failed", now_iso()
                job["error"] = "the launch did not complete (no thread was created)"
        self.save_jobs()

    def refresh(self, job):
        """Update a job from its T3 thread. Also re-checks that the thread is still the one this
        dispatcher started (same project and sandbox provider)."""
        if not job.get("thread_id"):
            return
        try:
            r = self.t3.call("t3_thread_read", {"threadId": job["thread_id"], "limit": 1, "runLimit": 1,
                                                "maxCharsPerItem": 1}, timeout=30)
        except T3Error as e:
            with self.lock:
                if e.code == "thread_not_found":
                    job["state"], job["note"] = "gone", "the thread was deleted in T3"
                    job["finished_at"] = job["finished_at"] or now_iso()
                else:
                    job["note"] = f"could not read the thread: {e}"
            return
        th, runs = r.get("thread") or {}, r.get("recentRuns") or []
        if th.get("projectId") != job["project_id"] or th.get("providerInstanceId") != job["provider"]:
            with self.lock:
                job["state"], job["mismatch"], job["finished_at"] = "gone", True, now_iso()
                job["error"] = (f"the T3 thread runs on {th.get('providerInstanceId')} in project {th.get('projectId')},"
                                f" not {job['provider']} in the profile's project")
            return
        run = runs[0] if runs else {}
        status = th.get("status")
        with self.lock:
            job.update(t3_status=status, worktree=th.get("worktreePath") or job["worktree"],
                       branch=th.get("branch") or job["branch"], pending_requests=th.get("pendingRequestCount") or 0,
                       thread_runtime_mode=th.get("runtimeMode"), items=th.get("itemCount") or 0,
                       run={"id": run.get("runId"), "status": run.get("status"), "started_at": run.get("startedAt"),
                            "completed_at": run.get("completedAt")})
            if status in ACTIVE_T3:
                state = "active"
            elif status == "completed":
                state = "completed"
            elif status in ("failed", "rolled_back"):
                state = "failed"
            elif status in ("interrupted", "cancelled"):
                state = "cancelled" if job.get("cancel_requested") else "interrupted"
            else:  # idle: still being prepared right after the launch, or nothing ran
                state = "active" if age(job["created_at"]) < 180 and not run else "idle"
            if job.get("sent_at") and state not in RUNNING:
                # T3 may still report the previous run for a moment after a follow-up was queued
                if run.get("runId") == job.get("sent_after_run") and age(job["sent_at"]) < 180:
                    state = "active"
            if run.get("runId") != job.get("sent_after_run"):
                job.pop("sent_at", None)
            changed = state != job["state"]
            if changed:
                job["state"] = state
                job["finished_at"] = None if state in RUNNING else now_iso()
                log(f"job {job['id']}: {state} (T3 status {status})")
        if changed and state not in RUNNING:
            self.fetch_last_message(job)

    def fetch_last_message(self, job):
        try:
            r = self.t3.call("t3_thread_read", {
                "threadId": job["thread_id"], "view": "messages", "limit": 100, "maxCharsPerItem": 8000,
                "afterPosition": max(0, int(job.get("items") or 0) - 150)}, timeout=30)
        except T3Error as e:
            log(f"job {job['id']}: could not read the last message: {e}")
            return
        msgs = [i for i in r.get("items") or [] if i.get("type") == "assistant_message" and i.get("text")]
        if msgs:
            with self.lock:
                job["last_message"] = msgs[-1]["text"] + ("\n[... truncated; read the thread in T3]"
                                                          if msgs[-1].get("textTruncated") else "")

    def refresh_running(self):
        for job in list(self.jobs.values()):
            if job["state"] in RUNNING:
                if job.get("thread_id"):
                    self.refresh(job)
                else:
                    self.reconcile(job)
        self.save_jobs()

    def enforce_runtime(self, job):
        p = self.policy["profiles"].get(job["profile"]) or job
        limit = int(p.get("max_runtime_minutes") or job["max_runtime_minutes"])
        run = job.get("run") or {}
        if (job["state"] == "active" and run.get("started_at") and not run.get("completed_at")
                and run.get("id") != job.get("limit_run") and age(run["started_at"]) > limit * 60):
            job["limit_run"] = run["id"]
            job["note"] = f"turn interrupted: it ran longer than max_runtime_minutes={limit}"
            log(f"job {job['id']}: {job['note']}")
            self.interrupt(job, f"rsa dispatch: turn exceeded max_runtime_minutes={limit}")

    def interrupt(self, job, reason):
        n = job.setdefault("interrupts", 0) + 1
        job["interrupts"] = n
        self.t3.call("t3_thread_interrupt", {"threadId": job["thread_id"], "reason": reason,
                                             "clientRequestId": f"rsa-dispatch:{job['id']}:interrupt:{n}"})

    def monitor(self):
        threading.current_thread().name = "monitor"
        while not self.stopping.wait(10 if self.running_jobs() else 60):
            for job in list(self.jobs.values()):
                if job["state"] not in RUNNING or job["id"] in self.launching:
                    continue
                try:
                    if not job.get("thread_id"):
                        self.reconcile(job)
                        continue
                    self.refresh(job)
                    self.enforce_runtime(job)
                except Exception as e:  # keep monitoring whatever one job hits
                    log(f"monitor: job {job['id']}: {e!r}")
            try:
                self.save_jobs()
            except OSError as e:
                log(f"monitor: cannot save jobs: {e}")

    def job(self, jid):
        if not JOB_RE.match(str(jid)) or jid not in self.jobs:
            raise DispatchError(f"no rsa dispatch job {jid!r} (only jobs started by rsa dispatch can be used)",
                                "not_found", 404)
        return self.jobs[jid]

    def status_of(self, jid):
        job = self.job(jid)
        if job["state"] in RUNNING and jid not in self.launching:
            if job.get("thread_id"):
                self.refresh(job)
            else:
                self.reconcile(job)
            self.save_jobs()
        return self.view(job)

    def wait(self, jid, timeout):
        job = self.job(jid)
        deadline = time.time() + max(0, min(int(timeout or 0), 600))
        while True:
            if job.get("thread_id"):
                self.refresh(job)
            else:
                self.reconcile(job)
            left = deadline - time.time()
            if job["state"] not in RUNNING or left <= 0:
                break
            t0 = time.time()
            if job.get("thread_id") and job.get("t3_status") != "waiting":
                try:
                    self.t3.call("t3_thread_wait", {"threadId": job["thread_id"], "timeoutMs": int(min(left, 25) * 1000)},
                                 timeout=min(left, 25) + 30)
                except T3Error:
                    pass
            time.sleep(max(0, min(deadline - time.time(), 2 - (time.time() - t0))))  # at most one round per 2 s
        self.save_jobs()
        return self.view(job)

    def send(self, jid, req):
        message = text_field(req, "message", MAX_PROMPT, True)
        key = matched(text_field(req, "key", 100), KEY_RE, "key")
        job = self.job(jid)
        if job.get("thread_id"):
            self.refresh(job)
        with self.lock:
            if key and key in job["send_keys"]:
                return {**self.view(job), "duplicate": True}
            p = self.policy["profiles"].get(job["profile"])
            hard = self.hard_errors(job["provider"], job["runtime_mode"])
            if not p:
                hard.append(f"profile {job['profile']} was removed, so its jobs take no more messages")
            if not job.get("thread_id") or job["state"] == "gone":
                hard.append(f"job {jid} has no T3 thread to message ({job['state']})")
            if job.get("cancel_requested"):
                hard.append(f"job {jid} was cancelled")
            if hard:
                raise denied("rsa dispatch cannot send this follow-up", hard)
            soft = []
            if job["followups"] >= p["max_followups"]:
                soft.append(f"job {jid} already had {job['followups']} follow-up(s) (limit {p['max_followups']})")
            soft += self.limit_violations(job["profile"], p, exclude=jid, starting=job["state"] not in RUNNING)
            if soft and not req.get("ask"):
                raise denied("this follow-up is outside the user's approved profile; retry with --ask to request a "
                             "one-time approval", soft)
        if soft:
            text = (f"An orchestrator asks to send another message to rsa dispatch job {jid} ({job['title']}), "
                    "outside your profile:\n\n" + "\n".join(f" - {s}" for s in soft) + "\n\nMessage (start):\n"
                    + "\n".join("    " + line[:120] for line in message.strip().splitlines()[:8])
                    + "\n\nOK = allow this message.   Cancel = deny.")
            if not confirm("approve one follow-up", text):
                raise denied("the user did not approve this follow-up", soft)
        n = job["followups"] + 1
        res = self.t3.call("t3_thread_send", {"threadId": job["thread_id"], "message": message, "mode": "queue",
                                              "clientRequestId": f"rsa-dispatch:{jid}:send:{key or n}"})
        with self.lock:
            job["followups"] = n
            if key:
                job["send_keys"] = (job["send_keys"] + [key])[-50:]
            job["sent_at"], job["sent_after_run"] = now_iso(), (job.get("run") or {}).get("id")
            job["state"], job["finished_at"], job["last_message"] = "active", None, None
            job["note"] = f"follow-up {n} {res.get('delivery') or 'sent'}"
        log(f"job {jid}: follow-up {n} ({res.get('delivery')})")
        self.save_jobs()
        return self.view(job)

    def cancel(self, jid):
        job = self.job(jid)
        with self.lock:
            job["cancel_requested"] = True
        self.save_jobs()
        if job.get("thread_id") and job["state"] in RUNNING:
            self.interrupt(job, "cancelled via rsa dispatch")
            log(f"job {jid}: cancel requested")
            for _ in range(10):
                time.sleep(1)
                self.refresh(job)
                if job["state"] not in RUNNING:
                    break
        self.save_jobs()
        return self.view(job)

    def view(self, job):
        keys = ("id", "state", "t3_status", "profile", "repo", "provider", "model", "runtime_mode", "title", "base",
                "branch", "worktree", "thread_id", "created_at", "finished_at", "followups", "requester",
                "approved_by_user", "pending_requests", "note", "error", "last_message")
        v = {k: job.get(k) for k in keys}
        tid = job.get("thread_id")
        if tid:
            v["link"] = f"t3-thread://v1/{tid}"
            v["markdown_link"] = f"[{job['title']} ({job['id']})](t3-thread://v1/{tid})"
        v["needs_user"] = job.get("t3_status") == "waiting" or bool(job.get("pending_requests"))
        p = self.policy["profiles"].get(job["profile"])
        v["followups_left"] = max(0, p["max_followups"] - job["followups"]) if p else 0
        return v

    def list_jobs(self, everything):
        jobs = sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)
        if not everything:
            jobs = [j for j in jobs if j["state"] in RUNNING or age(j["created_at"]) < 86400]
        return [self.view(j) for j in jobs[:200]]

    # -- authorization
    def authorize(self):
        with self.lock:
            if self.flow and self.flow.state == "pending" and time.time() < self.flow.expires:
                return self.flow.view()
            self.flow = Authorization(self)
        log("sign-in started; waiting for the user to approve it in T3 Code")
        try:
            open_browser(self.flow.url)
        except OSError as e:
            log(f"could not open a browser: {e}")
        return self.flow.view()

    def deauthorize(self):
        creds = self.vault.t3
        self.vault.set_t3(None)
        self.t3.reset()
        log("authorization removed by request")
        return {"removed": bool(creds), "note": "the local credential is deleted. T3 keeps the session until it "
                                                "expires; revoke it now in T3 Settings > Connections or with "
                                                "`t3 auth session list` / `t3 auth session revoke <id>`."}

    def status(self):
        c = self.vault.t3 or {}
        days = None if not c else round(-age(c["expires_at"]) / 86400, 1)
        st = bridge.running_status()
        return {
            "dispatcher": "running", "version": core.VERSION, "pid": os.getpid(), "port": self.port,
            "uptime_sec": int(time.time() - self.started),
            "authorization": {"authorized": bool(c) and days > 0, "access": c.get("access"),
                              "expires_at": c.get("expires_at"), "days_left": days,
                              "t3_environment": c.get("environment_label"), "origin": c.get("origin")},
            "policy": {"enabled": self.policy.get("enabled", True), "integrity_error": self.policy_error,
                       "max_concurrent": self.policy.get("max_concurrent"),
                       "profiles": sorted(self.policy["profiles"])},
            "bridge": "connected" if st and st.get("upstream") == "connected" else ("running" if st else "stopped"),
            "jobs": {"running": len(self.running_jobs()), "known": len(self.jobs)},
            "pending_sign_in": self.flow.view() if self.flow and self.flow.state == "pending" else None}

    def profiles(self):
        return {"enabled": self.policy.get("enabled", True), "max_concurrent": self.policy.get("max_concurrent"),
                "integrity_error": self.policy_error, "profiles": self.policy["profiles"]}


# ------------------------------------------------------------------ local API (127.0.0.1 only)

class Api(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rsa-dispatch"

    def log_message(self, *args):
        pass

    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        port = self.server.server_address[1]
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False  # DNS rebinding
        if self.headers.get("Origin") or (self.headers.get("Sec-Fetch-Site") or "none").lower() not in (
                "none", "same-origin"):
            return False  # browsers
        want = "Bearer " + self.server.dispatcher.client_key
        return hmac.compare_digest((self.headers.get("Authorization") or "").encode(), want.encode())

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise DispatchError("request too large", "invalid")
        if not n:
            return {}
        try:
            obj = json.loads(self.rfile.read(n))
        except ValueError:
            raise DispatchError("request body is not JSON", "invalid")
        if not isinstance(obj, dict):
            raise DispatchError("request body must be a JSON object", "invalid")
        return obj

    def handle_api(self):
        if not self.allowed():
            return self.reply(403, {"ok": False, "error": "forbidden", "code": "forbidden"})
        d = self.server.dispatcher
        path, _, query = self.path.partition("?")
        parts = [p for p in path.split("/") if p]
        m = self.command
        try:
            body = self.body() if m == "POST" else {}
            if parts[:1] != ["v1"]:
                raise DispatchError("not found", "not_found", 404)
            route = parts[1:]
            if route == ["status"] and m == "GET":
                out = d.status()
            elif route == ["profiles"] and m == "GET":
                out = d.profiles()
            elif route == ["jobs"] and m == "GET":
                out = {"jobs": d.list_jobs("all=1" in query)}
            elif route == ["jobs"] and m == "POST":
                out = {"job": d.submit(body)}
            elif len(route) == 2 and route[0] == "jobs" and m == "GET":
                out = {"job": d.status_of(route[1])}
            elif len(route) == 3 and route[0] == "jobs" and m == "POST" and route[2] in ("wait", "send", "cancel"):
                jid, act = route[1], route[2]
                out = {"job": d.wait(jid, body.get("timeout") or 0) if act == "wait" else
                       d.send(jid, body) if act == "send" else d.cancel(jid)}
            elif route == ["policy"] and m == "POST":
                out = d.change_policy(body)
            elif route == ["authorize"]:
                out = {"sign_in": d.authorize() if m == "POST" else (d.flow.view() if d.flow else {"state": "none"})}
            elif route == ["deauthorize"] and m == "POST":
                out = d.deauthorize()
            elif route == ["shutdown"] and m == "POST":
                self.reply(200, {"ok": True, "dispatcher": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                raise DispatchError("not found", "not_found", 404)
            self.reply(200, {"ok": True, **out})
        except DispatchError as e:
            self.reply(e.status, {"ok": False, "error": str(e), "code": e.code, **e.extra})
        except T3Error as e:
            self.reply(502, {"ok": False, "error": str(e), "code": e.code})
        except Exception as e:
            import traceback
            log(f"error on {m} {path}: {e!r}\n{traceback.format_exc()}")
            self.reply(500, {"ok": False, "error": f"internal error: {e}", "code": "error"})

    do_GET = do_POST = handle_api


class LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse a second listener on the port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def port():
    return int(os.environ.get("RSA_DISPATCH_PORT") or core.CFG.get("dispatch_port") or 47961)


def serve():
    """Create the daemon's dispatcher and API server (not yet serving); tests drive it in-process."""
    d = Dispatcher()
    d.port = port()
    try:
        srv = LoopbackServer(("127.0.0.1", d.port), Api)
    except OSError as e:
        raise core.RsaError(f"cannot listen on 127.0.0.1:{d.port}: {e} (is rsa dispatch already running?)")
    srv.dispatcher = d
    return srv


def cmd_run(a):
    threading.current_thread().name = "main"
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5_000_000:
        os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
    try:
        srv = serve()
    except DispatchError as e:
        log(f"cannot start: {e}")
        raise core.RsaError(str(e))
    d = srv.dispatcher
    PID_FILE.write_text(str(os.getpid()))
    log(f"rsa dispatch listening on 127.0.0.1:{d.port}; policy: {len(d.policy['profiles'])} profile(s)"
        + (f", REJECTED ({d.policy_error})" if d.policy_error else "")
        + f"; authorized: {bool(d.vault.t3)}")
    threading.Thread(target=d.monitor, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        d.stopping.set()
        PID_FILE.unlink(missing_ok=True)
        log("rsa dispatch stopped")
    return 0


# ------------------------------------------------------------------ CLI (what orchestrators and the user run)

class Denied(Exception):
    def __init__(self, reply):
        super().__init__(reply.get("error"))
        self.reply = reply


def api(method, path, body=None, timeout=60):
    try:
        key = CLIENT_KEY.read_text(encoding="utf-8").strip()
    except OSError:
        raise core.RsaError("rsa dispatch is not set up on this machine (the user starts it with `rsa dispatch start`)")
    conn = http.client.HTTPConnection("127.0.0.1", port(), timeout=timeout)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(),
                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        r = conn.getresponse()
        reply = json.loads(r.read() or b"{}")
    except ConnectionRefusedError:
        raise core.RsaError("rsa dispatch is not running (the user starts it with `rsa dispatch start`)")
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise core.RsaError(f"rsa dispatch: {e}")
    finally:
        conn.close()
    if reply.get("ok"):
        return reply
    if reply.get("code") == "denied":
        raise Denied(reply)
    if reply.get("code") == "forbidden":
        raise core.RsaError("rsa dispatch refused the request (client key mismatch: restart it with `rsa dispatch restart`)")
    raise core.RsaError(reply.get("error") or "rsa dispatch error")


def running():
    try:
        return api("GET", "/v1/status", timeout=5)
    except core.RsaError:
        return None


def checkout_info():
    """Main checkout and branch of the current directory's repository (for --repo/--base defaults)."""
    common = git_out(".", "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None, None, False
    main = os.path.dirname(os.path.normpath(common)) if os.path.basename(os.path.normpath(common)) == ".git" else None
    branch = git_out(".", "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = git_out(".", "rev-parse", "HEAD")
    return main, branch, bool(git_out(".", "status", "--porcelain"))


def exit_for(job):
    if job["state"] in RUNNING:
        return core.EXIT_PENDING
    return 0 if job["state"] == "completed" else core.EXIT_FAILED


def print_job(j):
    print(f"Job {j['id']}  [{j['state']}{'/' + j['t3_status'] if j.get('t3_status') else ''}]")
    rows = [("title", j.get("title")), ("thread", j.get("markdown_link")),
            ("profile", f"{j['profile']}  ({j['repo']})"),
            ("worker", f"{j['provider']} / {j['model']}, {j['runtime_mode']}"),
            ("workspace", f"{j.get('worktree') or '(being prepared)'}  branch {j.get('branch')}  from {j.get('base')}"),
            ("created", j.get("created_at")), ("finished", j.get("finished_at")),
            ("follow-ups", f"{j['followups']} sent, {j['followups_left']} left"),
            ("note", j.get("note")), ("error", j.get("error"))]
    if j.get("needs_user"):
        rows.append(("ATTENTION", "the thread is waiting for the user in T3"))
    if j.get("approved_by_user"):
        rows.append(("approval", "started with a one-off approval by the user"))
    for k, v in rows:
        if v:
            print(f"  {k + ':':<11}{v}")
    if j.get("last_message"):
        print("  last message:")
        for line in j["last_message"].splitlines():
            print(f"    {line}")


def show(a, reply, key="job"):
    obj = reply.get(key, reply)
    if getattr(a, "json", False):
        print(json.dumps(obj, indent=2))
    elif key == "job":
        print_job(obj)
    return obj


def read_message(a):
    if not (a.file or a.prompt) and sys.stdin.isatty():
        raise core.RsaError("no message: pass it as an argument or with -f FILE")
    return core.read_prompt(a)


def cmd_submit(a):
    prompt = read_message(a)
    main, branch, dirty = checkout_info()
    req = {"prompt": prompt, "title": a.title, "model": a.model, "base": a.base, "key": a.key,
           "requester": a.requester, "ask": a.ask, "profile": a.profile,
           "repo": os.path.realpath(a.repo) if a.repo else None, "cwd_repo": main, "cwd_branch": branch}
    if dirty and not a.base and not a.json:
        print("note: uncommitted changes in this checkout are NOT sent; the worker starts from the committed "
              "base branch", file=sys.stderr)
    reply = api("POST", "/v1/jobs", req, timeout=DIALOG_TIMEOUT + 360)
    job = reply["job"]
    if a.wait is not None:
        return wait_for(job["id"], a.wait, a.json)
    show(a, reply)
    if not a.json:
        print(f"\n  {'(duplicate of an earlier submission) ' if job.get('duplicate') else ''}"
              f"follow: rsa dispatch wait {job['id']}  |  rsa dispatch status {job['id']}")
    return core.EXIT_FAILED if job["state"] in ("failed", "gone") else 0


def wait_for(jid, timeout, as_json):
    deadline = time.time() + timeout if timeout and timeout > 0 else None
    while True:
        chunk = 240 if deadline is None else max(0, min(240, int(deadline - time.time())))
        job = api("POST", f"/v1/jobs/{jid}/wait", {"timeout": chunk}, timeout=chunk + 120)["job"]
        if job["state"] not in RUNNING or (deadline is not None and time.time() >= deadline):
            break
        if not as_json:
            print(f"... {jid} {job['state']}/{job.get('t3_status')}", file=sys.stderr, flush=True)
    if as_json:
        print(json.dumps(job, indent=2))
    else:
        print_job(job)
        if job["state"] in RUNNING:
            print(f"(still {job['state']}; wait again with `rsa dispatch wait {jid}`)")
    return exit_for(job)


def cmd_status(a):
    if a.id:
        return exit_for(show(a, api("GET", f"/v1/jobs/{a.id}")))
    st = running()
    if a.json:
        print(json.dumps({k: v for k, v in st.items() if k != "ok"} if st else {"dispatcher": "stopped"}, indent=2))
        return 0 if st else 1
    if not st:
        print("rsa dispatch: stopped (the user starts it with `rsa dispatch start`)")
        return 1
    au, po = st["authorization"], st["policy"]
    print(f"rsa dispatch: running on 127.0.0.1:{st['port']} (pid {st['pid']}, up {st['uptime_sec']}s)")
    print(f"  T3 authorization: " + (f"{au['access']}, expires {au['expires_at']} ({au['days_left']} days)"
                                    if au["authorized"] else "NONE (the user runs `rsa dispatch authorize`)"))
    print(f"  policy: {'enabled' if po['enabled'] else 'DISABLED'}, max {po['max_concurrent']} running, "
          f"profiles: {', '.join(po['profiles']) or 'none (everything is denied)'}")
    if po.get("integrity_error"):
        print(f"  POLICY REJECTED: {po['integrity_error']}")
    print(f"  bridge: {st['bridge']}; jobs: {st['jobs']['running']} running, {st['jobs']['known']} known")
    if st.get("pending_sign_in"):
        print(f"  sign-in waiting for approval: {st['pending_sign_in']['url']}")
    return 0


def cmd_list(a):
    jobs = api("GET", "/v1/jobs" + ("?all=1" if a.all else ""))["jobs"]
    if a.json:
        print(json.dumps(jobs, indent=2))
        return 0
    if not jobs:
        print("no dispatched jobs" + ("" if a.all else " in the last 24 hours (--all for older ones)"))
    for j in jobs:
        print(f"{j['id']:<23} {j['state']:<11} {j['profile']:<14} {j['model'][:34]:<34} {j['title'][:50]}")
    return 0


def cmd_wait(a):
    return wait_for(a.id, a.timeout, a.json)


def cmd_send(a):
    reply = api("POST", f"/v1/jobs/{a.id}/send", {"message": read_message(a), "key": a.key, "ask": a.ask},
                timeout=DIALOG_TIMEOUT + 120)
    show(a, reply)
    return 0


def cmd_cancel(a):
    show(a, api("POST", f"/v1/jobs/{a.id}/cancel", {}, timeout=120))
    return 0


def cmd_result(a):
    job = api("GET", f"/v1/jobs/{a.id}")["job"]
    wt = job.get("worktree")
    changes = None
    if wt and os.path.isdir(wt):
        g = ["-c", "core.fsmonitor=false"]
        changes = {"status": git_out(wt, *g, "status", "--porcelain=v1", "-uall").splitlines(),
                   "diffstat": git_out(wt, *g, "diff", "--no-ext-diff", "--no-textconv", "--stat=100", "HEAD")}
    if a.json:
        print(json.dumps({**job, "changes": changes}, indent=2))
        return exit_for(job)
    print_job(job)
    if changes is None:
        print("\n  (no worktree on this machine yet)")
        return exit_for(job)
    print(f"\n  changes in {wt} (uncommitted, review before using):")
    for line in changes["status"] or ["(none)"]:
        print(f"    {line}")
    if changes["diffstat"]:
        print("\n" + "\n".join("    " + l for l in changes["diffstat"].splitlines()))
    print(f"\n  review:    git -C \"{wt}\" diff    (or the diff panel of the T3 thread)")
    print("  integrate: only with the user's approval (commit in the worktree, then merge its branch)")
    return exit_for(job)


def cmd_profiles(a):
    reply = api("GET", "/v1/profiles")
    if a.json:
        print(json.dumps({k: reply[k] for k in ("enabled", "max_concurrent", "integrity_error", "profiles")}, indent=2))
        return 0
    print(f"dispatching {'enabled' if reply['enabled'] else 'DISABLED'}; at most {reply['max_concurrent']} jobs run at once")
    if reply.get("integrity_error"):
        print(f"POLICY REJECTED: {reply['integrity_error']}")
    if not reply["profiles"]:
        print("no profiles: every request is denied")
    for name, p in sorted(reply["profiles"].items()):
        print(f"\n{name}: {p['repo']}  (T3 project {p['project_title']})")
        print(f"  worker {p['provider']}, {p['runtime_mode']}; models {', '.join(p['models'])} "
              f"(default {p['default_model']})")
        print(f"  limits: {p['max_concurrent']} running, {p['max_runtime_minutes']} min/turn, "
              f"{p['max_followups']} follow-ups/job, {p['max_jobs_per_day']} jobs/day")
    return 0


def policy_change(a, body):
    print("waiting for your confirmation in the rsa dispatch dialog ...", file=sys.stderr, flush=True)
    reply = api("POST", "/v1/policy", body, timeout=DIALOG_TIMEOUT + 120)
    print(json.dumps({k: v for k, v in reply.items() if k != "ok"}, indent=2))
    return 0


def cmd_profile(a):
    if a.action == "remove":
        return policy_change(a, {"op": "profile-remove", "name": a.name})
    if a.action == "list":
        return cmd_profiles(a)
    body = {"op": "profile-add", "name": a.name, "repo": os.path.realpath(a.repo), "models": a.model,
            "default_model": a.default_model, "provider": a.provider, "runtime_mode": a.mode}
    for k in LIMITS:
        if getattr(a, k) is not None:
            body[k] = getattr(a, k)
    return policy_change(a, body)


def cmd_limits(a):
    return policy_change(a, {"op": "max-concurrent", "value": a.max_concurrent})


def cmd_toggle(a):
    if a.dispatch_cmd == "disable":
        print(json.dumps({k: v for k, v in api("POST", "/v1/policy", {"op": "disable"}).items() if k != "ok"}, indent=2))
        return 0
    return policy_change(a, {"op": "enable"})


def cmd_authorize(a):
    flow = api("POST", "/v1/authorize", {}, timeout=60)["sign_in"]
    print("Approve rsa dispatch in T3 Code. A browser tab with T3's sign-in page should have opened; if not, open:\n"
          f"\n  {flow['url']}\n\nThere, choose \"Full access\" and approve. If T3 asks for a pairing code, create "
          "one in T3 Settings > Connections (or run `t3 auth pairing create`).", flush=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(2)
        flow = api("GET", "/v1/authorize")["sign_in"]
        if flow["state"] != "pending":
            break
    if flow["state"] == "done":
        print(f"\nauthorized: {flow['access']}")
        if flow["access"] != "full-access":
            print("note: Full Access workers need the \"Full access\" choice; run `rsa dispatch authorize` again")
        return 0
    raise core.RsaError(f"sign-in {flow['state']}: {flow.get('error') or 'not completed'}")


def cmd_deauthorize(a):
    print(json.dumps({k: v for k, v in api("POST", "/v1/deauthorize", {}).items() if k != "ok"}, indent=2))
    return 0


def cmd_start(a):
    st = running()
    if st:
        print(f"rsa dispatch already running (pid {st['pid']})")
        return 0
    if os.name != "nt":
        raise core.RsaError("rsa dispatch runs on Windows only (DPAPI vault, native confirmation dialogs)")
    HOME.mkdir(parents=True, exist_ok=True)
    bridge.spawn_detached(["dispatch", "run"])
    for _ in range(40):
        time.sleep(0.5)
        st = running()
        if st:
            break
    if not st:
        tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-3:] if LOG_FILE.exists() else []
        raise core.RsaError("rsa dispatch did not start" + (": " + " | ".join(tail) if tail else f"; see {LOG_FILE}"))
    print(f"rsa dispatch running on 127.0.0.1:{st['port']} (pid {st['pid']})")
    if not st["authorization"]["authorized"]:
        print("  not authorized in T3 yet: run `rsa dispatch authorize`")
    return 0


def cmd_stop(a):
    st = running()
    if st:
        try:
            api("POST", "/v1/shutdown", {}, timeout=10)
        except core.RsaError:
            pass
        for _ in range(20):
            time.sleep(0.5)
            if not running():
                break
    if running() and PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"] if os.name == "nt" else ["kill", pid], capture_output=True)
    print("rsa dispatch stopped" if st else "rsa dispatch was not running")
    return 0


def cmd_restart(a):
    cmd_stop(a)
    return cmd_start(a)


def cmd_logs(a):
    if not LOG_FILE.exists():
        print("no log yet")
        return 0
    print("\n".join(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-a.lines:]))
    return 0


def cmd_autostart(a):
    return bridge.set_autostart("rsa-dispatch", ["dispatch", "start"], a.state == "on")


def add_parser(sub):
    s = sub.add_parser("dispatch", help="user-authorized Full Access sandbox workers as T3 threads (for orchestrators)")
    d = s.add_subparsers(dest="dispatch_cmd", required=True)

    def cmd(name, help_, json_flag=True):
        c = d.add_parser(name, help=help_)
        if json_flag:
            c.add_argument("--json", action="store_true", help="machine-readable output")
        return c

    c = cmd("submit", "start a worker thread within an approved profile")
    c.add_argument("prompt", nargs="*", help="task (or -f FILE)")
    c.add_argument("-f", "--file", help="read the task from a UTF-8 file ('-' = stdin)")
    c.add_argument("--profile", help="approved profile (default: the one for this repository)")
    c.add_argument("--repo", help="repository (default: the current directory's)")
    c.add_argument("-m", "--model", help="model (default: the profile's default)")
    c.add_argument("--base", help="branch/commit to start from (default: the current branch)")
    c.add_argument("--title", help="thread title (default: the task's first line)")
    c.add_argument("--key", help="idempotency key: resubmitting with it returns the same job")
    c.add_argument("--requester", help="who is asking (shown to the user, e.g. your thread title)")
    c.add_argument("--ask", action="store_true",
                   help="if the request is outside the profile, ask the user once in a dialog instead of failing")
    c.add_argument("--wait", type=int, nargs="?", const=0, metavar="SEC", help="block until the job finishes")
    c = cmd("status", "show a job, or the dispatcher's state without an id")
    c.add_argument("id", nargs="?")
    c = cmd("list", "list dispatched jobs (last 24 hours unless --all)")
    c.add_argument("--all", action="store_true")
    c = cmd("wait", "block until a job's current turn finishes (exit 0 completed, 1 failed, 3 still running)")
    c.add_argument("id")
    c.add_argument("--timeout", type=int, default=0, help="give up after SEC seconds (exit 3)")
    c = cmd("send", "send a follow-up message to a job's thread")
    c.add_argument("id")
    c.add_argument("prompt", nargs="*", help="message (or -f FILE)")
    c.add_argument("-f", "--file")
    c.add_argument("--key", help="idempotency key for this message")
    c.add_argument("--ask", action="store_true", help="ask the user in a dialog if outside the profile")
    c = cmd("cancel", "interrupt a job's thread; it takes no further messages")
    c.add_argument("id")
    c = cmd("result", "show a job's final message and the changes in its worktree")
    c.add_argument("id")
    cmd("profiles", "list the approved profiles (repositories, models, limits)")
    c = cmd("profile", "(user) add, replace or remove a profile; adding asks for confirmation", json_flag=False)
    c.add_argument("action", choices=["add", "remove", "list"])
    c.add_argument("name", nargs="?")
    c.add_argument("--repo", default=".", help="repository root (a T3 project)")
    c.add_argument("--model", action="append", default=[], help="allowed model or glob; repeat")
    c.add_argument("--default-model")
    c.add_argument("--provider", help=f"T3 provider instance served by the rsa bridge (default {bridge.T3_INSTANCE_ID})")
    c.add_argument("--mode", choices=MODES, default=None, help="runtime mode of worker threads (default full-access)")
    for k, (dflt, lo, hi) in LIMITS.items():
        c.add_argument("--" + k.replace("_", "-"), dest=k, type=int, help=f"{lo}-{hi}, default {dflt}")
    c = cmd("limits", "(user) global limit of running jobs; raising it asks for confirmation", json_flag=False)
    c.add_argument("--max-concurrent", type=int, required=True)
    cmd("enable", "(user) re-enable dispatching (asks for confirmation)", json_flag=False)
    cmd("disable", "stop all new dispatching immediately (no confirmation needed)", json_flag=False)
    cmd("authorize", "(user) connect the dispatcher to T3 Code: approve it in T3's sign-in page", json_flag=False)
    cmd("deauthorize", "delete the dispatcher's T3 credential", json_flag=False)
    cmd("run", "run the dispatcher in the foreground", json_flag=False)
    cmd("start", "start the dispatcher in the background", json_flag=False)
    cmd("stop", "stop the dispatcher", json_flag=False)
    cmd("restart", "restart the dispatcher", json_flag=False)
    c = cmd("logs", "show the dispatcher log", json_flag=False)
    c.add_argument("-n", "--lines", type=int, default=60)
    c = cmd("autostart", "start the dispatcher at Windows logon", json_flag=False)
    c.add_argument("state", choices=["on", "off"])
    return s


def main(a, rsa_module, bridge_module):
    global core, bridge
    core, bridge = rsa_module, bridge_module
    bridge.core = rsa_module
    fn = {"submit": cmd_submit, "status": cmd_status, "list": cmd_list, "wait": cmd_wait, "send": cmd_send,
          "cancel": cmd_cancel, "result": cmd_result, "profiles": cmd_profiles, "profile": cmd_profile,
          "limits": cmd_limits, "enable": cmd_toggle, "disable": cmd_toggle, "authorize": cmd_authorize,
          "deauthorize": cmd_deauthorize, "run": cmd_run, "start": cmd_start, "stop": cmd_stop,
          "restart": cmd_restart, "logs": cmd_logs, "autostart": cmd_autostart}[a.dispatch_cmd]
    if a.dispatch_cmd == "profile" and a.action != "list" and not a.name:
        raise core.RsaError("give the profile name")
    if getattr(a, "id", None) and not JOB_RE.match(a.id):
        raise core.RsaError(f"invalid job id: {a.id}")
    try:
        return fn(a)
    except Denied as e:
        r = e.reply
        if getattr(a, "json", False):
            print(json.dumps({k: v for k, v in r.items() if k != "ok"}, indent=2))
        else:
            print(f"rsa dispatch: DENIED: {r.get('error')}", file=sys.stderr)
            for v in r.get("violations") or []:
                print(f"  - {v}", file=sys.stderr)
        return EXIT_DENIED
