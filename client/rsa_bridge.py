"""rsa bridge: sandboxed OpenCode on the Linux host as a native T3 Code provider.

T3 Code talks to this process as if it were an external OpenCode server (an OpenCode
provider instance whose serverUrl is http://127.0.0.1:<port>). The bridge forwards the
OpenCode v2 API through an SSH tunnel to `opencode serve` running in an rsa sandbox
container on the Linux host, and:

- maps each thread's Windows directory to a sandbox workspace (/ws/<name>-<key>) and
  translates absolute path values between the two in requests, responses and events;
- before a turn starts, copies the checkout's current files (committed, uncommitted and
  untracked, honouring .gitignore) into the workspace;
- when a turn ends, holds the completion event until the agent's file changes have been
  written into the Windows checkout, so T3's checkpoints, diffs and review include them.

Only working-tree files flow back, after validation: never .git, never the repo's hooks
directory, never symlinks or paths outside the checkout. Git runs on agent-controlled
directories only inside the sandbox; Windows receives a git bundle (data) instead.
"""
import base64
import hashlib
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

core = None  # the rsa.py module: call(), remote_cmd(), ssh_base(), git helpers, CFG

STATE_DIR = Path.home() / ".rsa" / "bridge"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bridge.log"
PID_FILE = STATE_DIR / "bridge.pid"
T3_SETTINGS = Path.home() / ".t3" / "userdata" / "settings.json"
T3_INSTANCE_ID = "opencode_linux"
T3_DISPLAY_NAME = "OpenCode · Linux sandbox"

END_EVENTS = {"session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"}
START_EVENTS = {"session.execution.started"}
TURN_RE = re.compile(r"^/api/session/(ses_[A-Za-z0-9]+)/(prompt|command|shell)$")
SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
       "host", "content-length", "accept-encoding", "authorization", "origin"}
RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
SHORT_NAME = re.compile(r"~\d+(\.[^.]*)?$")  # NTFS 8.3 aliases such as GIT~1 or GITHOO~1
WIN_FLAGS = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW

_log_lock = threading.Lock()


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{threading.current_thread().name}] {msg}"
    with _log_lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if sys.stderr and sys.stderr.isatty():
            print(line, file=sys.stderr, flush=True)


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def git(root, *args, input=None, env=None, check=True, timeout=600):
    p = subprocess.run(["git", *args], cwd=root, input=input, env=env, capture_output=True, timeout=timeout,
                       **WIN_FLAGS)
    if check and p.returncode != 0:
        raise core.RsaError(f"git {' '.join(args[:2])}: {p.stderr.decode('utf-8', 'replace').strip()[-500:]}")
    return p.stdout


def gits(root, *args, **kw):
    return git(root, *args, **kw).decode("utf-8", "replace").strip()


def remote_bytes(args, timeout=900):
    """rsa-server command whose stdout is binary (oc-ws export)."""
    p = subprocess.run(core.ssh_base() + [core.remote_cmd(args)], input=b"", capture_output=True, timeout=timeout)
    if p.returncode != 0:
        try:
            msg = json.loads(p.stdout).get("error")
        except ValueError:
            msg = p.stderr.decode("utf-8", "replace").strip()[-500:]
        raise core.RsaError(msg or f"remote command failed ({p.returncode})")
    return p.stdout


# ------------------------------------------------------------------ git snapshots

def git_env(root):
    env = dict(os.environ)
    if not gits(root, "config", "user.name", check=False):
        env.update(GIT_AUTHOR_NAME="rsa", GIT_COMMITTER_NAME="rsa")
    if not gits(root, "config", "user.email", check=False):
        env.update(GIT_AUTHOR_EMAIL="rsa@localhost", GIT_COMMITTER_EMAIL="rsa@localhost")
    return env


def snapshot_commit(root, parent):
    """Commit of the checkout's files on top of `parent` (or `parent` itself if identical).
    Uses a copy of the real index so only changed files are rehashed; nothing in the
    repository changes except new loose objects."""
    with tempfile.TemporaryDirectory() as td:
        idx = os.path.join(td, "index")
        real = gits(root, "rev-parse", "--git-path", "index")
        real = real if os.path.isabs(real) else os.path.join(root, real)
        if os.path.exists(real):
            shutil.copyfile(real, idx)
        env = dict(os.environ, GIT_INDEX_FILE=idx)
        git(root, "add", "-A", ".", env=env)
        tree = gits(root, "write-tree", env=env)
    if parent and tree == gits(root, "rev-parse", f"{parent}^{{tree}}"):
        return parent
    args = ["commit-tree", tree] + (["-p", parent] if parent else []) + ["-m", "rsa bridge: checkout snapshot"]
    return gits(root, *args, env=git_env(root))


