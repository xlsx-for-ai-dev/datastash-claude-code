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
#      /datastash:install: agent "$E2E_USER" (the computer's user, never "agent"), the install list and "Time per card is
#      on"; server: it is on. 1.0.16: every install and uninstall also checks what the person sees: the model's own reply
#      starts with the plugin's line ("Done. Datastash is on for <name>." / "Done. Datastash is uninstalled from ..."),
#      and nothing after the typed command reads "denied", "error" or "not run" (test/e2e_visible.py).
#      Computer B, repo folder ~/code/site (below, 8): the same README steps from a repo folder: agent "site".
#      Also: the first session in a folder Claude Code never ran in (A's ~/code/web-app, ~/code/notes) must work; when the
#      plugin gives no note there ("datastash did not answer") that FAILs, and the step is tried once more in a second
#      session so the rest of the matrix still runs.
#   2  A: /datastash:install again in a new session: the same agent, no "agent-2" (pane and server).
#   4  A: a third folder's session that is no agent (~/code/notes) types /datastash:uninstall web-app while A also holds
#      "agent"'s key (the owner's case): web-app off on the server, its key gone here at once (1.0.14: not at its next
#      start), "agent" untouched.
#      (A's ~/code/web-app gets its agent first: /datastash:install there, "web-app".)
#   3  A: /datastash:uninstall <home agent> from its own session: off on the server, its key gone here.
#   3b A, same session: /datastash:uninstall again: "Datastash isn't installed on this agent. Nothing was removed."
#   7  A, same session: /datastash:install after that uninstall: the same name again (no "-2", never "agent").
#   10 A, ~/code/named: /datastash:install pat-named (a typed name), uninstall it, then a plain /datastash:install in the
#      same session: "pat-named" again (the uninstall's name record), not the folder's "named"; then removed.
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
#   11 1.0.17, with E2E_CANDIDATE and E2E_NEXT_REF (a pushed test ref: the candidate's code at a higher version): right
#      after step 1, the candidate catalog moves to that ref and one ordinary message is sent; with self-update held off,
#      Claude Code's own auto-update (on since step 1's /datastash:install: "Auto-update is on for datastash.") must move
#      the installed version within 12 minutes, with nobody typing an update. Every 1.0.17+ install also checks the
#      setting is on; an uninstall of the last agent on a computer checks it is off ("Auto-update is off for datastash.").
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
#   new label. Agents are removed only by the person's typed uninstall (/datastash:uninstall all, then the typed
#   "remove all N"), never by a plain switch-off: an uninstalled name installs again under its own name (x-live #510),
#   a switched-off one is refused ("the name is switched off"), so a switch-off would break the next run.
#   Clean start: when agents are left on (a run that was killed before its cleanup), a cleanup computer (its own HOME, its
#   own sign-in, agent "e2e-clean" in ~/code/e2e-clean) installs and types /datastash:uninstall all; then the read-back
#   disconnects every app connection but its own (sync disconnect); the run stops if anything is left.
#   Recovery of a name an older harness switched off: E2E_SWITCH_ON="agent ..." first switches those back on through the
#   person's own switch-on (ledger_agent_switch_off action "on"), so the clean start's typed uninstall removes them.
# Every run tees its whole output to E2E_LOG (default ~/x/ops/proofs/plugin-e2e-real-<UTC time>-<pid>.md).
# Sign-in budget: a run counts the selftest sign-ins it needs (the read-back 1; core 2, step5 2, outbox 1, update 2) and
#   waits (one sleep, until enough of the oldest free) when those plus this harness's own sign-ins of the last hour
#   (E2E_SIGNIN_LEDGER, default ~/.local/state/datastash-e2e/selftest-signins: one time per line) would pass 20; a
#   cleanup computer counts one more. E2E_BUDGET_WAIT=0: stop instead of waiting. A server refusal (HTTP 429) names
#   its reason: rate_limited (the hourly limit) or too_many_accounts (no new label can be made).
# Options: E2E_ACCOUNT (selftest label, default plugin-e2e), E2E_MODEL (a --model for the sessions; default Claude Code's),
#   E2E_PARTS ("core update", default both; "step5" runs step 5 alone; "outbox" runs step 9 alone), E2E_OLD_TAG, E2E_KEEP=1 (say where the HOMEs went in ~/trash),
#   E2E_CANDIDATE=<branch or tag, pushed to the plugin repo>: test that commit before it is released: the core steps
#   install it from a local copy of the catalog pinned to it, and 6a updates to it; 6b (self-update moves only to a
#   release tag) stays on the published pin.
# Cleanup, pass or fail (its last step, or the trap when a run stops early): every agent left on the test account is
# removed by the typed uninstall from a computer of this run that holds one of their keys (else a cleanup computer),
# every app connection on it is disconnected (the read-back's own last), the tmux sessions end, processes from the
# throwaway HOMEs stop, the HOMEs go to ~/trash.
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
# 1.0.16: the throwaway computers' user name (USER/LOGNAME in every session): a home folder's agent is named after it,
# never "agent" (a tester's Mac: "tester-one").
E2E_USER="${E2E_USER:-e2e-home}"
HA="$E2E_USER"
PARTS="${E2E_PARTS:-core update}"
OLD_TAG="${E2E_OLD_TAG:-v1.0.12}"
export E2E_SIGNIN_LEDGER="${E2E_SIGNIN_LEDGER:-$REAL_HOME/.local/state/datastash-e2e/selftest-signins}"
SIGNIN_MAX_PER_HOUR=20
RUN="dsreal-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ds-e2e-real.XXXXXX")"
export E2E_SERVER_TOKEN_FILE="$WORK/server.tok"
SRV=(python3 -I "$HERE/e2e_server.py")
FAILS=0 WAITS=0 STARTS=0 T="" LOGIN=0 BADTOOL=0 NOANSWER=0 IN_CLEANUP=0 END_CLEAN=0 CLEAN_N=0 CLEAN_HOME=""
RESULTS=()

