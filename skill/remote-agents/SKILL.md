---
name: remote-agents
description: Delegate self-contained coding tasks to unattended OpenCode agents that run in isolated Docker sandboxes on the user's Linux server, using the `rsa` CLI. Covers submitting tasks, monitoring them, fetching their git results, and reviewing the changes. Use when work can run in the background or in parallel (implementing a well-specified change, fixing failing tests, writing tests, mechanical refactors, investigations), or when the user asks to offload, delegate, or run something on remote or sandboxed agents.
---

# Remote sandbox agents (`rsa`)

`rsa` sends a task to a queue on the user's Linux server (reached over SSH/Tailscale). Each
task runs OpenCode, unattended and with full tool permissions, inside a fresh Docker sandbox. The sandbox
has a private copy of the repository at a chosen commit, internet access, and no access
to the user's LAN, tailnet, or machines. The agent's commits come back as a git ref
(`refs/rsa/<id>`, usable as `rsa/<id>`) in the local repository. **rsa never changes the
local working tree, index, HEAD, or branches.** Integrating results is a separate, explicit step.

## Workflow

1. **Decide what to send.** The agent only sees the repository at a commit plus your prompt.
   By default that commit is `HEAD`, and uncommitted changes are *not* sent. Add `--dirty` to include
   uncommitted and untracked (non-ignored) files, or `--base <ref>` to pick another commit.
2. **Write a self-contained prompt to a file.** The agent cannot ask questions. Include:
   the goal, relevant files and context, constraints (what not to touch), acceptance criteria,
   and the exact commands to verify (tests, linters, builds). Keep one focused task per submission.
   Put the file outside the working tree (for example `%TEMP%\rsa-prompt-<slug>.md`), or inside
   `.git/` if you can only write in the workspace. Both locations stay out of snapshots and commits.
3. **Submit:** `rsa submit -f <prompt-file> --title "<short title>" --json`
   It returns `{"id": ...}`. Options: `-t <minutes>` timeout (default 60), `-m <provider/model>`,
   `--dirty`, `--base <ref>`. Short single-line prompts may be passed inline instead of `-f`.
   From bash you can use a heredoc: `rsa submit -f - --title "..." <<'EOF' ... EOF`.
4. **Monitor without blocking your own work:**
   - `rsa wait <id> --timeout 500 --json` waits up to about 8 minutes. Exit code 3 means it is still running;
     call it again or do other work. You can also run it as a background command.
   - `rsa logs <id> -n 40` shows recent agent activity (tool calls and messages). `rsa list` shows the queue.
5. **Collect:** `rsa result <id>` fetches the result into `refs/rsa/<id>` and prints the agent's
   summary, commits, and diffstat (`--json` for structured output).
6. **Review before trusting it.** `rsa diff <id>` (or `git diff <base> rsa/<id>`). Treat the
   agent's output as an untrusted contribution: read the diff before running its code locally.
   To test it in isolation, use `git worktree add ../<name>-<id> rsa/<id>`, then remove that worktree when done.
7. **Integrate only when the user asked for it or approved it:** `git merge --no-ff rsa/<id>` or
   `git cherry-pick <base>..rsa/<id>`. Then run `rsa clean <id>`.

Exit codes: 0 succeeded, 1 task failed or cancelled, 2 usage/connection error, 3 still running.
A failed task may still contain partial work (for example after a timeout). Check `rsa result`.
If a task fails, read `rsa status <id>` and `rsa logs <id>`, then `rsa retry <id>` (optionally
with `-m <model>`) or submit a clearer prompt. `rsa cancel <id>` stops a queued or running task.

## Boundaries

- `rsa` opens SSH connections to the user's own server. If your harness asks for approval for
  network access or for running `rsa`, request it normally. Never weaken sandbox or approval
  settings to avoid prompts.
- Do not run admin commands (`rsa server install|stop|config|env|auth|build|shell`) unless the
  user asked or agreed while troubleshooting. `rsa doctor` is read-only and is always fine.
- Never put secrets in prompts. Sandboxes have no access to the user's credentials beyond the
  model provider keys configured on the server.
- One worker runs tasks one at a time (FIFO). Prefer a few well-scoped tasks over many tiny ones.

## Reverse engineering inside the sandbox

- Both paths include Ghidra 12.1.4, JDK 21, and Reverse Engineer Anything (REA)
  6.7.0, with a local `rea` MCP server and the matching `reverse-engineer-anything`
  skill. Agents can also call `rea` or Ghidra's `analyzeHeadless` from the shell.
- Include the exact artifact path, investigation questions, requested reports,
  and whether runtime execution is needed in the task. For native analysis ask
  for `provider_id: "ghidra"`; for JavaScript/Electron/ASAR use REA's static
  application analysis. Normal source-repository questions do not require REA.
- Supply artifacts in the synced checkout, a committed queue input (or `--dirty`
  snapshot), or an authorized public download. The sandbox cannot read Windows
  applications or reach host/private services. Optional engines such as IDA,
  Hopper, JADX, browser runtimes, and firmware tools are not installed.
- Ask the child to read its installed REA skill, cite Evidence IDs, distinguish
  observations from inferences, keep databases/caches in `/tmp`, and return only
  requested reports or deliverables. The default Ghidra Java heap is 2 GiB and
  shares the container's memory limit. Static analysis does not require executing
  target binaries. Existing T3 permissions and sandbox isolation still apply.
