#!/usr/bin/env bash
# e2e-email-timing.sh (XLI-430): the email sign-in typed into a real interactive Claude Code on a throwaway HOME, with
# the time of every step. The person's two lines, `datastash sync EMAIL` and `datastash sync CODE`, are typed as a person
# types them; the code is read through datastash's selftest door for the selftest test account (x-live XLI-417,
# src/auth/selftest-email-code.ts: build box only, secret in a file, never printed). Two cases:
#   idle  the session waits for the person: each line is typed at the prompt.
#   busy  each line is typed while the session is still running a long command (the owner's case, 11-27): the line is
#         queued behind the agent's own work.
# Times, each from the Enter key: "sent" = the server sent the code (the plugin's code request is on disk), "signed in" =
# the server took the code and this computer holds its login, "shown" = the plugin's line is on screen.
# PASS when every step finishes and the CODE line is signed in and shown within E2E_CODE_LIMIT seconds (default 5) of
# the Enter key, or (busy) of the running command's end, when Claude Code writes the line.
# Options: E2E_REF (git ref of this repo to install; default: the working tree), E2E_CASES ("idle busy"),
#   E2E_CLAUDE_ENV (default /home/xpm/handover/claude-e2e.env, CLAUDE_CODE_OAUTH_TOKEN=...), E2E_SELFTEST_SECRET_FILE
#   (default /home/xpm/handover/selftest-oauth.secret), E2E_ACCOUNT (default plugin-e2e), E2E_BUSY_SECONDS (default 60).
# Exit 0 PASS, 1 FAIL, 2 can't run here. Cleanup: tmux sessions end, throwaway HOMEs go to ~/trash.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CLAUDE_BIN="$(command -v claude || true)"
CLAUDE_ENV="${E2E_CLAUDE_ENV:-/home/xpm/handover/claude-e2e.env}"
SECRET_FILE="${E2E_SELFTEST_SECRET_FILE:-/home/xpm/handover/selftest-oauth.secret}"
ACCOUNT="${E2E_ACCOUNT:-plugin-e2e}"
EMAIL="selftest-oauth-$ACCOUNT@example.invalid"
CASES="${E2E_CASES:-idle busy}"
LIMIT="${E2E_CODE_LIMIT:-5}"
BUSY="${E2E_BUSY_SECONDS:-60}"
REAL_HOME="$HOME"
RUN="dsmail-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ds-e2e-email.XXXXXX")"
FAILS=0
T=""

say() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
pass() { say "PASS $*"; }
fail() { say "FAIL $*"; FAILS=$((FAILS + 1)); }
cleanup() {
  local s
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep "^$RUN-"); do tmux kill-session -t "$s" 2>/dev/null; done
  pkill -f "$WORK/" 2>/dev/null; sleep 1; pkill -9 -f "$WORK/" 2>/dev/null
  mkdir -p "$REAL_HOME/trash"; mv "$WORK" "$REAL_HOME/trash/" 2>/dev/null
}
trap cleanup EXIT

command -v tmux >/dev/null || { echo "CAN'T RUN: tmux missing"; exit 2; }
[[ -n "$CLAUDE_BIN" ]] || { echo "CAN'T RUN: claude missing"; exit 2; }
[[ -r "$CLAUDE_ENV" ]] || { echo "CAN'T RUN: no Claude login file"; exit 2; }
[[ -r "$SECRET_FILE" ]] || { echo "CAN'T RUN: no selftest secret file"; exit 2; }

# The plugin to install: a local catalog holding this repo's plugin folder (at E2E_REF, or the working tree).
CAT="$WORK/catalog"
mkdir -p "$CAT/.claude-plugin" "$CAT/plugins"
if [[ -n "${E2E_REF:-}" ]]; then
  git -C "$REPO" archive "$E2E_REF" plugins/datastash | tar -x -C "$CAT" || { echo "CAN'T RUN: no ref $E2E_REF"; exit 2; }
else
  cp -r "$REPO/plugins/datastash" "$CAT/plugins/"
fi
python3 -I - "$REPO/.claude-plugin/marketplace.json" "$CAT/.claude-plugin/marketplace.json" <<'PY' || exit 2
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat["plugins"]:
    if p["name"] == "datastash":
        p["source"] = "./plugins/datastash"
json.dump(cat, open(sys.argv[2], "w"), indent=2)
PY
VERSION="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$CAT/plugins/datastash/.claude-plugin/plugin.json")"
say "plugin $VERSION (${E2E_REF:-working tree}), Claude Code $("$CLAUDE_BIN" --version | head -n 1)"

