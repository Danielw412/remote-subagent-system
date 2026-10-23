"""Offline test of `rsa dispatch`: sign-in, policy enforcement, ownership, integrity and recovery.

A fake T3 server (OAuth + MCP over streamable HTTP, threads kept in memory) stands in for
T3 Code. The dispatcher runs in this process with its own state directory and port; the CLI
runs as a real subprocess, as an orchestrator would run it. The user's clicks in the
confirmation dialog are simulated by replacing rsa_dispatch.confirm. Windows only (DPAPI).

  python tests/dispatch_test.py
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT = Path(__file__).resolve().parent.parent / "client"
TMP = Path(tempfile.mkdtemp(prefix="rsa-dispatch-test-"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ENV = dict(os.environ, RSA_DISPATCH_HOME=str(TMP / "home"), RSA_DISPATCH_PORT=str(free_port()),
           PYTHONIOENCODING="utf-8")
os.environ.update(RSA_DISPATCH_HOME=ENV["RSA_DISPATCH_HOME"], RSA_DISPATCH_PORT=ENV["RSA_DISPATCH_PORT"])
sys.path.insert(0, str(CLIENT))
import rsa  # noqa: E402

rsa_dispatch = rsa.local_modules()
rsa_bridge = rsa_dispatch.bridge
RANK = {m: i for i, m in enumerate(rsa_dispatch.MODES)}
results, outputs = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""))


# ------------------------------------------------------------------ fake T3 Code

class FakeT3:
    def __init__(self):
        self.access, self.tokens, self.codes, self.sessions = "full-access", {}, {}, set()
        self.threads, self.calls, self.auth_seen = {}, [], []
        self.drop_next_launch, self.launch_provider_override, self.sse = False, None, False
        self.lock = threading.Lock()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.srv.fake = self
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tool(self, name, a):
        self.calls.append((name, a))
        if name == "t3_project_list":
            return {"projects": [{"id": "proj-1", "title": "repo", "workspaceRoot": str(REPO)}], "nextCursor": None}
        if name == "t3_thread_launch":
            ceiling = self.tokens_access
            if RANK[a["runtimeMode"]] > RANK[ceiling]:
                return {"code": "runtime_mode_escalation_denied", "message": f"broader than {ceiling}"}, True
            tid = "thr-" + secrets.token_hex(4)
            ws = a["workspaceStrategy"]
            self.threads[tid] = {
                "threadId": tid, "projectId": a["projectId"], "title": a["title"], "status": "running",
                "providerInstanceId": self.launch_provider_override or a["modelSelection"]["instanceId"],
                "model": a["modelSelection"]["model"], "runtimeMode": a["runtimeMode"], "branch": ws["branch"],
                "worktreePath": str(TMP / "wt" / ws["branch"].replace("/", "-")), "pendingRequestCount": 0,
                "itemCount": 1, "messages": [], "runs": [self.new_run(tid, 1)]}
            if self.drop_next_launch:
                self.drop_next_launch = False
                return "DROP", False
            return {"threadId": tid, "projectId": a["projectId"], "modelSelection": a["modelSelection"],
                    "runId": self.threads[tid]["runs"][-1]["runId"], "status": "running"}
        th = self.threads.get(a.get("threadId"))
        if name == "t3_thread_list":
            hits = [t for t in self.threads.values() if a.get("titleContains", "") in t["title"]]
            return {"threads": [{"threadId": t["threadId"], "title": t["title"], "status": t["status"]} for t in hits]}
        if th is None:
            return {"code": "thread_not_found", "message": "The thread was not found."}, True
        if name == "t3_thread_read":
            view = {k: v for k, v in th.items() if k not in ("messages", "runs")}
            items = [{"type": "assistant_message", "text": m, "position": i} for i, m in enumerate(th["messages"])]
            return {"thread": view, "recentRuns": th["runs"][::-1][:1], "items": items if a.get("view") else items[:1]}
        if name == "t3_thread_send":
            th["status"] = "running"
            th["runs"].append(self.new_run(th["threadId"], len(th["runs"]) + 1))
            return {"delivery": "queued", "runId": th["runs"][-1]["runId"]}
        if name == "t3_thread_interrupt":
            th["status"] = "interrupted"
            th["runs"][-1].update(status="interrupted", completedAt=iso(0))
            return {"interrupted": True}
        if name == "t3_thread_wait":
            return {"status": th["status"], "timedOut": th["status"] == "running"}
        return {"code": "invalid_request", "message": f"unexpected tool {name}"}, True

    @staticmethod
    def new_run(tid, n):
        return {"runId": f"run:{tid}:{n}", "status": "running", "startedAt": iso(0), "completedAt": None}

    def finish(self, tid, text):
        th = self.threads[tid]
        th["status"] = "completed"
        th["runs"][-1].update(status="completed", completedAt=iso(0))
        th["messages"].append(text)
        th["itemCount"] += 2


def iso(ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))


class FakeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, status, obj=None, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", (headers or {}).get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        f, path = self.server.fake, urllib.parse.urlsplit(self.path)
        if self.headers.get("Authorization"):
            f.auth_seen.append(path.path)
        if path.path == "/.well-known/t3/environment":
            return self.send(200, {"environmentId": "env-test", "label": "FAKE-T3"})
        if path.path == "/.well-known/oauth-authorization-server":
            o = f.origin
            return self.send(200, {"issuer": o, "authorization_endpoint": o + "/oauth/mcp/authorize",
                                   "token_endpoint": o + "/oauth/mcp/token",
                                   "registration_endpoint": o + "/oauth/mcp/register"})
        if path.path == "/oauth/mcp/authorize":  # the user approves on T3's page with f.access
            q = dict(urllib.parse.parse_qsl(path.query))
            code = secrets.token_urlsafe(16)
            f.codes[code] = (q["client_id"], q["redirect_uri"], q["code_challenge"], f.access)
            loc = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"], "iss": f.origin})
            return self.send(302, {}, {"Location": loc})
        self.send(404, {})

    def do_POST(self):
        f, path = self.server.fake, self.path.split("?")[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Authorization"):
            f.auth_seen.append(path)
        if path == "/oauth/mcp/register":
            return self.send(201, {"client_id": "client-" + secrets.token_hex(4)})
        if path == "/oauth/mcp/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            client, redirect, challenge, access = f.codes.pop(form.get("code"), (None,) * 4)
            want = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=")
            if not client or form.get("redirect_uri") != redirect or want.decode() != challenge:
                return self.send(400, {"error": "invalid_grant", "error_description": "bad code"})
            token = "T3SECRET-" + secrets.token_hex(16)
            f.tokens[token] = access
            scope = {"full-access": "orchestration:read orchestration:operate settings:write source-control:write",
                     "read-only": "orchestration:read"}.get(access, "orchestration:read orchestration:operate")
            return self.send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 30 * 86400,
                                   "scope": scope})
        if path != "/mcp":
            return self.send(404, {})
        token = (self.headers.get("Authorization") or "")[7:]
        if token not in f.tokens:
            return self.send(401, {"error": "invalid_mcp_credential"})
        f.tokens_access = f.tokens[token]
        msg = json.loads(body)
        sid = self.headers.get("Mcp-Session-Id")
        if msg.get("method") == "initialize":
            sid = "sess-" + secrets.token_hex(4)
            f.sessions.add(sid)
            return self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}},
                             {"Mcp-Session-Id": sid})
        if sid not in f.sessions:
            return self.send(404, {"error": "unknown session"})
        if "id" not in msg:
            return self.send(202, raw=b"")
        out = f.tool(msg["params"]["name"], msg["params"]["arguments"])
        content, is_error = out if isinstance(out, tuple) else (out, False)
        if content == "DROP":  # the launch happened but its reply is lost
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        result = ({"isError": True, "content": [{"type": "text", "text": json.dumps(content)}]} if is_error else
                  {"content": [{"type": "text", "text": json.dumps(content)}], "structuredContent": content})
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if f.sse:
            data = b"event: message\ndata: " + json.dumps(reply).encode() + b"\n\n"
            return self.send(200, raw=data, headers={"Content-Type": "text/event-stream"})
        self.send(200, reply)


# ------------------------------------------------------------------ harness

def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout.decode().strip()


def rmtree(path):
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=retry)


REPO = TMP / "repo"
REPO.mkdir()
git(REPO, "init", "-q", "-b", "main")
(REPO / "README.md").write_text("hello\n")
git(REPO, "add", ".")
git(REPO, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
OTHER = TMP / "other"
OTHER.mkdir()
git(OTHER, "init", "-q")

fake = FakeT3()
(TMP / "server-runtime.json").write_text(json.dumps({"origin": fake.origin}))
rsa_dispatch.T3_RUNTIME = TMP / "server-runtime.json"
SETTINGS = {"providerInstances": {
    "opencode_linux": {"driver": "opencode", "enabled": True, "config": {"serverUrl": rsa_bridge.bridge_url()}},
    "claudeAgent": {"driver": "claudeAgent", "enabled": True}}}
rsa_bridge.T3_SETTINGS = TMP / "settings.json"
rsa_bridge.T3_SETTINGS.write_text(json.dumps(SETTINGS))
BRIDGE = {"up": True}
rsa_bridge.running_status = lambda: {"upstream": "connected"} if BRIDGE["up"] else None
ANSWERS, ASKED = [], []


def fake_confirm(title, text, timeout=0):
    ASKED.append((title, text))
    return ANSWERS.pop(0) if ANSWERS else False


def fake_browser(url):  # the user's browser: T3's page approves and redirects to the callback
    threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=30).read(), daemon=True).start()


rsa_dispatch.confirm = fake_confirm
rsa_dispatch.open_browser = fake_browser
STATE = {}


def start_daemon():
    srv = rsa_dispatch.serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE["srv"] = srv
    return srv.dispatcher


def stop_daemon():
    STATE["srv"].shutdown()
    STATE["srv"].server_close()


def cli(*args, cwd=REPO, stdin=None):
    p = subprocess.run([sys.executable, str(CLIENT / "rsa.py"), "dispatch", *args], cwd=cwd, env=ENV,
                       input=stdin, capture_output=True, timeout=120)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    outputs.append(out + err)
    return p.returncode, out, err


def cli_json(*args, **kw):
    rc, out, err = cli(*args, "--json", **kw)
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, {"_raw": out, "_err": err}


def raw_api(path, headers):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(ENV["RSA_DISPATCH_PORT"]), timeout=10)
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status


def launches():
    return [a for n, a in fake.calls if n == "t3_thread_launch"]


def calls(name):
    return [a for n, a in fake.calls if n == name]


def add_profile(*extra, answer=True):
    ANSWERS.append(answer)
    return cli("profile", "add", "work", "--repo", str(REPO), "--model", "opencode/free-*",
               "--default-model", "opencode/free-a", "--max-concurrent", "2", "--max-followups", "1", *extra)


# ------------------------------------------------------------------ tests

def main():
    d = start_daemon()
    key = (TMP / "home" / "client.key").read_text().strip()

    print("local API")
    check("request without the client key is refused", raw_api("/v1/status", {"Host": f"127.0.0.1:{d.port}"}) == 403)
    good = {"Host": f"127.0.0.1:{d.port}", "Authorization": "Bearer " + key}
    check("request with the key is accepted", raw_api("/v1/status", good) == 200)
    check("browser requests (Origin) are refused", raw_api("/v1/status", {**good, "Origin": "http://evil"}) == 403)
    check("foreign Host header is refused", raw_api("/v1/status", {**good, "Host": f"evil:{d.port}"}) == 403)

    print("default deny and sign-in")
    rc, j = cli_json("submit", "do something")
    check("no profile: submit denied (exit 4)", rc == 4 and j.get("code") == "denied", j)
    check("nothing was launched", not launches())
    rc, _, err = cli("profile", "add", "work", "--repo", str(REPO), "--model", "opencode/free-a")
    check("profile add before sign-in fails", rc == 2 and "not authorized" in err, err)
    fake.access = "auto"
    rc, out, err = cli("authorize")
    check("sign-in with a lower T3 ceiling completes", rc == 0 and "limited" in out, out + err)
    rc, out, err = add_profile()
    check("user approves profile 'work'", rc == 0 and ASKED and "WITHOUT asking you" in ASKED[-1][1], out + err)
    rc, j = cli_json("submit", "fix the bug")
    check("Full Access job refused while T3 approved a lower mode", rc == 4 and any(
        "choosing Full access" in v for v in j.get("violations", [])), j)
    fake.access = "full-access"
    rc, out, err = cli("authorize")
    check("sign-in again with Full access", rc == 0 and "full-access" in out, out + err)
    token = d.vault.t3["token"]
    check("token is DPAPI-encrypted at rest", token.encode() not in (TMP / "home" / "vault.bin").read_bytes())

    print("profiles are user-approved and bounded")
    n = len(ASKED)
    rc, _, err = cli("profile", "add", "evil", "--repo", str(REPO), "--model", "x/y", "--provider", "claudeAgent")
    check("host provider (not the rsa bridge) refused before any dialog", rc == 2 and "not served by the rsa bridge"
          in err and len(ASKED) == n, err)
    rc, _, err = cli("profile", "add", "other", "--repo", str(OTHER), "--model", "x/y")
    check("repository that is not a T3 project refused", rc == 2 and "not a T3 project" in err, err)
    rc, _, err = add_profile("--max-runtime-minutes", "30", answer=False)
    check("profile change the user cancels is denied (exit 4)", rc == 4, err)
    rc, j = cli_json("profiles")
    check("profile unchanged after the denial", j["profiles"]["work"]["max_runtime_minutes"] == 60, j)

    print("submit within the profile")
    fake.sse = True
    rc, j1 = cli_json("submit", "--requester", "test orchestrator", "-f", "-", stdin=b"Add a test\nmore detail")
    la = launches()[-1] if launches() else {}
    check("job started (exit 0) as a top-level thread", rc == 0 and j1.get("thread_id") and j1["state"] == "active", j1)
    check("launch: sandbox provider, Full Access, default interaction",
          la.get("modelSelection") == {"instanceId": "opencode_linux", "model": "opencode/free-a"}
          and la.get("runtimeMode") == "full-access" and la.get("interactionMode") == "default", la)
    check("launch: new worktree from the current branch, in the profile's project",
          la.get("workspaceStrategy") == {"type": "worktree", "baseRef": "main", "branch": f"rsa-dispatch/{j1['id']}",
                                          "startFromOrigin": False} and la.get("projectId") == "proj-1", la)
    check("prompt delivered with the unattended-run header", la.get("message", "").startswith("[rsa dispatch job")
          and "Add a test\nmore detail" in la.get("message", ""))
    check("reply has a clickable T3 link", j1.get("link") == f"t3-thread://v1/{j1['thread_id']}", j1)
    rc, j = cli_json("submit", "--requester", "test orchestrator", "-f", "-", stdin=b"Add a test\nmore detail")
    check("identical resubmission returns the same job", j.get("id") == j1["id"] and j.get("duplicate")
          and len(launches()) == 1, j)
    fake.sse = False
    rc, j2 = cli_json("submit", "--key", "k-1", "second task")
    rc2, j2b = cli_json("submit", "--key", "k-1", "second task")
    check("idempotency key returns the same job", rc == 0 and j2b.get("id") == j2.get("id") and len(launches()) == 2)
    rc, _, err = cli("submit", "--key", "k-1", "a different task")
    check("reused key with another request is refused", rc == 2 and "already used" in err, err)
    rc, j = cli_json("submit", "third task")
    check("profile concurrency limit denies a third running job", rc == 4 and any(
        "profile limit 2" in v for v in j.get("violations", [])), j)

    print("models, one-off approvals")
    fake.finish(j1["thread_id"], "Done: added tests/test_x.py; ran pytest (3 passed).")
    rc, j = cli_json("wait", j1["id"], "--timeout", "30")
    check("wait returns completed (exit 0) with the final message", rc == 0 and j["state"] == "completed"
          and "3 passed" in (j.get("last_message") or ""), j)
    rc, j = cli_json("submit", "-m", "deepseek/paid", "use a paid model")
    check("model outside the profile denied", rc == 4 and any("not allowed" in v for v in j.get("violations", [])), j)
    rc, j = cli_json("submit", "-m", "deepseek/paid", "--ask", "use a paid model")
    check("--ask: user cancels -> denied", rc == 4 and "did not approve" in j.get("error", ""), j)
    ANSWERS.append(True)
    rc, j3 = cli_json("submit", "-m", "deepseek/paid", "--ask", "use a paid model")
    check("--ask: user approves this one job", rc == 0 and j3.get("approved_by_user")
          and launches()[-1]["modelSelection"]["model"] == "deepseek/paid", j3)
    check("the dialog listed the violation", "not allowed" in ASKED[-1][1])

    print("follow-ups, cancel, ownership")
    rc, j = cli_json("cancel", j2["id"])
    check("cancel interrupts the thread", calls("t3_thread_interrupt")[-1]["threadId"] == j2["thread_id"]
          and j.get("state") == "cancelled", j)
    rc, j = cli_json("send", j2["id"], "continue")
    check("cancelled job takes no more messages", rc == 4, j)
    rc, j = cli_json("send", j1["id"], "now also update the docs")
    check("follow-up queued in the same thread", rc == 0 and calls("t3_thread_send")[-1]["threadId"] == j1["thread_id"]
          and calls("t3_thread_send")[-1]["mode"] == "queue" and j["followups"] == 1, j)
    fake.finish(j1["thread_id"], "Docs updated.")
    rc, j = cli_json("send", j1["id"], "and more")
    check("follow-up limit enforced", rc == 4 and any("follow-up" in v for v in j.get("violations", [])), j)
    rc, _, err = cli("send", "d20990101-000000-abcd", "hi")
    check("unknown job id refused (only dispatcher-started threads)", rc == 2 and "no rsa dispatch job" in err, err)
    rc, _, err = cli("status", "not-a-job")
    check("malformed job id refused", rc == 2)

    print("runtime limit, hard rules")
    job3 = d.jobs[j3["id"]]
    fake.threads[j3["thread_id"]]["runs"][-1]["startedAt"] = iso(2 * 3600)
    d.refresh(job3)
    d.enforce_runtime(job3)
    check("turn longer than max_runtime_minutes is interrupted", calls("t3_thread_interrupt")[-1]["threadId"]
          == j3["thread_id"] and "max_runtime" in calls("t3_thread_interrupt")[-1]["reason"])
    d.refresh(job3)
    SETTINGS["providerInstances"]["opencode_linux"]["config"]["serverUrl"] = "http://127.0.0.1:1"
    rsa_bridge.T3_SETTINGS.write_text(json.dumps(SETTINGS))
    rc, j = cli_json("submit", "x1")
    check("provider no longer served by the bridge: denied", rc == 4 and any(
        "not served by the rsa bridge" in v for v in j.get("violations", [])), j)
    SETTINGS["providerInstances"]["opencode_linux"]["config"]["serverUrl"] = rsa_bridge.bridge_url()
    rsa_bridge.T3_SETTINGS.write_text(json.dumps(SETTINGS))
    BRIDGE["up"] = False
    rc, j = cli_json("submit", "x2")
    check("bridge down: denied", rc == 4 and any("bridge" in v for v in j.get("violations", [])), j)
    BRIDGE["up"] = True
    rc, out, _ = cli("disable")
    rc, j = cli_json("submit", "x3")
    check("disable takes effect at once, without a dialog", rc == 4 and any("disabled" in v for v in j["violations"]))
    ANSWERS.append(True)
    rc, _, _ = cli("enable")
    check("enable needs (and got) the user's confirmation", rc == 0 and ASKED[-1][0] == "approve enable")

    print("lost replies, foreign threads, T3 restart")
    fake.drop_next_launch = True
    before = len(fake.threads)
    rc, j = cli_json("submit", "lost reply task")
    rc, j = cli_json("status", j["id"]) if j.get("id") else (rc, j)
    check("launch whose reply was lost is found, not repeated", j.get("thread_id") and len(fake.threads) == before + 1
          and j.get("state") == "active", j)
    fake.finish(j["thread_id"], "ok")
    cli_json("status", j["id"])
    fake.launch_provider_override = "claudeAgent"
    rc, j = cli_json("submit", "mismatch task")
    fake.launch_provider_override = None
    check("thread T3 put on another provider is interrupted and dropped", j.get("state") == "gone" and
          calls("t3_thread_interrupt")[-1]["threadId"] == j.get("thread_id"), j)
    fake.sessions.clear()
    rc, j = cli_json("status", j1["id"])
    check("T3 restart (MCP session lost): reconnects transparently", rc == 0 and j.get("id") == j1["id"], j)
    (TMP / "server-runtime.json").write_text(json.dumps({"origin": f"http://127.0.0.1:{free_port()}"}))
    rc, j = cli_json("submit", "while T3 is down")
    check("T3 not running: the job fails at once (exit 1) and holds no slot", rc == 1 and j.get("state") == "failed"
          and "not running" in (j.get("error") or "") and not d.running_jobs(), j)
    (TMP / "server-runtime.json").write_text(json.dumps({"origin": fake.origin}))
    rc, j = cli_json("status", j1["id"])
    check("T3 back: works again", rc == 0, j)
    rc, j = cli_json("submit", "--repo", str(OTHER), "y")
    check("repository without a profile: denied", rc == 4 and "no approved profile" in j.get("error", ""), j)

    print("restart recovery and integrity")
    stop_daemon()
    d = start_daemon()
    rc, j = cli_json("status", j1["id"])
    check("jobs survive a dispatcher restart", rc == 0 and j.get("thread_id") == j1["thread_id"], j)
    stop_daemon()
    jobs = json.loads((TMP / "home" / "jobs.json").read_text())
    jobs["jobs"][j1["id"]]["thread_id"] = "someone-elses-thread"
    (TMP / "home" / "jobs.json").write_text(json.dumps(jobs))
    d = start_daemon()
    rc, _, err = cli("send", j1["id"], "hello")
    check("edited job registry is rejected (cannot redirect a job to another thread)", rc == 2 and
          "no rsa dispatch job" in err and list((TMP / "home").glob("jobs.rejected-*.json")), err)
    stop_daemon()
    pol = json.loads((TMP / "home" / "policy.json").read_text())
    pol["profiles"]["work"]["models"].append("*")
    (TMP / "home" / "policy.json").write_text(json.dumps(pol))
    d = start_daemon()
    rc, j = cli_json("submit", "-m", "any/model", "after tamper")
    check("edited policy is rejected: everything denied", rc == 4 and "integrity" in json.dumps(j), j)
    rc, out, _ = cli("status")
    check("status reports the rejected policy", "POLICY REJECTED" in out, out)
    rc, _, _ = add_profile()
    check("a user-approved change replaces the rejected policy", rc == 0 and "replaces it" in ASKED[-1][1])
    rc, j = cli_json("submit", "-m", "any/model", "after reset")
    check("the forged model pattern is gone", rc == 4 and any("not allowed" in v for v in j.get("violations", [])), j)

    print("expiry, deauthorize, credential isolation")
    creds = dict(d.vault.t3, expires_at="2020-01-01T00:00:00Z")
    d.vault.set_t3(creds)
    rc, j = cli_json("submit", "expired")
    check("expired authorization: denied", rc == 4 and any("expired" in v for v in j.get("violations", [])), j)
    rc, out, _ = cli("deauthorize")
    rc, j = cli_json("status")
    check("deauthorize removes the credential", not j["authorization"]["authorized"] and d.vault.t3 is None, j)
    leaked = [o for o in outputs if "T3SECRET-" in o]
    check("no CLI output ever contained a T3 token", not leaked, leaked[:1])
    files = [p for p in (TMP / "home").iterdir() if p.is_file() and p.name != "vault.bin"]
    check("no state file or log contains a T3 token", not any(b"T3SECRET-" in p.read_bytes() for p in files))
    check("the token was only ever sent to /mcp", set(fake.auth_seen) == {"/mcp"}, fake.auth_seen)
    stop_daemon()


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("rsa dispatch runs on Windows only")
    try:
        main()
    finally:
        try:
            rmtree(TMP)
        except OSError:
            pass
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)
