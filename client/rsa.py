#!/usr/bin/env python3
"""rsa: delegate coding tasks to unattended OpenCode agents in sandboxes on a remote Linux host.

Runs on the orchestrating machine. Talks to `rsa-server` over SSH (Tailscale) and moves
code with plain git: the base commit is pushed to a per-repo bare repository on the
server, and results come back as refs/rsa/<task-id> in the local repository. The local
working tree, index, HEAD and branches are never modified. Python standard library only.
"""
import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
CONFIG_PATH = Path.home() / ".config" / "rsa" / "config.json"
DEFAULTS = {"host": "linuxbox", "remote": "~/.rsa/app/rsa-server", "ssh": "ssh",
            "bridge_port": "47960", "bridge_instance": "default", "bridge_password": ""}
ENV_OVERRIDES = {"host": "RSA_HOST", "remote": "RSA_REMOTE", "ssh": "RSA_SSH"}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
TERMINAL = {"succeeded", "failed", "cancelled"}
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
EXIT_FAILED, EXIT_ERROR, EXIT_PENDING = 1, 2, 3


class RsaError(Exception):
    pass


# ------------------------------------------------------------------ config + transport

def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for key, var in ENV_OVERRIDES.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    return cfg


CFG = load_config()


def ssh_base(tty=False):
    return [CFG["ssh"], *(["-t"] if tty else []), *SSH_OPTS, CFG["host"]]


def remote_cmd(args):
    return CFG["remote"] + " " + " ".join(shlex.quote(str(a)) for a in args)


def call(args, payload=None, retries=2, timeout=None):
    """Run an rsa-server command and return its parsed JSON reply."""
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(ssh_base() + [remote_cmd(args)], input=data, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RsaError(f"`{args[0]}` on {CFG['host']} timed out after {timeout}s")
        if p.returncode != 255 or attempt == retries:
            break
        time.sleep(2 * (attempt + 1))  # transient network/ssh failure
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode == 255:
        raise RsaError(f"ssh to {CFG['host']} failed: {err[-500:]}\nCheck Tailscale and run `rsa doctor`.")
    try:
        reply = json.loads(out)
    except json.JSONDecodeError:
        hint = " (is the server installed? run `rsa server install`)" if "No such file" in err else ""
        raise RsaError(f"unexpected reply from server (exit {p.returncode}){hint}: {(out or err)[-800:]}")
    if isinstance(reply, dict) and reply.get("ok") is False:
        raise RsaError(reply.get("error") or "server error")
    return reply


def passthrough(args, tty=False, stdin_data=None):
    """Run an rsa-server command with output streamed to this terminal."""
    sys.stdout.flush()
    if tty:
        return subprocess.call(ssh_base(tty) + [remote_cmd(args)])
    # Re-emit output through Python so UTF-8 renders correctly on Windows consoles.
    proc = subprocess.Popen(ssh_base() + [remote_cmd(args)], stdout=subprocess.PIPE,
                            stdin=subprocess.PIPE if stdin_data is not None else None)
    if stdin_data is not None:
        proc.stdin.write(stdin_data)
        proc.stdin.close()
    for line in proc.stdout:
        sys.stdout.write(line.decode("utf-8", "replace"))
        sys.stdout.flush()
    return proc.wait()


# ------------------------------------------------------------------ local git

def git(*args, cwd=None, check=True, env=None):
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True)
    if check and p.returncode != 0:
        raise RsaError(f"git {' '.join(args[:3])} failed: {p.stderr.decode('utf-8', 'replace').strip()[-800:]}")
    return p.stdout.decode("utf-8", "replace").rstrip()


def repo_root():
    root = git("rev-parse", "--show-toplevel", check=False)
    if not root:
        raise RsaError("not inside a git repository (cd into the repo you want to delegate work on)")
    return root