cat > "$WORK/launch.sh" <<'EOF'
#!/usr/bin/env bash
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
count() { pane | tr -s ' \t\n' ' ' | grep -oF -- "$1" | wc -l; }
now() { date +%s.%N; }
since() { python3 -I -c 'import sys; print(f"{float(sys.argv[2]) - float(sys.argv[1]):.1f}")' "$1" "$2"; }
type_line() { tmux send-keys -t "$T" -l -- "$1"; sleep 0.5; T0="$(now)"; tmux send-keys -t "$T" Enter; }
show_tail() { tmux capture-pane -t "$T" -p -J | grep -v '^[[:space:]]*$' | tail -n "${1:-12}" | sed 's/^/    | /'; }
wait_until() {  # wait_until <shell condition> <seconds>: prints the time it held (from T0), or returns 1
  local end
  end=$(( $(date +%s) + $2 ))
  while (( $(date +%s) < end )); do eval "$1" && { since "$T0" "$(now)"; return 0; }; sleep 0.1; done
  return 1
}
selftest_code() {  # the test account's kept code, once (never printed: it goes straight into the typed line)
  python3 -I - "$SECRET_FILE" "$ACCOUNT" <<'PY'
import json, sys, time, urllib.request
sec = open(sys.argv[1]).read().strip()
for _ in range(40):
    r = urllib.request.Request("https://getdatastash.com/api/v1/selftest/email-code", data=b"{}", method="POST", headers={
        "content-type": "application/json", "x-datastash-selftest-secret": sec, "x-datastash-selftest-account": sys.argv[2]})
    with urllib.request.urlopen(r, timeout=20) as f:
        code = json.loads(f.read()).get("code")
    if isinstance(code, str) and code.isdigit():
        print(code)
        break
    time.sleep(0.5)
PY
}
busy() {  # the session starts a long command of its own (the agent's own work)
  # (Claude Code 2.1.332 refuses a foreground `sleep`: a python wait is an ordinary long command)
  type_line "Use the Bash tool yourself, in the foreground (no subagent, no background run), to run this exact command and nothing else: python3 -c 'import time; time.sleep($BUSY)'; echo slept-$1"
  wait_until 'pane | grep -qF "time.sleep($BUSY)" && pane | grep -qE "esc to interrupt" && pgrep -f "time.sleep\\($BUSY\\)" >/dev/null' 90 >/dev/null || return 1
  BUSY_AT="$(now)"
  sleep 3
}

