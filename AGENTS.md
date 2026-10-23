# AGENTS.md

Guidance for coding agents working **on this repository**. To *use* the system from another
repo (delegating work to sandboxed agents), see `skill/remote-agents/SKILL.md`. For user-facing
setup and operations, see `README.md`.

## What this is

`rsa` runs OpenCode agents in rootless Docker sandboxes on a Linux host (`latitude7370`), driven
from a Windows machine over SSH and Tailscale. It has three parts:

- **Headless queue:** `rsa submit` → `rsa-worker.service` → one disposable container per task. Results
  come back as git bundles, landing in `refs/rsa/<id>`.
- **T3 bridge:** `client/rsa_bridge.py` is a localhost OpenCode-compatible endpoint that T3 Code uses as an
  external OpenCode server (provider instance `opencode_linux`). It forwards the OpenCode v2 API to a
  long-lived sandbox container (`rsa-oc-<instance>`), maps Windows paths to `/ws/...`, and syncs files
  around each turn.
- **Full Access dispatch:** `client/rsa_dispatch.py` is a local daemon (127.0.0.1:47961) that the user
  authorizes in T3 through T3's outside-agent MCP sign-in. Within user-approved profiles, it starts
  top-level `opencode_linux` threads in Full Access for orchestrators that run in Auto mode
  (`rsa dispatch submit|wait|send|cancel|result`).

## Layout

| Path | Runs on | Role |
|---|---|---|
| `client/rsa.py` | Windows (any OS) | CLI: task commands, `rsa server ...` passthrough over SSH, `rsa install` |
| `client/rsa_bridge.py` | Windows | `rsa bridge ...`: HTTP proxy, SSH tunnel, path mapping, workspace sync |
| `client/rsa_dispatch.py` | Windows | `rsa dispatch ...`: T3 MCP/OAuth client, DPAPI vault, signed policy and jobs, local API, CLI |
| `server/rsa-server` | Linux host | queue, worker, admin CLI, `oc-*` commands for bridge servers |
| `server/rsa-worker.service` | Linux host | systemd user unit for the worker |
| `server/sandbox/Dockerfile` | image | `rsa-sandbox:latest` (node 22 + git/python/build tools + `@opencode/cli`) |
| `server/sandbox/entrypoint.sh` | container | modes: `task`, `shell`, `opencode`, `selftest`, `server`, `fw` |
| `server/sandbox/rsa-ws` | container | bridge workspace ops via `docker exec` (`init`/`reset`/`export`/...) |
| `server/sandbox/opencode*.json` | container | OpenCode config for tasks / for the bridge server |
| `server/sandbox/rsa-ignore-build-trees` | container | excludes untracked CMake build trees before results are harvested |
| `server/sandbox/cmake.md`, `cmake-selftest.sh` | container | agent instructions for CMake builds; their selftest |
| `skill/remote-agents/SKILL.md` | agents | installed into `~/.claude/skills` and `~/.codex/skills` |
| `tests/` | Windows | `bridge_apply_test.py`, `dispatch_test.py` (offline); `e2e.py`, `bridge_api_test.py`, `dispatch_live_test.py` (live) |

## Hard rules

- **Never modify T3 Code** (its install, `server.asar`, or anything under `AppData\Local\Programs\t3code`).
  Integrate only through supported settings (`~/.t3/userdata/settings.json` → `providerInstances`)
  and the OpenCode API.
- **Never commit or push unless the user asks.** The deployed system runs straight from this checkout
  (the shims in `~/.local/bin` point here), so a broken edit breaks the live tools.
- **Python standard library only.** No pip dependencies. Client: Python 3.8+ (Windows). Server: 3.9+.
- **Don't touch unrelated things on the Linux host.** It runs other user services (a T3 server, MCP
  servers, dashboards) and a rootful Docker daemon. Only use the rootless daemon
  (`unix:///run/user/1000/docker.sock`), `~/.rsa`, `rsa-*` containers, images, and volumes.
- **Server files must keep LF endings** (`.gitattributes` enforces this; `rsa server install` also
  normalizes). Keep `rsa-server`, `entrypoint.sh`, `rsa-ws`, and `rsa-ignore-build-trees` executable
  (`server_bundle()` in `rsa.py` sets the mode by filename; update it if you add a script).

## Security invariants (don't regress these)

Treat everything produced inside a sandbox as hostile: files, `.git`, bundles, API responses, events.

