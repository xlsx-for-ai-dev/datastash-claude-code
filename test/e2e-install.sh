#!/usr/bin/env bash
# e2e-install.sh (XLI-390): the README's install steps, typed into a real interactive Claude Code on an EMPTY HOME, the
# way a person types them. Catches what the offline tests can't: Claude Code itself running the plugin's hooks and the
# connector's headersHelper as the terminal host ("cli"), which `claude plugin ...` and `claude mcp list` never do (they
# run as "sdk-cli", so a 1.0.1-style placeholder header never shows there).
#
# For each folder (E2E_FOLDERS, default "home repo": the home folder itself, and a fresh git repo ~/code/web-app), on its
# own brand-new HOME:
#   1. /plugin marketplace add xlsx-for-ai-dev/datastash-claude-code   (or E2E_TAG's pinned catalog, below)
#   2. /plugin install datastash@datastash, "Install for you (user scope)"
#   3. restart claude (/exit, start again)
#   4. /mcp, datastash: it must say it needs sign-in and offer Authenticate (1.0.1: "failed", the helper's placeholder
#      header turned Claude Code's OAuth off). Authenticate is chosen: the sign-in link Claude Code makes must open
#      datastash's own sign-in page (the page a person signs in on). The run stops there (Esc).
#   5. (1.0.23+: a not-signed-in computer shows "datastash sync you@email.com" and records no install.) /datastash:install: the plugin's own prompt hook takes the typed command and picks the agent's name: "agent" in
#      the home folder (1.0.1: the computer user's name), "web-app" in the repo.
#   6. /datastash:uninstall in that same session, which has no agent key on this computer (1.0.12 refused it there
#      with "Type /datastash:install again"): 1.0.16's prompt hook answers it with its own line, "Datastash isn't
#      installed on this agent. Nothing was removed." The key-less signing by another agent on this computer is covered
#      offline (test/test_install.py UninstallNoKey, UninstallNoKeyRealHooks).
# PASS only when every step passes in every folder. Exit 0 PASS, 1 FAIL, 2 can't run here (no tmux, claude, network).
#
# NOT covered here (said as NOT RUN at the end of every run): finishing Authenticate's sign-in, and what follows it
# (the agent made by /datastash:install, a retry that reuses it with no "-2", the install note accepted, and
# /datastash:uninstall cleaning up). Two reasons, both outside this script: (a) datastash's sign-in page offers Google
# sign-in only, and no selftest door finishes a connector (OAuth) sign-in for a throwaway account (the XLI-314 selftest
# approves device-login codes only, and a device login can't make the install's key: the key mint needs the
# connector's own sign-in); (b) the model turns of /datastash:install need a Claude login in the throwaway HOME, which
# this script never copies there (the session runs "Not logged in"; /plugin, /mcp and the plugin's hooks need none).
# The plugin side of those steps is covered offline by test/test_install.py (HomeFolderInstall, FreshHome).
#
# Options (environment):
#   E2E_TAG=v1.0.1   install that release instead: a local catalog, the same as the repo's own but pinned to that tag's
#                    commit (the plugin still comes from GitHub at that tag). Used to prove the check fails on 1.0.1.
#   E2E_FOLDERS      "home", "repo" or "home repo" (default).
#   E2E_KEEP=1       keep the throwaway HOMEs (moved to ~/trash either way; this only prints where).
# Nothing is printed from any credential; the throwaway HOMEs hold no login of any kind. Cleanup (pass or fail): the
# tmux sessions end, any process still running from a throwaway HOME is stopped, and the HOMEs go to ~/trash.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TAG="${E2E_TAG:-}"
FOLDERS="${E2E_FOLDERS:-home repo}"
MARKETPLACE="xlsx-for-ai-dev/datastash-claude-code"
CLAUDE_BIN="$(command -v claude || true)"
REAL_HOME="$HOME"
RUN="dse2e-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ds-e2e-install.XXXXXX")"
FAILS=0
T=""

say() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
pass() { say "PASS $*"; }
fail() { say "FAIL $*"; FAILS=$((FAILS + 1)); }

cleanup() {
  local s
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep "^$RUN-"); do tmux kill-session -t "$s" 2>/dev/null; done
  pkill -f "$WORK/" 2>/dev/null; sleep 1; pkill -9 -f "$WORK/" 2>/dev/null
  mkdir -p "$REAL_HOME/trash"
  mv "$WORK" "$REAL_HOME/trash/" 2>/dev/null && [[ "${E2E_KEEP:-0}" == 1 ]] && say "throwaway HOMEs kept in $REAL_HOME/trash/$(basename "$WORK")"
}
trap cleanup EXIT

command -v tmux >/dev/null || { echo "CAN'T RUN: tmux missing"; exit 2; }
[[ -n "$CLAUDE_BIN" ]] || { echo "CAN'T RUN: claude missing"; exit 2; }
curl -fsS -o /dev/null --max-time 20 https://getdatastash.com/.well-known/oauth-authorization-server \
  || { echo "CAN'T RUN: getdatastash.com does not answer"; exit 2; }

# The marketplace a person adds, or (E2E_TAG) the repo's own catalog pinned to that tag's commit, in a local folder.
SRC="$MARKETPLACE"
if [[ -n "$TAG" ]]; then
  SHA="$(git -C "$REPO" rev-parse --verify --quiet "$TAG^{commit}")" || { echo "CAN'T RUN: no tag $TAG in $REPO"; exit 2; }
  SRC="$WORK/catalog-$TAG"
  mkdir -p "$SRC/.claude-plugin"
  python3 -I - "$REPO/.claude-plugin/marketplace.json" "$SRC/.claude-plugin/marketplace.json" "$TAG" "$SHA" <<'PY' || exit 2
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat["plugins"]:
    if p["name"] == "datastash":
        p["source"].update(ref=sys.argv[3], sha=sys.argv[4])
json.dump(cat, open(sys.argv[2], "w"), indent=2)
PY
  say "catalog: $MARKETPLACE pinned to $TAG ($SHA)"
else
  say "catalog: $MARKETPLACE (as the README says)"
fi

pane() { tmux capture-pane -t "$T" -p -J -S -400 2>/dev/null; }
screen() { tmux capture-pane -t "$T" -p -J 2>/dev/null | grep -v '^[[:space:]]*$'; }
wait_for() {  # wait_for <ERE> [seconds]: the pane shows it
  local i
  for ((i = 0; i < ${2:-30}; i++)); do pane | grep -qE -- "$1" && return 0; sleep 1; done
  return 1
}
type_line() { tmux send-keys -t "$T" -l -- "$1"; sleep 1; tmux send-keys -t "$T" Enter; }
show_tail() { screen | tail -n "${1:-12}" | sed 's/^/    | /'; }

start_claude() {  # start_claude <HOME> <folder>: an interactive session, the folder trusted as a person does
  local h="$1" dir="$2"
  T="$RUN-$((++STARTS))"
  tmux new-session -d -s "$T" -x 200 -y 50 -c "$dir" \
    "env -i PATH='$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin' HOME='$h' USER=sam LOGNAME=sam TERM=xterm-256color LANG=C.UTF-8 '$CLAUDE_BIN'"
  if wait_for "trust this folder" 40; then
    tmux send-keys -t "$T" Down; sleep 0.5; tmux send-keys -t "$T" Enter
  fi
  wait_for "shift\+tab to cycle|for shortcuts|Not logged in" 40
}

stop_claude() {
  type_line "/exit"
  local i
  for ((i = 0; i < 15; i++)); do tmux has-session -t "$T" 2>/dev/null || return 0; sleep 1; done
  tmux kill-session -t "$T" 2>/dev/null
}

