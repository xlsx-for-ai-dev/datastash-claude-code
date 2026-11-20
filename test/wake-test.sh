#!/usr/bin/env bash
# wake-test.sh: proves that the plugin's background listener wakes an IDLE interactive Claude Code session when a
# message lands in the agent's inbox. Live, on getdatastash.com, test account only.
#
# Needs: tmux; claude; the test account's person key (PERSON_KEY, default ~/.config/x-hostproof/person.key, a key
# that never registered an agent); an agent key already fetched for AGENT (default "CC plugin test") at
# ~/.config/datastash/agent-cc-plugin-test.key (from the first-run test).
# Steps: makes a throwaway agent workbook as the person; links AGENT to a fresh folder with it; starts an interactive
# session there in tmux; lets it sit idle for IDLE seconds (default 90); sends one message to AGENT's inbox as the
# person; PASS when the listener logs a wake and the session replies to that message within 120 s.
# BURST=N (N > 1): sends N messages within seconds instead; PASS when every one gets a reply within 240 s and the
# listener logged 1 or 2 wakes for them (wake pacing), never one per message.
# The session gets a narrow test-only allow (datastash api, datastash status) on a throwaway workbook; do not copy it
# as a blanket allow (see README, "Untrusted text and permissions").
# Cleanup (pass or fail): ends the tmux session, restores AGENT's config, deletes the throwaway workbook.
# Never prints a key.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$HERE/../plugins/datastash"
PERSON_KEY="${PERSON_KEY:-$HOME/.config/x-hostproof/person.key}"
AGENT="${AGENT:-CC plugin test}"
IDLE="${IDLE:-90}"
BURST="${BURST:-1}"
SLUG="$(printf '%s' "$AGENT" | tr 'A-Z' 'a-z' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')"
CONF="$HOME/.config/datastash/agent-$SLUG.json"
LOG="$HOME/.config/datastash/state/agent-$SLUG.log"
TMUX_S="dswake-test-$$"
call() { python3 "$HERE/mcp_call.py" "$PERSON_KEY" "$@"; }
field() { python3 -I "$HERE/jfield.py" get "$1"; }

[[ -r "$PERSON_KEY" ]] || { echo "FAIL: person key not readable"; exit 2; }
[[ -r "$HOME/.config/datastash/agent-$SLUG.key" ]] || { echo "FAIL: no agent key for $AGENT (run the first-run setup)"; exit 2; }
command -v tmux >/dev/null || { echo "FAIL: tmux missing"; exit 2; }

WB_NAME="cc-plugin-wake-test $(date +%Y%m%d%H%M%S)"
WID="$(call ledger_agent_workbook "{\"action\":\"create\",\"name\":\"$WB_NAME\"}" | field answer.workbook_id)" || { echo "FAIL: could not make the throwaway workbook"; exit 1; }
PROJ="$(mktemp -d)"
SAVED="$(mktemp)"; cp "$CONF" "$SAVED" 2>/dev/null || : > "$SAVED"
cleanup() {
  tmux kill-session -t "$TMUX_S" 2>/dev/null
  if [[ -s "$SAVED" ]]; then cp "$SAVED" "$CONF"; chmod 600 "$CONF"; fi
  call ledger_workbook_delete "{\"workbook_id\":\"$WID\",\"confirm\":\"$WB_NAME\"}" >/dev/null 2>&1 || echo "note: throwaway workbook $WID not deleted"
}
trap cleanup EXIT

( cd "$PROJ" && python3 "$PLUGIN/scripts/datastash" link "$AGENT" --workbook "$WID" >/dev/null ) || { echo "FAIL: link"; exit 1; }
# The listener's first catch-up in a fresh workbook state must not count old mail: start clean.
STATE="$HOME/.config/datastash/state/agent-$SLUG.json"
LOG_LINES_BEFORE=$(wc -l < "$LOG" 2>/dev/null || echo 0)