1. **The host never runs git inside agent-controlled directories.** Agent output arrives as a git
   bundle (data) and is fetched with `transfer.fsckObjects`. In the bridge, all git on `/ws/*` runs
   inside the container (`rsa-ws`).
2. **Task containers** install the firewall in stage 1, then drop `NET_ADMIN` from the bounding set
   (`setpriv`) before anything else runs.
3. **Bridge server containers never get `NET_ADMIN`.** `docker exec` inherits container caps, so the
   firewall comes from the separate `fw` helper container sharing the network namespace. Do not mount
   host repos into the server container. Workspaces receive their own repo's objects only, as bundles on
   `docker exec -i` stdin.
4. **Write-back into Windows checkouts** (`apply_changes`) only writes regular files inside the checkout:
   - never `.git` (any case), the repo's `core.hooksPath` dir, symlinks or junctions, 8.3 aliases
     (`~N`), reserved device names, or names ending in a space or dot;
   - targets are re-checked after `realpath`;
   - content is handled as raw bytes: no git clean/smudge filters on agent data;
   - a user's local edit always wins and is reported as a conflict.
5. **The bridge only listens on 127.0.0.1.** It rejects foreign `Host` headers, any `Origin`, and
   cross-site `Sec-Fetch-Site`. It must never answer T3 permission or approval requests, alter
   session permission rules, or let a thread run with a broader mode than T3 granted.
6. **Session→workspace mappings** are learned from T3's own requests and responses, never from upstream events.
7. **Secrets:** `~/.rsa/secrets/opencode.env` is mode 600; the server password state file is 600. Never
   log secrets or put them on command lines. Note: the agent *can* read secrets inside its own
   container (inherent to OpenCode). Don't add more.
8. **Egress:** sandboxes reach the public internet only. LAN, `100.64/10` (Tailscale), and link-local are
   blocked. Replies on inbound connections are allowed via conntrack `ESTABLISHED,RELATED`.
9. **Dispatcher (`rsa_dispatch.py`):**
   - **The T3 credential.** It comes only from the user's own approval on T3's OAuth page; never
     reuse, read, or extract an agent's T3 credentials. It lives in daemon memory and in the DPAPI
     vault. It must never appear in output, logs, command lines, API replies, or the Linux side, and
     it is sent only to T3's loopback `/mcp` after checking the environment id.
   - **Default deny.** Policy is checked in code. Granting more (profiles, `enable`, raised limits,
     `--ask` exceptions) needs the native dialog (`confirm()`), never a flag or an API call.
     `policy.json` and `jobs.json` stay HMAC-signed; a bad signature fails closed.
   - **Sandbox providers only.** Dispatched threads only run on provider instances whose
     `serverUrl` is the rsa bridge; re-check the launched thread. Never add generic T3 tool
     pass-through (`T3_TOOLS` is the allowlist), and only act on threads in the signed job registry.
   - **No auto-merge.** Each job gets its own new worktree.

## Making changes take effect

| You changed | Run |
|---|---|
| `client/rsa.py` | nothing (shims point at this checkout) |
| `client/rsa_bridge.py` | `rsa bridge restart` |
| `client/rsa_dispatch.py` | `rsa dispatch restart` (running jobs re-attach; the CLI side needs nothing) |
| `server/rsa-server`, `rsa-worker.service` | `rsa server install` (re-deploys and restarts the worker; running tasks re-attach) |
| anything in `server/sandbox/` | `rsa server install` (rebuilds the image when its hash changes), then `rsa bridge restart --remote` to recreate the bridge server container |
| sandbox workspace format (`rsa-ws`, mounts) | also `rsa bridge stop; rsa bridge forget --all; rsa bridge restart --remote`. Workspaces re-provision on next use. |
| `skill/remote-agents/SKILL.md` | `python client/rsa.py install --skills` |

## Testing

Run what matches your change. Live tests use the real server, free OpenCode models, and temp
repos in `%TEMP%` that they clean up.

```powershell
python -I -m py_compile client\rsa.py client\rsa_bridge.py client\rsa_dispatch.py   # syntax (rsa-server: python -c "import ast; ...")
python tests\bridge_apply_test.py          # offline, ~5 s: write-back hardening (8.3 aliases, hooks, .git, CRLF, conflicts)
python tests\dispatch_test.py              # offline, ~30 s: dispatcher policy, sign-in, integrity, recovery (fake T3)
python tests\dispatch_live_test.py --profile NAME   # live, ~3 min: needs the user's `rsa dispatch authorize` + profile
rsa server selftest                         # sandbox isolation (firewall, caps, DNS)
python tests\bridge_api_test.py            # bridge, needs `rsa bridge start`; ~2 min
python tests\e2e.py                         # headless queue core, ~3 min
python tests\e2e.py --resilience            # + worker restart/crash/stop and cancel, ~8 min
rsa doctor                                  # end-to-end health incl. bridge and T3 entry
```