# --- the log: every run leaves its whole output under ~/x/ops/proofs (no secret is ever printed).
E2E_LOG="${E2E_LOG:-$REAL_HOME/x/ops/proofs/plugin-e2e-real-$(date -u +%Y%m%d-%H%M%SZ)-$$.md}"
mkdir -p "$(dirname "$E2E_LOG")" && touch "$E2E_LOG" || { echo "CAN'T RUN: the log $E2E_LOG can't be written"; exit 2; }
exec > >(tee -a "$E2E_LOG") 2>&1

say() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
pass() { say "PASS $*"; RESULTS+=("PASS  $*"); }
fail() { say "FAIL $*"; RESULTS+=("FAIL  $*"); FAILS=$((FAILS + 1)); }
waiting() { say "WAITING $*"; RESULTS+=("WAIT  $*"); WAITS=$((WAITS + 1)); }

cleanup() {
  local s left
  trap - EXIT
  IN_CLEANUP=1
  if [[ -s "$E2E_SERVER_TOKEN_FILE" ]]; then
    # The run stopped before its own cleanup step: the typed uninstall now (never a plain switch-off).
    ((END_CLEAN)) || ! declare -F typed_cleanup >/dev/null || typed_cleanup "cleanup (the run stopped early)" || true
    left="$("${SRV[@]}" agents 2>/dev/null)"
    [[ "$left" == "[]" ]] && say "cleanup: no agent left on the test account" || say "cleanup: WARNING agents left on the test account: ${left:-unknown}"
    "${SRV[@]}" disconnect-all 2>&1 | sed 's/^/cleanup: /'
    : > "$E2E_SERVER_TOKEN_FILE"
  fi
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep "^$RUN-"); do tmux kill-session -t "$s" 2>/dev/null; done
  pkill -f "$WORK/" 2>/dev/null; sleep 1; pkill -9 -f "$WORK/" 2>/dev/null
  mkdir -p "$REAL_HOME/trash"
  mv "$WORK" "$REAL_HOME/trash/" 2>/dev/null && [[ "${E2E_KEEP:-0}" == 1 ]] && say "throwaway HOMEs kept in $REAL_HOME/trash/$(basename "$WORK")"
  say "log: $E2E_LOG"
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
signins_last_hour() {
  python3 -I -c 'import sys, time
n = time.time()
try:
    print(sum(1 for l in open(sys.argv[1]) if l.strip().isdigit() and n - int(l) < 3600))
except OSError:
    print(0)' "$E2E_SIGNIN_LEDGER"
}
budget_wait() {  # budget_wait <sign-ins still to make>: one wait when the last hour's sign-ins plus these would pass 20
  local need="$1" used wait_s
  read -r used wait_s < <(python3 -I -c 'import sys, time
now, need, cap = time.time(), int(sys.argv[2]), int(sys.argv[3])
try:
    ts = sorted(int(l) for l in open(sys.argv[1]) if l.strip().isdigit() and now - int(l) < 3600)
except OSError:
    ts = []
over = len(ts) + need - cap
print(len(ts), -1 if need > cap else (max(0, int(ts[over - 1] + 3600 - now)) + 10 if over > 0 else 0))' \
    "$E2E_SIGNIN_LEDGER" "$need" "$SIGNIN_MAX_PER_HOUR")
  ((wait_s < 0)) && { say "sign-in budget: $need sign-ins can never fit in $SIGNIN_MAX_PER_HOUR an hour"; return 1; }
  if ((wait_s > 0)); then
    [[ "${E2E_BUDGET_WAIT:-1}" == 1 ]] || { say "sign-in budget: $need more needed, $used used in the last hour (limit $SIGNIN_MAX_PER_HOUR); E2E_BUDGET_WAIT=0, so no wait"; return 1; }
    say "sign-in budget: $need more needed, $used used in the last hour (limit $SIGNIN_MAX_PER_HOUR): waiting ${wait_s}s, until $(date -u -d "@$(($(date +%s) + wait_s))" +%H:%M:%SZ)"
    sleep "$wait_s"
    used="$(signins_last_hour)"
  fi
  say "sign-in budget: $need more needed; $used used in the last hour by this harness (limit $SIGNIN_MAX_PER_HOUR)"
}
NEED_START=$NEED
[[ -n "${E2E_SWITCH_ON:-}" ]] && NEED_START=$((NEED + 1))  # the recovery's cleanup computer
budget_wait "$NEED_START" || { echo "CAN'T RUN: the sign-in budget (above)"; exit 2; }

# --- the server read-back: its own connection to the selftest account (the clean start is below, before the matrix).
"${SRV[@]}" signin || { echo "CAN'T RUN: the read-back could not sign in to the selftest account \"$E2E_ACCOUNT\" (reason above)"; exit 2; }
agents() { "${SRV[@]}" agents; }
has_agent() { agents | python3 -I -c 'import json,sys; sys.exit(0 if sys.argv[1] in json.load(sys.stdin) else 1)' "$1"; }

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
      ((IN_CLEANUP)) && return 1
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
    "env -i PATH='$WORK/bin:$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin' HOME='$h' USER='$E2E_USER' LOGNAME='$E2E_USER' TERM=xterm-256color LANG=C.UTF-8 BROWSER='$WORK/bin/browser' ENABLE_CLAUDEAI_MCP_SERVERS=false E2E_CLAUDE_ENV_FILE='$( ((LOGIN)) && printf '%s' "$CLAUDE_ENV")' '$WORK/launch.sh' '$CLAUDE_BIN' ${model[*]}"
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
# 1.0.16: what the person sees. said: the model's own reply after the typed command starts with the plugin's line
# (relayed word for word); clean: nothing after it reads "denied", "error" or "not run".
said() { pane | python3 -I "$HERE/e2e_visible.py" said "$1" "$2"; }
clean_out() { pane | python3 -I "$HERE/e2e_visible.py" clean "$1"; }
visible_ok() {  # visible_ok <tag> <typed command> <expected start of the reply>
  local got
  if got="$(said "$2" "$3")"; then pass "$1: the reply the person sees: \"${got:0:160}\""
  else fail "$1: the person's visible reply is not the plugin's line \"$3\" ($got)"; show_tail 12; return 1; fi
  if got="$(clean_out "$2")"; then pass "$1: nothing visible says denied, error or not run"
  else fail "$1: the visible output reads as a failure: $got"; return 1; fi
}

new_lines() {  # new_lines <HOME>: the installed plugin is 1.0.16 or later (the visible result lines); 6b stays on the pin
  python3 -I -c 'import sys; v=tuple(int(x) for x in (sys.argv[1] or "0").split(".")[:3] if x.isdigit()); sys.exit(0 if v >= (1, 0, 16) else 1)' "$(installed_version "$1")"
}

key_file() { printf '%s/.config/datastash/agent-%s.key' "$1" "$2"; }

# 1.0.17: Claude Code's own auto-update for the datastash marketplace, as Claude Code reads it: the user settings'
# extraKnownMarketplaces entry first, then the /plugin toggle's known_marketplaces.json ("true", "false" or "none").
has_autoupdate() {  # has_autoupdate <HOME>: the installed plugin is 1.0.17 or later
  python3 -I -c 'import sys; v=tuple(int(x) for x in (sys.argv[1] or "0").split(".")[:3] if x.isdigit()); sys.exit(0 if v >= (1, 0, 17) else 1)' "$(installed_version "$1")"
}
autoupdate_state() {  # autoupdate_state <HOME>
  python3 -I - "$1/.claude/settings.json" "$1/.claude/plugins/known_marketplaces.json" <<'PY2'
import json, sys
def entry(path, keys):
    try:
        v = json.load(open(path))
    except (OSError, ValueError):
        return None
    for k in keys:
        v = v.get(k) if isinstance(v, dict) else None
    return v if isinstance(v, dict) else None
found = None
for path, keys in ((sys.argv[1], ["extraKnownMarketplaces", "datastash"]), (sys.argv[2], ["datastash"])):
    e = entry(path, keys)
    if e is None:
        continue
    found = found or "false"
    if isinstance(e.get("autoUpdate"), bool):
        print(str(e["autoUpdate"]).lower()); sys.exit()
print(found or "none")
PY2
}
local_agents() {  # local_agents <HOME>: how many agents are set up on this computer (the plugin's own records)
  python3 -I - "$1/.config/datastash" <<'PY2'
import json, os, re, sys
n = 0
try:
    names = os.listdir(sys.argv[1])
except OSError:
    names = []
for f in names:
    if re.fullmatch(r"agent-[a-z0-9\-]+\.json", f):
        try:
            c = json.load(open(os.path.join(sys.argv[1], f)))
        except (OSError, ValueError):
            continue
        n += bool(isinstance(c, dict) and c.get("name") and c.get("workbook_id"))
print(n)
PY2
}

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
  local cmd="/datastash:install${DS_INSTALL_ARG:+ $DS_INSTALL_ARG}"
  type_line "$cmd"
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
  grep -qF "Time per card is on." <<<"$f" || { fail "$tag: no \"Time per card is on\" line"; ok=0; }
  if new_lines "$h"; then visible_ok "$tag" "$cmd" "Done. Datastash is on for $name. Time per card is on." || ok=0; fi
  if has_autoupdate "$h"; then  # 1.0.17: Claude Code's own auto-update is on for datastash, and the person is told
    grep -qF "Auto-update is on for datastash." <<<"$f" || { fail "$tag: no \"Auto-update is on for datastash.\" line"; ok=0; }
    local au; au="$(autoupdate_state "$h")"
    [[ "$au" == true ]] && pass "$tag: Claude Code's auto-update setting for datastash is on" \
      || { fail "$tag: Claude Code's auto-update setting for datastash is \"$au\", wanted true"; ok=0; }
  fi
  [[ "$name" != agent ]] && grep -qF -- "- agent (" <<<"${f##*datastash is on for your agents:}" && { fail "$tag: an agent named \"agent\" was made"; ok=0; }
  [[ -s "$(key_file "$h" "$name")" ]] || { fail "$tag: no key file for \"$name\" on this computer"; ok=0; }
  has_agent "$name" || { fail "$tag: server: \"$name\" is not on"; ok=0; }
  agents | python3 -I -c 'import json,re,sys; n=sys.argv[1]; sys.exit(1 if any(re.fullmatch(re.escape(n)+r"-\d+", a) for a in json.load(sys.stdin)) else 0)' "$name" \
    || { fail "$tag: server: a \"$name-N\" exists"; ok=0; }
  ((ok)) && pass "$tag: /datastash:install: \"$name\", list and \"Time per card is on\"; key here; server: $(agents)"
  ((ok))
}