tmux new-session -d -s "$TMUX_S" -x 200 -y 50 -c "$PROJ" \
  "claude --setting-sources project,local --plugin-dir '$PLUGIN' --allowedTools 'Bash(datastash api:*)' 'Bash(datastash api *)' 'Bash(datastash status)' Skill"
# Trust the new folder (first prompt), then leave the session idle.
for _ in $(seq 1 30); do tmux capture-pane -t "$TMUX_S" -p | grep -q "trust this folder" && break; sleep 1; done
tmux send-keys -t "$TMUX_S" Down; sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
for _ in $(seq 1 30); do tail -n +$((LOG_LINES_BEFORE + 1)) "$LOG" 2>/dev/null | grep -q "listen: start" && break; sleep 1; done
tail -n +$((LOG_LINES_BEFORE + 1)) "$LOG" 2>/dev/null | grep -q "listen: start" || { echo "FAIL: the listener did not start at session start"; exit 1; }
# The test plays the person at the keyboard and gives the session its standing job once, up front. Message text alone
# is untrusted data (the plugin frames it so), so the session acts on the person's request only because the person
# set that job here.
sleep 5
tmux send-keys -t "$TMUX_S" -l "You are the datastash agent \"$AGENT\" in this folder, for a plugin test. Standing job: when a message from the person arrives in your inbox asking for a short reply word, send that reply to the message and mark it handled. Nothing else to do now; wait."
sleep 0.5; tmux send-keys -t "$TMUX_S" Enter
echo "session idle; waiting ${IDLE}s before sending"
sleep "$IDLE"

WAKES_BEFORE=$(tail -n +$((LOG_LINES_BEFORE + 1)) "$LOG" | grep -c "listen: wake")
MIDS=()
for i in $(seq 1 "$BURST"); do
  if [[ "$BURST" -gt 1 ]]; then TEXT="Burst test $i of $BURST: reply BURST-OK to this message, then mark it handled."
  else TEXT="Wake test: reply WAKE-OK to this message, then mark it handled."; fi
  M="$(call ledger_inbox_send "{\"workbook_id\":\"$WID\",\"to\":\"$AGENT\",\"subject\":\"wake test $i\",\"message\":\"$TEXT\"}" | field answer.message_id)" || { echo "FAIL: send $i"; exit 1; }
  MIDS+=("$M")
done
MID="${MIDS[*]}"
echo "sent message(s) $MID at $(date +%T)"
LIMIT=$(( BURST > 1 ? 240 : 120 ))
for _ in $(seq 1 "$LIMIT"); do
  REPLIED="$(call ledger_inbox_read "{\"workbook_id\":\"$WID\"}" | python3 -I "$HERE/jfield.py" replies answer.messages "${MIDS[@]}")"
  NREP="$(printf '%s' "$REPLIED" | python3 -I -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
  if [[ "$NREP" -eq "$BURST" ]]; then
    WOKE=$(( $(tail -n +$((LOG_LINES_BEFORE + 1)) "$LOG" | grep -c "listen: wake") - WAKES_BEFORE ))
    if [[ "$BURST" -gt 1 ]]; then
      if [[ "$WOKE" -ge 1 && "$WOKE" -le 2 ]]; then
        echo "PASS: burst of $BURST: the session woke $WOKE time(s) and replied to all $BURST messages ($MID)"; exit 0
      fi
      echo "FAIL: burst of $BURST: all replied, but the session woke $WOKE times (want 1 or 2)"; exit 1
    fi
    echo "PASS: idle session woke (listener wakes logged: $WOKE) and replied to message $MID (replied to $REPLIED)"
    exit 0
  fi
  sleep 1
done
echo "FAIL: replies to $NREP of $BURST message(s) ($MID) within $LIMIT s. Listener log tail:"; tail -n 5 "$LOG"
echo "Pane:"; tmux capture-pane -t "$TMUX_S" -p | grep -v '^$' | tail -15
exit 1
