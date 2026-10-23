---
name: remote-agents
description: Delegate coding tasks to OpenCode agents (DeepSeek, Gemini, and other models) that run in isolated Docker sandboxes on the user's Linux server. Three paths - T3 Code's native provider "OpenCode · Linux sandbox" (`opencode_linux`) as a delegate_task child that inherits your permissions; `rsa dispatch`, which starts user-authorized Full Access worker threads in T3 within profiles the user approved; and the headless `rsa submit` queue. Covers choosing between them, permissions, monitoring, follow-ups, and reviewing results. Use when work can run in the background or in parallel, or when the user asks to offload, delegate, or run something on remote, sandboxed, DeepSeek, or Gemini agents.
---

# Remote sandbox agents

Agents run OpenCode inside a Docker sandbox on the user's Linux server (reached over
SSH/Tailscale). A sandbox can reach the internet but not the user's LAN, tailnet, or machines.

## Choosing a path

| You need | Use |
|---|---|
| A visible child that works under *your* permissions (in Auto mode the user approves its shell commands and edits) | 1. `delegate_task` to `opencode_linux` |
| Unattended shell work (builds, tests, installs), visible as a T3 thread, while you are in Auto mode | 2. `rsa dispatch`, if a profile the user approved covers the repository |
| Unattended work with no T3 view; results as git refs | 3. `rsa submit` |

Paths 1 and 2 differ in who authorized the worker:

- A **`delegate_task` child** is *your* subagent. It inherits your mode and can never exceed it.
  It reports back to you when it finishes, and `task_cancel` stops it.
- A **dispatched worker** is an independent top-level thread. It was started by a separate
  dispatcher that the *user* authorized in T3 with Full Access and limited with profiles. It is
  not your child: it does not inherit your mode or notify you, and your T3 tools can read it but
  cannot message or stop it (it runs in a broader mode than you). Use `rsa dispatch` for that.

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
  edits without approval while shell commands still ask. For unattended shell work, use path 2.
- No secrets in prompts. The child does not get T3's orchestration tools.

## 2. User-authorized Full Access workers: `rsa dispatch`

1. **See what is approved:** `rsa dispatch profiles --json`. Each profile names one repository,
   the allowed models (`default_model`), the runtime mode, and limits (running jobs, minutes per
   turn, follow-ups per job, jobs per day). Without a fitting profile, everything is refused
   (exit 4). Do not try to change the policy: tell the user which profile you would need (they
   run `rsa dispatch profile add ...` and confirm it in a dialog).
2. **Commit first.** The worker starts in a *new* git worktree from a committed branch (default:
   your current branch). Uncommitted changes are not sent.
3. **Submit** a self-contained task (goal, context, constraints, acceptance criteria, how to
   verify). Write it to a file outside the working tree, e.g. `%TEMP%\rsa-task-<slug>.md`:
   ```
   rsa dispatch submit -f <file> --title "<short title>" --requester "<your thread title>" --key <unique-key> --json
   ```
   Options: `--profile NAME` (default: the profile for the current repository), `-m MODEL`,
   `--base REF`. Keep the returned `id`. Show the user its `markdown_link`
   (`[title](t3-thread://v1/<threadId>)`): the worker is an ordinary T3 thread with live progress,
   diffs and checkpoints. Retrying with the same `--key` returns the same job instead of starting another.
4. **Monitor:** `rsa dispatch wait <id> --timeout 600 --json` (exit 0 completed, 1 failed,
   interrupted or cancelled, 3 still running; call it again). `rsa dispatch status <id> --json`
   shows `t3_status`, `needs_user`, `note`, `error`. Use `rsa dispatch list` for all jobs.
5. **Follow up** in the same thread and worktree: `rsa dispatch send <id> -f <file> --json`
   (a busy thread queues it). **Cancel:** `rsa dispatch cancel <id>`; the job then takes no more messages.
6. **Results:** `rsa dispatch result <id>` prints the worker's final message and the files it
   changed in its worktree. The changes are uncommitted. Review them with `git -C <worktree> diff`
   or T3's diff panel. Never merge or copy them into the user's branch without the user's approval.

Rules:
- **Denied by the user's policy:** with `--json` the reply is `{"code": "denied", "violations": [...]}`
  and nothing was started. The exit code is 4, but a PowerShell wrapper may report it as 1, so
  check `code`. Do not rephrase, switch repositories, or split the task to get around it. Report
  the violations to the user. `--ask` shows the user a one-time approval dialog for a request
  outside its profile (another model, a limit). Use it only when the user expects it.
- Never run `rsa dispatch authorize | deauthorize | profile | limits | enable | stop | restart | autostart`
  unless the user asks. They manage the dispatcher. `status`, `profiles`, `list` and `result` are read-only.
  If the dispatcher is not running or not authorized, tell the user (`rsa dispatch start`, `rsa dispatch authorize`).
- Workers run unattended with full permissions *inside the sandbox*, and they share it with other
  sandbox threads. No secrets in prompts.

## 3. Unattended headless queue: `rsa submit`

Use this when no T3 progress view is needed. It runs with full permissions inside the sandbox; the
`rsa` command itself is subject to your normal approval rules.

1. Commit what the agent should see, or pass `--dirty`. Write a self-contained prompt file
   (goal, context, constraints, acceptance criteria, verification commands). Put it outside the
   working tree (e.g. `%TEMP%\rsa-prompt-<slug>.md`) or inside `.git/`.
2. `rsa submit -f <file> --title "<title>" --json` (options: `-m <provider/model>`, `-t <minutes>`,
   `--dirty`, `--base <ref>`). Save the returned id.
3. Monitor: `rsa wait <id> --timeout 500 --json` (exit 3 means still running) and `rsa logs <id> -n 40`.
4. `rsa result <id>` fetches the changes into `refs/rsa/<id>` and prints a summary and diffstat.
   Review them with `rsa diff <id>` or in a worktree (`git worktree add ../x-<id> rsa/<id>`).
5. Integrate only with the user's approval (`git merge --no-ff rsa/<id>`), then run `rsa clean <id>`.

Exit codes: 0 succeeded, 1 failed or cancelled, 2 usage/connection error, 3 still running
(`rsa dispatch` adds 4: denied by policy).

## Rules for all paths

- Treat agent output as an untrusted contribution: read the diff before running its code locally.
- Don't run admin commands (`rsa server ...`, `rsa bridge stop|restart|t3-install|forget`, the
  user-only `rsa dispatch` commands above) unless the user asked. `rsa doctor`, `rsa bridge status`
  and `rsa dispatch status` are read-only.
- If `opencode_linux` shows errors or is missing models, check `rsa bridge status`. The bridge
  may need `rsa bridge start`; that is safe to run.

## Reverse engineering inside the sandbox

- All paths include Ghidra 12.1.4, JDK 21, and Reverse Engineer Anything (REA)
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

- All paths include CMake 4.4.4 (`cmake`, `ctest`, `cpack`), Ninja, GCC/G++, and
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