ds_uninstall() {  # ds_uninstall <tag> <HOME> <args> <removed names...>: server: those off; their key files gone here
  local tag="$1" h="$2" args="$3"; shift 3
  local base_off base_no ok=1 n f m offtxt="Datastash is uninstalled from"
  new_lines "$h" || offtxt="datastash is off for"  # an older copy (6b): its own line
  ((LOGIN)) || { waiting "$tag: /datastash:uninstall $args (needs the Claude login)"; return 1; }
  BADTOOL=0 NOANSWER=0
  local base_done; base_done="$(count " · done ")"
  base_off="$(count "$offtxt")"; base_no="$(count "Not done")/$(count "Datastash isn't installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")"
  type_line "/datastash:uninstall${args:+ $args}"
  local cond='(( $(count "$offtxt") > base_off )) || [[ "$(count "Not done")/$(count "Datastash isn'"'"'t installed")/$(count "To go ahead, type exactly:")/$(count "Type the names to remove")/$(count "datastash did not answer")" != "$base_no" ]] || (( $(count " · done ") > base_done ))'
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
  if (( $(count "$offtxt") <= base_off )); then
    if grep -qF "datastash did not answer" <<<"$f"; then NOANSWER=1; fail "$tag: \"datastash did not answer\" (the plugin's prompt hook gave no note)"; show_tail 8; return 1; fi
    fail "$tag: /datastash:uninstall $args: no \"$offtxt\" line"; show_tail 20; return 1
  fi
  local line; line="$(grep -oE "$offtxt [^.]*\." <<<"$f" | tail -n 1)"
  for n in "$@"; do
    grep -qE -- "(from|,|and) \"?$n\"?(,| and|\.)" <<<"$line" || grep -qF -- "\"$n\"" <<<"$line" \
      || { fail "$tag: the uninstall line does not name \"$n\" ($line)"; ok=0; }
    has_agent "$n" && { fail "$tag: server: \"$n\" is still on"; ok=0; }
  done
  local last="/datastash:uninstall${args:+ $args}"
  [[ "$args" == all && -n "${m:-}" ]] && last="$m"
  [[ -z "$args" ]] && last="$*"
  if [[ "$offtxt" != "datastash is off for" ]]; then visible_ok "$tag" "$last" "Done. Datastash is uninstalled from" || ok=0; fi
  if has_autoupdate "$h"; then  # 1.0.17: the last agent on this computer turns auto-update off and says so; else it stays
    local au left; au="$(autoupdate_state "$h")"; left="$(local_agents "$h")"
    if ((left == 0)); then
      grep -qF "Auto-update is off for datastash." <<<"$f" || { fail "$tag: the last agent here: no \"Auto-update is off for datastash.\" line"; ok=0; }
      [[ "$au" == false ]] && pass "$tag: the last agent here: auto-update for datastash is off" \
        || { fail "$tag: the last agent here: auto-update for datastash is \"$au\", wanted false"; ok=0; }
    else
      [[ "$au" == true ]] && pass "$tag: $left agent(s) still here: auto-update for datastash stays on" \
        || { fail "$tag: $left agent(s) still here: auto-update for datastash is \"$au\", wanted true"; ok=0; }
    fi
  fi
  ((ok)) && pass "$tag: /datastash:uninstall $args: removed $*; server: $(agents)"
  ((ok))
}

