#!/usr/bin/env bash
# person-wake-test.sh: proves "datastash main": an IDLE interactive Claude Code session set up as the person's own session
# (datastash watch --person --setup, which links the "datastash main" folder) wakes when an agent's message lands in the agent workbook. Live, on
# getdatastash.com, test account only.
#
# Needs: tmux; claude; the test account's person key (PERSON_KEY, default ~/.config/x-hostproof/person.key, a key that
# never registered an agent); an agent key of that account (AGENT_KEY, default
# ~/.config/datastash/agent-cc-plugin-test.key) to send as the agent.
# Steps: the person makes a throwaway agent workbook and asks for a watch pickup code; the plugin trades it for the watch
# key in a throwaway config folder and links a fresh folder; an interactive session starts there in tmux and sits idle
# IDLE seconds (default 60); the agent sends the person an ask over the REST door. PASS when the person listener logs a
# wake and the session answers the plugin's note (it was told to say PERSON-WOKE) within 120 s, and the note carried
# no message text.
# ANYWHERE=1 MAIN_KEY_FILE=<file> (owner 11-19: "datastash main" in any clean session): the folder is linked to nobody
# (PM 11-19: only a session that never acted as an agent becomes main), nothing is linked as the person's. MAIN_KEY_FILE is a main key of the test account (from ledger_person_watch action "main")
# (copied into the throwaway config folder, never printed). In the session the person types "datastash main" (the
# prompt hook records it for that session), then "datastash main --on" runs inside that very session (Claude Code's "!"
# shell). PASS needs the same wake as the person, and a second plain process using that session's id is not the person.
# Without MAIN_KEY_FILE the ANYWHERE run says SKIP.
# The cleanup's "stop" stops the account's watch and main keys, so each ANYWHERE run needs a fresh main key.
# Cleanup (pass or fail): ends the tmux session, stops the account's watch keys, deletes the workbook and the
# throwaway config folder (moved to ~/trash). Never prints a key.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$HERE/../plugins/datastash"
PERSON_KEY="${PERSON_KEY:-$HOME/.config/x-hostproof/person.key}"
AGENT_KEY="${AGENT_KEY:-$HOME/.config/datastash/agent-cc-plugin-test.key}"
IDLE="${IDLE:-60}"
ANYWHERE="${ANYWHERE:-0}"
MAIN_KEY_FILE="${MAIN_KEY_FILE:-}"
if [[ "$ANYWHERE" == 1 && ! -r "$MAIN_KEY_FILE" ]]; then echo "SKIP: ANYWHERE needs MAIN_KEY_FILE (a main key of the test account)"; exit 3; fi
TMUX_S="dsperson-test-$$"
call() { python3 "$HERE/mcp_call.py" "$PERSON_KEY" "$@"; }
field() { python3 -I "$HERE/jfield.py" get "$1"; }

[[ -r "$PERSON_KEY" && -r "$AGENT_KEY" ]] || { echo "FAIL: person or agent key not readable"; exit 2; }
command -v tmux >/dev/null || { echo "FAIL: tmux missing"; exit 2; }

WB_NAME="cc-plugin-person-test $(date +%Y%m%d%H%M%S)"
WID="$(call ledger_agent_workbook "{\"action\":\"create\",\"name\":\"$WB_NAME\"}" | field answer.workbook_id)" || { echo "FAIL: could not make the throwaway workbook"; exit 1; }
PROJ="$(mktemp -d)"
export DATASTASH_CONFIG_DIR="$(mktemp -d)"
# The "datastash main" folder the plugin makes and links: a throwaway one here (never the real one in the home folder).
export DATASTASH_MAIN_DIR="$PROJ"
# ANYWHERE: no "datastash main" folder at all (a path that never exists); the session's folder is linked to nobody.
[[ "$ANYWHERE" == 1 ]] && export DATASTASH_MAIN_DIR="$DATASTASH_CONFIG_DIR/no-main-folder"
# Each person session has its own listener log (agent-@person-<session id>.log).
plog() { cat "$DATASTASH_CONFIG_DIR"/state/agent-@person*.log 2>/dev/null; }
cleanup() {
  tmux kill-session -t "$TMUX_S" 2>/dev/null
  call ledger_person_watch "{\"workbook_id\":\"$WID\",\"action\":\"stop\"}" >/dev/null 2>&1
  call ledger_workbook_delete "{\"workbook_id\":\"$WID\",\"confirm\":\"$WB_NAME\"}" >/dev/null 2>&1 || echo "note: throwaway workbook $WID not deleted"
  mkdir -p "$HOME/trash"; mv "$DATASTASH_CONFIG_DIR" "$HOME/trash/" 2>/dev/null; mv "$PROJ" "$HOME/trash/" 2>/dev/null
}
trap cleanup EXIT

CODE="$(call ledger_person_watch "{\"workbook_id\":\"$WID\",\"action\":\"key\"}" | field answer.pickup_code)" || { echo "FAIL: no watch pickup code"; exit 1; }
if [[ "$ANYWHERE" == 1 ]]; then
  # A clean folder (no link); the person's main key is copied in (never printed).
  ( umask 077; cp "$MAIN_KEY_FILE" "$DATASTASH_CONFIG_DIR/main.key" )
  # The throwaway workbook is the one this main watches (the account may hold others).
  ( umask 077; printf '{"workbook_id": "%s", "projects": []}\n' "$WID" > "$DATASTASH_CONFIG_DIR/person.json" )
