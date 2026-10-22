# remote-subagent-system (`rsa`)

Delegate coding tasks from an orchestrator (T3 Code, Codex, or Claude Code on Windows) to
unattended [OpenCode](https://opencode.ai) agents. Each agent runs in its own disposable,
rootless Docker sandbox on a Linux server, connected over Tailscale.

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
```

The client defaults to SSH host `linuxbox`. Change it with `rsa config host <alias>` or the `RSA_HOST` env var.

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
| want a clean slate | `rsa server stop`, then on the server `rm -rf ~/.rsa/tasks/* ~/.rsa/queue/*`, then `rsa server start` |

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
rm -rf ~/.rsa
```
```powershell
# on Windows
Remove-Item ~\.local\bin\rsa, ~\.local\bin\rsa.cmd, ~\.claude\skills\remote-agents, ~\.codex\skills\remote-agents -Recurse
git for-each-ref --format="%(refname)" refs/rsa/ | ForEach-Object { git update-ref -d $_ }   # per repo
```

## Repository layout

```
client/rsa.py                 Windows/any-OS client (stdlib Python)
server/rsa-server             server CLI + worker (stdlib Python)
server/rsa-worker.service     systemd user unit
server/sandbox/Dockerfile     sandbox image
server/sandbox/entrypoint.sh  in-container firewall, workspace, opencode run, result bundle
server/sandbox/opencode.json  OpenCode config for sandboxes (permissions, no updates/sharing/snapshots)
skill/remote-agents/SKILL.md  instructions for Claude Code / Codex orchestrators
tests/e2e.py                  end-to-end test against the live server
```