ds_not_installed() {  # ds_not_installed <tag>: /datastash:uninstall where nothing is installed: the owner's line, plainly
  local tag="$1" want="Datastash isn't installed on this agent. Nothing was removed." base
  base="$(count "$want")"
  type_line "/datastash:uninstall"
  drive_until '(( $(count "$want") > base ))' 60
  wait_idle 30
  if (( $(count "$want") > base )); then pass "$tag: the second /datastash:uninstall shows \"$want\""
  else fail "$tag: the second /datastash:uninstall did not show \"$want\""; show_tail 10; return 1; fi
  if pane | python3 -I "$HERE/e2e_visible.py" shown "/datastash:uninstall" "did not answer"; then
    fail "$tag: it said \"did not answer\""; return 1
  fi
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
  (cd "$dir" && env -i PATH="$WORK/bin:/usr/local/bin:/usr/bin:/bin" HOME="$h" USER="$E2E_USER" LOGNAME="$E2E_USER" LANG=C.UTF-8 CLAUDE_PROJECT_DIR="$dir" ${DS_AS:+DATASTASH_AGENT=$DS_AS} \
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
  ds_install "9 setup A home" "$A" "$HA" || { stop_claude; return; }
  stop_claude
  step9_outbox "$A" "$A" "$HA"
  start_claude "$A" "$A" || { fail "9 cleanup: claude did not start"; return; }
  ds_uninstall "9 cleanup" "$A" "$HA" "$HA"
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
  ds_install "1 A home" "$A" "$HA"  # 1.0.16: the computer's user name, never "agent"
  stop_claude
  [[ -n "${E2E_NEXT_REF:-}" ]] && step11_hands_free "$A"
  start_claude "$A" "$A" || { fail "2: claude did not start"; return; }
  ds_install "2 A home retry (no -2)" "$A" "$HA"
  TA="$T"  # the home agent's own session, kept for step 3
  say "== computer A, repo folder ~/code/web-app (a second agent on A)"
  new_repo "$A/code/web-app"
  first_then_second "4 setup: A repo" "$A" "$A/code/web-app" ds_install web-app "$HA"
  stop_claude
  say "== computer A, ~/code/notes: a session that is no agent"
  if first_then_second "4 non-agent session, uninstall web-app" "$A" "$A/code/notes" ds_uninstall web-app web-app; then
    key_gone "4 (at once, 1.0.14)" "$A" web-app 10  # a removed agent set up on this computer goes at once
    key_here "4" "$A" "$HA"
    has_agent "$HA" && pass "4: server: \"$HA\" is still on" || fail "4: server: \"$HA\" was removed too"
  fi
  stop_claude
  T="$TA"
  say "== computer A, the home agent's own session"
  ds_uninstall "3 own session, uninstall $HA" "$A" "$HA" "$HA" && key_gone "3" "$A" "$HA" 10
  # 1.0.16: the second run, same session: nothing is installed on A now (web-app went in step 4)
  ds_not_installed "3b second uninstall, same session"
  # 1.0.16 (owner's reinstall, 11-25): /datastash:install in the same session brings the same name back
  ds_install "7 reinstall after uninstall, same session" "$A" "$HA"
  stop_claude
  # 1.0.16: a name that is no default (typed once) comes back too: the uninstall's record, not the folder's name
  say "== computer A, ~/code/named: a typed name, uninstalled, then the same session's plain reinstall"
  new_repo "$A/code/named"
  start_claude "$A" "$A/code/named" || { fail "10: claude did not start"; return; }
  DS_INSTALL_ARG=pat-named ds_install "10 install with a typed name" "$A" pat-named "$HA"
  ds_uninstall "10 uninstall the typed name" "$A" pat-named pat-named && key_gone "10" "$A" pat-named 10
  ds_install "10 reinstall, same session: the same name, not the folder's" "$A" pat-named "$HA"
  grep -qF -- "- named (" <<<"$(flat)" && fail "10: the folder's name \"named\" was made instead"
  ds_uninstall "10 cleanup" "$A" pat-named pat-named
  stop_claude
  if ((LOGIN)) && has_agent "$HA"; then
    say "== computer A, home folder: the offline outbox (XLI-408)"
    step9_outbox "$A" "$A" "$HA"
  fi
  if ((LOGIN)) && [[ "$(agents)" == "[]" ]]; then  # step 5 needs an agent on A for its cross-computer half
    say "== computer A, ~/code/api: an agent on A for step 5 (A has none on)"
    new_repo "$A/code/api"
    first_then_second "5 setup: A api" "$A" "$A/code/api" ds_install api
    stop_claude
  fi

  step5_from_b "$A"
}

step11_hands_free() {  # step11_hands_free <A's HOME>: 1.0.17: a new release arrives with nobody typing an update.
  # The candidate catalog (a local copy, as in every candidate run) is moved to E2E_NEXT_REF, a pushed test ref with the
  # candidate's code and a higher version; self-update is held off for A (its day's check counts as done), so only
  # Claude Code's own auto-update (turned on by step 1's /datastash:install) can move it. One ordinary message, then wait
  # (Claude Code: a random delay of up to 10 minutes after the first message). No sign-in.
  local h="$1" sha want v i cat="$WORK/cand/catalog/.claude-plugin/marketplace.json"
  [[ -n "${E2E_CANDIDATE:-}" ]] || { say "11: skipped (needs E2E_CANDIDATE: the catalog is the published one)"; return; }
  git -C "$REPO" fetch -q "$(git -C "$REPO" remote | head -n 1)" "$E2E_NEXT_REF" 2>/dev/null
  sha="$(git -C "$REPO" rev-parse --verify --quiet FETCH_HEAD^{commit})" || { fail "11: $E2E_NEXT_REF is not on the plugin repo's remote"; return; }
  want="$(git -C "$REPO" show "$sha:plugins/datastash/.claude-plugin/plugin.json" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
  [[ "$(autoupdate_state "$h")" == true ]] || { fail "11: auto-update is not on for datastash after step 1"; return; }
  say "== 11 hands-free release: catalog moved to $E2E_NEXT_REF at ${sha:0:7} (plugin $want); installed $(installed_version "$h")"
  cp "$cat" "$cat.candidate"
  python3 -I - "$cat" "$E2E_NEXT_REF" "$sha" <<'PY2'
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat["plugins"]:
    if p["name"] == "datastash":
        p["source"].update(ref=sys.argv[2], sha=sys.argv[3])
json.dump(cat, open(sys.argv[1], "w"), indent=2)
PY2
  printf '{"checked_at": %s, "result": "current"}\n' "$(date +%s)" > "$h/.config/datastash/state/self-update.json"
  start_claude "$h" "$h" || { fail "11: claude did not start"; return; }
  ((LOGIN)) || { waiting "11: hands-free release (needs the Claude login for a first message)"; stop_claude; return; }
  type_line "Reply with only the word ok."
  for ((i = 0; i < 720; i++)); do
    answer_dialog >/dev/null 2>&1
    v="$(installed_version "$h")"
    [[ "$v" == "$want" ]] && break
    sleep 1
  done
  local r; r="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result",""))' "$h/.config/datastash/state/self-update.json" 2>/dev/null)"
  if [[ "$v" == "$want" && "$r" == current ]]; then
    pass "11: hands-free release: Claude Code's own auto-update moved $NEW_VERSION -> $v after ${i}s, no update typed (self-update held off: \"$r\")"
    pane | grep -qF "Plugin updated" && pass "11: Claude Code told the person: $(pane | grep -oE "Plugin updated[^·]*" | tail -n 1)"
  else
    fail "11: hands-free release: installed $v after ${i}s (wanted $want; self-update \"$r\")"; show_tail 10
  fi
  stop_claude
  mv -f "$cat.candidate" "$cat"  # the rest of the run (B, 6a) installs the candidate itself again
  say "11: the catalog is back on the candidate $E2E_CANDIDATE for the rest of the run"
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
      dir="$A/code/$n"; [[ "$n" == "$HA" ]] && dir="$A"
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
  ds_install "5 setup A home" "$A" "$HA" || { stop_claude; return; }
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

