#!/usr/bin/env bash
# e2e-real.sh: the REAL end-to-end gate for every datastash plugin release (owner 11-24). It extends test/e2e-install.sh
# (the README's steps typed into a real interactive Claude Code on an empty HOME) past the sign-in: Claude Code is logged
# in (a long-lived token, env only), /mcp Authenticate is finished through datastash's selftest sign-in (x-live XLI-314)
# for one comped, flagged test account, and every step is read back from the server (test/e2e_server.py), not only from
# what the plugin printed. Runs against prod (https://getdatastash.com) and the published plugin.
#
# The matrix (computer = one throwaway HOME; all of them share the one selftest test account, label E2E_ACCOUNT, the
# same fixed label every run):
#   1  computer A, home folder: marketplace add, install for you, /exit and restart, /mcp Authenticate (selftest),
#      /datastash:install: agent "agent", the install list and "Time per card is on"; server: "agent" is on.
#      Computer B, repo folder ~/code/site (below, 8): the same README steps from a repo folder: agent "site".
#      Also: the first session in a folder Claude Code never ran in (A's ~/code/web-app, ~/code/notes) must work; when the
#      plugin gives no note there ("datastash did not answer") that FAILs, and the step is tried once more in a second
#      session so the rest of the matrix still runs.
#   2  A: /datastash:install again in a new session: the same agent, no "agent-2" (pane and server).
#   4  A: a third folder's session that is no agent (~/code/notes) types /datastash:uninstall web-app while A also holds
#      "agent"'s key (the owner's case): web-app off on the server, its key gone here at once (1.0.14: not at its next
#      start), "agent" untouched.
#      (A's ~/code/web-app gets its agent first: /datastash:install there, "web-app".)
#   3  A: /datastash:uninstall agent from agent's own session: off on the server, its key gone here.
#   7  A: /datastash:install after that uninstall: "agent" again (no "-2"), on the server.
#   9  A (XLI-408, the offline outbox): datastash blocked for "agent" in A's home folder (its own datastash command with
#      HTTPS_PROXY at a closed local port, so every call fails to connect; no listener of A runs meanwhile): three inbox
#      writes answer "saved, will send when the server is back" and wait on disk (mode 700/600); a read fails plainly and
#      is not kept; server: none of them there. Then unblocked: A's next session start shows the person the waiting line
#      and sends them; server: each exactly once, in order; the outbox empty. E2E_PARTS=outbox runs 1, 9 and a cleanup.
#   8  computer B (second throwaway HOME, same account), repo folder: README steps, its own sign-in, /datastash:install:
#      "site"; the install list also shows A's agents that are on; server: all on. (When step 7 left A with none, A
#      gets one first in ~/code/api, so step 5 has a cross-computer half.)
#   5  B: /datastash:uninstall all, the typed "remove all N": server: none on; then A's next session start cleans its
#      agents up on A (their key files go).
#   6  update, two ways, each from an older tag (E2E_OLD_TAG, default v1.0.12) in a local copy of the catalog pinned to it,
#      then the catalog moved to the published pin (what a release does):
#      6a `/plugin marketplace update datastash` + `/reload-plugins` (self-update held off for that HOME): the installed
#         version moves to the published one; then sign-in and /datastash:install on the updated copy: on the server.
#      6b self-update at session start: the old copy updates itself; the next start says "datastash updated to vX";
#         then sign-in and /datastash:install: on the server.
# Every model turn's permission dialog is answered Yes only for a datastash connector tool; any other tool is refused
# (Esc) and fails the step. claude.ai connectors are off in the throwaway HOMEs (ENABLE_CLAUDEAI_MCP_SERVERS=false), so
# no step can reach any account but the selftest one.
#
# Inputs (files, never printed):
#   E2E_CLAUDE_ENV   default /home/xpm/handover/claude-e2e.env, one line CLAUDE_CODE_OAUTH_TOKEN=... (mode 600). Read by
#                    the launcher inside each throwaway HOME's tmux session and exported there only (never on a command
#                    line). Missing: the steps that need a model turn (every /datastash:install and :uninstall) are
#                    reported WAITING and everything else still runs.
#   E2E_SELFTEST_SECRET_FILE  default /home/xpm/handover/selftest-oauth.secret (the bare secret, mode 600).
# The test account: one fixed label (E2E_ACCOUNT, default plugin-e2e), the same every run. The server allows at most 10
#   selftest labels and 20 selftest sign-ins in a rolling hour (x-live src/auth/selftest-oauth.ts), so a run never makes a
#   new label. Clean start: before the matrix the read-back (the person's own connection) switches off every agent left
#   on and disconnects every app connection but its own (ledger_agent_switch_off, sync disconnect: the product's own
#   paths); the run stops if any is left. The same cleanup runs at the end, pass or fail (trap). An agent once switched
#   off is installed again under its own name (x-live #510), so the same label serves every run.
# Sign-in budget: a run counts the selftest sign-ins it needs (the read-back 1; core 2, step5 2, outbox 1, update 2) and
#   stops before it starts when those plus this harness's own sign-ins of the last hour (E2E_SIGNIN_LEDGER, default
#   ~/.local/state/datastash-e2e/selftest-signins: one time per line) would pass 20. A server refusal (HTTP 429) names
#   its reason: rate_limited (the hourly limit) or too_many_accounts (no new label can be made).
# Options: E2E_ACCOUNT (selftest label, default plugin-e2e), E2E_MODEL (a --model for the sessions; default Claude Code's),
#   E2E_PARTS ("core update", default both; "step5" runs step 5 alone; "outbox" runs step 9 alone), E2E_OLD_TAG, E2E_KEEP=1 (say where the HOMEs went in ~/trash),
#   E2E_CANDIDATE=<branch or tag, pushed to the plugin repo>: test that commit before it is released: the core steps
#   install it from a local copy of the catalog pinned to it, and 6a updates to it; 6b (self-update moves only to a
#   release tag) stays on the published pin.
# Cleanup, pass or fail: every agent left on the test account is switched off, every app connection on it disconnected
# (the read-back's own last), the tmux sessions end, processes from the throwaway HOMEs stop, the HOMEs go to ~/trash.
# Exit 0 PASS, 1 FAIL, 2 can't run here, 3 no step failed but some are WAITING (no Claude login yet).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
MARKETPLACE="xlsx-for-ai-dev/datastash-claude-code"
CLAUDE_BIN="$(command -v claude || true)"
REAL_HOME="$HOME"
CLAUDE_ENV="${E2E_CLAUDE_ENV:-/home/xpm/handover/claude-e2e.env}"
export E2E_SELFTEST_SECRET_FILE="${E2E_SELFTEST_SECRET_FILE:-/home/xpm/handover/selftest-oauth.secret}"
export E2E_ACCOUNT="${E2E_ACCOUNT:-plugin-e2e}"
PARTS="${E2E_PARTS:-core update}"
OLD_TAG="${E2E_OLD_TAG:-v1.0.12}"
export E2E_SIGNIN_LEDGER="${E2E_SIGNIN_LEDGER:-$REAL_HOME/.local/state/datastash-e2e/selftest-signins}"
SIGNIN_MAX_PER_HOUR=20
RUN="dsreal-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ds-e2e-real.XXXXXX")"
export E2E_SERVER_TOKEN_FILE="$WORK/server.tok"
SRV=(python3 -I "$HERE/e2e_server.py")
FAILS=0 WAITS=0 STARTS=0 T="" LOGIN=0 BADTOOL=0 NOANSWER=0
RESULTS=()

say() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
pass() { say "PASS $*"; RESULTS+=("PASS  $*"); }
fail() { say "FAIL $*"; RESULTS+=("FAIL  $*"); FAILS=$((FAILS + 1)); }
waiting() { say "WAITING $*"; RESULTS+=("WAIT  $*"); WAITS=$((WAITS + 1)); }

cleanup() {
  local s n
  trap - EXIT
  if [[ -s "$E2E_SERVER_TOKEN_FILE" ]]; then
    for n in $("${SRV[@]}" agents 2>/dev/null | python3 -I -c 'import json,sys; print(*json.load(sys.stdin))' 2>/dev/null); do
      "${SRV[@]}" off "$n" >/dev/null 2>&1 && say "cleanup: switched off leftover agent \"$n\""
    done
    left="$("${SRV[@]}" agents 2>/dev/null)"
    [[ "$left" == "[]" ]] && say "cleanup: no agent left on the test account" || say "cleanup: WARNING agents left on the test account: ${left:-unknown}"
    "${SRV[@]}" disconnect-all 2>&1 | sed 's/^/cleanup: /'
    : > "$E2E_SERVER_TOKEN_FILE"
  fi
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep "^$RUN-"); do tmux kill-session -t "$s" 2>/dev/null; done
  pkill -f "$WORK/" 2>/dev/null; sleep 1; pkill -9 -f "$WORK/" 2>/dev/null
  mkdir -p "$REAL_HOME/trash"
  mv "$WORK" "$REAL_HOME/trash/" 2>/dev/null && [[ "${E2E_KEEP:-0}" == 1 ]] && say "throwaway HOMEs kept in $REAL_HOME/trash/$(basename "$WORK")"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

command -v tmux >/dev/null || { echo "CAN'T RUN: tmux missing"; exit 2; }
[[ -n "$CLAUDE_BIN" ]] || { echo "CAN'T RUN: claude missing"; exit 2; }
curl -fsS -o /dev/null --max-time 20 https://getdatastash.com/.well-known/oauth-authorization-server \
  || { echo "CAN'T RUN: getdatastash.com does not answer"; exit 2; }
[[ -r "$E2E_SELFTEST_SECRET_FILE" ]] || { echo "CAN'T RUN: no selftest secret file ($E2E_SELFTEST_SECRET_FILE)"; exit 2; }
if [[ -r "$CLAUDE_ENV" ]] && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' "$CLAUDE_ENV"; then
  LOGIN=1; say "Claude login: from $CLAUDE_ENV (env only, never printed)"
else
  say "Claude login: NONE ($CLAUDE_ENV missing): every model step is WAITING"
fi

# --- the sign-in budget: what this run needs against the server's hourly limit, before anything signs in.
NEED=1  # the read-back's own sign-in
for p in $PARTS; do
  case "$p" in core|step5|update) NEED=$((NEED + 2)) ;; outbox) NEED=$((NEED + 1)) ;; esac
done
read -r USED FREE_AT < <(python3 -I - "$E2E_SIGNIN_LEDGER" <<'PY'
import sys, time
now = time.time()
try:
    ts = sorted(int(l) for l in open(sys.argv[1]) if l.strip().isdigit() and now - int(l) < 3600)
except OSError:
    ts = []
print(len(ts), time.strftime("%H:%M:%SZ", time.gmtime(ts[0] + 3600)) if ts else "-")
PY
)
if ((USED + NEED > SIGNIN_MAX_PER_HOUR)); then
  echo "CAN'T RUN: this run needs $NEED selftest sign-ins; $USED were used in the last hour (the server allows $SIGNIN_MAX_PER_HOUR an hour). The oldest frees at $FREE_AT; run again then."
  exit 2
fi
say "sign-in budget: this run needs $NEED selftest sign-ins; $USED used in the last hour by this harness (limit $SIGNIN_MAX_PER_HOUR)"

# --- the server read-back: its own connection to the selftest account; the account starts clean: no agent on, no app
# connection but the read-back's own.
"${SRV[@]}" signin || { echo "CAN'T RUN: the read-back could not sign in to the selftest account \"$E2E_ACCOUNT\" (reason above)"; exit 2; }
agents() { "${SRV[@]}" agents; }
has_agent() { agents | python3 -I -c 'import json,sys; sys.exit(0 if sys.argv[1] in json.load(sys.stdin) else 1)' "$1"; }
START_AGENTS="$(agents)" || { echo "CAN'T RUN: ledger_agents_list refused on the read-back"; exit 2; }
if [[ "$START_AGENTS" != "[]" ]]; then
  say "the test account has agents left from an earlier run: $START_AGENTS; switching them off first"
  for n in $(python3 -I -c 'import json,sys; print(*json.loads(sys.argv[1]))' "$START_AGENTS"); do "${SRV[@]}" off "$n"; done
  [[ "$(agents)" == "[]" ]] || { echo "CAN'T RUN: the test account still has agents on"; exit 2; }