command_record() {  # command_record <HOME>: kind and name of the plugin's newest typed-command record (no ids)
  python3 -I - "$1/.config/datastash/state" <<'PY'
import glob, json, os, sys
files = sorted(glob.glob(os.path.join(sys.argv[1], "command-*.json")), key=os.path.getmtime)
rec = json.load(open(files[-1])) if files else {}
print(rec.get("kind") or "-", rec.get("name") or "-")
PY
}

run_folder() {  # run_folder home|repo
  local where="$1" h dir want
  h="$WORK/$where/sam"  # the HOME's own name is the computer user's ("sam", USER in the session; "person" is a
  # name datastash keeps, so a user named that gets "person-2")
  mkdir -p "$h"
  # A brand-new computer, past Claude Code's first-run theme screen (the person picks a theme), with no Claude login.
  printf '{"hasCompletedOnboarding": true, "theme": "dark"}\n' > "$h/.claude.json"
  if [[ "$where" == repo ]]; then
    dir="$h/code/web-app"; want="web-app"
    mkdir -p "$dir" && git -C "$dir" init -q && git -C "$dir" -c user.name=Test -c user.email=test@example.invalid \
      commit -q --allow-empty -m init
  else
    dir="$h"; want="sam"  # 1.0.16: the home folder's agent is the computer's user, never "agent"
  fi
  say "== $where folder (claude started in ${dir#"$WORK/"})"

  start_claude "$h" "$dir" || { fail "$where: claude did not start"; show_tail; return; }
  type_line "/plugin marketplace add $SRC"
  if wait_for "Added marketplace|Successfully added marketplace" 120; then pass "$where step 1: marketplace added"
  else fail "$where step 1: marketplace not added"; show_tail; return; fi

  type_line "/plugin install datastash@datastash"
  if ! wait_for "Install for you \(user scope\)" 60; then fail "$where step 2: no install choice shown"; show_tail; return; fi
  # The first choice is "Install for you (user scope)"; the cursor starts on it.
  screen | grep -qE "❯ +Install for you \(user scope\)" || { fail "$where step 2: \"Install for you\" is not the selected choice"; show_tail; return; }
  tmux send-keys -t "$T" Enter
  if wait_for "Installed datastash" 180; then pass "$where step 2: installed for you (user scope)"
  else fail "$where step 2: install did not finish"; show_tail; return; fi

  stop_claude
  start_claude "$h" "$dir" || { fail "$where step 3: claude did not start again"; show_tail; return; }
  pass "$where step 3: claude restarted ($(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(*(e.get("version") for e in d["plugins"]["datastash@datastash"]))' "$h/.claude/plugins/installed_plugins.json" 2>/dev/null || echo '?'))"

  type_line "/mcp"
  if ! wait_for "plugin:datastash:datastash" 30; then fail "$where step 4: /mcp does not list datastash"; show_tail; return; fi
  sleep 3  # let the first connection attempt finish (it runs the headersHelper)
  tmux send-keys -t "$T" Enter
  wait_for "Status:" 20
  # Judge only a settled status: while it says "connecting", look again (back out and open it again every 10 s).
  local menu i
  for ((i = 1; i <= 60; i++)); do
    sleep 1
    menu="$(screen)"
    grep -qE "Status: +.*connecting" <<<"$menu" || break
    if ((i % 10 == 0)); then tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Enter; fi
  done
  if grep -qE "^ *(❯ +)?Authenticate$" <<<"$menu" && grep -qE "Status: +.*(needs sign-in|needs authentication)" <<<"$menu"; then
    pass "$where step 4: /mcp datastash needs sign-in and offers Authenticate"
  else
    fail "$where step 4: /mcp datastash offers no Authenticate"
    grep -E "Status:|Issue:|OAuth|Error|Auth:" <<<"$menu" | sed 's/^/    | /'
    tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Escape; sleep 1
    check_install_hook "$h" "$where" "$want"
    stop_claude
    return
  fi
  # Authenticate: Claude Code registers and builds the sign-in link; it must open datastash's own sign-in page.
  screen | grep -qE "❯ +Authenticate$" || tmux send-keys -t "$T" Up
  tmux send-keys -t "$T" Enter
  local url=""
  if wait_for "https://getdatastash\.com/oauth/[^ ]*auth" 45; then
    url="$(pane | tr -d '\n' | grep -oE "https://getdatastash\.com/oauth/[A-Za-z0-9_./-]*auth\?[^ │]*" | tail -n 1)"
  fi
  if [[ -z "$url" ]]; then
    fail "$where step 4: Authenticate made no datastash sign-in link"; show_tail
  else
    local page code
    page="$(curl -sS -L --max-time 30 -c "$WORK/$where.jar" -b "$WORK/$where.jar" -o "$WORK/$where-signin.html" -w '%{http_code} %{url_effective}' "$url")"
    code="${page%% *}"
    if [[ "$code" == 200 && "${page#* }" == https://getdatastash.com/oauth-interaction/* ]] && grep -qi "google" "$WORK/$where-signin.html"; then
      pass "$where step 4: Authenticate's link opens datastash's sign-in page (sign-in itself: NOT RUN, see below)"
    else
      fail "$where step 4: Authenticate's link did not open datastash's sign-in page (HTTP $code)"
    fi
  fi
  tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Escape; sleep 1; tmux send-keys -t "$T" Escape; sleep 1
  check_install_hook "$h" "$where" "$want"
  stop_claude
}

check_install_hook() {  # step 5, the plugin's half: the typed /datastash:install reaches its hook and names the agent
  local h="$1" where="$2" want="$3" got
  type_line "/datastash:install"
  # 1.0.23 (card 1): a computer that is not signed in (the fresh home here never is) answers /datastash:install with one
  # line asking for the email, and records no install. That is the intended behavior and what this step now expects.
  local i ok=0
  for ((i = 0; i < 30; i++)); do
    if pane | grep -qF -- "datastash sync you@email.com"; then ok=1; break; fi
    sleep 1
  done
  if ((ok)); then
    pass "$where step 5: /datastash:install on a not-signed-in computer shows \"datastash sync you@email.com\""
  else
    fail "$where step 5: /datastash:install did not show \"datastash sync you@email.com\" (command record: $(command_record "$h" 2>/dev/null || true))"
  fi
  tmux send-keys -t "$T" Escape; sleep 1
  # Step 6 (1.0.16): uninstall from this key-less session (no agent key on this computer, the install never finished)
  # is answered by the plugin's own prompt hook, before any step runs: the person sees its one line, and no other.
  local none_line="Datastash isn't installed on this agent. Nothing was removed."
  type_line "/datastash:uninstall"
  wait_for "$none_line" 30
  if pane | grep -qF -- "$none_line" && ! pane | grep -q "Type /datastash:install again. Nothing changed"; then
    pass "$where step 6: /datastash:uninstall from a session with no agent key shows \"$none_line\""
  else
    fail "$where step 6: /datastash:uninstall from a key-less session did not show \"$none_line\""; show_tail
  fi
  tmux send-keys -t "$T" Escape; sleep 1
}

STARTS=0
for f in $FOLDERS; do
  case "$f" in home | repo) run_folder "$f" ;; *) echo "unknown folder kind: $f"; exit 2 ;; esac
done

say "NOT RUN (can't be automated here): finishing Authenticate's sign-in (Google only; no selftest door signs a"
say "  throwaway account in through the connector's OAuth), and so the agent /datastash:install makes, a retry that"
say "  reuses it (no \"-2\"), the install note accepted, and /datastash:uninstall cleaning up. Also the session runs"
say "  with no Claude login. Covered offline by test/test_install.py (HomeFolderInstall, FreshHome, UninstallNoKey,"
say "  UninstallNoKeyRealHooks: the key-less uninstall signed by an agent on this computer)."
if ((FAILS == 0)); then say "RESULT: PASS (${TAG:-$MARKETPLACE}; folders: $FOLDERS)"; exit 0; fi
say "RESULT: FAIL, $FAILS step(s) (${TAG:-$MARKETPLACE}; folders: $FOLDERS)"
exit 1