clean_computer() {  # clean_computer <tag> <sign-ins still to make after it>: a fresh computer whose own agent "e2e-clean"
  # (removed by the uninstall that follows, so it installs again next time) can sign the person's typed names
  local tag="$1" h dir
  budget_wait $((1 + $2)) || return 1
  h="$(new_home "C$((++CLEAN_N))")"; dir="$h/code/e2e-clean"; new_repo "$dir"
  say "== $tag: a cleanup computer, repo folder ~/code/e2e-clean"
  readme_steps "$tag: cleanup computer" "$h" "$dir" "$CORE_SRC" || return 1
  authenticate "$tag: cleanup computer" || { stop_claude; return 1; }
  # shellcheck disable=SC2046
  ds_install "$tag: cleanup computer" "$h" e2e-clean $(on_names e2e-clean) || { stop_claude; return 1; }
  CLEAN_HOME="$h"
}

typed_cleanup() {  # typed_cleanup <tag> [<sign-ins still to make after it>]: every agent left on goes the person's way:
  # /datastash:uninstall all, then the typed "remove all N", from a computer of this run that holds one of their keys
  # (else a cleanup computer). Never a plain switch-off. True when no agent is left on.
  local tag="$1" on k n h="" dir keep="$T"
  on="$(on_names)" || { say "$tag: WARNING the read-back could not list the agents"; return 1; }
  [[ -z "$on" ]] && { say "$tag: no agent on the test account"; return 0; }
  ((LOGIN)) || { say "$tag: WARNING agents left on ($on) and no Claude login to uninstall them"; return 1; }
  say "$tag: agents on: $on; removing them with the typed uninstall"
  for k in "$WORK"/*/person/.config/datastash/agent-*.key; do
    [[ -s "$k" ]] || continue
    n="${k##*/agent-}"; n="${n%.key}"
    [[ " $on " == *" $n "* ]] || continue
    h="${k%/.config/datastash/*}"
    break
  done
  if [[ -n "$h" ]]; then
    dir="$h/code/$n"; [[ "$n" == "$HA" || ! -d "$dir" ]] && dir="$h"
    say "== $tag: ${h#"$WORK/"}, \"$n\"'s own folder (its key signs the typed names)"
    start_claude "$h" "$dir" || { say "$tag: WARNING claude did not start"; T="$keep"; return 1; }
  else
    clean_computer "$tag" "${2:-0}" || { say "$tag: WARNING the cleanup computer could not install"; T="$keep"; return 1; }
    h="$CLEAN_HOME"
  fi
  # shellcheck disable=SC2046
  ds_uninstall "$tag" "$h" all $(on_names)
  stop_claude
  T="$keep"
  [[ "$(agents)" == "[]" ]] && { say "$tag: no agent left on the test account"; return 0; }
  say "$tag: WARNING agents still on: $(agents)"
  return 1
}