def safe_relpath(path):
    """Lexical checks on a repo-relative path from the sandbox (before touching the disk)."""
    if not path or path.startswith("/") or "\\" in path or ":" in path or "\n" in path:
        return False
    for part in path.split("/"):
        if (part in ("", ".", "..") or part.lower() == ".git" or RESERVED.match(part) or SHORT_NAME.search(part)
                or part.endswith((" ", "."))):
            return False
    return True


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def holds(path, sha):
    """Does the file at `path` hold blob `sha` (None = absent)? Compares the raw bytes and their
    CRLF->LF form (autocrlf checkouts) without running any git filter."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return sha is None
    if sha is None:
        return False
    return blob_sha(data) == sha or (b"\0" not in data and blob_sha(data.replace(b"\r\n", b"\n")) == sha)


def read_blobs(root, shas):
    """Raw blob contents via `git cat-file --batch` (no filters, no attributes)."""
    out = {}
    if not shas:
        return out
    buf = git(root, "cat-file", "--batch", input=("\n".join(shas) + "\n").encode())
    i = 0
    for sha in shas:
        nl = buf.index(b"\n", i)
        header = buf[i:nl].decode().split()
        if len(header) != 3 or header[1] != "blob":
            raise core.RsaError(f"unexpected object in sync: {header}")
        size = int(header[2])
        out[sha] = buf[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return out


def apply_changes(root, old, new):
    """Write the file changes old..new into the checkout. Files the user changed since `old`
    are left alone and reported as conflicts. Returns (applied, conflicts, skipped).

    Only regular files inside the checkout are written: never .git, never the repo's hooks
    directory (core.hooksPath), never through symlinks/junctions, never 8.3 aliases; targets
    are checked again after resolving the real path. No git filters run on agent content."""
    real_root = os.path.realpath(root)
    protected = {".git"}
    hooks = gits(root, "config", "--get", "core.hooksPath", check=False)
    if hooks:
        hp = os.path.realpath(os.path.join(root, hooks))
        if norm(hp).startswith(norm(real_root) + os.sep):
            protected.add(os.path.relpath(hp, real_root).replace("\\", "/").lower())
    crlf = gits(root, "config", "--get", "core.autocrlf", check=False).lower() == "true"

    def contained(target):
        if os.path.islink(target) or (hasattr(os.path, "isjunction") and os.path.isjunction(target)):
            return False
        real = os.path.join(os.path.realpath(os.path.dirname(target)), os.path.basename(target))
        rel = os.path.relpath(real, real_root)
        if rel == os.curdir or rel.startswith(os.pardir) or os.path.isabs(rel):
            return False
        low = rel.replace("\\", "/").lower()
        return not any(low == p or low.startswith(p + "/") or ("/" + p + "/") in ("/" + low) for p in protected)

    raw = git(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", old, new)
    fields = raw.split(b"\0")
    entries = []
    for i in range(0, len(fields) - 1, 2):
        meta, path = fields[i].decode(), fields[i + 1].decode("utf-8", "replace")
        om, nm, osha, nsha, _status = meta.lstrip(":").split()
        entries.append((om, nm, osha, nsha, path))
    zero = "0" * 40
    applied, conflicts, skipped, todo = [], [], [], []
    for om, nm, osha, nsha, path in entries:
        target = os.path.join(root, *path.split("/")) if safe_relpath(path) else None
        if not target or nm in ("120000", "160000") or om in ("120000", "160000") or not contained(target):
            skipped.append(path)
            continue
        if os.path.exists(target) and not os.path.isfile(target):
            conflicts.append(path)
            continue
        want_old, want_new = (None if osha == zero else osha), (None if nsha == zero else nsha)
        if holds(target, want_new):
            continue
        if not holds(target, want_old):
            conflicts.append(path)
            continue
        todo.append((path, target, want_new))
    blobs = read_blobs(root, sorted({w for _, _, w in todo if w}))
    for path, target, want_new in todo:
        try:
            if want_new is None:
                os.remove(target)
            else:
                data = blobs[want_new]
                if crlf and b"\0" not in data:
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".rsa-tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, target)
        except OSError as e:
            log(f"could not write {path}: {e}")
            skipped.append(path)
            continue
        applied.append(path)
    return applied, conflicts, skipped


# ------------------------------------------------------------------ workspaces

class Workspace:
    def __init__(self, key, root, remote_dir, repo=None, repo_path=None, synced=None, plain=False):
        self.key, self.root, self.remote_dir = key, root, remote_dir
        self.repo, self.repo_path, self.synced, self.plain = repo, repo_path, synced, plain
        self.lock = threading.Lock()
        self.conflicts, self.last_sync, self.last_error = [], None, None

    @property
    def remote(self):
        return f"/ws/{self.remote_dir}"

    def to_dict(self):
        return {"key": self.key, "root": self.root, "remote_dir": self.remote_dir, "repo": self.repo,
                "repo_path": self.repo_path, "synced": self.synced, "plain": self.plain}


class Bridge:
    def __init__(self):
        cfg = core.CFG
        self.port = int(cfg.get("bridge_port") or 47960)
        self.instance = cfg.get("bridge_instance") or "default"
        self.password = cfg.get("bridge_password") or ""
        self.workspaces, self.sessions, self.busy = {}, {}, set()
        self.lock = threading.RLock()
        self.create_lock = threading.Lock()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.upstream_port, self.upstream_auth, self.remote_version = None, None, None
        self.tunnel = None
        self.end_syncs = {}  # completion event id -> threading.Event (dedupe across SSE streams)
        self.trace = bool(os.environ.get("RSA_BRIDGE_TRACE"))
        self.dir_keys = {}  # normalized thread directory -> workspace key
        self.started = time.time()
        self.load_state()

    # -- persistence
    def load_state(self):
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        if data.get("instance") != self.instance:
            return
        for w in data.get("workspaces", []):
            self.workspaces[w["key"]] = Workspace(**w)
        self.sessions = data.get("sessions", {})

    def save_state(self):
        with self.lock:
            data = {"instance": self.instance, "workspaces": [w.to_dict() for w in self.workspaces.values()],
                    "sessions": self.sessions}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)

    # -- path mapping
    def find_local(self, path):
        np = norm(path)
        best = None
        for w in self.workspaces.values():
            r = norm(w.root)
            if (np == r or np.startswith(r + os.sep)) and (best is None or len(w.root) > len(best.root)):
                best = w
        return best

    def to_remote_str(self, s):
        if s.startswith("file:///"):
            inner = urllib.parse.unquote(s[8:])
            mapped = self.to_remote_str(inner) if WIN_ABS.match(inner) else inner
            return s if mapped == inner else "file://" + urllib.parse.quote(mapped)
        if not WIN_ABS.match(s):
            return s
        w = self.find_local(s)
        if not w:
            return s
        rest = os.path.normpath(s)[len(os.path.normpath(w.root)):]
        return w.remote + rest.replace("\\", "/")

    def to_local_str(self, s):
        if not s.startswith("/ws/"):
            return s
        for w in self.workspaces.values():
            if s == w.remote or s.startswith(w.remote + "/"):
                return w.root + s[len(w.remote):].replace("/", "\\")
        return s

    def walk(self, obj, fn):
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, list):
            return [self.walk(v, fn) for v in obj]
        if isinstance(obj, dict):
            return {k: self.walk(v, fn) for k, v in obj.items()}
        return obj

    def locations(self, obj):
        """Directories named by `location: {directory}` anywhere in a request body."""
        if isinstance(obj, dict):
            loc = obj.get("location")
            if isinstance(loc, dict) and isinstance(loc.get("directory"), str):
                yield loc["directory"]
            for v in obj.values():
                yield from self.locations(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self.locations(v)

    def workspace_for_remote(self, path):
        for w in self.workspaces.values():
            if path == w.remote or path.startswith(w.remote + "/"):
                return w
        return None

    # -- provisioning and sync
    def ensure_workspace(self, directory):
        """Workspace for a thread directory: its own git checkout root, or the directory itself
        when it is not inside a repository. Parent workspaces never capture child repos."""
        if not WIN_ABS.match(directory or ""):
            return None
        key = self.dir_keys.get(norm(directory))
        if key in self.workspaces:
            return self.workspaces[key]
        with self.create_lock:
            top = gits(directory, "rev-parse", "--show-toplevel", check=False) if os.path.isdir(directory) else ""
            root = directory if top and norm(top) == norm(directory) else (os.path.normpath(top) if top else directory)
            key = hashlib.sha1(norm(root).encode()).hexdigest()[:10]
            self.dir_keys[norm(directory)] = key
            if key in self.workspaces:
                return self.workspaces[key]
            name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(norm(root)) or "ws").strip("-.")[:50] or "ws"
            if not top:
                w = Workspace(key, root, f"plain-{name}-{key}", plain=True)
                core.call(["oc-ws", self.instance, "plain", w.remote_dir], timeout=120)
                log(f"workspace {w.remote} for {root} (not a git repo: no file sync)")
            else:
                repo = core.repo_identity(root)
                repo_path = core.call(["ensure-repo", repo], timeout=120)["path"]
                head = gits(root, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False) or None
                base = snapshot_commit(root, head)
                w = Workspace(key, root, f"{name}-{key}", repo=repo, repo_path=repo_path)
                self.push(w, base)
                core.call(["oc-ws", self.instance, "init", w.remote_dir, repo, base], timeout=900)
                w.synced, w.last_sync = base, time.time()
                log(f"workspace {w.remote} for {root} at {base[:12]}")
            with self.lock:
                self.workspaces[key] = w
            self.save_state()
            return w

    def push(self, w, sha):
        git(w.root, "push", "--quiet", "--no-verify", f"{core.CFG['host']}:{w.repo_path}",
            f"+{sha}:refs/rsa/ws/{w.key}", env=core.git_ssh_env(w.root))
        git(w.root, "update-ref", f"refs/rsa/ws/{w.key}", sha)

    def sync_to_sandbox(self, w):
        """Copy the checkout's current files into the workspace (between turns)."""
        if w.plain:
            return
        with w.lock:
            sha = snapshot_commit(w.root, w.synced)
            if sha == w.synced:
                return
            self.push(w, sha)
            core.call(["oc-ws", self.instance, "reset", w.remote_dir, w.repo, sha, "--have", w.synced],
                      timeout=900)
            w.synced, w.last_sync = sha, time.time()
            log(f"pushed {w.root} -> {w.remote} ({sha[:12]})")
        self.save_state()

    def sync_from_sandbox(self, w):
        """Write the agent's file changes back into the checkout (after a turn)."""
        if w.plain:
            return
        with w.lock:
            data = remote_bytes(["oc-ws", self.instance, "export", w.remote_dir, "_", w.synced])
            if not data:
                w.last_sync = time.time()
                return
            with tempfile.TemporaryDirectory() as td:
                bundle = os.path.join(td, "changes.bundle")
                with open(bundle, "wb") as f:
                    f.write(data)
                tmpref = f"refs/rsa/ws/{w.key}-incoming"
                git(w.root, "-c", "transfer.fsckObjects=true", "fetch", "-q", "--no-tags",
                    "--no-write-fetch-head", bundle, f"+refs/rsa/ws:{tmpref}")
            new = gits(w.root, "rev-parse", f"{tmpref}^{{commit}}")
            applied, conflicts, skipped = apply_changes(w.root, w.synced, new)
            git(w.root, "update-ref", f"refs/rsa/ws/{w.key}", new)
            git(w.root, "update-ref", "-d", tmpref)
            w.synced, w.last_sync, w.conflicts = new, time.time(), conflicts
            log(f"pulled {w.remote} -> {w.root}: {len(applied)} file(s) written"
                + (f", conflicts kept local: {conflicts}" if conflicts else "")
                + (f", refused: {skipped}" if skipped else ""))
        self.save_state()

    def session_workspace(self, sid):
        key = self.sessions.get(sid)
        if key is None and self.ready.is_set():
            try:
                info = self.upstream_json("GET", f"/api/session/{sid}")
                directory = ((info.get("data") or info).get("location") or {}).get("directory", "")
                w = self.workspace_for_remote(directory)
                if w:
                    self.remember(sid, w)
                    key = w.key
            except Exception as e:
                log(f"could not look up session {sid}: {e}")
        return self.workspaces.get(key) if key else None

    def remember(self, sid, w):
        with self.lock:
            if self.sessions.get(sid) == w.key:
                return
            self.sessions[sid] = w.key
        self.save_state()

    def before_turn(self, sid):
        with self.lock:
            if sid in self.busy:
                return
        w = self.session_workspace(sid)
        if w:
            try:
                self.sync_from_sandbox(w)  # never overwrite agent work that has not come back yet
                self.sync_to_sandbox(w)
            except Exception as e:
                w.last_error = f"sync before turn failed: {e}"
                log(w.last_error)

    def after_turn(self, sid, event_id):
        with self.lock:
            done = self.end_syncs.get(event_id)
            owner = done is None
            if owner:
                done = self.end_syncs[event_id] = threading.Event()
                if len(self.end_syncs) > 2000:
                    self.end_syncs.clear()
                    self.end_syncs[event_id] = done
        if not owner:
            done.wait(300)
            return
        w = None
        try:
            w = self.session_workspace(sid)
            if w:
                self.sync_from_sandbox(w)
        except Exception as e:
            log(f"sync from sandbox failed for {sid}: {e}")
            if w:
                w.last_error = f"sync from sandbox failed: {e}"
        finally:
            done.set()

    def observe(self, ev):
        """Called for every upstream event, before path translation. Session-to-workspace
        mappings are learned from T3's own requests, never from upstream events."""
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        sid, kind = data.get("sessionID"), ev.get("type")
        if not sid or kind not in START_EVENTS | END_EVENTS:
            return
        with self.lock:
            (self.busy.add if kind in START_EVENTS else self.busy.discard)(sid)
        if kind in END_EVENTS:
            seq = (ev.get("durable") or {}).get("seq") if isinstance(ev.get("durable"), dict) else None
            self.after_turn(sid, ev.get("id") or f"{sid}:{kind}:{seq}")

    # -- upstream connection (SSH tunnel to the sandbox server)
    def upstream_json(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=30)
        try:
            conn.request(method, path, body=body, headers={"Authorization": self.upstream_auth,
                                                           "Accept": "application/json"})
            r = conn.getresponse()
            return json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def tunnel_loop(self):
        threading.current_thread().name = "tunnel"
        backoff, first = 2, True
        while not self.stopping.is_set():
            try:
                info = core.call(["oc-up", self.instance], timeout=3600)
                lport = free_port()
                *opts, host = core.ssh_base()
                cmd = [*opts, "-T", "-o", "ExitOnForwardFailure=yes",
                       "-L", f"127.0.0.1:{lport}:127.0.0.1:{info['port']}",
                       host, core.remote_cmd(["oc-attach", self.instance])]
                with open(LOG_FILE, "ab") as errlog:
                    self.tunnel = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                   stderr=errlog)
                auth = "Basic " + base64.b64encode(f"opencode:{info['password']}".encode()).decode()
                self.upstream_port, self.upstream_auth = lport, auth
                deadline = time.time() + 30
                while time.time() < deadline and self.tunnel.poll() is None:
                    try:
                        self.remote_version = self.upstream_json("GET", "/api/info").get("version")
                        break
                    except OSError:
                        time.sleep(0.5)
                if not self.remote_version or self.tunnel.poll() is not None:
                    raise core.RsaError("tunnel did not come up")
                self.ready.set()
                backoff = 2
                log(f"connected to sandbox server rsa-oc-{self.instance} (opencode {self.remote_version}"
                    f"{', new container' if info.get('started') else ''}{', image is stale' if info.get('stale') else ''})")
                if first:
                    first = False
                    threading.Thread(target=self.reconcile, name="reconcile", daemon=True).start()
                self.tunnel.wait()
                self.ready.clear()
                if not self.stopping.is_set():
                    log("tunnel closed; reconnecting")
            except Exception as e:
                self.ready.clear()
                log(f"cannot reach sandbox server: {e}")
                if self.tunnel and self.tunnel.poll() is None:
                    self.tunnel.kill()
            self.stopping.wait(backoff)
            backoff = min(backoff * 2, 60)

    def reconcile(self):
        """After (re)start, bring back changes made while the bridge was away."""
        for w in list(self.workspaces.values()):
            try:
                self.sync_from_sandbox(w)
            except Exception as e:
                w.last_error = f"reconcile failed: {e}"
                log(f"reconcile {w.root}: {e}")

    def status(self):
        return {"bridge": "running", "pid": os.getpid(), "port": self.port, "instance": self.instance,
                "uptime_sec": int(time.time() - self.started), "upstream": "connected" if self.ready.is_set()
                else "disconnected", "opencode_version": self.remote_version, "busy_sessions": sorted(self.busy),
                "workspaces": [{**w.to_dict(), "remote": w.remote, "conflicts": w.conflicts,
                                "last_error": w.last_error,
                                "last_sync": datetime.fromtimestamp(w.last_sync).isoformat(timespec="seconds")
                                if w.last_sync else None} for w in self.workspaces.values()],
                "sessions": len(self.sessions)}

    def stop(self):
        self.stopping.set()
        if self.tunnel and self.tunnel.poll() is None:
            try:
                self.tunnel.stdin.close()
            except OSError:
                pass
            self.tunnel.terminate()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ HTTP front end

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rsa-bridge"

    def log_message(self, fmt, *args):
        pass

    @property
    def bridge(self):
        return self.server.bridge

    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        b = self.bridge
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{b.port}", f"localhost:{b.port}"):
            return False  # DNS rebinding
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if self.headers.get("Origin") or site not in ("", "none", "same-origin"):
            return False  # browsers
        if b.password:
            want = "Basic " + base64.b64encode(f"opencode:{b.password}".encode()).decode()
            return self.headers.get("Authorization") == want
        return True

    def read_body(self):
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_ANY(self):
        try:
            self.handle_request()
        except Exception as e:
            import traceback
            log(f"bridge error on {self.command} {self.path}: {e!r}\n{traceback.format_exc()}")
            try:
                self.reply(502, {"name": "RsaBridgeError", "data": {"message": f"rsa bridge error: {e}"}})
            except OSError:
                pass

    def handle_request(self):
        b = self.bridge
        if b.trace:
            log(f"{self.command} {self.path[:160]}")
        if not self.allowed():
            return self.reply(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if path == "/rsa/status":
            return self.reply(200, b.status())
        if path == "/rsa/shutdown" and self.command == "POST":
            self.reply(200, {"bridge": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        body = self.read_body()
        try:
            if query:
                pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
                for k, v in pairs:
                    if "directory" in k:
                        b.ensure_workspace(v)
                query = urllib.parse.urlencode([(k, b.to_remote_str(v)) for k, v in pairs],
                                               quote_via=urllib.parse.quote)
            if body and body[:1] in (b"{", b"["):
                obj = json.loads(body)
                for d in set(b.locations(obj)):
                    b.ensure_workspace(d)
                body = json.dumps(b.walk(obj, b.to_remote_str), separators=(",", ":")).encode()
        except core.RsaError as e:
            log(f"workspace preparation failed: {e}")
            return self.reply(502, {"name": "RsaBridgeError", "data": {"message": f"rsa bridge: {e}"}})
        turn = TURN_RE.match(path) if self.command == "POST" else None
        if not b.ready.wait(45):
            return self.reply(503, {"name": "RsaBridgeError",
                                    "data": {"message": "rsa bridge: sandbox server unreachable (see `rsa bridge status`)"}})
        if turn:
            b.before_turn(turn.group(1))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Authorization"] = b.upstream_auth
        headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", b.upstream_port, timeout=3600)
        try:
            conn.request(self.command, path + ("?" + query if query else ""), body=body or None, headers=headers)
            resp = conn.getresponse()
            if "text/event-stream" in (resp.getheader("Content-Type") or ""):
                return self.stream(resp)
            data = resp.read()
            if data[:1] in (b"{", b"[") and "json" in (resp.getheader("Content-Type") or "json"):
                try:
                    obj = json.loads(data)
                    self.learn(path, obj)
                    data = json.dumps(b.walk(obj, b.to_local_str), separators=(",", ":")).encode()
                except ValueError:
                    pass
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (ConnectionError, OSError, http.client.HTTPException) as e:
            log(f"upstream error on {self.command} {path}: {e}")
            try:
                self.reply(502, {"name": "RsaBridgeError", "data": {"message": f"rsa bridge upstream error: {e}"}})
            except OSError:
                pass
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_ANY

    def learn(self, path, obj):
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and SESSION_RE.match(str(data.get("id", ""))):
            directory = (data.get("location") or {}).get("directory", "")
            w = self.bridge.workspace_for_remote(directory) if directory else None
            if w:
                self.bridge.remember(data["id"], w)

    def stream(self, resp):
        b = self.bridge
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        frame = []
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                if line.strip():
                    frame.append(line)
                    continue
                out = []
                for raw in frame:
                    if raw.startswith(b"data:"):
                        try:
                            ev = json.loads(raw[5:])
                        except ValueError:
                            out.append(raw)
                            continue
                        b.observe(ev)
                        raw = b"data: " + json.dumps(b.walk(ev, b.to_local_str), separators=(",", ":")).encode() + b"\n"
                    out.append(raw)
                chunk = b"".join(out) + line
                frame = []
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (ConnectionError, OSError):
            pass
        self.close_connection = True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse a second listener on the port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ------------------------------------------------------------------ commands

def bridge_url(path=""):
    return f"http://127.0.0.1:{int(core.CFG.get('bridge_port') or 47960)}{path}"


def local_request(method, path, timeout=5):
    port = int(core.CFG.get("bridge_port") or 47960)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {}
        if core.CFG.get("bridge_password"):
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"opencode:{core.CFG['bridge_password']}".encode()).decode()
        conn.request(method, path, headers=headers)
        r = conn.getresponse()
        return json.loads(r.read() or b"{}")
    finally:
        conn.close()


def running_status():
    try:
        return local_request("GET", "/rsa/status")
    except (OSError, ValueError):
        return None


def cmd_run(a):
    threading.current_thread().name = "main"
    b = Bridge()
    try:
        srv = Server(("127.0.0.1", b.port), Handler)
    except OSError as e:
        raise core.RsaError(f"cannot listen on 127.0.0.1:{b.port}: {e} (is the bridge already running?)")
    srv.bridge = b
    PID_FILE.write_text(str(os.getpid()))
    log(f"rsa bridge listening on http://127.0.0.1:{b.port} for instance '{b.instance}'")
    threading.Thread(target=b.tunnel_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        b.stop()
        PID_FILE.unlink(missing_ok=True)
        log("rsa bridge stopped")
    return 0


def cmd_start(a):
    st = running_status()
    if st:
        print(f"bridge already running (pid {st['pid']}, upstream {st['upstream']})")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(Path(core.__file__).resolve()), "bridge", "run"]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        base = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, creationflags=base | 0x01000000, **kw)  # + CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, creationflags=base, **kw)
    else:
        subprocess.Popen(args, start_new_session=True, **kw)
    for _ in range(40):
        time.sleep(0.5)
        st = running_status()
        if st:
            break
    if not st:
        raise core.RsaError(f"bridge did not start; see {LOG_FILE}")
    for _ in range(a.wait * 2):
        if st.get("upstream") == "connected":
            break
        time.sleep(0.5)
        st = running_status() or st
    print(f"bridge running on {bridge_url()} (pid {st['pid']}), sandbox server: {st['upstream']}")
    if st.get("upstream") != "connected":
        print(f"  still connecting; check `rsa bridge status` or {LOG_FILE}")
    return 0


def cmd_stop(a):
    st = running_status()
    if st:
        try:
            local_request("POST", "/rsa/shutdown")
        except OSError:
            pass
        for _ in range(20):
            time.sleep(0.5)
            if not running_status():
                break
    if running_status() and PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"] if os.name == "nt" else ["kill", pid],
                       capture_output=True)
    print("bridge stopped" if st else "bridge was not running")
    if a.remote:
        print(json.dumps(core.call(["oc-down", core.CFG.get("bridge_instance") or "default"]), indent=2))
    return 0