fi
START_APPS="$("${SRV[@]}" other-apps)" || { echo "CAN'T RUN: sync apps refused on the read-back"; exit 2; }
if [[ "$START_APPS" != "[]" ]]; then
  say "the test account has app connections left from an earlier run: $START_APPS; disconnecting them first"
  "${SRV[@]}" disconnect-others | sed 's/^/  /'
  [[ "$("${SRV[@]}" other-apps)" == "[]" ]] || { echo "CAN'T RUN: the test account still has other app connections"; exit 2; }
fi
say "server: test account \"$E2E_ACCOUNT\" signed in for read-back; clean: no agent on, no other app connection"

# --- catalogs: the published one (as the README says), or a local copy pinned to a tag (for the update steps).
pinned_catalog() {  # pinned_catalog <dir> <tag>: the repo's catalog with datastash pinned to <tag>'s commit
  local dir="$1" tag="$2" sha
  sha="$(git -C "$REPO" rev-parse --verify --quiet "$tag^{commit}")" || return 1
  mkdir -p "$dir/.claude-plugin"
  python3 -I - "$REPO/.claude-plugin/marketplace.json" "$dir/.claude-plugin/marketplace.json" "$tag" "$sha" <<'PY'
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat["plugins"]:
    if p["name"] == "datastash":
        p["source"].update(ref=sys.argv[3], sha=sys.argv[4])
json.dump(cat, open(sys.argv[2], "w"), indent=2)
PY
}
PUBLISHED_REF="$(python3 -I -c 'import json,sys; print(next(p for p in json.load(open(sys.argv[1]))["plugins"] if p["name"]=="datastash")["source"]["ref"])' "$REPO/.claude-plugin/marketplace.json")"
PUBLISHED_VERSION="${PUBLISHED_REF#v}"
say "published: $MARKETPLACE pins $PUBLISHED_REF"
CORE_SRC="$MARKETPLACE" NEW_REF="$PUBLISHED_REF" NEW_VERSION="$PUBLISHED_VERSION"
if [[ -n "${E2E_CANDIDATE:-}" ]]; then  # a pushed branch or tag: its commit, its own plugin version
  git -C "$REPO" fetch -q "$(git -C "$REPO" remote | head -n 1)" "$E2E_CANDIDATE" 2>/dev/null
  cand_sha="$(git -C "$REPO" rev-parse --verify --quiet FETCH_HEAD^{commit})" \
    || { echo "CAN'T RUN: candidate $E2E_CANDIDATE is not on the plugin repo's remote"; exit 2; }
  git -C "$REPO" tag -f "e2e-cand-$$" "$cand_sha" >/dev/null || exit 2
  NEW_VERSION="$(git -C "$REPO" show "$cand_sha:plugins/datastash/.claude-plugin/plugin.json" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
  NEW_REF="$E2E_CANDIDATE"
  pinned_catalog "$WORK/cand/catalog" "e2e-cand-$$" || exit 2
  python3 -I - "$WORK/cand/catalog/.claude-plugin/marketplace.json" "$E2E_CANDIDATE" <<'PY'
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat["plugins"]:
    if p["name"] == "datastash":
        p["source"]["ref"] = sys.argv[2]
json.dump(cat, open(sys.argv[1], "w"), indent=2)
PY
  git -C "$REPO" tag -d "e2e-cand-$$" >/dev/null
  CORE_SRC="$WORK/cand/catalog"
  say "candidate: $E2E_CANDIDATE at ${cand_sha:0:7} (plugin $NEW_VERSION): the core steps and 6a use it; 6b stays on $PUBLISHED_REF"
fi

# --- the launcher: a clean env per throwaway HOME; the Claude token (if any) is read and exported inside the session.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/browser" <<EOF
#!/bin/sh
printf '%s\n' "\$1" >> "$WORK/browser-urls"
EOF
cp "$WORK/bin/browser" "$WORK/bin/xdg-open"
chmod 700 "$WORK/bin/browser" "$WORK/bin/xdg-open"
cat > "$WORK/launch.sh" <<'EOF'
#!/usr/bin/env bash
# launch.sh <claude> [args...]: run in a tmux pane with a clean env; the token never goes on a command line.
if [[ -n "${E2E_CLAUDE_ENV_FILE:-}" && -r "$E2E_CLAUDE_ENV_FILE" ]]; then
  tok="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$E2E_CLAUDE_ENV_FILE" | head -n 1 | tr -d '\r')"
  tok="${tok#\"}"; tok="${tok%\"}"; tok="${tok#\'}"; tok="${tok%\'}"
  [[ -n "$tok" ]] && export CLAUDE_CODE_OAUTH_TOKEN="$tok"
  unset tok
fi
unset E2E_CLAUDE_ENV_FILE
exec "$@"
EOF
chmod 700 "$WORK/launch.sh"

pane() { tmux capture-pane -t "$T" -p -J -S -3000 2>/dev/null; }
flat() { pane | tr -s ' \t\n' ' '; }  # the whole pane as one line (a wrapped line reads as one)
screen() { tmux capture-pane -t "$T" -p -J 2>/dev/null | grep -v '^[[:space:]]*$'; }
count() { flat | grep -oF -- "$1" | wc -l; }
type_line() { tmux send-keys -t "$T" -l -- "$1"; sleep 1; tmux send-keys -t "$T" Enter; }
show_tail() { screen | tail -n "${1:-14}" | sed 's/^/    | /'; }
wait_for() {  # wait_for <ERE> [seconds]: the pane shows it (no model turn; no dialog answered)
  local i
  for ((i = 0; i < ${2:-30}; i++)); do pane | grep -qE -- "$1" && return 0; sleep 1; done
  return 1
}

