"""Live test of `rsa dispatch` against the real T3 Code, bridge and sandbox (~10 minutes).

Needs: `rsa bridge start`, `rsa dispatch start`, the user's `rsa dispatch authorize` (Full access)
and a user-approved profile for the repository under test. Run it from an orchestrator in
Auto mode to exercise the intended path. It creates T3 threads with worktrees on branches
rsa-dispatch/<job> in that repository; they are left for inspection (delete them in T3).

  python tests/dispatch_live_test.py --profile NAME [--model opencode/deepseek-v4.1-flash-free]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results, outputs = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({str(detail)[:400]})" if detail and not cond else ""),
          flush=True)


def rsa(*args, stdin=None, timeout=1200, cwd=None):
    p = subprocess.run([sys.executable, str(ROOT / "client" / "rsa.py"), *args], input=stdin, capture_output=True,
                       timeout=timeout, cwd=cwd)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    outputs.append(out + err)
    return p.returncode, out, err


def rsa_json(*args, **kw):
    rc, out, err = rsa(*args, "--json", **kw)
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, {"_out": out, "_err": err}


def ssh(cmd, timeout=60):
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "linuxbox", cmd], capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    a = ap.parse_args()

    print("preconditions")
    rc, st = rsa_json("dispatch", "status")
    check("dispatcher running and authorized with Full access", rc == 0 and st["authorization"]["authorized"]
          and st["authorization"]["access"] == "full-access", st)
    rc, prof = rsa_json("dispatch", "profiles")
    p = (prof.get("profiles") or {}).get(a.profile)
    check(f"profile {a.profile} approved", p is not None, prof)
    if not all(results):
        return
    repo = p["repo"]

    print("authorization denial (nothing may start)")
    rc, before = rsa_json("dispatch", "list", "--all")
    rc, j = rsa_json("dispatch", "submit", "--profile", a.profile, "-m", "deepseek/not-in-profile", "print hello")
    check("model outside the profile: exit 4, denied", rc == 4 and j.get("code") == "denied", j)
    rc, j = rsa_json("dispatch", "submit", "--repo", str(ROOT.parent), "print hello")
    check("repository without a profile: exit 4, denied", rc == 4, j)
    rc, after = rsa_json("dispatch", "list", "--all")
    check("no job was created by denied requests", len(after) == len(before), (len(before), len(after)))

    print("unattended Full Access job in the Linux sandbox")
    task = ("Use the shell. In the repository root, create the file dispatch-probe.txt containing exactly three "
            "lines: the output of `uname -s`, the output of `id -u`, and the output of `python3 -c 'print(6*7)'`. "
            "Do not change any other file.")
    t0 = time.time()
    rc, job = rsa_json("dispatch", "submit", "--profile", a.profile, "-m", a.model, "--title", "dispatch live probe",
                       "--requester", "dispatch_live_test (Auto-mode orchestrator)", "--key",
                       f"live-{int(t0)}", task)
    check("submitted: thread id and clickable link returned", rc == 0 and job.get("thread_id") and
          job.get("link", "").startswith("t3-thread://v1/"), job)
    if rc != 0:
        return
    jid = job["id"]
    print(f"    job {jid}: {job['markdown_link']}", flush=True)
    rc, j = rsa_json("dispatch", "submit", "--profile", a.profile, "-m", a.model, "--title", "dispatch live probe",
                     "--requester", "dispatch_live_test (Auto-mode orchestrator)", "--key", f"live-{int(t0)}", task)
    check("resubmission with the same key returns the same job", j.get("id") == jid and j.get("duplicate"), j)
    rc, mid = rsa_json("dispatch", "status", jid)
    check("progress visible while running (T3 status)", mid.get("t3_status") in
          ("preparing", "queued", "starting", "running", "completed"), mid)
    rc, j = rsa_json("dispatch", "wait", jid, "--timeout", "900")
    check("job completed without any approval (exit 0)", rc == 0 and j["state"] == "completed" and not j["needs_user"], j)
    wt = j.get("worktree")
    check("worker ran in its own new worktree", wt and os.path.isdir(wt) and "rsa-dispatch" in (j.get("branch") or ""), j)
    probe = Path(wt or ".") / "dispatch-probe.txt"
    lines = probe.read_text().split() if probe.exists() else []
    check("shell ran inside the Linux sandbox as its root (Linux, uid 0) and the file synced back",
          lines[:3] == ["Linux", "0", "42"], lines)
    rc, out, _ = rsa("dispatch", "result", jid)
    check("result shows only the probe file changed", rc == 0 and "dispatch-probe.txt" in out and
          not re.search(r"^\s+ M ", out, re.M), out[-800:])
    main_status = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True).stdout.decode()
    check("the main checkout was not touched", "dispatch-probe" not in main_status, main_status)
    check("final message reported", bool(j.get("last_message")), j)

    print("follow-up and dispatcher restart in the middle of it")
    rc, j = rsa_json("dispatch", "send", jid, "Append one more line `followup-ok` to dispatch-probe.txt. Change "
                                              "nothing else.")
    check("follow-up accepted in the same thread", rc == 0 and j["followups"] == 1, j)
    rc, out, err = rsa("dispatch", "restart", timeout=120)
    check("dispatcher restarted", rc == 0, out + err)
    rc, j = rsa_json("dispatch", "wait", jid, "--timeout", "900")
    check("job re-attached after restart and the follow-up finished", rc == 0 and j["state"] == "completed", j)
    text = probe.read_text() if probe.exists() else ""
    check("follow-up's change synced back", "followup-ok" in text, text)

    print("cancellation")
    rc, job2 = rsa_json("dispatch", "submit", "--profile", a.profile, "-m", a.model, "--title", "dispatch cancel probe",
                        "Run the shell command `sleep 600 && echo late > cancel-probe.txt` and wait for it.")
    check("second job started", rc == 0 and job2.get("thread_id"), job2)
    deadline = time.time() + 300
    while rc == 0 and time.time() < deadline:
        rc2, s = rsa_json("dispatch", "status", job2["id"])
        if s.get("t3_status") == "running" and s.get("worktree"):
            break
        time.sleep(5)
    time.sleep(45)  # let the agent reach the sleep
    rc, j = rsa_json("dispatch", "cancel", job2["id"])
    check("cancel interrupts the running turn", j.get("state") == "cancelled" and j.get("t3_status") in
          ("interrupted", "cancelled"), j)
    rc, j = rsa_json("dispatch", "send", job2["id"], "continue")
    check("cancelled job refuses follow-ups (exit 4)", rc == 4, j)
    wt2 = (j.get("worktree") if isinstance(j, dict) else None) or rsa_json("dispatch", "status", job2["id"])[1].get("worktree")
    check("the cancelled command never completed", not (Path(wt2 or ".") / "cancel-probe.txt").exists())

    print("credential isolation")
    check("no dispatch output mentions a bearer token or the vault contents",
          not any(re.search(r"(?i)bearer|access_token|\"token\"", o) for o in outputs))
    rc, out, _ = ssh("docker exec rsa-oc-default env | cut -d= -f1 | sort | tr '\\n' ' '")
    check("sandbox server environment has no T3 or dispatcher variables",
          rc == 0 and not re.search(r"T3|DISPATCH|MCP", out), out)
    rc, out, _ = ssh("docker exec rsa-oc-default curl -s -m 10 -o /dev/null -w '%{http_code}' https://1.1.1.1/")
    check("control: the sandbox's curl reaches the internet", rc == 0 and out.strip() not in ("", "000"), out)
    targets = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' "
                              "-and $_.IPAddress -notlike '169.254.*' }).IPAddress"],
                             capture_output=True, text=True).stdout.split()  # LAN, Tailscale, Hyper-V/WSL
    check("found this PC's addresses to probe", bool(targets), targets)
    for ip in targets:
        rc, out, _ = ssh(f"docker exec rsa-oc-default curl -s -m 5 -o /dev/null -w '%{{http_code}}' "
                         f"http://{ip}:3773/.well-known/t3/environment; echo \" rc=$?\"")
        check(f"sandbox cannot reach T3 Code on this PC ({ip}:3773)", "rc=0" not in out, out)


if __name__ == "__main__":
    try:
        main()
    finally:
        print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if results and all(results) else 1)