def cmd_status(a):
    st = running_status()
    remote = None
    try:
        remote = core.call(["oc-status", core.CFG.get("bridge_instance") or "default"])
    except core.RsaError as e:
        remote = {"error": str(e)}
    if a.json:
        print(json.dumps({"bridge": st or {"bridge": "stopped"}, "server": remote}, indent=2))
        return 0 if st else 1
    if not st:
        print(f"bridge: stopped (start with `rsa bridge start`)")
    else:
        print(f"bridge: running on {bridge_url()} (pid {st['pid']}, up {st['uptime_sec']}s), "
              f"upstream {st['upstream']}, opencode {st.get('opencode_version')}")
        print(f"  busy sessions: {', '.join(st['busy_sessions']) or 'none'}; known sessions: {st['sessions']}")
        for w in st["workspaces"]:
            print(f"  {w['root']}  ->  {w['remote']}  (last sync {w['last_sync'] or 'never'})"
                  + (f"\n    conflicts kept local: {w['conflicts']}" if w["conflicts"] else "")
                  + (f"\n    last error: {w['last_error']}" if w["last_error"] else ""))
    if remote and "error" not in remote:
        print(f"server: {remote['container']} {remote['state']}"
              f"{', healthy' if remote['healthy'] else ''}{', IMAGE STALE (rsa bridge restart --remote)' if remote['stale_image'] else ''}"
              f", {len(remote['workspaces'])} workspace(s)")
    elif remote:
        print(f"server: {remote['error']}")
    return 0 if st else 1


