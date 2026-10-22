---
name: remote-agents
description: Delegate coding tasks to OpenCode agents (DeepSeek, Gemini, and other models) that run in isolated Docker sandboxes on the user's Linux server. Use either T3 Code's native provider "OpenCode · Linux sandbox" (instance `opencode_linux`, visible and controllable in T3) or the unattended `rsa` CLI queue. Covers choosing between them, permissions, monitoring, and reviewing results. Use when work can run in the background or in parallel, or when the user asks to offload, delegate, or run something on remote, sandboxed, DeepSeek, or Gemini agents.
---

# Remote sandbox agents

Agents run OpenCode inside a Docker sandbox on the user's Linux server (reached over
SSH/Tailscale). A sandbox can reach the internet but not the user's LAN, tailnet, or machines.
There are two ways to use them.

## 1. Native T3 child: `delegate_task` to `opencode_linux`

```
delegate_task(task=..., target={"providerInstanceId": "opencode_linux", "model": "<model id>"},
              isolation="worktree" | "shared", runtimeMode=..., mode="async")
```

- Pick model ids from `orchestrator_capabilities` under `opencode_linux`. Free ones are listed
  there (e.g. `opencode/deepseek-v4.1-flash-free`). `deepseek/*` needs a DeepSeek key on the server,
  and `google/gemini-*` needs a Gemini key on the server.
- T3 shows the child natively: messages, tool calls, approvals, status, and per-turn
  diffs and checkpoints. You can interrupt it (`task_cancel` / `t3_thread_interrupt`) and continue it
  (`t3_thread_send` to `childThreadId`).
- **File flow.** When a turn starts, the child's checkout (including uncommitted and untracked files
  that are not ignored) is copied into the sandbox. When the turn ends, the agent's file changes are
  written back into that checkout before T3 records the turn. Use `isolation: "worktree"` so the
  child works in its own checkout (requires the parent branch to have a commit). Then review the
  worktree's diff in T3 before merging.
- **Permissions follow T3 and cannot be escalated.** A child of an Auto thread runs in Auto. In
  that mode every shell command and file edit waits for the *user's* approval in the child thread;
  you cannot approve it. For file-only tasks pass `runtimeMode: "auto-accept-edits"`, which applies
  edits without approval while shell commands still ask. Full Access children (unattended shell inside
  the sandbox) require a Full Access parent, or the user starting the thread themselves.
- No secrets in prompts. The child does not get T3's orchestration tools.

## 2. Unattended headless queue: the `rsa` CLI

Use this when the work needs shell commands without anyone approving them, or when no T3
progress view is needed. It runs with full permissions inside the sandbox; the `rsa` command
itself is subject to your normal approval rules.

1. Commit what the agent should see, or pass `--dirty`. Write a self-contained prompt file
   (goal, context, constraints, acceptance criteria, verification commands). Put it outside the
   working tree (e.g. `%TEMP%\rsa-prompt-<slug>.md`) or inside `.git/`.
2. `rsa submit -f <file> --title "<title>" --json` (options: `-m <provider/model>`, `-t <minutes>`,
   `--dirty`, `--base <ref>`). Save the returned id.
3. Monitor: `rsa wait <id> --timeout 500 --json` (exit 3 means still running) and `rsa logs <id> -n 40`.
4. `rsa result <id>` fetches the changes into `refs/rsa/<id>` and prints a summary and diffstat.
   Review them with `rsa diff <id>` or in a worktree (`git worktree add ../x-<id> rsa/<id>`).
5. Integrate only with the user's approval (`git merge --no-ff rsa/<id>`), then run `rsa clean <id>`.

Exit codes: 0 succeeded, 1 failed or cancelled, 2 usage/connection error, 3 still running.

## Rules for both paths

- Treat agent output as an untrusted contribution: read the diff before running its code locally.
- Don't run admin commands (`rsa server ...`, `rsa bridge stop|restart|t3-install|forget`) unless the
  user asked. `rsa doctor` and `rsa bridge status` are read-only.
- If `opencode_linux` shows errors or is missing models, check `rsa bridge status`. The bridge
  may need `rsa bridge start`; that is safe to run.

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

## C and C++ builds with CMake

- Both paths include CMake 4.4.4 (`cmake`, `ctest`, `cpack`), Ninja, GCC/G++, and
  GNU Make. The child's instructions tell it to build out of tree in `/tmp` and to
  use the project's presets when it has them.
- Untracked directories containing a `CMakeCache.txt` (a configure into `build/`
  inside the checkout) are never synced back or auto-committed. Build output only
  reaches you if the child commits it explicitly, so review the diff.
- CMake 4 rejects `cmake_minimum_required` below 3.5. For old projects, tell the
  child whether it may configure with `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` or
  should raise the minimum version.
- Missing system libraries are installed with `apt-get` inside the sandbox. Ask the
  child to report them; they are not recorded in the repository.