# --- the clean start: no agent on (removed by the typed uninstall), no app connection but the read-back's own.
if [[ -n "${E2E_SWITCH_ON:-}" ]]; then
  say "recovery: switching on $E2E_SWITCH_ON (the person's own switch-on), so the typed uninstall removes it and its name installs again"
  for n in $E2E_SWITCH_ON; do
    "${SRV[@]}" on "$n" | sed 's/^/  /'
    ((PIPESTATUS[0] == 0)) || { echo "CAN'T RUN: the switch-on of \"$n\" was refused (reason above)"; exit 2; }
  done
fi
START_AGENTS="$(agents)" || { echo "CAN'T RUN: ledger_agents_list refused on the read-back"; exit 2; }
if [[ "$START_AGENTS" != "[]" ]]; then
  say "the test account has agents on from an earlier run: $START_AGENTS"
  IN_CLEANUP=1
  typed_cleanup "clean start" $((NEED - 1)) || { END_CLEAN=1; echo "CAN'T RUN: the test account still has agents on: $(agents)"; exit 2; }
  IN_CLEANUP=0
fi
START_APPS="$("${SRV[@]}" other-apps)" || { echo "CAN'T RUN: sync apps refused on the read-back"; exit 2; }
if [[ "$START_APPS" != "[]" ]]; then
  say "the test account has app connections left from an earlier run: $START_APPS; disconnecting them first"
  "${SRV[@]}" disconnect-others | sed 's/^/  /'
  [[ "$("${SRV[@]}" other-apps)" == "[]" ]] || { echo "CAN'T RUN: the test account still has other app connections"; exit 2; }
fi
say "server: test account \"$E2E_ACCOUNT\" signed in for read-back; clean: no agent on, no other app connection"

for p in $PARTS; do
  case "$p" in
    core) core ;;
    step5) step5_only ;;
    outbox) outbox_only ;;
    update) update_part a; update_part b ;;
    *) echo "unknown part: $p"; exit 2 ;;
  esac
done

IN_CLEANUP=1
if typed_cleanup "cleanup"; then pass "cleanup: no agent left on the test account (typed uninstall only)"
else fail "cleanup: agents left on the test account: $(agents)"; fi
END_CLEAN=1 IN_CLEANUP=0

say "---- results (${E2E_ACCOUNT}; published $PUBLISHED_REF${E2E_CANDIDATE:+; candidate $E2E_CANDIDATE}; old $OLD_TAG)"
printf '  %s\n' "${RESULTS[@]}"
if ((FAILS > 0)); then say "RESULT: FAIL, $FAILS step(s); $WAITS waiting"; exit 1; fi
if ((WAITS > 0)); then say "RESULT: NOT COMPLETE, $WAITS step(s) WAITING (no Claude login); nothing failed"; exit 3; fi
say "RESULT: PASS"
exit 0