answer_dialog() {  # a permission dialog on screen: Yes for a datastash connector tool, else Esc (and the step fails)
  local s
  s="$(screen)"
  grep -qE "Do you want to (proceed|make this edit|create|allow)" <<<"$s" || return 1
  if grep -qiE "claude[._]ai" <<<"$s"; then BADTOOL=1; tmux send-keys -t "$T" Escape
  elif grep -qE "ledger_(agent|agents)_[a-z_]+|plugin[:_]datastash[:_]datastash" <<<"$s"; then tmux send-keys -t "$T" Enter
  else BADTOOL=1; say "    refused a non-datastash tool:"; grep -vE "^[[:space:]]*$" <<<"$s" | tail -n 8 | sed 's/^/    | /'; tmux send-keys -t "$T" Escape
  fi
  sleep 2
  return 0
}

drive_until() {  # drive_until <shell condition> [seconds]: answer dialogs until the condition holds
  local i
  for ((i = 0; i < ${2:-240}; i++)); do
    eval "$1" && return 0
    if screen | grep -qE "API Error: 40[13]|Please run /login|OAuth access token is invalid"; then
      fail "the Claude login was refused in the session (the token in $CLAUDE_ENV); the run stops here"; show_tail 6
      exit 1
    fi
    answer_dialog || sleep 1
  done
  return 1
}

wait_idle() {  # the model's turn is over: no "esc to interrupt" for 3 s running (dialogs answered meanwhile)
  local i quiet=0
  for ((i = 0; i < ${1:-180}; i++)); do
    answer_dialog && { quiet=0; continue; }
    if screen | grep -qE "esc to interrupt"; then quiet=0; else quiet=$((quiet + 1)); fi
    ((quiet >= 3)) && return 0
    sleep 1
  done
  return 1
}

new_home() {  # new_home <id>: a brand-new computer past Claude Code's first-run theme screen
  local h="$WORK/$1/person"
  mkdir -p "$h/code"
  printf '{"hasCompletedOnboarding": true, "theme": "dark"}\n' > "$h/.claude.json"
  printf '%s' "$h"
}
new_repo() {  # new_repo <dir>
  mkdir -p "$1" && git -C "$1" init -q && git -C "$1" -c user.name=Test -c user.email=test@example.invalid commit -q --allow-empty -m init
}

start_claude() {  # start_claude <HOME> <folder>: an interactive session, the folder trusted as a person does
  local h="$1" dir="$2" model=(--permission-mode default)  # every tool call asks, so answer_dialog decides (never auto mode)
  [[ -n "${E2E_MODEL:-}" ]] && model+=(--model "$E2E_MODEL")
  T="$RUN-$((++STARTS))"
  mkdir -p "$dir"
  tmux new-session -d -s "$T" -x 250 -y 50 -c "$dir" \
    "env -i PATH='$WORK/bin:$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin' HOME='$h' TERM=xterm-256color LANG=C.UTF-8 BROWSER='$WORK/bin/browser' ENABLE_CLAUDEAI_MCP_SERVERS=false E2E_CLAUDE_ENV_FILE='$( ((LOGIN)) && printf '%s' "$CLAUDE_ENV")' '$WORK/launch.sh' '$CLAUDE_BIN' ${model[*]}"
  if wait_for "trust this folder" 40; then
    # The dialog's default is "No, exit" and it shows its text before it takes keys: a Down sent too soon is dropped and
    # the Enter then picks No ("Claude Code didn't start because this folder isn't trusted"). A home folder's Yes lasts
    # one session, so every start in a home folder meets this. Press Down until Yes is the selected choice, then Enter.
    local i
    for ((i = 0; i < 20; i++)); do
      screen | grep -qE "❯ +(1\. |2\. )?Yes, I trust this folder" && break
      tmux send-keys -t "$T" Down; sleep 0.5
    done
    screen | grep -qE "❯ +(1\. |2\. )?Yes, I trust this folder" || return 1
    tmux send-keys -t "$T" Enter
  fi
  wait_for "shift\+tab to cycle|for shortcuts|Not logged in|\? for" 40 || return 1
  sleep 3  # the session-start hooks
}

stop_claude() {
  local i
  tmux has-session -t "$T" 2>/dev/null || return 0
  tmux send-keys -t "$T" Escape; sleep 0.5
  type_line "/exit"
  for ((i = 0; i < 15; i++)); do tmux has-session -t "$T" 2>/dev/null || return 0; sleep 1; done
  tmux kill-session -t "$T" 2>/dev/null
}

installed_version() {  # installed_version <HOME>
  python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(*(e.get("version") for e in d["plugins"]["datastash@datastash"]))' \
    "$1/.claude/plugins/installed_plugins.json" 2>/dev/null || echo "?"
}
key_file() { printf '%s/.config/datastash/agent-%s.key' "$1" "$2"; }

readme_steps() {  # readme_steps <tag> <HOME> <folder> <catalog>: marketplace add, install for you, /exit, start again
  local tag="$1" h="$2" dir="$3" src="$4"
  start_claude "$h" "$dir" || { fail "$tag: claude did not start"; show_tail; return 1; }
  type_line "/plugin marketplace add $src"
  wait_for "Added marketplace|Successfully added marketplace" 120 || { fail "$tag: marketplace not added"; show_tail; return 1; }
  type_line "/plugin install datastash@datastash"
  wait_for "Install for you \(user scope\)" 60 || { fail "$tag: no install choice shown"; show_tail; return 1; }
  screen | grep -qE "❯ +Install for you \(user scope\)" || { fail "$tag: \"Install for you\" is not the selected choice"; show_tail; return 1; }
  tmux send-keys -t "$T" Enter
  wait_for "Installed datastash" 180 || { fail "$tag: install did not finish"; show_tail; return 1; }
  stop_claude
  start_claude "$h" "$dir" || { fail "$tag: claude did not start again"; show_tail; return 1; }
  pass "$tag: marketplace added, installed for you, restarted (plugin $(installed_version "$h"))"
}