def repo_identity(root):
    url = git("config", "--get", "remote.origin.url", cwd=root, check=False)
    key = url or str(Path(root).resolve()).lower()
    name = re.split(r"[/:\\]", url.rstrip("/"))[-1] if url else Path(root).name
    name = re.sub(r"\.git$", "", name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"
    return f"{name[:60]}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def git_ssh_env(root):
    env = dict(os.environ)
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")
            or git("config", "--get", "core.sshCommand", cwd=root, check=False)):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    return env


def snapshot_worktree(root, base):
    """Commit object for the working tree (tracked + untracked, honouring .gitignore)
    on top of `base`, built with a throwaway index: nothing in the repo changes."""
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "index"))
        git("read-tree", base, cwd=root, env=env)
        git("add", "-A", ".", cwd=root, env=env)
        tree = git("write-tree", cwd=root, env=env)
    if tree == git("rev-parse", f"{base}^{{tree}}", cwd=root):
        return base, False
    env = dict(os.environ)
    if not git("config", "user.name", cwd=root, check=False):
        env.update(GIT_AUTHOR_NAME="rsa", GIT_COMMITTER_NAME="rsa")
    if not git("config", "user.email", cwd=root, check=False):
        env.update(GIT_AUTHOR_EMAIL="rsa@localhost", GIT_COMMITTER_EMAIL="rsa@localhost")
    sha = git("commit-tree", tree, "-p", base, "-m", "rsa: snapshot of uncommitted changes", cwd=root, env=env)
    return sha, True


def fetch_result(root, info):
    """Fetch the task's result (and base, if missing) into refs/rsa/ of the local repo."""
    tid = info["id"]
    if repo_identity(root) != info["repo"]:
        raise RsaError(f"task {tid} belongs to repo {info['repo']}, but this is {repo_identity(root)}; "
                       "run the command inside the repository the task was submitted from")
    path = call(["ensure-repo", info["repo"]])["path"]
    specs = [f"+refs/rsa/result/{tid}:refs/rsa/{tid}"]
    has_base = subprocess.run(["git", "cat-file", "-e", f"{info['base']}^{{commit}}"], cwd=root,
                              capture_output=True).returncode == 0
    if not has_base:  # e.g. a --dirty snapshot commit that was garbage-collected locally
        specs.append(f"+refs/rsa/base/{tid}:refs/rsa/base/{tid}")
    git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", f"{CFG['host']}:{path}", *specs,
        cwd=root, env=git_ssh_env(root))
    got = git("rev-parse", f"refs/rsa/{tid}^{{commit}}", cwd=root)
    if info.get("result") and got != info["result"]:
        raise RsaError(f"fetched {got[:12]} but the server reported {info['result'][:12]}")
    return f"rsa/{tid}"


# ------------------------------------------------------------------ formatting

def local_time(iso):
    if not iso:
        return "-"
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S")


def duration(a, b=None):
    if not a:
        return ""
    t0 = datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if b else datetime.now(timezone.utc)
    s = max(0, int((t1 - t0).total_seconds()))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def changes_text(info):
    if "commits" not in info:
        return ""
    if not info.get("files_changed"):
        return "no changes"
    return (f"{info['commits']} commit(s), {info['files_changed']} file(s) "
            f"(+{info.get('insertions', 0)} -{info.get('deletions', 0)})")