def cmd_logs(a):
    if not LOG_FILE.exists():
        print("no log yet")
        return 0
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-a.lines:]))
    if a.follow:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
    return 0


def cmd_restart(a):
    cmd_stop(argparse_ns(remote=False))
    if a.remote:
        core.call(["oc-up", core.CFG.get("bridge_instance") or "default", "--restart"])
        print("sandbox server restarted (new container; workspaces and sessions kept)")
    return cmd_start(argparse_ns(wait=a.wait))


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def cmd_t3_install(a):
    if not T3_SETTINGS.exists():
        raise core.RsaError(f"T3 Code settings not found at {T3_SETTINGS}")
    settings = json.loads(T3_SETTINGS.read_text(encoding="utf-8"))
    backup = T3_SETTINGS.with_name(f"settings.json.rsa-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copyfile(T3_SETTINGS, backup)
    instances = settings.setdefault("providerInstances", {})
    if a.remove:
        existed = instances.pop(a.id, None) is not None
        print(f"removed T3 provider instance {a.id}" if existed else f"no T3 provider instance {a.id}")
    else:
        entry = instances.setdefault(a.id, {"driver": "opencode", "enabled": True})
        entry.update(driver="opencode", displayName=a.name, enabled=True)
        entry.setdefault("config", {})["serverUrl"] = bridge_url()
        print(f"T3 provider instance '{a.id}' ({a.name}) -> {bridge_url()}")
    tmp = T3_SETTINGS.with_suffix(".rsa-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, T3_SETTINGS)
    print(f"T3 reloads settings automatically (backup: {backup.name})")
    return 0


def cmd_autostart(a):
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target = startup / "rsa-bridge.vbs"
    if a.state == "off":
        target.unlink(missing_ok=True)
        print("autostart disabled")
        return 0
    if not startup.is_dir():
        raise core.RsaError(f"Startup folder not found: {startup}")
    py = Path(sys.executable)
    target.write_text('Set sh = CreateObject("WScript.Shell")\r\n'
                      f'sh.Run """{py}"" ""{Path(core.__file__).resolve()}"" bridge start", 0, False\r\n',
                      encoding="utf-8")
    print(f"autostart enabled: {target}")
    return 0


def cmd_forget(a):
    st = running_status()
    if st:
        raise core.RsaError("stop the bridge first (`rsa bridge stop`), then forget workspaces")
    data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"workspaces": []}
    keep, gone = [], []
    for w in data.get("workspaces", []):
        drop = (a.all or norm(w["root"]) == norm(a.dir or "") or w["key"] == a.dir
                or (a.missing and not os.path.isdir(w["root"])))
        (gone if drop else keep).append(w)
    for w in gone:
        try:
            core.call(["oc-ws", data.get("instance") or "default", "remove", w["remote_dir"]])
        except core.RsaError as e:
            print(f"warning: {e}")
    data["workspaces"] = keep
    data["sessions"] = {s: k for s, k in data.get("sessions", {}).items() if k in {w["key"] for w in keep}}
    STATE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"forgot {len(gone)} workspace(s)")
    return 0


def main(a, rsa_module):
    global core
    core = rsa_module
    return {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart, "status": cmd_status,
            "logs": cmd_logs, "t3-install": cmd_t3_install, "autostart": cmd_autostart,
            "forget": cmd_forget}[a.bridge_cmd](a)