authenticate() {  # authenticate <tag>: /mcp, datastash, Authenticate, the selftest sign-in; server: one more app connection
  local tag="$1" after url="" i menu
  : > "$WORK/browser-urls"
  type_line "/mcp"
  wait_for "plugin:datastash:datastash" 30 || { fail "$tag: /mcp does not list datastash"; show_tail; return 1; }
  sleep 3
  for ((i = 0; i < 10; i++)); do screen | grep -qE "❯.*datastash" && break; tmux send-keys -t "$T" Down; sleep 0.5; done
  tmux send-keys -t "$T" Enter
  wait_for "Status:" 20
  for ((i = 1; i <= 60; i++)); do
    sleep 1; menu="$(screen)"
    grep -qE "Status: +.*connecting" <<<"$menu" || break
    if ((i % 10 == 0)); then tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Enter; fi
  done
  if ! grep -qE "^ *(❯ +)?Authenticate *$" <<<"$menu"; then
    fail "$tag: /mcp datastash offers no Authenticate"; grep -E "Status:|Auth" <<<"$menu" | sed 's/^/    | /'
    tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Escape; return 1
  fi
  screen | grep -qE "❯ +Authenticate *$" || tmux send-keys -t "$T" Up
  tmux send-keys -t "$T" Enter
  for ((i = 0; i < 45; i++)); do
    url="$(grep -oE "https://getdatastash\.com/oauth/auth\?[^ ]*" "$WORK/browser-urls" 2>/dev/null | tail -n 1)"
    [[ -n "$url" ]] && break
    url="$(pane | tr -d '\n' | grep -oE "https://getdatastash\.com/oauth/auth\?[^ │]*" | tail -n 1)"
    [[ -n "$url" ]] && break
    sleep 1
  done
  [[ -n "$url" ]] || { fail "$tag: Authenticate made no datastash sign-in link"; show_tail; return 1; }
  "${SRV[@]}" browse "$url" || { fail "$tag: the selftest sign-in did not finish"; show_tail; return 1; }
  if ! wait_for "uthentication successful|[Cc]onnected to plugin:datastash|Status: +.*connected" 90; then
    fail "$tag: Claude Code did not finish the connector sign-in"; show_tail; return 1
  fi
  tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Escape; sleep 1
  # Server: the account lists the connector's app (every Claude Code sign-in is one client, so one app however many
  # computers signed in); the agent each later step makes through this sign-in is the per-computer proof.
  after="$("${SRV[@]}" other-apps)"
  if [[ -n "$after" && "$after" != "[]" ]]; then pass "$tag: /mcp Authenticate finished (selftest); server lists the app: $after"
  else fail "$tag: /mcp Authenticate: the server lists no app connection besides the read-back"; return 1; fi
}

ds_install() {  # ds_install <tag> <HOME> <expected agent name> [<also listed>...]
  local tag="$1" h="$2" name="$3"; shift 3
  local base_on base_fail ok=1 n f
  ((LOGIN)) || { waiting "$tag: /datastash:install (needs the Claude login)"; return 1; }
  BADTOOL=0 NOANSWER=0
  local base_done; base_done="$(count " · done ")"  # the turn's own end line ("✻ Worked for 7s · done 12:27 AM")
  base_on="$(count "datastash is on for your agents:")"; base_fail="$(count "Not done")$(count "could not finish the install")$(count "datastash did not answer")"
  type_line "/datastash:install"
  drive_until '(( $(count "datastash is on for your agents:") > base_on )) || [[ "$(count "Not done")$(count "could not finish the install")$(count "datastash did not answer")" != "$base_fail" ]] || (( $(count " · done ") > base_done ))' 300
  wait_idle 120
  f="$(flat)"
  ((BADTOOL)) && { fail "$tag: the model asked for a tool that is not datastash's (refused)"; ok=0; }
  if (( $(count "datastash is on for your agents:") <= base_on )); then
    if grep -qF "datastash did not answer" <<<"$f"; then NOANSWER=1; fail "$tag: \"datastash did not answer\" (the plugin's prompt hook gave no note)"; show_tail 8; return 1; fi
    fail "$tag: /datastash:install showed no install list"; show_tail 14; return 1
  fi
  grep -qF -- "- $name (" <<<"${f##*datastash is on for your agents:}" || { fail "$tag: the install list has no \"$name\""; ok=0; }
  grep -qE -- "- $name-[0-9]+ \(" <<<"${f##*datastash is on for your agents:}" && { fail "$tag: the install list has a \"$name-N\""; ok=0; }
  for n in "$@"; do grep -qF -- "- $n (" <<<"${f##*datastash is on for your agents:}" || { fail "$tag: the install list lacks \"$n\""; ok=0; }; done
  grep -qF "Time per card is on." <<<"${f##*datastash is on for your agents:}" || { fail "$tag: no \"Time per card is on\" line"; ok=0; }
  [[ -s "$(key_file "$h" "$name")" ]] || { fail "$tag: no key file for \"$name\" on this computer"; ok=0; }
  has_agent "$name" || { fail "$tag: server: \"$name\" is not on"; ok=0; }
  agents | python3 -I -c 'import json,re,sys; n=sys.argv[1]; sys.exit(1 if any(re.fullmatch(re.escape(n)+r"-\d+", a) for a in json.load(sys.stdin)) else 0)' "$name" \
    || { fail "$tag: server: a \"$name-N\" exists"; ok=0; }
  ((ok)) && pass "$tag: /datastash:install: \"$name\", list and \"Time per card is on\"; key here; server: $(agents)"
  ((ok))
}

