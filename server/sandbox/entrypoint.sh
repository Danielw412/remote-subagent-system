#!/usr/bin/env bash
# rsa sandbox entrypoint. Runs as root inside a fresh, disposable container.
#
#   rsa-entrypoint task             run the task described by /rsa/in and $RSA_* (default)
#   rsa-entrypoint shell            interactive shell, for troubleshooting
#   rsa-entrypoint opencode ARGS    run the opencode CLI (auth, models, ...)
#   rsa-entrypoint selftest         verify the sandbox's isolation guarantees
#   rsa-entrypoint server           long-running `opencode serve` for the T3 bridge
#   rsa-entrypoint fw               (helper) firewall another container's network namespace
#
# Stage 1 installs the egress firewall while the container still holds NET_ADMIN,
# then re-executes this script with NET_ADMIN removed from the capability bounding
# set, so nothing the agent runs can change or remove the rules.
#
# Server containers never hold NET_ADMIN at all: `docker exec` sessions get the
# container's full capability set, so the rules are installed from outside by a
# one-shot `fw` helper that joins the server's network namespace.
set -uo pipefail

OUT=/rsa/out
log() { echo "[rsa $(date -u +%H:%M:%S)] $*" >&2; }

# Destinations the sandbox must never reach: this host's LAN, the Tailscale tailnet
# (CGNAT range), link-local/metadata, and other non-public ranges.
BLOCKED_V4="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 198.18.0.0/15 224.0.0.0/3"
BLOCKED_V6="fc00::/7 fe80::/10"

install_firewall() {
  local ns net
  # Replies on connections opened from outside (the server's published port) stay allowed.
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
  # Keep DNS working when the configured resolver itself sits on a private network.
  for ns in $(awk '/^nameserver/ {print $2}' /etc/resolv.conf); do
    case "$ns" in *:*) continue ;; esac
    iptables -A OUTPUT -d "$ns" -p udp --dport 53 -j ACCEPT || return 1
    iptables -A OUTPUT -d "$ns" -p tcp --dport 53 -j ACCEPT || return 1
  done
  for net in $BLOCKED_V4; do
    iptables -A OUTPUT -d "$net" -j REJECT || return 1
  done
  for net in $BLOCKED_V6; do
    if ! ip6tables -A OUTPUT -d "$net" -j REJECT 2>/dev/null; then
      # Only acceptable when the container has no IPv6 route at all.
      [ -z "$(ip -6 route show 2>/dev/null | grep -v '^\(fe80\|::1\|ff00\|multicast\|local\)')" ] || return 1
    fi
  done
}

case "${1:-}" in
  fw)
    install_firewall || { log "ERROR: could not install the egress firewall"; exit 70; }
    touch /rsa/fw/ready
    log "egress firewall installed in the server's network namespace"
    exit 0
    ;;
  server)
    # The fw helper signals through a host directory mounted read-only here.
    for _ in $(seq 1 60); do [ -e /rsa/fw/ready ] && break; sleep 1; done
    [ -e /rsa/fw/ready ] || { log "ERROR: egress firewall not confirmed; refusing to start"; exit 70; }
    cp /etc/rsa/opencode-server.json /root/.config/opencode/opencode.json
    mkdir -p /ws && cd /ws
    log "starting opencode server for the T3 bridge"
    exec opencode serve --hostname 0.0.0.0 --port 4096
    ;;
esac

if [ "${1:-}" != "--stage2" ]; then
  if [ "${1:-task}" = task ]; then
    exec >>"$OUT/sandbox.log" 2>&1
  fi
  if [ "${RSA_BLOCK_PRIVATE:-1}" = 1 ]; then
    if ! install_firewall; then
      log "ERROR: could not install the egress firewall; refusing to run"
      exit 70
    fi
    log "egress firewall on: LAN, Tailscale and link-local destinations are blocked"
  else
    log "WARNING: egress firewall disabled (block_private_network=false)"
  fi
  exec setpriv --bounding-set=-net_admin --inh-caps=-net_admin -- "$0" --stage2 "$@"
fi
shift

MODE=${1:-task}
[ $# -gt 0 ] && shift
case "$MODE" in
  task) ;;
  shell) exec bash -l ;;
  opencode) exec opencode "$@" ;;
  selftest)
    rc=0
    check() { if eval "$2" >/dev/null 2>&1; then echo "PASS $1"; else echo "FAIL $1"; rc=1; fi; }
    probe() { timeout 4 bash -c "exec 3<>/dev/tcp/${1%:*}/${1##*:}"; }
    check "firewall rules cannot be changed by the agent" "! iptables -F OUTPUT"
    check "NET_ADMIN is not in the capability bounding set" \
      "[ \$(( 0x\$(awk '/CapBnd/ {print \$2}' /proc/self/status) >> 12 & 1 )) -eq 0 ]"
    for t in ${RSA_PROBE_BLOCKED:-}; do check "blocked: $t" "! probe $t"; done
    for t in ${RSA_PROBE_OPEN:-}; do check "reachable: $t" "probe $t"; done
    check "DNS resolves github.com" "getent hosts github.com"
    check "opencode runs ($(opencode --version 2>/dev/null))" "opencode --version"
    check "git available" "git --version"
    check "Ghidra and REA analyze a native fixture over MCP" \
      "python3 /etc/rsa/reverse-engineering-selftest.py"
    check "CMake builds and tests a C++ fixture; build trees stay out of results" \
      "bash /etc/rsa/cmake-selftest.sh"
    exit $rc
    ;;
  *) log "unknown mode: $MODE"; exit 64 ;;