def print_task(info):
    print(f"Task {info['id']}  [{info['state']}]")
    rows = [
        ("title", info.get("title")),
        ("repo", f"{info['repo']}  base {info['base'][:12]}"
                 + (f" ({info['source'].get('branch')})" if info.get("source", {}).get("branch") else "")
                 + ("  [includes uncommitted changes]" if info.get("source", {}).get("snapshot") == "True" else "")),
        ("model", f"{info.get('model')}{'  agent ' + info['agent'] if info.get('agent') else ''}"
                  f"  timeout {info.get('timeout_minutes')}m  attempts {info.get('attempts', 0)}"),
        ("created", local_time(info.get("created_at"))),
    ]
    if info.get("queue_position"):
        rows.append(("queue", f"position {info['queue_position']}"))
    if info.get("started_at"):
        rows.append(("started", f"{local_time(info['started_at'])}  ({duration(info['started_at'], info.get('finished_at'))}"
                                 f"{'' if info.get('finished_at') else ' so far'})"))
    if info.get("finished_at"):
        rows.append(("finished", local_time(info["finished_at"])))
    if "commits" in info:
        rows.append(("changes", changes_text(info)))
    if info.get("tokens"):
        rows.append(("usage", f"{info['tokens'].get('input', 0)} in / {info['tokens'].get('output', 0)} out tokens, "
                              f"{info.get('tool_calls', 0)} tool calls, cost ${info.get('cost', 0)}"))
    if info.get("retry_of"):
        rows.append(("retry of", info["retry_of"]))
    if info.get("error"):
        rows.append(("error", info["error"]))
    elif info.get("last_error") and info["state"] != "succeeded":
        rows.append(("last error", info["last_error"]))
    for w in info.get("warnings") or []:
        rows.append(("warning", w))
    for k, v in rows:
        if v:
            print(f"  {k + ':':<10}{v}")
    if info.get("summary"):
        print("  summary:")
        for line in info["summary"].splitlines():
            print(f"    {line}")


def print_list(tasks):
    if not tasks:
        print("no tasks")
        return
    print(f"{'ID':<22} {'STATE':<10} {'TIME':>8}  {'CHANGES':<34} TITLE")
    for t in tasks:
        when = duration(t.get("started_at"), t.get("finished_at")) if t.get("started_at") else (
            f"#{t['queue_position']}" if t.get("queue_position") else "")
        title = (t.get("title") or "").replace("\n", " ")
        print(f"{t['id']:<22} {t['state']:<10} {when:>8}  {changes_text(t)[:34]:<34} {title[:60]}")


def exit_for(info):
    if info["state"] not in TERMINAL:
        return EXIT_PENDING
    return 0 if info["state"] == "succeeded" else EXIT_FAILED


# ------------------------------------------------------------------ task commands

