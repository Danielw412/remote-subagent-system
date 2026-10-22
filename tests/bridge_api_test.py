#!/usr/bin/env python3
"""API-level test of the T3 bridge, emulating what T3 Code sends for a Full Access thread.

Needs a running bridge (`rsa bridge start`). Creates a throwaway git repo in the temp
directory, opens an OpenCode session through the bridge with T3's full-access permission
rule, and checks that:
  - shell commands run inside the sandbox (Linux workspace path, NET_ADMIN dropped);
  - the internet is reachable but the LAN, the tailnet and this host are not, and the
    agent cannot change the firewall;
  - files the agent creates or edits come back into the Windows checkout at turn end;
  - an edit made locally during a turn is kept and reported as a conflict.

  python tests/bridge_api_test.py [--model opencode/deepseek-v4.1-flash-free]
"""
import argparse
import http.client
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

def rmtree(path):
    """shutil.rmtree that also removes git's read-only object files on Windows."""
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=retry)


PORT = 47960
RESULTS = []
FULL_ACCESS = [{"action": "*", "resource": "*", "effect": "allow"}]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" ({detail})" if detail and not cond else ""), flush=True)


def api(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


class Events(threading.Thread):
    """Collects the bridge's SSE stream (after path translation, as T3 sees it)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.events, self.lock = [], threading.Lock()

    def run(self):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3600)
        conn.request("GET", "/api/event", headers={"Accept": "text/event-stream"})
        r = conn.getresponse()
        while True:
            line = r.readline()
            if not line:
                return
            if line.startswith(b"data:"):
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                with self.lock:
                    self.events.append(ev)

    def for_session(self, sid):
        with self.lock:
            return [e for e in self.events if (e.get("data") or {}).get("sessionID") == sid]

    def wait_end(self, sid, since, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.for_session(sid)[since:]:
                if e.get("type") in ("session.execution.succeeded", "session.execution.failed",
                                     "session.execution.interrupted"):
                    return e["type"]
            time.sleep(1)
        return None


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def prompt(sid, text):
    api("POST", f"/api/session/{sid}/prompt",
        {"id": f"msg_rsatest{secrets.token_hex(8)}", "text": text, "delivery": None, "resume": None})


def tool_outputs(events):
    out = []
    for e in events:
        if e.get("type") == "session.tool.success":
            for c in (e.get("data") or {}).get("content") or []:
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    a = ap.parse_args()
    provider, model = a.model.split("/", 1)
    tmp = Path(tempfile.mkdtemp(prefix="rsa-bridge-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "rsa-test")
    git(repo, "config", "user.email", "rsa-test@localhost")
    (repo / "README.md").write_text("# bridge test\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    directory = str(repo.resolve())
    ev = Events()
    ev.start()
    time.sleep(1)
    try:
        print("full-access session through the bridge")
        s = api("POST", "/api/session", {"title": "rsa bridge api test", "model": {"id": model, "providerID": provider},
                                         "location": {"directory": directory}, "permissions": FULL_ACCESS})["data"]
        sid = s["id"]
        check("session location is reported as the Windows path", s["location"]["directory"] == directory,
              s["location"]["directory"])
        st = api("GET", "/rsa/status")
        ws = next((w for w in st["workspaces"] if w["root"] == directory), None)
        check("workspace provisioned for the checkout", ws is not None)

        n = len(ev.for_session(sid))
        prompt(sid, "Run each of these shell commands separately, one tool call each, and then reply 'done'. "
                    "Do not change any other file.\n"
                    "1. `pwd`\n"
                    "2. `grep CapBnd /proc/self/status`\n"
                    "3. `curl -s -m 8 -o /dev/null -w 'internet=%{http_code}\\n' https://example.com`\n"
                    "4. `curl -s -m 5 http://100.87.157.44:443 >/dev/null && echo tailnet=OPEN || echo tailnet=BLOCKED`\n"
                    "5. `curl -s -m 5 http://192.168.1.1 >/dev/null && echo lan=OPEN || echo lan=BLOCKED`\n"
                    "6. `iptables -F OUTPUT 2>/dev/null && echo firewall=FLUSHED || echo firewall=INTACT`\n"
                    "7. `echo created-by-shell > from-shell.txt && printf 'agent edit\\n' >> README.md`")
        end = ev.wait_end(sid, n)
        check("turn completed", end == "session.execution.succeeded", end)
        out = tool_outputs(ev.for_session(sid)[n:])
        check("commands ran in the Linux workspace", "/ws/" in out)
        cap = next((l for l in out.splitlines() if "CapBnd" in l), "")
        bits = int(cap.split()[-1], 16) if cap else 1 << 12
        check("NET_ADMIN is not in the agent's capability bounding set", cap and not bits >> 12 & 1, cap)
        for marker in ("internet=", "tailnet=", "lan=", "firewall="):
            seen = next((l.strip() for l in out.splitlines() if l.strip().startswith(marker)), "(not run by the model)")
            print(f"         agent saw: {seen}")
        check("agent output never shows the LAN, tailnet or firewall open",
              not any(m in out for m in ("tailnet=OPEN", "lan=OPEN", "firewall=FLUSHED")))
        # Small models skip or rewrite steps (or refuse a firewall flush), so the isolation itself is
        # verified deterministically: same container, same root user and capability set as the agent.
        probe = ("curl -s -m 8 -o /dev/null -w internet=%{http_code}\\\\n https://example.com; "
                 "curl -s -m 5 http://100.87.157.44:443 >/dev/null && echo tailnet=OPEN || echo tailnet=BLOCKED; "
                 "curl -s -m 5 http://192.168.1.1 >/dev/null && echo lan=OPEN || echo lan=BLOCKED; "
                 "iptables -F OUTPUT 2>/dev/null && echo firewall=FLUSHED || echo firewall=INTACT")
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "linuxbox", "docker", "exec", "rsa-oc-default",
                              "bash", "-c", f'"{probe}"'], capture_output=True, text=True).stdout
        check("internet reachable from the sandbox", "internet=200" in res or "internet=30" in res, res)
        check("tailnet (this host's Tailscale IP) blocked", "tailnet=BLOCKED" in res, res)
        check("LAN router blocked", "lan=BLOCKED" in res, res)
        check("agent-level root cannot change the firewall", "firewall=INTACT" in res, res)
        check("file created by shell synced back", (repo / "from-shell.txt").read_text().strip() == "created-by-shell")
        check("edit synced back", "agent edit" in (repo / "README.md").read_text())
        check(".git was not touched by the sync", git(repo, "log", "--oneline").count("\n") == 0)

        print("local edit during a turn is protected")
        n = len(ev.for_session(sid))
        prompt(sid, "Run the shell command `sleep 20` and wait for it. Then append the line `agent second edit` "
                    "to README.md with a shell command, then reply 'done'.")
        time.sleep(8)
        (repo / "README.md").write_text("# bridge test\nlocal edit during the turn\n")
        end = ev.wait_end(sid, n)
        check("second turn completed", end == "session.execution.succeeded", end)
        text = (repo / "README.md").read_text()
        check("local edit kept", "local edit during the turn" in text and "agent second edit" not in text, text)
        st = api("GET", "/rsa/status")
        ws = next((w for w in st["workspaces"] if w["root"] == directory), {})
        check("conflict reported in bridge status", "README.md" in (ws.get("conflicts") or []), ws.get("conflicts"))
    finally:
        rmtree(tmp)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