run_case() {  # run_case idle|busy
  local c="$1" h dir t code shown
  h="$WORK/$c/person"; dir="$h/code/e2e430-$c-$(date +%s)"  # a new agent name every run
  mkdir -p "$dir" && git -C "$dir" init -q
  python3 -I - "$h/.claude.json" "$dir" <<'PY'
import json, sys
json.dump({"hasCompletedOnboarding": True, "theme": "dark",
           "projects": {sys.argv[2]: {"hasTrustDialogAccepted": True}}}, open(sys.argv[1], "w"))
PY
  mkdir -p "$h/.claude"
  python3 -I - "$h/.claude/settings.json" "$BUSY" <<'PY'
import json, sys
json.dump({"permissions": {"allow": [f"Bash(python3 -c 'import time; time.sleep({sys.argv[2]})'; echo slept-{n})"
                                     for n in (1, 2)]}}, open(sys.argv[1], "w"))
PY
  env -i PATH="$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin" HOME="$h" "$CLAUDE_BIN" plugin marketplace add "$CAT" >/dev/null 2>&1 \
    && env -i PATH="$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin" HOME="$h" "$CLAUDE_BIN" plugin install datastash@datastash --scope user >/dev/null 2>&1 \
    || { fail "$c: plugin not installed"; return; }
  T="$RUN-$c"
  tmux new-session -d -s "$T" -x 220 -y 50 -c "$dir" \
    "env -i PATH='$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin' HOME='$h' USER=person LOGNAME=person TERM=xterm-256color LANG=C.UTF-8 ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 E2E_CLAUDE_ENV_FILE='$CLAUDE_ENV' '$WORK/launch.sh' '$CLAUDE_BIN' --permission-mode default"
  T0="$(now)"; wait_until 'pane | grep -qE "for shortcuts|shift\+tab"' 60 >/dev/null || { fail "$c: claude did not start"; show_tail; return; }
  sleep 4
  say "== $c"
  local req="ls $h/.config/datastash/state/email-code-*.json >/dev/null 2>&1" login="[[ -e $h/.config/datastash/login.email.json ]]"
  if [[ "$c" == busy ]]; then
    # the long command runs in this session's own turn (with clean sessions on, the session would hand it to a subagent,
    # which the model may start in the background): the person's typed "turn off clean sessions" first
    type_line "turn off clean sessions"
    wait_until 'pane | grep -qF "Clean sessions is off"' 120 >/dev/null || { fail "$c: clean sessions not turned off"; show_tail; return; }
    wait_until '! pane | grep -qE "esc to interrupt"' 60 >/dev/null; sleep 2
    busy 1 || { fail "$c: the long command did not start"; show_tail; return; }
  fi
  type_line "datastash sync $EMAIL"
  if t="$(wait_until "$req" 180)"; then say "$c EMAIL: sent ${t}s"; else fail "$c EMAIL: no code sent in 180 s"; show_tail; fi
  if t="$(wait_until 'pane | grep -qF "Check your email for a 6-digit code"' 180)"; then say "$c EMAIL: shown ${t}s"; else fail "$c EMAIL: line not shown"; fi
  code="$(selftest_code)"
  [[ -n "$code" ]] || { fail "$c: the selftest door gave no code"; return; }
  if [[ "$c" == busy ]]; then
    wait_until '! pane | grep -qE "esc to interrupt"' 200 >/dev/null
    sleep 3; busy 2 || { fail "$c: the second long command did not start"; show_tail; return; }
  fi
  shown="$(count "Done.")"
  local queued
  queued="$(cat "$h"/.claude/projects/*/*.jsonl 2>/dev/null | grep -c '"type":"queued_command"')"
  type_line "datastash sync $code"; code=""
  local base=0 end
  if [[ "$c" == busy ]]; then  # Claude Code writes a line typed during a tool call only once that call ends
    end="$(wait_until "[[ \$(cat $h/.claude/projects/*/*.jsonl 2>/dev/null | grep -c '\"type\":\"queued_command\"') -gt $queued ]]" 200)" \
      && base="$end" && say "$c CODE: Claude Code wrote the typed line ${end}s after Enter (when the running tool call ended)"
  fi
  within() { python3 -I -c 'import sys; t, b, l = map(float, sys.argv[1:]); print(f"{max(t - b, 0):.1f}"); sys.exit(t - b > l)' "$1" "$base" "$LIMIT"; }
  if t="$(wait_until "$login" 180)"; then
    if d="$(within "$t")"; then pass "$c CODE: signed in ${t}s after Enter (${d}s after the line was written)"
    else fail "$c CODE: signed in ${t}s after Enter (${d}s, over ${LIMIT}s)"; fi
  else fail "$c CODE: not signed in within 180 s"; show_tail; fi
  if t="$(wait_until '[[ "$(count "Done.")" -gt '"$shown"' ]]' 180)"; then
    if d="$(within "$t")"; then pass "$c CODE: Done. shown ${t}s after Enter (${d}s after the line was written)"
    else fail "$c CODE: Done. shown ${t}s after Enter (${d}s, over ${LIMIT}s)"; fi
  else fail "$c CODE: Done. not shown within 180 s"; show_tail; fi
  remove_all
  tmux kill-session -t "$T" 2>/dev/null
}

remove_all() {  # the test account back at rest (as test/e2e-real.sh leaves it): the person's typed uninstall of all
  [[ -e "$h/.config/datastash/login.email.json" ]] || return 0
  wait_until '! pane | grep -qE "esc to interrupt"' 200 >/dev/null; sleep 2
  type_line "/datastash:uninstall all"
  local m
  if wait_until 'pane | grep -qE "type exactly: \"?remove all [0-9]+"' 120 >/dev/null; then
    m="$(pane | tr -s ' \t\n' ' ' | grep -oE 'remove all [0-9]+' | tail -n 1)"
    wait_until '! pane | grep -qE "esc to interrupt"' 120 >/dev/null; sleep 2
    type_line "$m"
    if wait_until 'pane | grep -qiE "uninstalled|removed [0-9]"' 120 >/dev/null; then say "$c cleanup: $m done"
    else say "$c cleanup: no uninstall line seen (a later test/e2e-real.sh clean start removes what is left)"; fi
  else say "$c cleanup: no agents listed"; fi
}

for c in $CASES; do run_case "$c"; done
((FAILS)) && { say "RESULT: FAIL ($FAILS)"; exit 1; }
say "RESULT: PASS"