ds_uninstall() {  # ds_uninstall <tag> <HOME> <args> <removed names...>: server: those off; their key files gone here
  local tag="$1" h="$2" args="$3"; shift 3
  local base_off base_no ok=1 n f m
  ((LOGIN)) || { waiting "$tag: /datastash:uninstall $args (needs the Claude login)"; return 1; }
  BADTOOL=0 NOANSWER=0
  local base_done; base_done="$(count " · done ")"
  base_off="$(count "datastash is off for")"; base_no="$(count "Not done")/$(count "Datastash isn't installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")"
  type_line "/datastash:uninstall${args:+ $args}"
  local cond='(( $(count "datastash is off for") > base_off )) || [[ "$(count "Not done")/$(count "Datastash isn'"'"'t installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")" != "$base_no" ]] || (( $(count " · done ") > base_done ))'
  drive_until "$cond" 300
  wait_idle 120
  f="$(flat)"
  if [[ "$args" == all ]] && m="$(grep -oE "To go ahead, type exactly: remove all [0-9]+\." <<<"$f" | tail -n 1)" && [[ -n "$m" ]]; then
    m="${m#To go ahead, type exactly: }"; m="${m%.}"
    pass "$tag: \"all\" listed every agent and asked for the typed confirm (\"$m\")"
    base_no="$(count "Not done")/$(count "Datastash isn't installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")"
    base_done="$(count " · done ")"
    type_line "$m"
    drive_until "$cond" 300
    wait_idle 120
    f="$(flat)"
  elif [[ -z "$args" ]] && grep -qF "Type the names to remove" <<<"$f"; then
    base_no="$(count "Not done")/$(count "Datastash isn't installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")"
    base_done="$(count " · done ")"
    type_line "$*"
    drive_until "$cond" 300
    wait_idle 120
    f="$(flat)"
  fi
  ((BADTOOL)) && { fail "$tag: the model asked for a tool that is not datastash's (refused)"; ok=0; }
  if (( $(count "datastash is off for") <= base_off )); then
    if grep -qF "datastash did not answer" <<<"$f"; then NOANSWER=1; fail "$tag: \"datastash did not answer\" (the plugin's prompt hook gave no note)"; show_tail 8; return 1; fi
    fail "$tag: /datastash:uninstall $args: no \"datastash is off for\" line"; show_tail 20; return 1
  fi
  for n in "$@"; do
    grep -qF -- "\"$n\"" <<<"${f##*datastash is off for}" || { fail "$tag: the uninstall line does not name \"$n\""; ok=0; }
    has_agent "$n" && { fail "$tag: server: \"$n\" is still on"; ok=0; }
  done
  ((ok)) && pass "$tag: /datastash:uninstall $args: removed $*; server: $(agents)"
  ((ok))
}

key_gone() {  # key_gone <tag> <HOME> <name> [seconds]
  local i
  for ((i = 0; i < ${4:-5}; i++)); do [[ -e "$(key_file "$2" "$3")" ]] || { pass "$1: \"$3\"'s key file is gone on this computer"; return 0; }; sleep 1; done
  fail "$1: \"$3\"'s key file is still on this computer"
}
next_start_cleans() {  # next_start_cleans <tag> <HOME> <folder> <name>: that agent's own next session start (in its own
  # folder) cleans it up here: its key file goes ("they clean up when they next start")
  local keep="$T"
  start_claude "$2" "$3" || { fail "$1: claude did not start in ${3#"$2/"}"; show_tail; T="$keep"; return 1; }
  key_gone "$1 (its next start, in ${3#"$2/"})" "$2" "$4" 120
  stop_claude
  T="$keep"
}
ds_cli() {  # ds_cli <HOME> <folder> <proxy or ""> <args...>: the installed plugin's own datastash command in that folder, as
  # a person's terminal would run it (no session; DS_AS names the agent, as DATASTASH_AGENT: a home folder is never
  # linked, its agent comes from its session); a proxy blocks datastash for this one run
  local h="$1" dir="$2" proxy="$3" script; shift 3
  script="$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["plugins"]["datastash@datastash"][0]["installPath"])' \
    "$h/.claude/plugins/installed_plugins.json" 2>/dev/null)/scripts/datastash"
  [[ -x "$script" || -r "$script" ]] || { echo "no installed datastash command"; return 9; }
  (cd "$dir" && env -i PATH="$WORK/bin:/usr/local/bin:/usr/bin:/bin" HOME="$h" LANG=C.UTF-8 CLAUDE_PROJECT_DIR="$dir" ${DS_AS:+DATASTASH_AGENT=$DS_AS} \
    ${proxy:+HTTPS_PROXY=$proxy} ${proxy:+https_proxy=$proxy} python3 "$script" "$@")
}

step9_outbox() {  # step9_outbox <HOME> <folder> <agent>: XLI-408, server blocked, three writes, unblocked, each once in order
  local h="$1" dir="$2" name="$3" dead="http://127.0.0.1:9" wb mark out i n got ok=1 ob
  ob="$h/.config/datastash/state/outbox-$name"
  wb="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["workbook_id"])' "$h/.config/datastash/agent-$name.json" 2>/dev/null)"
  [[ -n "$wb" ]] || { fail "9: no workbook for \"$name\" on this computer"; return 1; }
  mark="e2e-outbox-$RUN-$(date +%s)"
  pkill -f "$h/.claude/plugins/" 2>/dev/null; sleep 2  # no listener of this computer sends while datastash is "down"
  for i in 1 2 3; do
    out="$(DS_AS="$name" ds_cli "$h" "$dir" "$dead" api POST /api/v1/agent/inbox "{\"workbook_id\":\"$wb\",\"to\":\"$name\",\"message\":\"$mark #$i\"}" 2>&1)"
    grep -qF "saved, will send when the server is back" <<<"$out" || { fail "9: write $i while blocked was not kept: $(head -c 300 <<<"$out")"; ok=0; }
  done
  out="$(DS_AS="$name" ds_cli "$h" "$dir" "$dead" api GET "/api/v1/agent/inbox?workbook_id=$wb" 2>&1)"
  grep -qF "nothing was read. Try again shortly." <<<"$out" || { fail "9: a read while blocked did not fail plainly: $(head -c 300 <<<"$out")"; ok=0; }
  n="$(find "$ob" -maxdepth 1 -regextype posix-extended -regex '.*/[0-9]{16}\.json' 2>/dev/null | wc -l)"
  ((n == 3)) || { fail "9: $n write(s) kept on disk, wanted 3 (the read is never kept)"; ok=0; }
  [[ "$(stat -c %a "$ob" 2>/dev/null)" == 700 ]] || { fail "9: the outbox folder is not mode 700"; ok=0; }
  [[ -z "$(find "$ob" -maxdepth 1 -type f ! -perm 600 2>/dev/null)" ]] || { fail "9: a kept write is not mode 600"; ok=0; }
  got="$("${SRV[@]}" inbox-marks "$wb" "$name" "$mark")" || { fail "9: the read-back could not read \"$name\"'s inbox"; return 1; }
  [[ "$got" == "[]" ]] || { fail "9: server: writes arrived while datastash was blocked: $got"; ok=0; }
  ((ok)) && pass "9: blocked: 3 writes kept (\"saved, will send when the server is back\", mode 700/600), the read failed plainly, server: none yet"
  say "== computer A: unblocked; the next session start sends the kept writes"
  start_claude "$h" "$dir" || { fail "9: claude did not start"; return 1; }
  if wait_for "writes? from \"$name\" waiting on this computer" 20; then pass "9: the session start told the person what is waiting"
  else fail "9: the session start showed no waiting line"; show_tail 8; fi
  for ((i = 0; i < 120; i++)); do
    got="$("${SRV[@]}" inbox-marks "$wb" "$name" "$mark")"
    [[ "$got" == "[1, 2, 3]" ]] && break
    sleep 2
  done
  sleep 10  # anything sent twice would show by now
  got="$("${SRV[@]}" inbox-marks "$wb" "$name" "$mark")"
  n="$(find "$ob" -maxdepth 1 -regextype posix-extended -regex '.*/[0-9]{16}\.json' 2>/dev/null | wc -l)"
  if [[ "$got" == "[1, 2, 3]" ]] && ((n == 0)); then pass "9: unblocked: server has each write exactly once, in order ($got); the outbox is empty"
  else fail "9: unblocked: server has $got (wanted [1, 2, 3]); $n write(s) still kept"; fi
  stop_claude
}

