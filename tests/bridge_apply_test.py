"""Offline test of the bridge write-back hardening (8.3 aliases, hooks dir, .git, CRLF, conflicts).

No server needed: builds hostile trees locally and runs rsa_bridge.apply_changes on them.

  python tests/bridge_apply_test.py
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

client = Path(__file__).resolve().parent.parent / "client"
sys.path.insert(0, str(client))
import rsa  # noqa: E402
import rsa_bridge  # noqa: E402

def rmtree(path):
    """shutil.rmtree that also removes git's read-only object files on Windows."""
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=retry)


rsa_bridge.core = rsa
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" ({detail})" if detail and not cond else ""))


def git(repo, *args, input=None):
    return subprocess.run(["git", *args], cwd=repo, input=input, capture_output=True, check=True).stdout.decode().strip()


def blob(repo, data):
    return git(repo, "hash-object", "-w", "--stdin", "--no-filters", input=data)


def commit_with(repo, parent, files):
    """Commit = `parent`'s files plus raw (path -> bytes) entries, built with `git mktree`, which
    (unlike the index) accepts hostile names, just as a crafted bundle could carry them."""
    entries = {}
    for line in git(repo, "ls-tree", "-r", "-z", parent).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            entries[path] = meta.split()[2]
    for path, data in files.items():
        entries[path] = blob(repo, data)

    def build(prefix_entries):
        files_here, dirs = {}, {}
        for path, sha in prefix_entries.items():
            head, _, rest = path.partition("/")
            if rest:
                dirs.setdefault(head, {})[rest] = sha
            else:
                files_here[head] = sha
        lines = [f"100644 blob {sha}\t{name}" for name, sha in files_here.items()]
        lines += [f"040000 tree {build(sub)}\t{name}" for name, sub in dirs.items()]
        return git(repo, "mktree", input=("\n".join(lines) + "\n").encode())

    return git(repo, "commit-tree", build(entries), "-p", parent, "-m", "agent")


repo = Path(tempfile.mkdtemp(prefix="rsa-apply-")) / "repo"
repo.mkdir()
git(repo, "init", "-q", "-b", "main")
git(repo, "config", "user.name", "t"), git(repo, "config", "user.email", "t@t")
git(repo, "config", "core.autocrlf", "true")
git(repo, "config", "core.hooksPath", ".githooks")
(repo / ".githooks").mkdir()
(repo / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
(repo / "keep.txt").write_bytes(b"line1\r\nline2\r\n")   # CRLF working copy of an LF blob
(repo / "edited.txt").write_bytes(b"original\r\n")
git(repo, "add", "-A")
git(repo, "commit", "-qm", "base")
base = git(repo, "rev-parse", "HEAD")
short = subprocess.run(["cmd", "/c", "dir", "/x", "/a"], cwd=repo, capture_output=True, text=True).stdout
alias = next((tok for line in short.splitlines() if ".githooks" in line for tok in line.split() if "~" in tok), None)
print(f"8.3 alias of .githooks on this volume: {alias or '(none: 8.3 names disabled)'}")

evil = {"new.txt": b"hello\nworld\n", "edited.txt": b"agent version\n", "keep.txt": b"line1\nline2\n",
        ".githooks/pre-commit": b"#!/bin/sh\necho pwned\n", "sub/.GIT/config": b"[core]\n",
        "GITHOO~1/pre-commit": b"evil\n", "a/git~1/x": b"evil\n", "trail./x": b"evil\n"}
if alias:
    evil[f"{alias}/post-commit"] = b"evil\n"
new = commit_with(repo, base, evil)
(repo / "edited.txt").write_bytes(b"user edit during turn\r\n")   # local change after `base`
applied, conflicts, skipped = rsa_bridge.apply_changes(str(repo), base, new)
print("applied:", applied, "\nconflicts:", conflicts, "\nskipped:", skipped)
check("new file written", (repo / "new.txt").read_bytes() == b"hello\r\nworld\r\n")
check("unchanged-by-user CRLF file not rewritten needlessly", "keep.txt" not in applied and "keep.txt" not in conflicts)
check("local edit kept, reported as conflict", "edited.txt" in conflicts
      and (repo / "edited.txt").read_bytes() == b"user edit during turn\r\n")
check("hooks dir untouched", (repo / ".githooks" / "pre-commit").read_text() == "#!/bin/sh\nexit 0\n")
check("hooks dir write refused", ".githooks/pre-commit" in skipped)
check("8.3-looking paths refused", all(p in skipped for p in ("GITHOO~1/pre-commit", "a/git~1/x")))
check("nested .git refused", "sub/.GIT/config" in skipped)
check("trailing-dot component refused", "trail./x" in skipped)
if alias:
    check(f"real 8.3 alias {alias} refused", f"{alias}/post-commit" in skipped)
    check("no file planted in hooks dir via alias", not (repo / ".githooks" / "post-commit").exists())
check("no stray files", not (repo / "a").exists() and not (repo / "trail.").exists())
print(f"{sum(results)}/{len(results)} passed")
rmtree(repo.parent)
sys.exit(0 if all(results) else 1)