def read_prompt(a):
    if a.file:
        if a.file == "-":
            raw = sys.stdin.buffer.read()
        else:
            raw = Path(a.file).read_bytes()
        for enc in ("utf-8-sig", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
    elif a.prompt:
        text = " ".join(a.prompt)
    elif not sys.stdin.isatty():
        text = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    else:
        raise RsaError("no prompt: pass it as an argument, with -f FILE, or on stdin")
    if not text.strip():
        raise RsaError("prompt is empty")
    return text.strip() + "\n"


def cmd_submit(a):
    root = repo_root()
    prompt = read_prompt(a)
    base_ref = a.base or "HEAD"
    base = git("rev-parse", "--verify", f"{base_ref}^{{commit}}", cwd=root)
    dirty = bool(git("status", "--porcelain", cwd=root))
    snapshot = False
    if a.dirty and dirty:
        base, snapshot = snapshot_worktree(root, base)
    elif dirty and not a.json:
        print("note: uncommitted changes are NOT sent (commit them or pass --dirty)", file=sys.stderr)
    repo = repo_identity(root)
    tid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    path = call(["ensure-repo", repo])["path"]
    git("push", "--quiet", "--no-verify", f"{CFG['host']}:{path}", f"+{base}:refs/rsa/base/{tid}",
        cwd=root, env=git_ssh_env(root))
    req = {"id": tid, "repo": repo, "base": base, "prompt": prompt, "model": a.model, "agent": a.agent,
           "timeout_minutes": a.timeout, "title": a.title,
           "source": {"path": root, "ref": base_ref, "snapshot": str(snapshot), "client": platform.node(),
                      "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=root, check=False)}}
    try:
        reply = call(["submit"], payload=req)
    except RsaError as e:
        if "already exists" not in str(e):  # a retried submit that had already landed
            raise
        reply = {"id": tid, "state": call(["status", tid])["state"]}
    if a.wait is not None:
        return wait_for(tid, a.wait, a.json)
    if a.json:
        print(json.dumps(reply, indent=2))
        return 0
    pos = f" (queue position {reply.get('queue_position')})" if reply.get("queue_position") else ""
    print(f"submitted {tid}{pos}: base {base[:12]}{' + uncommitted changes' if snapshot else ''}")
    if reply.get("worker") not in (None, "active"):
        print(f"warning: the server worker is {reply['worker']}; start it with `rsa server start`", file=sys.stderr)
    print(f"  follow:  rsa logs {tid} -f\n  wait:    rsa wait {tid}\n  results: rsa result {tid}")
    return 0


def wait_for(tid, timeout, as_json):
    deadline = time.time() + timeout if timeout and timeout > 0 else None
    while True:
        chunk = 240 if deadline is None else max(0, min(240, int(deadline - time.time())))
        info = call(["wait", tid, "--timeout", str(chunk)])
        if info["state"] in TERMINAL or (deadline is not None and time.time() >= deadline):
            break
        if not as_json:
            print(f"... {tid} {info['state']} {duration(info.get('started_at'))}", file=sys.stderr, flush=True)
    info.pop("done", None)
    if as_json:
        print(json.dumps(info, indent=2))
    else:
        print_task(info)
        if info["state"] not in TERMINAL:
            print(f"(still {info['state']}; wait again with `rsa wait {tid}`)")
        elif info.get("commits"):
            print(f"\nNext: rsa result {tid}")
    return exit_for(info)


def cmd_wait(a):
    return wait_for(a.id, a.timeout, a.json)


def cmd_status(a):
    if not a.id:
        return cmd_list(a)
    args = ["status", a.id] + (["--prompt"] if a.prompt else [])
    info = call(args)
    if a.json:
        print(json.dumps(info, indent=2))
    else:
        print_task(info)
        if a.prompt:
            print("  prompt:\n    " + "\n    ".join(info.get("prompt", "").splitlines()))
    return exit_for(info)


def cmd_list(a):
    args = ["list", "--limit", str(getattr(a, "limit", 20) or 20)]
    if getattr(a, "active", False):
        args.append("--active")
    if not getattr(a, "all", False):
        root = git("rev-parse", "--show-toplevel", check=False)
        if root:
            args += ["--repo", repo_identity(root)]
    reply = call(args)
    if a.json:
        print(json.dumps(reply, indent=2))
    else:
        print_list(reply["tasks"])
        if reply.get("worker") != "active":
            print(f"\nwarning: the server worker is {reply.get('worker')}; start it with `rsa server start`")
    return 0


def cmd_logs(a):
    args = ["logs", a.id] + (["-f"] if a.follow else []) + (["--raw"] if a.raw else []) + \
        (["--sandbox"] if a.sandbox else []) + (["-n", str(a.tail)] if a.tail else [])
    return passthrough(args)


def cmd_result(a):
    info = call(["status", a.id])
    if info["state"] not in TERMINAL:
        if a.json:
            print(json.dumps(info, indent=2))
        else:
            print_task(info)
            print(f"\n(task is still {info['state']}; use `rsa wait {a.id}`)")
        return EXIT_PENDING
    ref = None
    if info.get("result") and not a.no_fetch:
        root = repo_root()
        ref = fetch_result(root, info)
        info["local_ref"] = f"refs/rsa/{a.id}"
        info["commit_log"] = git("log", "--oneline", "--no-decorate", f"{info['base']}..{ref}", cwd=root).splitlines()
        info["diffstat"] = git("diff", "--stat=100", info["base"], ref, cwd=root)
    if a.json:
        print(json.dumps(info, indent=2))
        return exit_for(info)
    print_task(info)
    if ref and info.get("commits"):
        b = info["base"][:12]
        print("\n  commits:")
        for line in info["commit_log"]:
            print(f"    {line}")
        print("\n  diffstat:")
        for line in info["diffstat"].splitlines():
            print(f"    {line}")
        print(f"\n  fetched into refs/rsa/{a.id} (your branches and working tree are untouched)")
        print(f"  review:     git diff {b} {ref}      (or: rsa diff {a.id})")
        print(f"  test alone: git worktree add ../{Path(repo_root()).name}-{a.id} {ref}")
        print(f"  integrate:  git merge --no-ff {ref}   or   git cherry-pick {b}..{ref}")
        print(f"  clean up:   rsa clean {a.id}")
    elif info["state"] != "cancelled":
        print("\n  the agent made no changes")
    return exit_for(info)


def cmd_diff(a):
    root = repo_root()
    info = call(["status", a.id])
    if not info.get("result"):
        raise RsaError(f"task {a.id} has no result yet ({info['state']})")
    ref = fetch_result(root, info)
    args = ["--no-pager", "diff"] + (["--stat"] if a.stat else []) + [info["base"], ref]
    if a.paths:
        args += ["--", *a.paths]
    return subprocess.call(["git", *args], cwd=root)


def cmd_cancel(a):
    info = call(["cancel", a.id])
    if a.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"{a.id}: {info['state']}" + (f" ({info['note']})" if info.get("note") else ""))
    return 0


def cmd_retry(a):
    reply = call(["retry", a.id] + (["--model", a.model] if a.model else []))
    if a.json:
        print(json.dumps(reply, indent=2))
    else:
        print(f"retrying {a.id} as {reply['id']} (queue position {reply.get('queue_position')})")
    return 0


def cmd_clean(a):
    root = repo_root()
    refs = git("for-each-ref", "--format=%(refname)", "refs/rsa/", cwd=root).split()
    if not a.all:
        if not a.ids:
            raise RsaError("give task ids, or --all to drop every fetched rsa ref")
        refs = [r for r in refs if r.rsplit("/", 1)[-1] in a.ids]
    for r in refs:
        git("update-ref", "-d", r, cwd=root)
    print(f"deleted {len(refs)} local ref(s)")
    return 0


# ------------------------------------------------------------------ setup, admin, diagnostics

def cmd_doctor(a):
    ok = True

    def line(good, name, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    print("local:")
    line(sys.version_info >= (3, 8), "python", sys.version.split()[0])
    gv = git("--version", check=False)
    line(bool(gv), "git", gv or "not found")
    ssh_path = shutil.which(CFG["ssh"])
    line(bool(ssh_path), "ssh", ssh_path or f"{CFG['ssh']} not on PATH")
    target = CFG["host"]
    try:
        resolved = subprocess.run([CFG["ssh"], "-G", CFG["host"]], capture_output=True, text=True).stdout
        target = re.search(r"^hostname (\S+)", resolved, re.M).group(1)
    except Exception:
        pass
    if shutil.which("tailscale"):
        ts = subprocess.run(["tailscale", "status"], capture_output=True, text=True).stdout
        peer = next((l for l in ts.splitlines() if re.search(rf"\s{re.escape(target)}\s", f" {l} ")), "")
        line(bool(peer) and "offline" not in peer, f"tailscale peer {target}",
             " ".join(peer.split()[2:6]) if peer else "not found in `tailscale status`")
    t0 = time.time()
    p = subprocess.run(ssh_base() + ["true"], capture_output=True, text=True)
    line(p.returncode == 0, f"ssh {CFG['host']}", f"{time.time() - t0:.1f}s" if p.returncode == 0 else p.stderr.strip()[-300:])
    root = git("rev-parse", "--show-toplevel", check=False)
    if root:
        print(f"  [info] current repo: {repo_identity(root)}")
    if p.returncode != 0:
        return EXIT_FAILED
    print("server:")
    try:
        rep = call(["doctor"])
    except RsaError as e:
        line(False, "rsa-server", str(e))
        return EXIT_FAILED
    for c in rep["checks"]:
        line(c["ok"], c["name"], c["detail"])
    print("T3 bridge:")
    sys.path.insert(0, str(HERE))
    import rsa_bridge
    rsa_bridge.core = sys.modules[__name__]
    st = rsa_bridge.running_status()
    line(bool(st and st.get("upstream") == "connected"), "bridge", (
        f"running on {rsa_bridge.bridge_url()}, sandbox server {st['upstream']}" if st else
        "not running (`rsa bridge start`; `rsa bridge autostart on` to start at logon)"))
    try:
        inst = json.loads(rsa_bridge.T3_SETTINGS.read_text(encoding="utf-8")).get("providerInstances", {})
        entry = next((v for v in inst.values() if (v.get("config") or {}).get("serverUrl") == rsa_bridge.bridge_url()),
                     None)
        line(bool(entry and entry.get("enabled", True)), "T3 provider instance",
             entry.get("displayName", "?") if entry else "missing (`rsa bridge t3-install`)")
    except (OSError, ValueError):
        print("  [info] T3 Code settings not found (T3 integration not set up on this machine)")
    print("\nall checks passed" if ok else "\nsome checks failed (see README troubleshooting)")
    return 0 if ok else EXIT_FAILED


def cmd_config(a):
    cfg = {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if a.key is None:
        print(json.dumps({**load_config(), "_file": str(CONFIG_PATH)}, indent=2))
        return 0
    if a.key not in DEFAULTS:
        raise RsaError(f"unknown key {a.key!r}; known: {', '.join(DEFAULTS)}")
    if a.value is None:
        print(load_config()[a.key])
        return 0
    if a.value == "default":
        cfg.pop(a.key, None)
    else:
        cfg[a.key] = a.value
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"{a.key} = {load_config()[a.key]}")
    return 0


def server_bundle():
    """tar.gz of server/ with LF line endings and exec bits, built in memory."""
    import io
    import tarfile
    buf = io.BytesIO()
    src = PROJECT / "server"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(src.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            data = f.read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f.relative_to(src).as_posix())
            info.size, info.mtime = len(data), int(time.time())
            info.mode = 0o755 if f.name in ("rsa-server", "entrypoint.sh", "rsa-ws", "rsa-ignore-build-trees") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def cmd_server(a):
    act, rest = a.action, a.args
    if act == "install":
        print(f"deploying server files to {CFG['host']}:~/.rsa/app ...", flush=True)
        script = ("set -e; mkdir -p ~/.rsa && chmod 700 ~/.rsa && cd ~/.rsa && rm -rf app.new app.old "
                  "&& mkdir app.new && tar -xzf - -C app.new && { [ ! -d app ] || mv app app.old; } "
                  "&& mv app.new app && rm -rf app.old")
        p = subprocess.run(ssh_base() + [script], input=server_bundle())
        if p.returncode != 0:
            raise RsaError("deploy failed")
        return passthrough(["setup"])
    if act in ("start", "stop", "restart", "status"):
        if act == "status":
            print(json.dumps(call(["service", "status"]), indent=2))
            return 0
        print(json.dumps(call(["service", act]), indent=2))
        return 0
    if act == "logs":
        return passthrough(["service", "logs", *rest])
    if act == "env" and rest[:1] == ["set"] and len(rest) == 2:
        value = getpass.getpass(f"value for {rest[1]} (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
        return passthrough(["env", "set", rest[1]], stdin_data=value.strip().encode("utf-8"))
    tty = act in ("auth", "shell") and sys.stdin.isatty()
    return passthrough([act, *rest], tty=tty)


def cmd_bridge(a):
    sys.path.insert(0, str(HERE))
    import rsa_bridge
    return rsa_bridge.main(a, sys.modules[__name__])


def cmd_install(a):
    bindir = Path(a.bin).expanduser() if a.bin else Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py, script = Path(sys.executable).resolve(), Path(__file__).resolve()
    if os.name == "nt":
        (bindir / "rsa.cmd").write_text(f'@"{py}" "{script}" %*\r\n', encoding="utf-8")
        (bindir / "rsa").write_text(f'#!/bin/sh\nexec "{py.as_posix()}" "{script.as_posix()}" "$@"\n', encoding="utf-8")
        print(f"installed {bindir / 'rsa.cmd'} (cmd/PowerShell) and {bindir / 'rsa'} (Git Bash)")
    else:
        target = bindir / "rsa"
        target.write_text(f'#!/bin/sh\nexec "{py}" "{script}" "$@"\n', encoding="utf-8")
        target.chmod(0o755)
        print(f"installed {target}")
    if str(bindir).lower() not in os.environ.get("PATH", "").lower():
        print(f"warning: {bindir} is not on PATH")
    if a.skills:
        src = PROJECT / "skill" / "remote-agents" / "SKILL.md"
        for base in (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"):
            if base.is_dir():
                dest = base / "remote-agents"
                dest.mkdir(exist_ok=True)
                shutil.copyfile(src, dest / "SKILL.md")
                print(f"installed skill {dest / 'SKILL.md'}")
    return 0


# ------------------------------------------------------------------ main

def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="rsa", description="Delegate coding tasks to sandboxed OpenCode agents "
                                "on a remote Linux host. Exit codes: 0 ok/succeeded, 1 task failed, "
                                "2 error, 3 task still running.")
    p.add_argument("--version", action="version", version=f"rsa {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_cmd(name, fn, help_, with_json=True):
        s = sub.add_parser(name, help=help_)
        if with_json:
            s.add_argument("--json", action="store_true", help="machine-readable output")
        s.set_defaults(fn=fn)
        return s

    s = task_cmd("submit", cmd_submit, "queue a task for the current repository")
    s.add_argument("prompt", nargs="*", help="task description (or use -f)")
    s.add_argument("-f", "--file", help="read the prompt from a UTF-8 file ('-' = stdin)")
    s.add_argument("--base", help="commit/branch to start from (default: HEAD)")
    s.add_argument("--dirty", action="store_true", help="include uncommitted and untracked (non-ignored) changes")
    s.add_argument("-m", "--model", help="provider/model (default: server default_model)")
    s.add_argument("--agent", help="OpenCode agent to use (default: build)")
    s.add_argument("-t", "--timeout", type=int, help="minutes before the agent is stopped")
    s.add_argument("--title", help="short title shown in listings")
    s.add_argument("--wait", type=int, nargs="?", const=0, metavar="SEC",
                   help="block until the task finishes (optionally at most SEC seconds)")
    s = task_cmd("status", cmd_status, "show one task, or list tasks for this repo")
    s.add_argument("id", nargs="?"); s.add_argument("--prompt", action="store_true", help="include the full prompt")
    s.add_argument("--all", action="store_true"); s.add_argument("-n", "--limit", type=int, default=20)
    s = task_cmd("list", cmd_list, "list tasks (current repo unless --all)")
    s.add_argument("--all", action="store_true", help="tasks from every repository")
    s.add_argument("--active", action="store_true", help="only queued/running")
    s.add_argument("-n", "--limit", type=int, default=20)
    s = task_cmd("wait", cmd_wait, "block until a task finishes")
    s.add_argument("id"); s.add_argument("--timeout", type=int, default=0, help="give up after SEC seconds (exit 3)")
    s = task_cmd("logs", cmd_logs, "show agent activity (tool calls, messages)", with_json=False)
    s.add_argument("id"); s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--raw", action="store_true", help="raw OpenCode JSON events")
    s.add_argument("--sandbox", action="store_true", help="container/setup log instead of agent activity")
    s.add_argument("-n", "--tail", type=int, default=0)
    s = task_cmd("result", cmd_result, "fetch a finished task's changes and print a review report")
    s.add_argument("id"); s.add_argument("--no-fetch", action="store_true")
    s = task_cmd("diff", cmd_diff, "show the task's diff against its base", with_json=False)
    s.add_argument("id"); s.add_argument("--stat", action="store_true"); s.add_argument("paths", nargs="*")
    s = task_cmd("cancel", cmd_cancel, "cancel a queued or running task"); s.add_argument("id")
    s = task_cmd("retry", cmd_retry, "re-run a finished task as a new task")
    s.add_argument("id"); s.add_argument("-m", "--model")
    s = task_cmd("clean", cmd_clean, "delete fetched refs/rsa/* refs from this repo", with_json=False)
    s.add_argument("ids", nargs="*"); s.add_argument("--all", action="store_true")
    task_cmd("doctor", cmd_doctor, "check the local setup, connectivity and the server", with_json=False)
    s = task_cmd("config", cmd_config, "show/set client settings (host, remote, ssh)", with_json=False)
    s.add_argument("key", nargs="?"); s.add_argument("value", nargs="?")
    s = task_cmd("install", cmd_install, "install the `rsa` command on this machine", with_json=False)
    s.add_argument("--bin", help="directory on PATH (default ~/.local/bin)")
    s.add_argument("--skills", action="store_true", help="also install the agent skill for Claude Code / Codex")
    s = task_cmd("server", cmd_server, "administer the server", with_json=False)
    s.add_argument("action", choices=["install", "setup", "start", "stop", "restart", "status", "logs", "build",
                                      "config", "env", "auth", "models", "shell", "selftest", "gc", "doctor"])
    s.add_argument("args", nargs=argparse.REMAINDER)

    s = sub.add_parser("bridge", help="T3 Code integration: sandboxed OpenCode as a native T3 provider")
    bsub = s.add_subparsers(dest="bridge_cmd", required=True)
    bsub.add_parser("run", help="run the bridge in the foreground")
    b = bsub.add_parser("start", help="start the bridge in the background")
    b.add_argument("--wait", type=int, default=30, help="seconds to wait for the sandbox server")
    b = bsub.add_parser("stop"); b.add_argument("--remote", action="store_true", help="also stop the sandbox server")
    b = bsub.add_parser("restart"); b.add_argument("--remote", action="store_true",
                                                   help="also recreate the sandbox server (after image updates)")
    b.add_argument("--wait", type=int, default=30)
    b = bsub.add_parser("status"); b.add_argument("--json", action="store_true")
    b = bsub.add_parser("logs"); b.add_argument("-f", "--follow", action="store_true")
    b.add_argument("-n", "--lines", type=int, default=60)
    b = bsub.add_parser("t3-install", help="add (or --remove) the provider instance in T3 Code settings")
    b.add_argument("--id", default="opencode_linux"); b.add_argument("--name", default="OpenCode · Linux sandbox")
    b.add_argument("--remove", action="store_true")
    b = bsub.add_parser("autostart", help="start the bridge at Windows logon")
    b.add_argument("state", choices=["on", "off"])
    b = bsub.add_parser("forget", help="drop a workspace mapping (bridge must be stopped)")
    b.add_argument("dir", nargs="?"); b.add_argument("--all", action="store_true")
    b.add_argument("--missing", action="store_true", help="workspaces whose Windows directory no longer exists")
    s.set_defaults(fn=cmd_bridge)

    a = p.parse_args()
    if a.cmd == "status" and a.id and not ID_RE.match(a.id):
        p.error(f"invalid task id: {a.id}")
    try:
        sys.exit(a.fn(a) or 0)
    except RsaError as e:
        print(f"rsa: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