- **Assert sandbox properties deterministically**, e.g. `docker exec rsa-oc-default ...` as root.
  Don't rely on the model's output: small models skip, rewrite, or refuse commands (notably `iptables -F`).
- **The T3-native path** (delegation, approvals, interrupt, checkpoints) can only be exercised from
  inside a T3 thread (`delegate_task` to `opencode_linux`). An Auto-mode thread cannot create T3
  projects or Full Access children. Use `runtimeMode: "auto-accept-edits"` with file-only tasks,
  and clean up any test files it writes.
- **The dispatcher's authorization and profile approvals are the user's.** Never complete T3's sign-in
  yourself (for example with `t3 auth pairing create`), never click or script the confirmation
  dialog, and never sign policy files by hand. Ask the user to approve, then run the live test.
- When debugging the bridge, start it with `RSA_BRIDGE_TRACE=1` to log every request; see `rsa bridge logs`.

## Conventions

- Match the surrounding style: small functions, comments that explain *why*, no new abstractions for one caller.
- **Wire formats:**
  - Machine-facing `rsa-server` commands print one JSON document. Failures print
    `{"ok": false, "error": ...}` and exit 2.
  - `oc-ws export` is the exception: raw bundle bytes on stdout.
  - Client exit codes: 0 ok/succeeded, 1 task failed, 2 error, 3 still running; `rsa dispatch` adds
    4 for denied by policy (nothing started). Keep `--json` output stable.
- Remote calls go through `call()` / `remote_bytes()` with validated arguments. Free text (prompts)
  travels as JSON on stdin, never in the SSH command line.
- **Windows specifics:**
  - pass `CREATE_NO_WINDOW` for subprocesses in the background bridge;
  - use `SO_EXCLUSIVEADDRUSE`, because Windows `SO_REUSEADDR` allows double binds;
  - `shutil.rmtree` needs a chmod retry for git's read-only objects;
  - `rsa.cmd` goes through cmd.exe quoting, so pass long prompts with `-f`.
- Shell scripts: `set -euo pipefail` (or `-uo` where errors are handled explicitly). Validate
  ids, repo names, and SHAs before use (`ID_RE`, `REPO_RE`, `SHA_RE`, `WSDIR_RE`).

## Facts the bridge depends on (T3 Code 0.1.1, OpenCode 2.1.3)

Re-verify these if either is upgraded:

- **External server mode.** T3 uses OpenCode instance settings `binaryPath | serverUrl | serverPassword`.
  With `serverUrl`, T3 does not spawn OpenCode and does not inject its MCP tools.
- **Directory.** It is sent once, in `POST /api/session` as `location.directory`; some GETs carry
  `location[directory]`.
- **Events.** One global SSE stream at `GET /api/event`. Turns end with
  `session.execution.{succeeded,failed,interrupted}`. T3 takes its turn checkpoint after the end event,
  which is why the bridge holds that event until write-back completes.
- **Permissions.** T3 sends per-session permission rules: Full Access is allow-all, anything else
  `ask`s for shell and edit. Children can never exceed the parent's mode.

## Facts the dispatcher depends on (T3 Code 0.1.2)

- **Outside-agent MCP.** T3 serves MCP (streamable HTTP, `Mcp-Session-Id`) at `<origin>/mcp`; the
  origin is in `~/.t3/userdata/server-runtime.json`.
- **OAuth.** Discovery is at `/.well-known/oauth-authorization-server`. Registration is dynamic
  (JSON), and loopback redirects are matched by host and path, not port. PKCE S256 is required.
  The token request is form-encoded; there is no refresh token, and the TTL is 30 days.
- **Access.** The approval page offers `read-only`, `approval-required`, `auto-accept-edits`,
  `auto`, and `full-access`. Only `full-access` grants the `settings:write` scope. The token is
  accepted at `/mcp` only.
- **Launching.** `t3_thread_launch` takes `runtimeMode`, `modelSelection`, and `workspaceStrategy`.
  Modes above the session's ceiling fail with `runtime_mode_escalation_denied`. Tool failures come
  back as `isError` with JSON `{code, message}`. Thread ids look like `mcp:<uuid>`, and links are
  `t3-thread://v1/<threadId>` (not URL-encoded).