outbox_only() {  # E2E_PARTS=outbox: step 1 (A home), step 9, then the agent's own uninstall
  local A
  A="$(new_home A)"
  say "== outbox only: computer A, home folder: one agent"
  readme_steps "9 setup A home" "$A" "$A" "$CORE_SRC" || return
  authenticate "9 setup A home" || { stop_claude; return; }
  ds_install "9 setup A home" "$A" agent || { stop_claude; return; }
  stop_claude
  step9_outbox "$A" "$A" agent
  start_claude "$A" "$A" || { fail "9 cleanup: claude did not start"; return; }
  ds_uninstall "9 cleanup" "$A" agent agent
  stop_claude
}

key_here() { [[ -s "$(key_file "$2" "$3")" ]] && pass "$1: \"$3\"'s key file is still here" || fail "$1: \"$3\"'s key file is gone"; }

on_names() { python3 -I -c 'import json,sys; print(*[n for n in json.load(sys.stdin) if n not in sys.argv[1:]])' "$@" <<<"$(agents)"; }

first_then_second() {  # first_then_second <tag> <HOME> <folder> <ds_install|ds_uninstall> <args...>: in the folder's FIRST
  # session (a folder Claude Code never ran in); when the plugin gave no note there, once more in a second session, so the
  # rest of the matrix still runs (the first try's FAIL stays).
  local tag="$1" h="$2" dir="$3" fn="$4"; shift 4
  start_claude "$h" "$dir" || { fail "$tag: claude did not start in ${dir#"$h/"}"; return 1; }
  "$fn" "$tag (first session in a new folder)" "$h" "$@" && return 0
  ((NOANSWER)) || return 1
  stop_claude
  start_claude "$h" "$dir" || { fail "$tag: claude did not start again"; return 1; }
  "$fn" "$tag (second session)" "$h" "$@"
}

core() {
  local A B TA others
  # --- computer A: steps 1 (home folder), 2, 4, 3, 7
  A="$(new_home A)"
  say "== computer A, home folder"
  readme_steps "1 A home" "$A" "$A" "$CORE_SRC" || return
  authenticate "1 A home" || { stop_claude; return; }
  ds_install "1 A home" "$A" agent
  stop_claude
  start_claude "$A" "$A" || { fail "2: claude did not start"; return; }
  ds_install "2 A home retry (no -2)" "$A" agent
  TA="$T"  # agent's own session, kept for step 3
  say "== computer A, repo folder ~/code/web-app (a second agent on A)"
  new_repo "$A/code/web-app"
  first_then_second "4 setup: A repo" "$A" "$A/code/web-app" ds_install web-app agent
  stop_claude
  say "== computer A, ~/code/notes: a session that is no agent"
  if first_then_second "4 non-agent session, uninstall web-app" "$A" "$A/code/notes" ds_uninstall web-app web-app; then
    key_gone "4 (at once, 1.0.14)" "$A" web-app 10  # a removed agent set up on this computer goes at once
    key_here "4" "$A" agent
    has_agent agent && pass "4: server: \"agent\" is still on" || fail "4: server: \"agent\" was removed too"
  fi
  stop_claude
  T="$TA"
  say "== computer A, agent's own session"
  ds_uninstall "3 agent session, uninstall agent" "$A" agent agent && key_gone "3" "$A" agent 10
  stop_claude
  start_claude "$A" "$A" || { fail "7: claude did not start"; return; }
  ds_install "7 reinstall after uninstall" "$A" agent
  stop_claude
  if ((LOGIN)) && has_agent agent; then
    say "== computer A, home folder: the offline outbox (XLI-408)"
    step9_outbox "$A" "$A" agent
  fi
  if ((LOGIN)) && [[ "$(agents)" == "[]" ]]; then  # step 5 needs an agent on A for its cross-computer half
    say "== computer A, ~/code/api: an agent on A for step 5 (A has none on)"
    new_repo "$A/code/api"
    first_then_second "5 setup: A api" "$A" "$A/code/api" ds_install api
    stop_claude
  fi

  step5_from_b "$A"
}