else
  printf '%s\n' "$CODE" | python3 "$PLUGIN/bin/datastash" watch --person --setup "$WID" - --project "$PROJ" >/dev/null || { echo "FAIL: setup"; exit 1; }
  python3 "$PLUGIN/bin/datastash" main | grep -q "person watch set up on this machine: yes" || { echo "FAIL: datastash main does not see the setup"; exit 1; }
fi
CODE=""

tmux new-session -d -s "$TMUX_S" -x 200 -y 50 -c "$PROJ" -e "DATASTASH_CONFIG_DIR=$DATASTASH_CONFIG_DIR" -e "DATASTASH_MAIN_DIR=$DATASTASH_MAIN_DIR" \
  "claude --setting-sources project,local --plugin-dir '$PLUGIN'"
for _ in $(seq 1 30); do tmux capture-pane -t "$TMUX_S" -p | grep -q "trust this folder" && break; sleep 1; done
tmux send-keys -t "$TMUX_S" Down; sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
if [[ "$ANYWHERE" == 1 ]]; then
  sleep 8
  tmux send-keys -t "$TMUX_S" -l "datastash main"; sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
  for _ in $(seq 1 20); do ls "$DATASTASH_CONFIG_DIR"/state/main-request-* >/dev/null 2>&1 && break; sleep 1; done
  ls "$DATASTASH_CONFIG_DIR"/state/main-request-* >/dev/null 2>&1 || { echo "FAIL: the prompt hook did not record the person's request"; exit 1; }
  # Let the session's AI finish its reply to the phrase (it can't reach a connector here), then interrupt anything left.
  sleep 40; tmux send-keys -t "$TMUX_S" Escape; sleep 2
  tmux send-keys -t "$TMUX_S" -l "!python3 '$PLUGIN/bin/datastash' main --on"; sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
  for _ in $(seq 1 30); do ls "$DATASTASH_CONFIG_DIR"/state/main-session-* >/dev/null 2>&1 && break; sleep 1; done
  ls "$DATASTASH_CONFIG_DIR"/state/main-session-* >/dev/null 2>&1 || { echo "FAIL: the session did not become the person's main"; tmux capture-pane -t "$TMUX_S" -p | grep -v '^$' | tail -8; exit 1; }
  SID="$(basename "$(ls "$DATASTASH_CONFIG_DIR"/state/main-session-* | head -1)" .json)"; SID="${SID#main-session-}"
  # The same session id from a plain process (not that session's Claude Code) is not the person.
  ( cd "$PROJ" && CLAUDE_CODE_SESSION_ID="$SID" python3 "$PLUGIN/bin/datastash" main | grep -q "person's datastash main: no" ) || { echo "FAIL: a copied session id was taken as main"; exit 1; }
  # The person listener starts when that turn ends (Stop hook).
  tmux send-keys -t "$TMUX_S" -l "Say OK."; sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
fi
for _ in $(seq 1 60); do plog | grep -q "listen: start" && break; sleep 1; done
plog | grep -q "listen: start" || { echo "FAIL: the person listener did not start"; exit 1; }
sleep 5
tmux send-keys -t "$TMUX_S" -l "This is the person's datastash main session, for a plugin test. When a datastash note about new items arrives, answer in this chat with the word PERSON-WOKE and the number of new items, nothing else. Wait until then."
sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
echo "session idle; waiting ${IDLE}s before the agent writes"
sleep "$IDLE"

SECRET="person-wake-secret-$RANDOM$RANDOM"
python3 -I - "$AGENT_KEY" "$WID" "$SECRET" <<'PY' || { echo "FAIL: the agent could not send"; exit 1; }
import json, sys, urllib.request
k = open(sys.argv[1]).read().strip()
body = json.dumps({"workbook_id": sys.argv[2], "to": "person", "kind": "ask", "subject": "may I go on", "message": sys.argv[3]}).encode()
req = urllib.request.Request("https://getdatastash.com/api/v1/agent/inbox", data=body, method="POST",
                             headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    assert r.status == 200
PY
echo "agent sent an ask at $(date +%T)"
for _ in $(seq 1 120); do
  if plog | grep -q "listen: wake" && tmux capture-pane -t "$TMUX_S" -p -S -200 | grep -q "PERSON-WOKE"; then
    if tmux capture-pane -t "$TMUX_S" -p -S -400 | grep -q "$SECRET"; then echo "FAIL: message text reached the session"; exit 1; fi
    echo "PASS: the idle person session woke ($(plog | grep -c 'listen: wake') wake) on the agent's ask; no message text in the session"
    exit 0
  fi
  sleep 1
done
echo "FAIL: no wake within 120 s. Listener log tail:"; plog | tail -n 5
echo "Pane:"; tmux capture-pane -t "$TMUX_S" -p | grep -v '^$' | tail -15
exit 1
