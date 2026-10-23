# remote-subagent-system (`rsa`)

Delegate coding tasks from an orchestrator (T3 Code, Codex, or Claude Code on Windows) to
[OpenCode](https://opencode.ai) agents (DeepSeek, Gemini, and other models). Each agent runs in a
rootless Docker sandbox on a Linux server, connected over Tailscale. There are two ways in:

- **Inside T3 Code**, as a native provider called **"OpenCode · Linux sandbox"**. Threads and
  `delegate_task` children show messages, tool calls, approvals, and diffs in T3, and can be
  interrupted, continued, and reviewed like any other T3 thread. See
  [T3 Code integration](#t3-code-integration).
- **Full Access dispatch**, with `rsa dispatch`: an orchestrator in T3's Auto mode starts
  unattended worker threads in T3 on that provider. The workers run in Full Access, but only
  within profiles that you approved, and through a dispatcher that you authorized in T3 yourself.
  See [Full Access dispatch](#full-access-dispatch).
- **Headless**, with `rsa submit`: an unattended queue whose results come back as git refs (described below).

```
 Windows laptop (orchestrator)                       latitude7370 (Linux Mint, Tailscale)
 ┌──────────────────────────────┐   ssh (Tailscale)  ┌──────────────────────────────────────────┐
 │ Claude Code / Codex (T3 Code)│ ─────────────────▶ │ ~/.rsa/app/rsa-server  (CLI over SSH)    │
 │   └─ rsa CLI (client/rsa.py) │  git push base ──▶ │ ~/.rsa/repos/<repo>.git  (bare, per repo)│
 │                              │ ◀── git fetch res. │ ~/.rsa/queue, ~/.rsa/tasks/<id>/         │
 │ local repo: only refs/rsa/*  │                    │ rsa-worker.service (systemd --user)      │
 │ are ever written             │                    │   └─ docker run (rootless) per task:     │
 └──────────────────────────────┘                    │       firewall → opencode run → bundle   │
                                                     └──────────────────────────────────────────┘
```

**Task lifecycle**

1. `rsa submit` pushes the base commit (default `HEAD`; `--dirty` adds a snapshot of
   uncommitted work) to `refs/rsa/base/<id>` in the server's bare repo, then queues the task.
2. The worker (one at a time, FIFO) starts a fresh container from `rsa-sandbox:latest`, with:
   - the bare repo mounted **read-only**, and a private `/workspace` clone inside the container;
   - an egress firewall that blocks LAN, Tailscale (100.64/10), and link-local addresses.
     `NET_ADMIN` is dropped before the agent starts, so the agent cannot change the rules;
   - CPU, memory, and PID limits, plus `no-new-privileges`.
3. `opencode run --auto` works on the task with every permission allowed, except the interactive
   `question` tool. Leftover changes are auto-committed, and everything after the base is
   exported as a `git bundle`.
4. The host imports the bundle as plain data (`git fetch` with `fsckObjects`) into
   `refs/rsa/result/<id>`. It never runs git inside the agent's workspace. The container is removed.
5. `rsa result <id>` fetches that ref into the local `refs/rsa/<id>` for review.
   Merging is always a separate, explicit step.

## Requirements (already true for this setup)

| Where   | What |
|---------|------|
| Windows | Python 3.8+, Git, OpenSSH client, Tailscale; an SSH alias (`linuxbox` in `~/.ssh/config`) with key auth |
| Linux   | Python 3.9+, Git, **rootless Docker** with cgroup v2 delegation (cpu, memory, pids), systemd user session with lingering, Tailscale |

## Install

```powershell
# 1. Put `rsa` on PATH (adds rsa.cmd + a Git Bash shim to ~/.local/bin), and install the agent skill
python client\rsa.py install --skills

# 2. Deploy the server: copies server/ to ~/.rsa/app, builds the sandbox image,
#    installs and starts rsa-worker.service. Safe to re-run after any change (this is also "update").
rsa server install

# 3. Verify everything
rsa doctor
rsa server selftest       # proves the sandbox cannot reach the LAN, the tailnet or this host

# 4. Optional: T3 integration (below), then Full Access dispatch for Auto-mode orchestrators
rsa dispatch start; rsa dispatch autostart on; rsa dispatch authorize; rsa dispatch profile add ...
```

**Updating:** after `git pull`, re-run `python client\rsa.py install --skills` (skill), `rsa server
install` (server side), `rsa bridge restart`, and `rsa dispatch restart`.

The client defaults to SSH host `linuxbox`. Change it with `rsa config host <alias>` or the `RSA_HOST` env var.

## T3 Code integration

T3 Code is not modified. The integration uses T3's supported OpenCode provider with an external
`serverUrl`, a provider-instance entry in T3's `settings.json` that T3 hot-reloads, and a small local bridge:

```
 T3 Code (Windows)                    rsa bridge (Windows, 127.0.0.1:47960)        latitude7370
 provider instance "opencode_linux"   OpenCode v2 API proxy                        rsa-oc-default container
 serverUrl=http://127.0.0.1:47960 ──▶ - Windows path <-> /ws/<name>-<key>  ──ssh──▶ opencode serve (sandboxed)
 (threads, delegate_task children)    - before a turn: checkout -> sandbox  tunnel   /ws/<workspaces>
                                      - turn end: sandbox -> checkout,               firewall from a helper,
                                        then the completion reaches T3               no NET_ADMIN at all
```

**How work flows.** Each thread directory (a project root, or a T3 worktree for
`isolation: "worktree"`) gets its own sandbox workspace. Before each turn, the checkout's files
(committed, uncommitted, and untracked, honouring `.gitignore`) are copied in. When the turn
ends, the bridge holds T3's completion event until the agent's changed files are written into the
checkout. T3's per-turn checkpoints, diff panel, and commit/PR flow then work as for a local agent.
Only working-tree files come back: never `.git`, the repo's `core.hooksPath` directory, symlinks or
junctions, Windows 8.3 short-name aliases (such as `GITHOO~1`), or anything whose resolved path
falls outside the checkout. Files are compared and written as raw bytes; no git clean/smudge filters
run on agent content. Git runs on agent-controlled directories only inside the sandbox. Each
workspace receives only its own repository's objects, streamed in as git bundles; no host
repository is mounted into the sandbox server.
If you edit a file locally while a turn is running, your version wins, and `rsa bridge status`
lists it as a conflict.

### Setup (one time)

```powershell
rsa server install            # deploy/update the server side (adds the bridge server mode)
rsa bridge start              # local bridge + sandboxed OpenCode server on the Linux host
rsa bridge t3-install         # adds the "OpenCode · Linux sandbox" provider to T3 (backs up settings.json)
rsa bridge autostart on       # start the bridge at Windows logon (Startup folder entry)

# DeepSeek / Gemini API models (free OpenCode models, incl. DeepSeek V4.1 Flash Free, need no key)
rsa server env set DEEPSEEK_API_KEY      # unlocks deepseek/* models
rsa server env set GEMINI_API_KEY        # unlocks google/gemini-* models
rsa bridge restart --remote              # the sandbox server reads keys when its container starts
```

### Daily use

- **Manual:** in T3 pick the provider **OpenCode · Linux sandbox** and a model. With
  **Full access** the agent runs unattended (shell, edits, package installs) inside the sandbox
  only. In **Auto** or stricter modes, T3 asks you to approve each command and edit, as with any OpenCode thread.
- **From an orchestrator** (Codex, Claude): `delegate_task` with
  `target {"providerInstanceId": "opencode_linux", "model": "..."}`. T3 never lets a child run with
  broader permissions than its parent. A child of an **Auto** orchestrator therefore asks you to
  approve shell commands and edits in the child thread. `runtimeMode: "auto-accept-edits"` lets
  file-only work run without approvals. For fully unattended shell work from an Auto orchestrator,
  use [`rsa dispatch`](#full-access-dispatch) (visible T3 threads, your profiles) or `rsa submit` (headless).
- **Monitor, interrupt, interact, and reconnect:** use T3 as usual (stop button, follow-up messages,
  `task_cancel`, `t3_thread_send`). Sessions live in the sandbox server and survive restarts of
  T3 and of the bridge; on reconnect, the bridge brings back changes made while it was away.
- **Review:** use T3's diff panel and checkpoints for the thread's checkout (or worktree), then
  commit, merge, or open a PR from T3.

### Bridge commands

| Command | Purpose |
|---|---|
| `rsa bridge start` / `stop [--remote]` / `restart [--remote]` | run the bridge; `--remote` also stops or recreates the sandbox server container (needed after `rsa server env set` or image updates) |
| `rsa bridge status [--json]` | connection, busy sessions, workspaces, last sync, conflicts, errors |
| `rsa bridge logs [-f]` | bridge log (`~/.rsa/bridge/bridge.log`); set `RSA_BRIDGE_TRACE=1` before `start` to log every request |
| `rsa bridge t3-install [--remove]` | add or remove the T3 provider instance (`--id`, `--name` to customize) |
| `rsa bridge autostart on\|off` | Startup-folder entry that runs `rsa bridge start` at logon |
| `rsa bridge forget <dir>\|--missing\|--all` | drop workspace mappings and delete their sandbox copies (bridge stopped); `--missing` prunes checkouts that no longer exist, such as merged T3 worktrees |
| `rsa server auth --bridge default login` | OAuth logins for the bridge server's own credential store |

Settings (client config, `rsa config <key> <value>`): `bridge_port` (47960), `bridge_instance`
(`default`; one sandbox server per instance), `bridge_password` (optional; if set, put the same
value in T3's provider settings as *Server password*). The bridge only listens on
127.0.0.1. It rejects requests that carry a browser `Origin` or `Sec-Fetch-Site`, and Host
headers other than localhost (DNS rebinding).

### Verified

These were exercised against the live system:
- Delegation from a T3 thread (`delegate_task`, DeepSeek V4.1 Flash Free) showed messages, reasoning,
  tool calls, file changes, and T3 checkpoints listing the agent's files.
- Two-way sync: a local deletion reached the sandbox before the next turn, and new files came back after it.
- Follow-up messages to the same child thread.
- Auto-mode approval requests surfaced in T3 (not bypassed).
- Interrupt via `task_cancel`.
- Bridge restart while idle and **during** a running turn: the turn finished, files synced, and the checkpoint was correct.
- Full-access session emulation (`tests/bridge_api_test.py`, 16/16 checks):
  - commands ran in the sandbox's Linux workspace without `NET_ADMIN`;
  - internet allowed; LAN, tailnet, and host blocked (checked by the agent *and* deterministically);
  - firewall immutable for agent-level root;
  - edits and shell-created files synced back; `.git` untouched;
  - a local edit made during a turn was kept and reported as a conflict.
- Write-back hardening (`tests/bridge_apply_test.py`, 11/11, offline). Hostile trees are refused:
  the hooks directory, its real 8.3 alias `GITHOO~1`, nested `.GIT`, and trailing-dot names. CRLF
  checkouts are handled without filters.
- An independent security review (separate agent) found the 8.3-alias, git-filter, and shared-repo-mount
  issues; all three are fixed above and re-tested.

### Not verified / limitations

- **Gemini and paid DeepSeek models.** Configuring the API keys makes them appear (checked with
  dummy keys), but no real key was available, so actual runs with them are untested.
- **Full Access started from the T3 UI.** It wasn't clicked through. It sends exactly the allow-all
  rule that the API test emulates.
- **T3 restart during a running turn.** Not tested (the server and sessions persist; T3 reconnects by session id).
- **Thread directories that are not git repositories** (e.g. T3 scratch threads) get a sandbox
  workspace but no file sync.
- **Worktree isolation needs a commit.** `isolation: "worktree"` children need the parent branch to
  have a commit (a T3 rule).
- **Shared sandbox server.** All threads of one bridge instance share one container, so they
  share its CPU and memory limits and can read and modify each other's workspaces. Changes in another
  workspace still return to that repo only through its own write-back checks. Use another
  `bridge_instance` (with its own T3 provider entry and port) for stronger separation.
- **The agent can read the container's secrets.** These are the provider API keys and the sandbox
  OpenCode server's password, because OpenCode runs tools inside its server process. An agent
  allowed to run shell commands (Full Access, or approved commands) can therefore call that server
  directly. That includes answering its own approval prompts and reading other sessions in the same
  instance. This never extends beyond the sandbox, but an approval policy inside one instance is
  only as strong as the shell permission. Use low-limit API keys.
- **No T3 orchestration tools.** Sandboxed agents don't get T3's MCP tools, because T3 doesn't inject
  them into external OpenCode servers. That is intentional here.
- **Path display.** Tool *outputs* show the Linux paths the agent actually used (`/ws/...`); path
  fields (tool inputs, file changes) are shown as Windows paths.

### Model credentials

Out of the box, sandboxes use OpenCode's free `opencode/*` models (default `opencode/big-pickle`).
To use your own providers:

```powershell
rsa server env set ANTHROPIC_API_KEY     # prompts for the value (hidden); OPENAI_API_KEY, OPENROUTER_API_KEY, ... work too
rsa server models                        # list the models now available inside the sandbox
rsa server config default_model anthropic/claude-sonnet-5-5
```

API keys are stored on the server in `~/.rsa/secrets/opencode.env` (mode 600). They are
injected as environment variables into each task container and never written to disk inside it.
OAuth or subscription logins are stored in the persistent `rsa-opencode-data` volume. You can do them
interactively with `rsa server auth login`. Alternatively, log in with OpenCode wherever a browser
is available and copy the credentials over:
`opencode auth export | ssh linuxbox ~/.rsa/app/rsa-server auth import`.

## Full Access dispatch

T3 never lets a thread start another thread with broader permissions than its own. An orchestrator
(Codex, Claude Code) in **Auto** mode therefore cannot start an unattended worker: its
`delegate_task` children ask you to approve every shell command. `rsa dispatch` is a separate,
small local service (`client/rsa_dispatch.py`) that **you** authorize instead:

- It connects to T3 through T3's documented **outside-agent MCP connection**: the `/mcp` endpoint
  with OAuth sign-in, the same mechanism as "Copy MCP URL" in Settings → Connections. You approve
  it on T3's own page and choose **Full access** there. It does not use, read, or extract any
  agent's T3 credentials.
- You approve **profiles**: a repository (a T3 project), the sandbox provider, the allowed models,
  the runtime mode, and limits. Within a profile, `rsa dispatch submit` starts an ordinary
  **top-level T3 thread** on `opencode_linux` in Full Access, in a **new git worktree**, without
  asking you again. It returns the thread id and a clickable `t3-thread://` link. Anything outside
  a profile is refused, or waits for your click in a Windows dialog. The dispatcher enforces this
  in code; nothing depends on the prompt.
- Workers are normal T3 threads. Progress, tool calls, diffs, checkpoints, follow-ups, stop, and
  reconnection behave exactly as for any `opencode_linux` thread, using the same bridge and file
  sync. Changes stay in the worker's worktree. **Nothing is merged automatically.**

```
 Orchestrator (Auto mode)       rsa dispatch (daemon, 127.0.0.1:47961)            T3 Code            rsa bridge -> sandbox
 rsa dispatch submit ...  ───▶  client key auth; profile + limits check   ──MCP──▶ t3_thread_launch ──▶ opencode_linux thread,
   (never holds the T3 token)   T3 token in memory + DPAPI vault          OAuth    (full-access ceiling   Full Access, new worktree
 rsa dispatch wait/send/...     signed policy/jobs, dialogs for exceptions token    set by the user)    (unattended, sandbox only)
```

### Setup (once, by you)

```powershell
rsa dispatch start                  # the local dispatcher (Windows; keeps its state in ~\.rsa\dispatch)
rsa dispatch autostart on           # start it at logon (recommended: then it never runs inside an agent's sandbox)
rsa dispatch authorize              # opens T3's sign-in page: choose "Full access" and approve
rsa dispatch profile add myrepo --repo C:\path\to\repo --model opencode/deepseek-v4.1-flash-free
                                    # a dialog shows exactly what you grant: click OK
rsa dispatch status                 # authorization (expiry), policy, bridge, running jobs
```

- `authorize`: if T3 asks for a **pairing code**, create one in T3 Settings → Connections (or
  `t3 auth pairing create`). The credential lasts **30 days**. T3 has no refresh tokens, so run
  `authorize` again before it expires (`rsa dispatch status` and `rsa doctor` show the days left).
  `rsa dispatch deauthorize` deletes the local credential; revoke the session in T3 too
  (Settings → Connections, or `t3 auth session list` / `t3 auth session revoke <id>`).
- The repository must already be a T3 project (add it in T3, or `t3 project add <path>`), and the
  bridge must be set up ([T3 Code integration](#t3-code-integration)).
- Profile options (defaults in brackets): `--model` (repeat; globs like `opencode/*` allowed),
  `--default-model` [the first concrete model], `--provider` [`opencode_linux`], `--mode`
  [`full-access`; or `auto-accept-edits`/`auto`/`approval-required` for workers that ask you],
  `--max-concurrent` [1], `--max-runtime-minutes` [60 per turn], `--max-followups` [10 per job],
  `--max-jobs-per-day` [20]. `profile add` with an existing name replaces that profile.
  `rsa dispatch profiles` lists them.
- Global: `rsa dispatch limits --max-concurrent N` [2 running jobs in total]. `rsa dispatch disable`
  stops all new dispatching at once; `enable` turns it back on.
- **What needs your click:** anything that grants more (adding/replacing a profile, raising the
  global limit, `enable`, and one-off `--ask` exceptions). Tightening (`profile remove`,
  `disable`, lowering limits, `deauthorize`) applies immediately. The dialog comes from the
  dispatcher process. Only **OK** approves; Cancel, closing it, or 5 minutes without an answer deny.

### Use (orchestrators, or you)

```powershell
rsa dispatch profiles --json                      # what is approved
rsa dispatch submit -f task.md --title "Add retry" --requester "<orchestrator>" --key <unique> --json
rsa dispatch wait <job> --timeout 600 --json      # 0 completed, 1 failed/interrupted/cancelled, 3 still running
rsa dispatch status <job> --json                  # T3 status, worktree, branch, link, last message, needs_user
rsa dispatch send <job> -f followup.md --json     # follow-up in the same thread (queued if busy)
rsa dispatch cancel <job>                         # interrupt; the job takes no further messages
rsa dispatch result <job>                         # final message + files changed in the worktree (uncommitted)
rsa dispatch list [--all]
```

- **Exit 4** means *denied by policy*, and nothing was started. With `--json`, the reply is
  `{"code": "denied", "violations": [...]}`. Agents should check `code`: `powershell -Command`
  wrappers turn any non-zero exit code into 1. `--ask` asks you once, in the dialog, instead of failing outright: for a model
  outside the profile, a full concurrency, follow-up or daily limit. Requests for unknown
  repositories, a non-sandbox provider, a disabled dispatcher, or an expired authorization are
  always refused.
- Workers start from a **committed** branch: by default the submitter's current branch when it is
  in the same repository, otherwise the profile checkout's branch. Uncommitted changes are not
  sent. Each job gets the branch `rsa-dispatch/<job>` and a T3 worktree. Review with T3's diff
  panel or `git -C <worktree> diff`, then commit and merge yourself. Delete finished threads in T3
  to remove their worktrees (T3's storage cleanup settings apply).
- **Duplicates:** `--key` makes submissions (and `send` messages) idempotent. Without a key, an
  identical request while the same job is still running returns that job. If T3's reply to a
  launch is lost, the dispatcher finds the thread by the job id in its title instead of
  launching twice.
- **Restarts:** jobs are recorded in `~\.rsa\dispatch\jobs.json`. After a dispatcher restart (or
  T3/bridge restarts) it re-attaches and keeps monitoring. Turns longer than
  `max_runtime_minutes` are interrupted.
- The Auto orchestrator's **own** T3 tools can read these threads, but cannot message or stop them.
  T3 refuses with `runtime_mode_escalation_denied`, because the threads run in a broader mode.

### Security model and limitations

What is enforced, and where:

| Rule | Enforced by |
|---|---|
| Dispatched threads never exceed the access you chose on T3's sign-in page | T3 (session ceiling) |
| Default deny; repository, models, mode, and limits from your profiles | dispatcher |
| Workers only run on a T3 provider instance whose `serverUrl` is the rsa bridge (the Linux sandbox). Host providers such as `claudeAgent` or `codex` are refused whatever a profile says. The launched thread is re-checked, and interrupted if T3 put it elsewhere | dispatcher (hard rule) |
| Only threads the dispatcher started can be messaged or interrupted through it; it never merges | dispatcher |
| It calls a fixed list of 7 T3 tools; there is no pass-through to other T3 tools or to shell commands | dispatcher |
| Workers execute only inside the sandbox: egress firewall, no `NET_ADMIN`, rootless Docker | sandbox (unchanged) |
| Agent files come back only through the bridge's write-back (never `.git` or hooks; your local edits win) | bridge (unchanged) |

Credentials:

- **T3 credential:** held in the dispatcher's memory, and at rest in `~\.rsa\dispatch\vault.bin`,
  encrypted with Windows DPAPI for your account. It is never printed, logged, returned by any
  command, or put on a command line. It is sent only to T3's loopback `/mcp`, after checking T3's
  environment id. The `rsa dispatch` CLI that orchestrators run never holds it. The CLI talks to
  the daemon with `~\.rsa\dispatch\client.key`, which only unlocks the policy-limited dispatch
  API. Browsers (`Origin`), foreign `Host` headers, and requests without the key are refused.
- **Docker workers** never receive it, because it never leaves Windows. Sandboxes cannot connect
  to this PC on any of its addresses (LAN, Tailscale, Hyper-V/WSL), so they cannot reach T3 either.
- **Integrity:** `policy.json` and `jobs.json` are signed with a key from the vault. If either was
  edited, it is rejected: an edited policy denies everything until you approve a change, and an
  edited job registry is set aside.

Limitations (read before relying on it):

- **Same Windows account.** Orchestrator agents run as your user. DPAPI keeps the vault from other
  accounts and offline copies. It does not stop a process of your own account that deliberately
  decrypts it. The same is true of re-signing the policy, or clicking the dialog with UI
  automation. Such a process could equally mint T3 credentials with T3's own CLI. The dispatcher
  does not widen what an agent can do *outside* the `rsa dispatch` interface: that remains
  governed by the agent's own mode. If you pre-approve `rsa dispatch` for your agents (see
  [Daily use](#daily-use)), do not also pre-approve arbitrary scripts or reading `~\.rsa\dispatch`.
- **Shared sandbox container.** Dispatched jobs run in the same `rsa-oc-<instance>` container as
  each other and as your interactive `opencode_linux` threads. A Full Access worker can read and
  change other workspaces in that container. It can also read the sandbox server's password and
  your provider API keys, and so drive other sessions there. Changes it makes in another workspace
  reach that checkout only through the normal write-back, and show up in that thread's diff. Keep
  secrets out of profiled repositories, use low-limit API keys, and keep `max_concurrent` small.
- **Resources** (CPU, memory, PIDs) are limited per container, and every job in it shares them. The
  dispatcher limits the number of jobs and their duration, not CPU per job.
- **The bridge port** (127.0.0.1:47960) is itself an unauthenticated local entry to the sandbox
  server unless `bridge_password` is set. The dispatcher does not change that.
- Windows only (DPAPI, native dialog). No refresh tokens: re-authorize every 30 days.

### Verified

Exercised on 2026-10-22 against T3 Code 0.1.2 (outside-agent MCP), the live bridge, and the sandbox:

- `tests/dispatch_test.py` (offline, 58/58). It uses a fake T3 that speaks OAuth and MCP (JSON
  and SSE replies), runs the real dispatcher, and calls the CLI as a subprocess. Covered: sign-in;
  a lower T3 ceiling refusing Full Access; default deny; dialog approve/deny; host providers and
  non-projects refused; launch arguments; idempotency; concurrency, model, follow-up, and runtime
  limits; `--ask`; cancel; ownership; lost launch replies; foreign-provider threads; T3 down and
  restarted; dispatcher restarts; tampered policy and job files; expiry; and no token in any
  output or file.
- `tests/dispatch_live_test.py` (live, 30/30), run by a Claude orchestrator in **Auto** mode after
  the user authorized the dispatcher in T3 and approved a profile in the dialog. The checks:
  - Out-of-profile requests were refused, and nothing started.
  - Full Access worker threads appeared in T3 (`creationSource: mcp`, `opencode_linux`,
    `full-access`, their own worktree).
  - The worker ran shell commands with no approvals (`uname -s` → `Linux`, `id -u` → `0`), and
    its file synced back into the worktree only.
  - A follow-up ran in the same thread, across a dispatcher restart.
  - `cancel` interrupted a running `sleep 600`. The command never completed, and the job refused
    further messages.
  - The sandbox could not reach T3 on any of this PC's addresses (with a working internet control),
    and its environment holds no T3 variables.
- The Auto orchestrator's own `t3_thread_send` to a dispatched thread was refused by T3
  (`runtime_mode_escalation_denied`). The orchestrator's thread stayed in Auto.
- A **Codex** child in Auto mode (`gpt-6.1-sol`) ran `rsa dispatch profiles` without any sandbox
  or approval prompt. Its out-of-profile `submit` was refused (`code: denied`), and Codex's
  PowerShell wrapper reported that as exit 1.
- Not exercised live: `--ask` one-off approvals with a real click (only the profile dialog was
  clicked live; the dialog's timeout was tested, and the offline test covers both answers); a T3
  restart while a job runs (the offline test covers losing the MCP session); and token expiry
  against the real T3.
- The native dialog appears as a visible top-level window, and its timeout counts as a denial.

## Daily use

```powershell
cd C:\path\to\some\repo
rsa submit -f task.md --title "Add retry to HTTP client"      # or: rsa submit "short prompt"
rsa list                       # tasks for this repo (--all for every repo, --active for queue only)
rsa logs <id> -f               # live agent activity; --sandbox for the container log, --raw for JSON events
rsa wait <id> --timeout 600    # exit 0 succeeded, 1 failed/cancelled, 3 still running
rsa result <id>                # fetch into refs/rsa/<id> + summary, commits, diffstat
rsa diff <id>                  # full diff vs. the base
git worktree add ..\try-<id> rsa/<id>    # test it in isolation
git merge --no-ff rsa/<id>               # integrate (your decision)
rsa clean <id>                 # drop the local ref afterwards
rsa cancel <id> / rsa retry <id> [-m provider/model]
```

Every task command supports `--json`. Prompts can come from an argument, `-f FILE` (UTF-8,
recommended for anything multi-line), or stdin (`-f -`).

**Orchestrator agents.** `rsa install --skills` copies `skill/remote-agents/SKILL.md` into
`~/.claude/skills/` and `~/.codex/skills/`, so Claude Code and Codex know when and how to
delegate, monitor, and review. `rsa` makes SSH connections, so a sandboxed Codex/Claude session
will ask for approval the first time, exactly as for any other networked command. To pre-approve
it, add a rule in your own settings: in Claude Code, `"permissions": {"allow": ["Bash(rsa:*)",
"PowerShell(rsa:*)"]}`; in Codex, a `prefix_rule(pattern=["rsa"], decision="allow")` rule in
`~/.codex/rules/`. This is optional and left to you, because it changes your approval boundary.
A narrower choice for unattended T3 workers is to allow only `rsa dispatch` (e.g. `Bash(rsa dispatch:*)`,
`pattern=["rsa", "dispatch"]`). Its commands enforce your profiles themselves, and its
policy-widening commands still need your click in the dialog.

## Administration

| Command | Purpose |
|---|---|
| `rsa server install` | deploy/update server code, rebuild the image if `server/sandbox/` changed, restart the worker |
| `rsa server start` / `stop` / `restart` / `status` | control `rsa-worker.service`. `stop` also stops running sandboxes and requeues their tasks; `restart` leaves them running and re-attaches |
| `rsa server logs [-f]` | worker journal (`journalctl --user -u rsa-worker`) |
| `rsa server config [key [value\|default]]` | view or change server settings (below) |
| `rsa server env list\|set KEY\|unset KEY` | provider API keys for sandboxes |
| `rsa server auth ...` / `models` | `opencode auth ...` / `opencode models` inside a sandbox |
| `rsa server build [--pull]` | rebuild the sandbox image (e.g. after editing the Dockerfile) |
| `rsa server shell` | interactive shell in a throwaway sandbox (same isolation as tasks) |
| `rsa server selftest` | verify firewall, capability drop, DNS, OpenCode |
| `rsa server gc` | prune old tasks now (also runs hourly) |
| `rsa doctor` | end-to-end health check (local tools, Tailscale peer, SSH, server checks) |

Server settings (`~/.rsa/config.json`; defaults in `server/rsa-server`):

| Key | Default | Notes |
|---|---|---|
| `default_model` | `opencode/big-pickle` | any `provider/model` that `rsa server models` lists |
| `timeout_minutes` | 60 | per-task default; `rsa submit -t` overrides |
| `cpus` / `memory` / `pids_limit` | `3` / `6g` / 2048 | per sandbox (host has 4 CPUs, 16 GB) |
| `workers` | 1 | concurrent tasks; lower `memory` before raising this, then `rsa server restart` |
| `max_attempts` | 2 | automatic retries, only for infrastructure failures (sandbox didn't start, host/docker restart) |
| `retention_days` | 14 | finished tasks, logs and server-side refs are pruned after this |
| `block_private_network` | `true` | egress firewall in every sandbox |
| `dns` | `["1.1.1.1", "9.9.9.9"]` | public resolvers, so the LAN resolver isn't needed |
| `require_rootless` | `true` | the worker refuses to run on a rootful Docker daemon |
| `opencode_version` | `2.1.3` | pinned in the image; change it, then run `rsa server build` |
| `max_bundle_mb` | 200 | results larger than this are rejected |

**Customizing the sandbox:** edit `server/sandbox/Dockerfile` to add toolchains (Go, Rust,
Java, ...) and `server/sandbox/opencode.json` for OpenCode settings, then run `rsa server install`.

## Files and logs

Server state lives in `~/.rsa/` on latitude7370:

```
app/                     deployed code (from server/ in this repo)
config.json              settings overrides
secrets/opencode.env     provider API keys (600)
repos/<repo>-<hash>.git  one bare repo per source repository
queue/<id>               pending tasks
tasks/<id>/
  in/prompt.md, in/task.json     what was asked
  status.json                    state, timings, result sha, stats, summary
  task.log                       lifecycle (queued, started, finished, retries)
  out/events.jsonl               every OpenCode event (tool calls, messages, tokens)
  out/sandbox.log                container log (firewall, workspace, opencode stderr)
  out/result.json                sandbox-side outcome
```

Docker objects: the image `rsa-sandbox:latest`, containers `rsa-<id>` (removed after each task),
and the volume `rsa-opencode-data` (OpenCode credentials and session DB, shared by tasks).

## Security model

- **Boundary = container on rootless Docker.** Container root maps to the unprivileged
  user `daniel`, not host root. The only host paths a sandbox sees are its repo (read-only), its task
  `in/` (read-only), and its `out/` directory. There is no Docker socket, no SSH keys, no
  GitHub token, and no home directory. The rootful Docker daemon on the host is never used.
- **Network:** public internet only. LAN (`192.168/16`, `10/8`, `172.16/12`), Tailscale
  (`100.64/10`, which includes this host's own tailnet IP and the T3 Code `tailscale serve`
  endpoint), link-local/metadata, and IPv6 ULA/link-local addresses are rejected. Host loopback
  services are not reachable with rootless networking.
- **Agent permissions:** unrestricted *inside* the sandbox (root, package installs, shell).
- **Git transfer:** the host imports agent output only from a bundle (data), validated with
  `fsckObjects`. It never runs git commands in agent-controlled directories, so no hooks or config
  from the agent can execute on the host. Locally, `rsa` writes only `refs/rsa/*`.
- **Your Codex/Claude approval settings are unchanged.** `rsa` is an ordinary command
  subject to them.

Residual risks you should know about:

- Sandboxes can reach the internet, so a prompt-injected agent could exfiltrate the repository
  it was given and the provider API keys in its environment. Don't delegate repos containing
  secrets you can't afford to expose that way.
- The `rsa-opencode-data` volume (OAuth credentials, OpenCode DB) is shared by all tasks and
  is writable by them. A malicious task could tamper with it and affect later tasks, though it
  cannot affect the host. Reset it with
  `docker volume rm rsa-opencode-data` on the server (you will need to log in again afterwards).
- A container escape through a kernel or runtime bug would land as the user `daniel`. For a stronger
  boundary, run the server part under a dedicated Linux user with its own rootless Docker (needs sudo once).

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `ssh to linuxbox failed` | `tailscale status` (is latitude7370 online?), `ssh linuxbox true` |
| `unexpected reply from server` / `No such file` | server not deployed: `rsa server install` |
| tasks stay `queued` | `rsa server status`, `rsa server logs`; start with `rsa server start` |
| `docker unavailable, holding the queue` in logs | on the server: `systemctl --user status docker`; tasks resume automatically |
| task failed: `Model unavailable: x/y` | provider key or login missing: `rsa server env set ...` or `rsa server auth login`; check `rsa server models` |
| task failed with no agent output | `rsa logs <id> --sandbox` (container log), then `rsa server selftest` |
| `ran out of memory` | `rsa server config memory 8g`, or ask for smaller steps |
| timed out | partial work is still harvested (`rsa result <id>`); retry with `-t` larger |
| `belongs to repo X, but this is Y` | run `rsa result/diff` inside the repository the task came from |
| image `stale` in doctor | `rsa server build` (or `rsa server install`) |
| worker gone after logout/reboot | needs lingering: `sudo loginctl enable-linger daniel` (already enabled here) |
| T3 shows the sandbox provider with an error | `rsa bridge status`; `rsa bridge start` (enable `rsa bridge autostart on`) |
| sandbox thread turn fails to start | `rsa bridge logs` (look for `bridge error` / `workspace preparation failed`) |
| agent's changes missing from the checkout | `rsa bridge status` shows `conflicts` (your local edits won) or `last_error`; the agent's version is in `refs/rsa/ws/<key>` |
| new API key not picked up by T3 threads | `rsa bridge restart --remote` |
| `rsa dispatch` exit 4 (denied) | the listed violations say why; `rsa dispatch profiles` / `status`. Widen the profile yourself (`rsa dispatch profile add`, confirm the dialog) |
| `rsa dispatch is not running` | `rsa dispatch start` (and `rsa dispatch autostart on`); `rsa dispatch logs` |
| dispatcher not authorized / expired / `t3_mismatch` | `rsa dispatch authorize` (choose Full access); T3 must be running |
| `POLICY REJECTED` in `rsa dispatch status` | `policy.json` was edited outside rsa dispatch; re-add your profiles with `rsa dispatch profile add` (each asks for confirmation) |
| `cannot read ...vault.bin` | the vault is damaged or from another account: delete `~\.rsa\dispatch\vault.bin`, then `rsa dispatch restart`, `authorize`, and re-add profiles |
| no dialog appeared for a profile change | the dispatcher must run in your desktop session: `rsa dispatch restart` from your own terminal, or use autostart |
| want a clean slate | `rsa server stop`, then on the server `rm -rf ~/.rsa/tasks/* ~/.rsa/queue/*`, then `rsa server start` |

## Reverse engineering tools

Every sandbox includes [Ghidra 12.1.4](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build),
a full JDK 21, and [Reverse Engineer Anything (REA) 6.7.0](https://github.com/morluto/rea).
REA is pinned to its supported Ghidra 12.1 series. The Ghidra release ZIP is checked
against its SHA-256 during the image build; no tools are downloaded at agent startup.

Queue tasks and T3 bridge sessions both load the local `rea` MCP server and its
matching `reverse-engineer-anything` skill. The additive config at
`/etc/rsa/reverse-engineering.json` supplies tools and instructions without changing
their permission rules. Agents can use native decompilation, disassembly, strings,
and cross-references, plus REA's built-in static JavaScript/Electron/ASAR and artifact
inspection. Optional engines (IDA, Hopper, JADX, firmware tools, browser runtimes)
are not included. Only the capabilities reported by the installed REA are available.

Supply target artifacts inside the repository (use `rsa submit --dirty` for
uncommitted files), or an authorized public download URL. Windows installations
and host applications are not mounted into the sandbox. Ask agents to keep analysis
databases in `/tmp` and return requested reports in the checkout. For example:

```text
Analyze artifacts/sample.elf with REA's Ghidra provider. Find the input validation
function, explain it with Evidence IDs and pseudocode, and write findings.md.
Keep analysis databases outside the checkout; do not execute the target binary.
```

The CLI is also available as `rea`; direct Ghidra scripts can use `analyzeHeadless`.
`GHIDRA_INSTALL_DIR=/opt/ghidra`, `JAVA_HOME=/opt/java/openjdk`, and a default
`GHIDRA_HEADLESS_MAXMEM=2G` are configured. The Java heap shares the sandbox's total
memory budget with OpenCode, REA, and other processes; large targets may need a
larger container memory limit or a smaller analysis scope.

Update existing installations with `rsa server install`, then
`rsa bridge restart --remote` when the bridge has no active turns. New queue tasks
use the rebuilt image automatically. `rsa server selftest` now compiles a small
native fixture and verifies Ghidra pseudocode and JavaScript evidence over REA MCP,
in addition to the isolation checks. This test does not execute the native fixture
or require a model. For the tool test alone inside a sandbox:

```bash
python3 /etc/rsa/reverse-engineering-selftest.py
```

## C and C++ builds with CMake

Every sandbox includes [CMake 4.4.4](https://github.com/Kitware/CMake/releases/tag/v4.4.4)
(`cmake`, `ctest`, `cpack` from Kitware's release build, checked against its SHA-256),
Ninja, GCC/G++, GNU Make, and `pkg-config`. The agent instructions at `/etc/rsa/cmake.md`
are loaded by the same additive config as the reverse engineering tools. They tell
agents to configure out of tree (`cmake -S . -B /tmp/build/<project> -G Ninja`), use
the project's `CMakePresets.json` when present, run tests with `ctest`, and report any
Debian `-dev` packages they had to install.

CMake build trees are filtered out of results. Before harvesting, the queue's auto-commit and
the bridge's write-back run `rsa-ignore-build-trees`. It adds every untracked directory
that holds a `CMakeCache.txt` and no tracked files to the workspace's `.git/info/exclude`,
so a configure into `build/` inside the checkout does not reach your checkout or count
toward `max_bundle_mb`. In bridge workspaces those trees are kept, so incremental builds
survive between turns. An in-source build at the repository root (`cmake .`) cannot be
told apart from sources and is not filtered. Neither is build output that an agent
commits itself.

CMake 4 no longer configures projects whose `cmake_minimum_required` is below 3.5.
Agents are told to retry with `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` and to say so,
rather than editing the project. Compilers share the sandbox's `cpus` and `memory`
limits. For large C++ builds, raise `rsa server config memory` or ask for fewer
parallel jobs.

`rsa server selftest` builds and tests a small C/C++ project with both the Ninja and
Makefile generators, and checks the build-tree filter. To run it alone inside a
sandbox:

```bash
bash /etc/rsa/cmake-selftest.sh
```

## Limitations

- Agents see committed state (or a `--dirty` snapshot). Git submodules are not populated, and
  Git LFS files stay as pointer files.
- Very large repositories: the first push sends the full history (later pushes are incremental),
  and auto-committed artifacts count toward `max_bundle_mb`.
- `rsa` prompts passed inline through `rsa.cmd` are subject to cmd.exe quoting (`%`, `"`,
  newlines). Use `-f FILE` for anything non-trivial.
- One worker by default. More can run concurrently (`workers`), but OpenCode's shared data volume
  then sees concurrent writers. That is fine for SQLite WAL, but untested at high parallelism.
- The free default model is fine for small tasks. Configure a stronger provider for real work.

## Uninstall

```bash
# on the server
systemctl --user disable --now rsa-worker.service && rm ~/.config/systemd/user/rsa-worker.service
docker rm -f $(docker ps -aq --filter label=rsa.task) 2>/dev/null; docker rmi rsa-sandbox:latest; docker volume rm rsa-opencode-data
docker rm -f rsa-oc-default; docker volume rm rsa-oc-default-data
rm -rf ~/.rsa
```
```powershell
# on Windows
rsa dispatch deauthorize; rsa dispatch stop; rsa dispatch autostart off   # then revoke it in T3 Settings > Connections
rsa bridge stop; rsa bridge autostart off; rsa bridge t3-install --remove
Remove-Item ~\.rsa\bridge, ~\.rsa\dispatch -Recurse
Remove-Item ~\.local\bin\rsa, ~\.local\bin\rsa.cmd, ~\.claude\skills\remote-agents, ~\.codex\skills\remote-agents -Recurse
git for-each-ref --format="%(refname)" refs/rsa/ | ForEach-Object { git update-ref -d $_ }   # per repo
```

## Repository layout

```
AGENTS.md                     guidance for coding agents working on this repo
client/rsa.py                 Windows/any-OS client (stdlib Python)
client/rsa_bridge.py          T3 bridge: local OpenCode-compatible endpoint, path mapping, file sync
client/rsa_dispatch.py        Full Access dispatch: T3 outside-agent MCP client, profiles, local API, CLI
server/rsa-server             server CLI + worker (stdlib Python)
server/rsa-worker.service     systemd user unit
server/sandbox/Dockerfile     sandbox image
server/sandbox/entrypoint.sh  in-container firewall, workspace, opencode run, result bundle
server/sandbox/rsa-ignore-build-trees  keeps CMake build trees out of task results and write-back
server/sandbox/cmake.md       agent instructions for CMake builds (cmake-selftest.sh: its selftest)
server/sandbox/opencode.json  OpenCode config for headless tasks (permissions, no updates/sharing/snapshots)
server/sandbox/opencode-server.json  OpenCode config for the bridge server (T3 supplies per-session rules)
server/sandbox/rsa-ws         workspace operations run inside the bridge server (init/reset/export)
skill/remote-agents/SKILL.md  instructions for Claude Code / Codex orchestrators
tests/e2e.py                  end-to-end test of the headless queue against the live server
tests/bridge_api_test.py      end-to-end test of the bridge (full-access emulation, isolation, sync)
tests/dispatch_test.py        offline test of rsa dispatch against a fake T3 (policy, sign-in, integrity, recovery)
tests/dispatch_live_test.py   live test of rsa dispatch (real T3, bridge and sandbox; needs authorization + a profile)
```