esac

# ---------------------------------------------------------------- task mode
STAGE=setup
OC_EXIT=""
TIMED_OUT=false
HEAD_SHA=""

write_result() { # $1 = error message (empty on success)
  jq -n --arg stage "$STAGE" --arg exit "$OC_EXIT" --arg head "$HEAD_SHA" --arg err "${1:-}" \
    --argjson timed_out "$TIMED_OUT" --argjson elapsed "$SECONDS" \
    '{stage: $stage,
      opencode_exit: (if $exit == "" then null else ($exit | tonumber) end),
      timed_out: $timed_out, head: $head, elapsed_sec: $elapsed,
      error: (if $err == "" then null else $err end)}' >"$OUT/result.json"
}
fail() { log "ERROR: $*"; write_result "$*"; exit 1; }

: "${RSA_TASK_ID:?}" "${RSA_BASE:?}"
TIMEOUT_SEC=${RSA_TIMEOUT_SEC:-3600}
BRANCH="rsa/$RSA_TASK_ID"

git config --global user.name "${RSA_GIT_NAME:-rsa-agent}"
git config --global user.email "${RSA_GIT_EMAIL:-rsa-agent@localhost}"
git config --global init.defaultBranch main
git config --global advice.detachedHead false

# The workspace borrows objects from the read-only host repo (alternates), so nothing
# is copied and the agent cannot modify the host repo.
git init -q -b "$BRANCH" /workspace || fail "git init failed"
cd /workspace || fail "no workspace"
echo /rsa/origin/objects >.git/objects/info/alternates
git reset -q --hard "$RSA_BASE" || fail "cannot check out base commit $RSA_BASE"
printf '%s\n' node_modules/ .venv/ venv/ __pycache__/ '*.pyc' .pytest_cache/ .mypy_cache/ \
  .ruff_cache/ .DS_Store >>.git/info/exclude
log "workspace ready on $BRANCH at $(git log -1 --format='%h %s')"

PROMPT=$(cat <<EOF
You are an autonomous coding agent running unattended in an isolated, disposable Linux sandbox.
No human will answer questions during this run: make reasonable assumptions, state them, and finish the task.

Environment:
- Repository: /workspace (git branch $BRANCH, based on commit ${RSA_BASE:0:12}). It has no git remotes; do not try to push.
- You are root in this container and may install packages (apt-get, pip, npm). The public internet is reachable; LAN and private addresses are blocked.
- Time limit: about $((TIMEOUT_SEC / 60)) minutes.

Rules:
- Keep changes focused on the task. Commit your work with clear messages when done; anything left uncommitted is committed automatically.
- Never commit dependencies, build output, caches, or secrets.
- Verify your work: run the relevant tests, linters, or builds when they exist.
- End with a short final report: what changed, how it was verified (with results), and any open issues.

Task:
$(cat /rsa/in/prompt.md)
EOF
)

STAGE=agent
args=(run --standalone --auto --format json --title "rsa $RSA_TASK_ID")
[ -n "${RSA_MODEL:-}" ] && args+=(--model "$RSA_MODEL")
[ -n "${RSA_AGENT:-}" ] && args+=(--agent "$RSA_AGENT")
log "starting opencode (model: ${RSA_MODEL:-default}, timeout: ${TIMEOUT_SEC}s)"
started=$SECONDS
timeout --kill-after=30 "$TIMEOUT_SEC" opencode "${args[@]}" "$PROMPT" >"$OUT/events.jsonl" </dev/null
OC_EXIT=$?
[ $((SECONDS - started)) -ge "$TIMEOUT_SEC" ] && TIMED_OUT=true
log "opencode exited with code $OC_EXIT after $((SECONDS - started))s$([ "$TIMED_OUT" = true ] && echo ' (timed out)')"

# Harvest: commit leftovers and bundle everything after the base commit. The host
# imports the bundle as plain data and never runs git inside this workspace.
STAGE=finalize
cd /workspace 2>/dev/null || fail "workspace disappeared"
rsa-ignore-build-trees || log "warning: could not check for CMake build trees"
git add -A . >/dev/null 2>&1 || log "warning: git add failed"
if ! git diff --cached --quiet 2>/dev/null; then
  git -c core.hooksPath=/dev/null -c commit.gpgsign=false commit -q --no-verify \
    -m "rsa: auto-commit uncommitted work from task $RSA_TASK_ID" || log "warning: auto-commit failed"
fi
HEAD_SHA=$(git rev-parse -q --verify 'HEAD^{commit}') || fail "workspace has no HEAD commit"
if [ "$HEAD_SHA" != "$RSA_BASE" ]; then
  git update-ref "refs/heads/$BRANCH" "$HEAD_SHA" || fail "could not update $BRANCH"
  git bundle create -q "$OUT/result.bundle" "refs/heads/$BRANCH" "^$RSA_BASE" || fail "could not bundle the result"
  log "result: $(git rev-list --count "$RSA_BASE..$HEAD_SHA") commit(s),$(git diff --shortstat "$RSA_BASE" "$HEAD_SHA")"
else
  log "result: no changes"
fi
write_result ""
exit 0
