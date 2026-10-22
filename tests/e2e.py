#!/usr/bin/env python3
"""End-to-end tests for rsa against the live server.

Creates a throwaway git repo in the temp directory, drives the real `rsa` client, and
checks results. Uses the server's default model, so runs cost nothing with the free
OpenCode models.

  python tests/e2e.py                 core scenarios (~3 min)
  python tests/e2e.py --resilience    + worker restart/crash/stop and cancel (~8 min)
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RSA = [sys.executable, str(Path(__file__).resolve().parent.parent / "client" / "rsa.py")]
RESULTS = []


def rsa(*args, repo=None, check=True):
    p = subprocess.run(RSA + list(args), cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode not in (0, 1, 3):
        raise RuntimeError(f"rsa {' '.join(args)} -> {p.returncode}: {p.stderr.strip()}")
    return p


def rsa_json(*args, repo=None):
    p = rsa(*args, "--json", repo=repo)
    return json.loads(p.stdout)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" ({detail})" if detail and not cond else ""), flush=True)
    return cond


def submit(repo, prompt, *extra):
    return rsa_json("submit", *extra, prompt, repo=repo)["id"]


def wait(repo, tid, timeout=600):
    return rsa_json("wait", tid, "--timeout", str(timeout), repo=repo)


def wait_state(repo, tid, state, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        info = rsa_json("status", tid, repo=repo)
        if info["state"] == state:
            return info
        time.sleep(3)
    return rsa_json("status", tid, repo=repo)


def server(*args):
    return rsa("server", *args, check=False)


def ssh(cmd):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "linuxbox", cmd], capture_output=True, text=True)


def make_repo(root):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "rsa-test")
    git(repo, "config", "user.email", "rsa-test@localhost")
    (repo / "calc.py").write_text("def divide(a, b):\n    return a * b  # BUG\n")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import divide\n\n\nclass T(unittest.TestCase):\n"
        "    def test_divide(self):\n        self.assertEqual(divide(6, 3), 2)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


SLOW = "Run the shell command `sleep {n}` (wait for it to finish), then create {f} containing 'ok' and commit it."


def core(repo):
    print("core: fix a bug, review, local repo untouched")
    head, status = git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")
    tid = submit(repo, "Fix calc.divide so `python -m unittest` passes. Commit the fix.", "-t", "10")
    info = wait(repo, tid)
    check("task succeeded", info["state"] == "succeeded", info.get("error"))
    check("produced commits", info.get("commits", 0) >= 1)
    res = rsa_json("result", tid, repo=repo)
    check("result fetched into refs/rsa/<id>", git(repo, "rev-parse", f"rsa/{tid}") == res["result"])
    wt = repo.parent / f"wt-{tid}"
    git(repo, "worktree", "add", "-q", str(wt), f"rsa/{tid}")
    ok = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=wt, capture_output=True).returncode == 0
    git(repo, "worktree", "remove", "--force", str(wt))
    check("agent's change passes the tests", ok)
    check("local HEAD, branch and status untouched",
          git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == status
          and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main")

    print("core: --dirty sends uncommitted + untracked work")
    (repo / "notes.txt").write_text("draft\n")
    tid = submit(repo, "Append the line 'seen' to notes.txt and commit. Change nothing else.", "--dirty", "-t", "10")
    info = wait(repo, tid)
    rsa_json("result", tid, repo=repo)
    notes = git(repo, "show", f"rsa/{tid}:notes.txt") if info["state"] == "succeeded" else ""
    check("dirty snapshot task succeeded", info["state"] == "succeeded", info.get("error"))
    check("agent saw the untracked file", notes.startswith("draft") and "seen" in notes, notes)
    check("local untracked file left alone", (repo / "notes.txt").read_text() == "draft\n")
    (repo / "notes.txt").unlink()

    print("core: unknown model fails fast with a clear error")
    tid = submit(repo, "Say hi.", "-m", "nosuch/model")
    info = wait(repo, tid)
    check("failed", info["state"] == "failed")
    check("error names the model", "nosuch/model" in (info.get("error") or ""), info.get("error"))
    p = rsa("wait", tid, repo=repo)
    check("`rsa wait` exits 1 for failed tasks", p.returncode == 1, p.returncode)

    print("core: retry with another model")
    new = rsa_json("retry", tid, "-m", "opencode/big-pickle", repo=repo)["id"]
    info = wait(repo, new)
    check("retry succeeded", info["state"] == "succeeded" and info.get("retry_of") == tid, info.get("error"))

    print("core: timeout harvests partial work")
    tid = submit(repo, "This is a deliberate timeout test. Create partial.txt containing 'partial' and commit it. "
                       "Then run the shell command `sleep 600` in the FOREGROUND and wait for it to finish; do not "
                       "background it, shorten it, or set a tool timeout.", "-t", "1")
    info = wait(repo, tid)
    check("failed as timed out", info["state"] == "failed" and info.get("timed_out"), info.get("error"))
    check("partial commit harvested", info.get("commits", 0) >= 1, info.get("commits"))

    print("core: sandbox isolation")
    p = server("selftest")
    check("selftest passes", p.returncode == 0 and "FAIL" not in p.stdout, p.stdout[-400:])


def resilience(repo):
    print("resilience: cancel queued and running tasks")
    t1 = submit(repo, SLOW.format(n=60, f="a.txt"), "-t", "10")
    t2 = submit(repo, SLOW.format(n=5, f="b.txt"), "-t", "10")
    wait_state(repo, t1, "running")
    check("queued task cancelled", rsa_json("cancel", t2, repo=repo)["state"] == "cancelled")
    time.sleep(8)
    check("running task cancelled", rsa_json("cancel", t1, repo=repo)["state"] == "cancelled")
    check("its container is gone", "rsa-" + t1 not in ssh("docker ps -a --format '{{.Names}}'").stdout)

    print("resilience: worker restart re-attaches to the running sandbox")
    tid = submit(repo, SLOW.format(n=45, f="c.txt"), "-t", "10")
    wait_state(repo, tid, "running")
    time.sleep(8)
    server("restart")
    info = wait(repo, tid)
    log = ssh(f"cat ~/.rsa/tasks/{tid}/task.log").stdout
    check("task survived restart", info["state"] == "succeeded" and info["attempts"] == 1, info.get("error"))
    check("worker re-attached", "re-attaching" in log)

    print("resilience: worker crash (SIGKILL) is recovered by systemd")
    tid = submit(repo, SLOW.format(n=45, f="d.txt"), "-t", "10")
    wait_state(repo, tid, "running")
    time.sleep(5)
    ssh("systemctl --user kill -s KILL rsa-worker.service")
    info = wait(repo, tid)
    check("task survived worker crash", info["state"] == "succeeded", info.get("error"))

    print("resilience: stop requeues, start resumes")
    tid = submit(repo, SLOW.format(n=30, f="e.txt"), "-t", "10")
    wait_state(repo, tid, "running")
    time.sleep(5)
    server("stop")
    info = rsa_json("status", tid, repo=repo)
    check("stopped task back in queue", info["state"] == "queued", info["state"])
    server("start")
    info = wait(repo, tid)
    check("resumed task succeeded", info["state"] == "succeeded", info.get("error"))
    check("stop did not consume an attempt", info["attempts"] == 1, info["attempts"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resilience", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the temp repo")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="rsa-e2e-"))
    repo = make_repo(tmp)
    print(f"test repo: {repo}")
    try:
        core(repo)
        if a.resilience:
            resilience(repo)
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