step5_from_b() {  # step5_from_b <A's HOME>: computer B: step 1 from a repo folder, 8, 5 (folder "site": a name no
  # earlier step switched off); then A's next start cleans up each agent B removed
  local A="$1" B others
  B="$(new_home B)"
  new_repo "$B/code/site"
  say "== computer B (same account), repo folder ~/code/site"
  readme_steps "1/8 B repo" "$B" "$B/code/site" "$CORE_SRC" || return
  authenticate "8 B" || { stop_claude; return; }
  others="$(on_names site)"
  # shellcheck disable=SC2086
  ds_install "1/8 B repo (second computer; lists A's: ${others:-none})" "$B" site $others
  local all_on
  all_on="$(on_names)"
  # shellcheck disable=SC2086
  ds_uninstall "5 B uninstall all" "$B" all $all_on && key_gone "5 B" "$B" site 10
  [[ "$(agents)" == "[]" ]] && pass "5: server: no agent on" || fail "5: server: agents still on: $(agents)"
  stop_claude
  local n a_keys=() dir
  for n in $others; do [[ -e "$(key_file "$A" "$n")" ]] && a_keys+=("$n"); done
  if ((${#a_keys[@]})); then
    say "== computer A again: each removed agent's next start cleans it up (${a_keys[*]})"
    for n in "${a_keys[@]}"; do
      dir="$A/code/$n"; [[ "$n" == agent ]] && dir="$A"
      next_start_cleans "5 A" "$A" "$dir" "$n"
    done
  elif ((LOGIN)); then
    fail "5: A had no agent key when B removed all, so A's cleanup at its next start was not checked"
  fi
}

step5_only() {  # E2E_PARTS=step5: step 5 alone: A (home folder) gets "agent", then step5_from_b
  local A
  A="$(new_home A)"
  say "== step 5 only: computer A, home folder: one agent"
  readme_steps "5 setup A home" "$A" "$A" "$CORE_SRC" || return
  authenticate "5 setup A home" || { stop_claude; return; }
  ds_install "5 setup A home" "$A" agent || { stop_claude; return; }
  stop_claude
  step5_from_b "$A"
}

update_part() {  # update_part a|b
  local how="$1" h dir cat_dir name v to_ref="$NEW_REF" to_version="$NEW_VERSION"
  [[ "$1" == b ]] && to_ref="$PUBLISHED_REF" to_version="$PUBLISHED_VERSION"  # self-update moves only to a release tag
  h="$(new_home "U$how")"; name="upd-$how"; dir="$h/code/$name"; new_repo "$dir"
  cat_dir="$WORK/U$how/catalog"
  pinned_catalog "$cat_dir" "$OLD_TAG" || { fail "6$how: no tag $OLD_TAG here"; return; }
  # Hold self-update off while the old copy goes in (its day's check counts as done): else the restart's own check
  # would find the catalog still on the old pin and wait a day. 6b clears it once the catalog moved.
  mkdir -p "$h/.config/datastash/state"
  printf '{"checked_at": %s, "result": "current"}\n' "$(date +%s)" > "$h/.config/datastash/state/self-update.json"
  say "== update $how: $OLD_TAG first"
  readme_steps "6$how $OLD_TAG" "$h" "$dir" "$cat_dir" || return
  v="$(installed_version "$h")"
  [[ "$v" == "${OLD_TAG#v}" ]] || { fail "6$how: installed $v, wanted ${OLD_TAG#v}"; stop_claude; return; }
  if [[ "$to_ref" == "$PUBLISHED_REF" ]]; then
    pinned_catalog "$cat_dir" "$PUBLISHED_REF"  # the release: the catalog now pins the published tag
  else
    cp "$WORK/cand/catalog/.claude-plugin/marketplace.json" "$cat_dir/.claude-plugin/marketplace.json"  # the candidate
  fi
  if [[ "$how" == a ]]; then
    type_line "/plugin marketplace update datastash"
    wait_for "[Uu]pdated|up to date|Successfully" 120 || { fail "6a: /plugin marketplace update did not finish"; show_tail; }
    sleep 2
    type_line "/reload-plugins"
    wait_for "[Rr]eloaded|[Rr]eload" 60
    sleep 3
    v="$(installed_version "$h")"
    [[ "$v" == "$to_version" ]] && pass "6a: marketplace update + /reload-plugins: $OLD_TAG -> $v" \
      || { fail "6a: after marketplace update + /reload-plugins the installed version is $v, wanted $to_version"; show_tail; }
    stop_claude; start_claude "$h" "$dir"
  else
    stop_claude
    printf '{"checked_at": 0}\n' > "$h/.config/datastash/state/self-update.json"  # a day has passed
    start_claude "$h" "$dir" || { fail "6b: claude did not start"; return; }
    local i r=""
    for ((i = 0; i < 300; i++)); do
      r="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result",""))' "$h/.config/datastash/state/self-update.json" 2>/dev/null)"
      [[ "$r" == updated || ( -n "$r" && "$r" != checking ) ]] && break
      sleep 1
    done
    [[ "$r" == updated ]] || { fail "6b: self-update at session start: result \"${r:-none}\", wanted updated"; stop_claude; return; }
    stop_claude
    start_claude "$h" "$dir" || { fail "6b: claude did not start after the update"; return; }
    v="$(installed_version "$h")"
    if [[ "$v" == "$PUBLISHED_VERSION" ]] && wait_for "datastash updated to v$PUBLISHED_VERSION" 20; then
      pass "6b: self-update at session start: $OLD_TAG -> $v; the next start said \"datastash updated to v$v\""
    else
      fail "6b: after self-update: installed $v; \"datastash updated to v$PUBLISHED_VERSION\" $(pane | grep -qF "datastash updated to v" && echo shown || echo 'not shown')"
    fi
  fi
  authenticate "6$how" || { stop_claude; return; }
  ds_install "6$how updated copy" "$h" "$name" && ds_uninstall "6$how cleanup" "$h" "$name" "$name"
  stop_claude
}

for p in $PARTS; do
  case "$p" in
    core) core ;;
    step5) step5_only ;;
    outbox) outbox_only ;;
    update) update_part a; update_part b ;;
    *) echo "unknown part: $p"; exit 2 ;;
  esac
done

say "---- results (${E2E_ACCOUNT}; published $PUBLISHED_REF${E2E_CANDIDATE:+; candidate $E2E_CANDIDATE}; old $OLD_TAG)"
printf '  %s\n' "${RESULTS[@]}"
if ((FAILS > 0)); then say "RESULT: FAIL, $FAILS step(s); $WAITS waiting"; exit 1; fi
if ((WAITS > 0)); then say "RESULT: NOT COMPLETE, $WAITS step(s) WAITING (no Claude login); nothing failed"; exit 3; fi
say "RESULT: PASS"
exit 0
