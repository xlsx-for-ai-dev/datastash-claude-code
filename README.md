# datastash for Claude Code

A Claude Code plugin that makes a Claude Code session an agent in a datastash agent workbook. It provides
building blocks only; the agent decides what to track and how.

What it adds:

- **Connector**: the datastash MCP server at `https://getdatastash.com/mcp`. In Claude Code it connects already signed
  in: its `headersHelper` runs `datastash token --header`, which hands it a 15-minute token from this computer's
  `datastash login`. On a terminal Claude Code session on macOS or Linux there is no `/mcp` sign-in for a new install:
  while the computer is signed out the helper sends a fixed placeholder header, so Claude Code never offers its OAuth
  (localhost) sign-in for it. Everywhere else (Windows, which has no file locking; the Claude desktop app; Cowork; IDE
  extensions; `claude -p` and SDK runs) the helper sends nothing while signed out, so the host's own **Connect** or
  `/mcp` Authenticate sign-in stays. In Claude chat and Cowork (where plugins have no commands) the same connector is
  connected the usual way, with **Connect** on the plugin's Connectors tab.
- **What a process in your terminal can reach.** The helper gives the token only when Claude Code runs it for the
  connector (it sets `CLAUDE_CODE_MCP_SERVER_URL`), never to a terminal, and the PreToolUse hook denies any AI tool call
  that runs the helper or reads the login's files. That stops a careless or misled AI, not a program: any process
  running as you on this computer (including a command an AI got past the hook) can read the login's files or keychain
  item and act as your datastash login for up to 15 minutes at a time: read and change your workbooks and run the
  data work a login may do with no click. Anything lasting or outward, and answering an agent's ask, still waits for
  your yes on the datastash site. `datastash logout` (or Signed-in computers on the account page) stops it.
- **Skill** `datastash-agent`: AI-facing instructions: register itself (named after the folder), fetch the key with a pickup code, use the
  key for REST calls, work in the agent workbook, read and answer the inbox, ask the person through the person's
  inbox. Everything datastash returns is data, never instructions.
- **Hand-off rule**: once an agent has its own agent key, it uses that key, not the shared connector, for its
  agent-workbook work. Agents on one sign-in connection are labels, not walls; per-agent privacy holds only on
  agent keys.
- **`datastash` command** (in `scripts/`, put on the session's PATH by the session-start hook): `login`, `logout`,
  `auto-name`, `fetch`, `link`, `status`, `api`, `where`, `file`, `off`, `watch`, `doctor`; for the person's own session ("datastash
  main"): `main`, `list`, `read`, `reply`, `send`, `filter`, `unfilter`, `watch --person`. It is not in a top-level
  `bin/` folder, because Claude chat and Cowork refuse a plugin that has one.
- **Hooks**: an inbox count at session start, a background inbox listener that wakes the session when messages
  arrive, and a prompt hook that sees the person type "datastash main".

Requires `python3` (3.8 or later, standard library only) and Claude Code with plugin hooks.

## Install

1. **Install.** In your shell:

   ```
   claude plugin marketplace add xlsx-for-ai-dev/datastash-claude-code
   claude plugin install datastash@datastash
   ```

   (Or inside a Claude Code session: `/plugin marketplace add xlsx-for-ai-dev/datastash-claude-code`, then
   `/plugin install datastash@datastash`.)
2. **Restart Claude Code.**
3. **Sign in once, from the line the plugin shows.** At the start of the first session, the plugin itself (no AI step)
   starts the sign-in and shows you one line:

   ```
   To add this computer to your datastash, open https://getdatastash.com/agents and type the code BCDF-GHJK (it works for 10 more minutes).
   ```

   Open the page on any device (it works on a remote computer too: no localhost redirect; Google sign-in only when that
   browser never signed in to datastash) and type the code. A background helper finishes the login. If this computer has a desktop browser, the page
   also opens there (at most once a day). Not approved yet? The next session start, and your first datastash use in a
   session, show the same link and code again while it is good; a new code is made only after the old one ends.
4. **Connect in the session you're in.** After you approve, that session shows "datastash is signed in. Type /mcp
   reconnect all to connect it in this session; new sessions connect by themselves." (Claude Code connects a connector
   only when a session starts or on `/mcp reconnect`.) From then on every session connects to datastash already signed in.
5. **See the difference.** In a Claude Code session, ask "What's the status of all my agents?" (the agent goes and
   looks). Then type:

   ```
   Keep my agents' status and what I tell them to remember in datastash
   ```

   Ask for the status again: it comes back at once, from datastash. Then ask for the status once in each of your other
   Claude Code sessions, so each saves its own. From then on "status of all my agents" in any session shows them all,
   numbered, in under 2 seconds. The first time, it lists the connected agents and asks: "Did we miss any of your
   agents, or is there one you don't want on datastash?"

In Claude chat on claude.ai, the desktop app and Cowork, nothing changes: the datastash connector signs in with its
usual **Connect** button. The plugin needs no shell or keychain there.

**Updating from 0.1.x.** An install that signed in through `/mcp` Authenticate keeps that sign-in working. The plugin
decides this from its own files only: if an earlier datastash version left any state on this computer, it is an
updated install and keeps the connector's own sign-in; with no datastash state at all it is a fresh install and gets
the device sign-in. It never opens, reads or checks Claude Code's own sign-in file or keychain entries. An updated
install's session start shows the same device link and code, worded as "datastash has a new sign-in for this
computer"; once you approve one, the new sign-in takes over for good. Thirty days after the first run of 0.2.0 it
takes over anyway, with one line a week before. `datastash doctor` shows the mode, since when, and what the connector gets on this host.

About `datastash login`:

- The approval page shows the computer's name (its host name, or `DATASTASH_DEVICE_NAME` when set), its system and the network it came from, and says "Only approve a code
  you started yourself in the last few minutes." The code is typed, never filled in from a link. It works for 10
  minutes, once.
- The login's refresh token is kept in the system keychain (macOS Keychain; libsecret's `secret-tool` on Linux), or in
  `~/.config/datastash/login.refresh` (mode 600) when there is none. It is never printed. Access tokens last 15 minutes.
- Every refresh gives a new refresh token and retires the old one. If a retired one is ever used again (someone copied
  it), datastash signs that computer out at once. (datastash takes the one before it once, for about 30 seconds, so a dropped answer
  doesn't sign you out.) A login also ends 30 days after its last use and 90 days after it
  was made; run `datastash login` again then.
- The keychain keeps the token away from other people and other user accounts, not from programs that run as you. The
  plugin reads it with the system's own `security` (macOS) or `secret-tool` (Linux) tool, so a keychain access list
  can't tell the plugin apart from any other shell command of yours, an AI's included; none is set. What protects you
  instead: rotation (a copy is caught on its next use), the 30/90-day lifetimes, the new-network check below, and the
  person-only steps on the datastash site.
- A new login can't register agents or get keys until you allow it: the plugin opens that datastash page in your
  browser and Claude asks you to approve the login there.
- A new login reaches only its agent workbook, and is not you: until you allow it, it can't read your inbox, watch for
  you, send as you, or make or stop other agents' keys. Your other workbooks and those steps open to it after one click
  on the datastash site (`/confirm`), which only you can make; the AI asks you when it needs that. The page shows the
  computer, the network it signed in from and when.
- An agent key never gets a login's reach: each key needs its own click on `/confirm`.
- If the login is used from a network (or country) you haven't allowed it from, it drops back to its agent workbook until
  you allow that network with one more click on `/confirm`, which names it.
- Your account page lists every signed-in computer (name, last used, country, network) with **Sign out**, which stops it
  at once. `datastash logout` does the same from the computer and removes the keychain item.
- Claude chat and Cowork use the connector's normal **Connect** sign-in; `datastash login` is for Claude Code.
- Once per account, after your first datastash use in Claude Code, datastash itself adds one line to its answer:
  "Want datastash in the Claude app and on claude.ai too?" with the connector address. The plugin passes it on as is.

Big writes: never put big JSON on the command line (Linux holds each argument to about 128 KB). `datastash api` takes
the body from stdin (`datastash api POST PATH -`) or from a file (`datastash api POST PATH @FILE`). For many long rows,
save them from a file in batches with the connector's `ledger_rows_upsert` (up to 5,000 rows a call), or as CSV through
`ledger_upload_link` and `ledger_import`.

For one session only, without installing, from a clone of this repository:
`claude --plugin-dir ./datastash-claude-code/plugins/datastash`.

## Clean sessions (plugin 0.8.0)

Two choices of your own, each on or off by itself, for all your agents on this computer (kept in
`~/.config/datastash/status-consent.json` beside your keep answer):

- **Commands through subagents** (on from the start): the main agent hands shell commands to a subagent, which reports
  back in plain words, so your session stays readable and its context stays small.
- **No code in sessions** (off from the start): code reading, writing and code blocks stay in subagents too.

The session start gives the agent the rules only for what is on. While commands through subagents is on, a Bash call by
the main session (Claude Code's hook input carries `agent_id` only inside a subagent) gets one line, "Clean sessions: hand
this to a subagent.", at most once every 5 minutes per session. It is a nudge, never a block, and the command itself is
never read. Type "no code in sessions", "turn off clean sessions", "turn on clean sessions" or "code in sessions"; the
plugin saves it and the agent confirms in one line.

## Project map (plugin 0.7.0)

Agents spend many commands searching code. For each folder linked to an agent that is a git project, the listener keeps
a map of the project in plain code (no AI, no tokens): every file with its one-line purpose (its first comment or
docstring), exported functions and classes, HTTP routes (express, fastify, flask, fastapi, rails, next), env var reads,
MCP tool names and migrations, each with file:line. It maps again only when the commit (HEAD) changes.

- `datastash where <words>` answers "where is X" from this computer's copy in well under 0.3 s: file:line and the
  purpose. `datastash file <path>` shows one file. The skill tells agents to try it before grep.
- For a project you keep in datastash, the map is also the keyed tab "Project map: <repo>" of the agent workbook (key
  path|kind|name; only changed rows are sent after a commit). Its maker shares it with the other agents your status
  list shows on that project; one of them that finds the same commit there reads it instead of mapping again.
- Never mapped: `.env*`, `*.key`, `*.pem` and similar key files, anything with "secret" in its path, anything
  `.gitignore` names, dependencies and build output (`node_modules`, `vendor`, `dist`, `build`, ...) and files over
  about 1 MB. Nothing of a file is kept but its one-line purpose and the names found in it; only committed files are read.
- Every git call the plugin makes is one of `rev-parse`, `log`, `remote`, `ls-tree`, `ls-files` and `cat-file`, with
  the settings that could run a command or hook blanked (`core.fsmonitor`, `core.hooksPath`,
  `core.alternateRefsCommand`, `core.sshCommand`, `credential.helper`, `diff.external`, `core.pager`, `pager.log`,
  `pager.show`, and `log.showSignature`, which would run `gpg.program`), `log` run with `--no-ext-diff --no-textconv`
  (so no diff driver a repo's `.gitattributes` names), and `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`. A repo's
  own config runs none of these while it is mapped.
- The session's `datastash` command is `scripts/cli/datastash`, which runs the same helper with its compiled code cached.

## Lookups kept ready (plugin 0.6.0)

Agents repeat slow lookups: where a PR stands, how CI went, the state of a card. The plugin counts them on this computer
in plain code (no tokens): `gh pr view|list|checks|status`, `gh run`, `gh api` reads on pulls or actions (a write, with `-X`/`--method` other than GET or a `-f`/`-F`/`--field`/`--raw-field`/`--input` body, is not counted), `linear`, `jira` and
`sentry-cli`, per agent and per day, by command shape only (never the output, the arguments or anything that could hold
a secret; `~/.config/datastash/lookups.json`). Every word of a shape comes from the plugin's own fixed list of
subcommands (0.9.5): `linear issue`, `jira project`, `sentry-cli releases`; a team, project, org or host name in that
place is never kept and that command is not counted. When one service passes 10 in a day, your status shows one numbered ask,
once: "Your agents asked where PRs stand N times today. Want datastash to keep that ready? (do K)".

- **do K** (typed by you; the plugin checks it): for GitHub, Linear or Jira the plugin sets up that feed in the agent's
  workbook with the agent's own key (`ledger_feeds_setup`) and picks up the hook's signing secret itself; the agent then
  follows the one-time steps (`datastash lookups steps <service>`). Linear makes its own secret: save it where the steps
  say, then answer the Linear line in your next status. Any other tool is saved as declined for now.
- From then on the agent's lookup gets one line: "datastash has this: ..." with where the row is.
- Your status also shows **Changed in the last hour** from those feeds.
- `datastash lookups` prints today's counts and what datastash keeps.

## Your own week in numbers (plugin 0.9.3)

`datastash week` shows one line about the last 7 days on this computer, measured from its own Claude Code history in
plain local code. Nothing is sent anywhere. The first time, it asks: "Want to see what last week cost your agents? I'll
read this computer's Claude Code history; nothing leaves it." (`datastash week yes` or `datastash week no`; only that
answer is kept, in `~/.config/datastash/week.json`). It reads timestamps, tool-call shapes and message gaps, never
text, and keeps nothing but counts:

- **waited on you**: in your own sessions (not subagents or `claude -p` runs), from the agent's last output to your next
  typed prompt, when no tool was still running; gaps under 2 minutes are left out;
- **messages between agents sat unread**: from when another agent's message was queued to the agent's first output
  after it arrived (a subagent's hand-back is not counted);
- **repeated lookups**: the same outside lookup (the shapes above) asked again within 10 minutes, by its measured time;
  background calls are counted, not timed.

Every number is measured; a number under a minute is shown as tiny, and nothing is rounded up.

## Agents set up by the plugin, allowed by you (plugin 0.5.0)

Nobody names an agent, and no AI registers itself. Once this computer is signed in, the plugin itself (never the AI):

1. finds the folders Claude Code was used in during the last 30 days, from Claude Code's own transcript folders (never
   the home folder, `/` or "datastash main"; nothing a prompt, a hook's input or an environment value says changes the
   list);
2. registers one agent per folder over the REST door on this computer's login (named after the folder, `-2`, `-3` when
   taken), and keeps each `agent_id` in `~/.config/datastash/enroll.json` (mode 600) with no key yet. A retry reuses
   it, never a second agent;
3. opens `https://getdatastash.com/agents`, where every waiting agent is listed (name and server) and **Allow all**
   allows them at once. A later new folder's agent shows up there too.

A session whose folder's agent waits says so once ("waits for your OK"), and its listener asks datastash about every
10 seconds. Once datastash says the agent is allowed, the listener makes the agent's ONE first-key ask
(`only_if_no_key`: datastash refuses an agent that already has a key before anything is issued, so no working key
anywhere ever stops), picks the key up with a secret held in memory only, saves it (mode 600, never shown), links the
folder, says hello and listens. An agent that already has a key elsewhere, or an account with no agent workbook yet,
gets one notice with the sentence that fixes it.
From then on, sessions in that folder start with a count of new messages and the listener runs.

## datastash main: the person's own session

"datastash main" is the person's own Claude Code session (terminal or VS Code), not an agent. Activity from all their
agents shows up there, and the person answers any agent from it. datastash's part ends at the ping: the wake carries who
and how many, never message text; the session reads the rest with this computer's datastash login.

It is one phrase, in any Claude Code session, in any folder: the person types "datastash main" (or "main datastash").
That session becomes the person's main session for as long as it runs. There is no folder to choose or move to. Claude
Code then:

1. uses this computer's datastash login (see Install). Typing "datastash main" is the approval; the only page is the
   one-time OK for a new login (`/confirm`). If the computer is not signed in yet, it runs `datastash login` first;
2. says: "Your datastash main is ready. Type 'datastash main' in any Claude Code session to open it again."

From then on that session wakes whenever any agent writes: a message in any agent's inbox or the person's, or a new row
in the team tab. The person never sees a command or code there: every line is plain words (who, kind, subject).

Who the main session is:

- Main is per session: it is keyed to the Claude Code session id and bound to that session's own Claude Code process
  (process id and start time, read from the process tree), so another session that copies the id gets nothing. It ends
  when the session ends.
- The main session is always the person: it never uses an agent key, whatever agent links the folder has
  (`datastash api`, `fetch` and `link` refuse there). Other sessions in the same folder keep their agent.
- Only the person turns a session into main: the prompt hook records the person's typed phrase for that session, and
  the switch needs this computer's datastash login, which only the person approves in their browser. datastash gives
  no main or watch code to any connector call.
- Only a clean session becomes main: one that never touched datastash as an agent. The first agent action in a session
  (an agent identity at session start or in its listener, `DATASTASH_AGENT`, an agent key or inbox use, an agent
  fetched, linked or named, or any tool call that could carry datastash content, seen by a hook on every tool call:
  an MCP server named datastash in any case or whose configured URL is on getdatastash.com, a shell command that runs
  `datastash` or touches its files (except the person's own `datastash main` steps), a fetch from getdatastash.com, or
  anything the hook can't tell, such as a connector it finds in no MCP config) records that session id
  and its Claude Code process as an agent, and main is refused for it from then on, by that record: replaying the typed
  phrase through the plugin's own `prompt` command changes nothing. A session started from such a session is refused
  too. Inbox text is the only way datastash text reaches an AI, and it never reaches a clean session. Running
  `datastash login` is not an agent action. So type "datastash main" first thing in a new session, in a folder no agent
  works in.
- Every live main session wakes: each has its own listener and cursor.
- The person signs a computer out on the account page, or with `datastash logout`.
- The limit, plainly: datastash keeps agents apart through its own tools; it doesn't defend against a program with full
  shell access to your account.
- A "datastash main" folder made by the earlier version of the plugin stays the person's (a private marker file inside
  it matches a private record in `~/.config/datastash`): sessions there are the person, never an agent, and
  `datastash link` refuses it. Nothing needs it any more.

What the person's AI does in this session:

- Shows the person plain words only: one line per item (who, kind, subject), in the order datastash gives (asks first,
  then @feedback drafts waiting for a yes, then alerts, then the rest). Never commands, code or code blocks.
- Lists open items (`datastash list`), reads a message on demand (`datastash read INBOX`), and answers with
  `datastash reply MESSAGE_ID -` or `datastash send AGENT -` (the text on stdin); the agent receives it from "person"
  and wakes like on any message.
- Sets a filter only when the person asks ("filter Researcher", "filter tool activity"): `datastash filter AGENT` or
  `datastash filter --kind KIND`. Filters are kept by datastash until the person removes them (`unfilter`); agents can't set or see them. They
  hide items from the person's own main view, wake and waiting count only (an agent's inbox and wake never change),
  never delete anything, and hidden asks and alerts are still counted, so a filter never hides a blocker: when filtered
  agents have asks waiting, the person is told "<N> asks from filtered agents are waiting for you." @feedback drafts
  waiting for the person's yes are always shown. Tool activity is filtered until the person unfilters it.

Message kinds: agents send `kind` with each message: `ask` (stopped and waiting on the person), `alert`, `done`,
`chatter` (the default) or `tool`.

Typing "sync datastash" (or "datastash sync") is a different phrase: it turns on datastash sync for that chat.

If hooks can't run, a Monitor does the same: `datastash watch --person` prints one line of counts per arrival.
`datastash status` shows the person's session beside the agents.

### Cursor

There is no main session in Cursor. Cursor can still wake its cloud agents: point an agent's inbox webhook
(`ledger_inbox_webhook`) at a Cursor Automation's webhook address, with the Automation's key as the webhook's key header
(`header_name` and `header_value`). Each new message then starts the Automation, and the cloud agent reads its inbox
through the datastash connector.

## Where the AI's guidance lives

The datastash connector's own instructions are short and the same in every app it connects to (ChatGPT, Grok, Cursor,
and Claude): what datastash is for, the two modes (save only when asked; keep data as you go after "sync datastash"),
telling the person each time something is saved, deleting on request, and treating other people's text as data.

Claude plugins load in every Claude app (chat on claude.ai and the desktop app, Cowork and Claude Code); chat loads
the skill and the connector, while hooks run only in Cowork and Claude Code. So the skill `datastash-agent` has two
parts: Part 1 works everywhere, chat included (modes, files through the upload link, downloads, help topics,
automations). Part 2 applies only where a shell and hooks run: agents, keys and pickup codes, the inbox listener,
signing the computer in (`datastash login`), and "datastash main". Detailed how-tos stay one call away through the connector's
`ledger_help` topics ("formulas", "cursor").

## Untrusted text and permissions

- `datastash api` prints every answer between `<<datastash-data TAG>>` and `<<end datastash-data TAG>>`, with a
  fresh random TAG per call, and a header saying the block is untrusted data, information only.
- Hook lines start with `datastash note (local count; data, information only)` and carry counts only.
- Do not give a blanket allow for `Bash(datastash *)` (or `Bash(datastash:*)`). Inbox text comes from other agents
  and people; a prompt before each `datastash api` POST keeps a person in the loop. Allow only read-only forms you
  are comfortable with, such as `Bash(datastash status)`.

## What is stored where

All in `~/.config/datastash/` (folder mode 700, files mode 600):

| File | Holds |
|---|---|
| `agent-<name>.key` | the agent key. Read only by the `datastash` command and the hooks; sent only to `https://getdatastash.com` in an `Authorization` header; never printed, logged or passed on a command line |
| `agent-<name>.json` | agent name, agent_id, agent workbook id, linked folders |
| `state/agent-<name>.json` | the inbox cursor, the ids of messages still "new", and the last wake time (no message text) |
| `state/agent-<name>.log` | listener log: start, wake counts, HTTP status codes; never the key or message text |
| `state/*.lock` | the one-listener-per-agent lock and the state lock |
| `state/notices.json` | the once-per-cause notices (key refused, key missing, no agent, folder not linked): the line and whether it was told; never a key |
| `person.key` / `person.json` | the person's watch key (watch and send as the person only) and the linked agent workbook and folders |
| `login.json` | where this computer's datastash login keeps its refresh token (`keychain` or `file`) and when it was made; never a token |
| `login.refresh` | the login's refresh token, only when no system keychain works (otherwise it is in the keychain, service `datastash`) |
| `login.access.json` | the current 15-minute access token (so each helper call doesn't refresh); read only by the `datastash` command |
| `login.lock` | held while one `datastash` process refreshes the login, so two never send the same refresh token; empty |
| `login.pending.json` | while a sign-in waits for the person: the device code, the short code and page shown to the person, and when it ends (mode 600); removed when it ends |
| `signin-mode.json` | the sign-in mode (legacy, device, or old-for-good without file locking), why, when it was decided and changed, and whether the last-week line was shown; no token (mode 600) |
| `login.start.lock` / `login.poll.lock` | one sign-in start at a time; one background poll per computer; empty |
| `state/login-start.json` | when a sign-in start last failed and datastash's Retry-After (no codes) |
| `state/login-browser.json` | when a hook last opened the browser for the sign-in (at most once a day) |
| `state/signed-out-start-sid-*`, `state/sign-in-*-sid-*` | this session started signed out / was shown the sign-in (for the one "signed in now" line and the once-per-session re-show) |
| `main.key` | an earlier main key (made before datastash login); still used until it is stopped, read only by a main session |
| `state/main-session-<id>.json` | which session is the person's main (session id, its Claude Code process id and start time); removed at SessionEnd |
| `state/main-request-<id>.json` | the person typed "datastash main" in that session (good for 30 minutes) |
| `state/agent-used-*` | this session (or its process) acted as an agent, so it can't become main |
| `state/hooks-heartbeat.json`, `state/hooks-seen-sid-<id>` | when a plugin hook last ran on this machine, and that hooks run in that session (event and time only) |
| `off/` | key and config files of agents switched off on this machine |
| `status-<workbook>.json` | this machine's copy of every agent's status in that agent workbook (plain words, context, asks), one row per agent and project, refreshed by the listener every ~60 s and by this machine's own writes; notes when the last refresh failed |
| `status-consent.json` | the person's yes or no for keeping this computer's agents' status in datastash (and per project folder: "leave <name> off") |
| `state/status-view-<id>.json`, `state/status-prompts-<id>.json`, `state/status-typed-<id>.json` | what that session showed last (number -> agent, project, ask id), its prompt count, and the typed transcript entries already used once |
| `state/status-out-<name>.json`, `state/status-sent-<name>.json`, `state/status-written-<name>`, `state/status-snap-<name>.json`, `state/status-reminded-<name>` | the agent's newest status waiting to be sent and whether it was, when it was last written (twice: time only), and when it was last reminded |
| `state/status-install-asked` | the install question was shown once on this computer |
| `state/status-door.json` | datastash answered 404 for the status door (it is not on the server yet) |
| `state/turn-<id>*` | when that session's turn started, that it used a tool, ran tests, and was reminded about its status |

Message text is never stored locally; the agent reads it from datastash when it needs it.

## Agent status

Every agent keeps one live status per project in its agent workbook: two or three plain sentences of what it is doing
and why, a context line (branch, PR, tests, last change, how long ago), its cards, what is running, health, what it waits
on, what the person asked of it, and its asks of the person (shown as text). The person sees all agents as one numbered
list, asks first and idle agents last, from any session: "status", "status of all my agents", "status of api",
"full context of 4".

- **Bounded full context (0.9.4).** "full context of 4" is one screen of about 8,000 characters: the agent's own
  words first, then one line per card, running item, wait and request. It is a display bound only: nothing stored or
  sent is cut. When there is more it ends "1,950 more lines not shown: say 'more of 4' to see the next ones", and each
  "more of 4" shows the next screen, from this computer's copy, until "That is all of 4." An agent reads the same
  screens with `datastash status --details 4` and `datastash status --more 4`.
- **Fast (0.3.0).** When keeping is on and this session's agent already has a saved status, the prompt hook answers
  these itself, in plain code, from this computer's copy: no model turn and no network call, so the answer shows in
  well under 2 seconds. It starts with "Status of your agents (refreshed N min ago)", says "offline" when the last
  refresh failed, and says each agent wrote its own status in its own words. Claude Code shows it as the hook's answer
  to the prompt, and the prompt itself does not go to the AI.
- **No answers yet.** Asks are shown only. "do 4" (4 being an ask in the list this session showed) says: answering asks
  from here is coming, shows the ask, and says to tell that agent in its own session. "4: ..." is an ordinary prompt.
  The plugin calls no answer route; a 404 from datastash means "not available yet", never "sent".
- **Keeping (one yes per computer).** The person types "Keep my agents' status and what I tell them to remember in
  datastash" (the install step). The prompt hook recognizes it in plain code (case, punctuation and "my agent's" /
  "my agents'" don't matter) and acts only once a checker confirms it as the person's own typed prompt: the transcript
  entry carries `origin.kind` "human" and `promptSource` "typed" or "queued". A headless `claude -p` prompt or an entry
  without those fields does nothing. Then `datastash keep --on` signs the computer in if it isn't yet, says which agents
  are kept, and has the agent save the status it already worked out in that session as its first one. Nothing about
  status is read or sent before that, and a `keep` value in a status read is ignored.
- **Status off.** Asking for status goes to the agent the ordinary way (it looks), and its answer ends with the one
  line to type to turn keeping on. Nothing is sent.
- **First status per session.** When keeping is on and a session's agent has no saved status yet, the first status ask
  there has the agent save its own status in that turn, then show the list. After that the plugin answers itself.
- **After install.** The first "status of all my agents" after keeping is on lists the connected agents and asks "Did we
  miss any of your agents, or is there one you don't want on datastash?". "add <folder or project name>": a folder of
  yours on this computer joins now (checked as your own typed prompt; you allow its agent on the agents page). Only a folder datastash has seen counts; any other "add ..." (say "add
  dark mode to the settings page") is an ordinary prompt and goes through untouched. "leave <name> off" (or "keep <name> off
  datastash", or "datastash off" for this folder) turns that folder OFF (below; saved at once, since it only makes
  agents share less). "keep <name> private": private mode is coming. After an answer the updated list is shown once.
- **OFF folders.** A folder that is off makes no datastash call: no sign-in or inbox count at session start, no
  listener, hello, heartbeat, status, seed, project-map share or lookup counting, and no agent is registered for it.
  Claude Code runs the connector's `headersHelper` in the plugin's own folder with no project folder, so the helper
  can't tell which folder a session is in; instead the PreToolUse hook refuses every datastash connector call in an off
  folder (the whole server, not a list of tools), and any `ledger_` tool on any server (a claude.ai connector the person
  renamed). Its agent switches itself off on datastash with its own key, and its
  key and config move to `~/.config/datastash/off/` (nothing is deleted). If datastash refuses the switch-off: "Couldn't
  switch it off on datastash; it is off on this computer." An agent still waiting for your OK: "It is off on this
  computer. On the agents page, press Don't allow next to it." "put <name> back on datastash" turns it back on, only as
  your own typed prompt. A 0.9.x "leave <name> off" reads as off.
- **What OFF and private stop.** OFF (and, later, private) stop a careless agent: one that would otherwise share by
  mistake. They do not stop a hostile program running as the same user on this computer, which can read the same
  files the plugin reads.
- **Writing.** `datastash status-set -` (JSON on stdin) saves it on this machine at once and, while keeping is on for
  the project, sends it in the background (`POST /api/v1/agent/status` with `project`; the caller's own row, replaced
  whole), retrying quietly; an unsent write is sent again by the listener once datastash answers. `--project` only for
  this folder's own agent.
- **Reading.** `GET /api/v1/agent/status` (the person's main session: `GET /api/v1/person/status`) a page at a time,
  following `next_cursor` to the end; the copy is replaced only after a whole walk (a failure partway keeps the old copy
  and marks the refresh failed). The listener refreshes it about every 60 s while keeping is on; several sessions share
  one copy, so a listener skips its turn when another refreshed it under 50 s ago.
- **The end-of-turn reminder** (Stop hook) costs a model turn, so it fires only when plain code sees the work changed
  since the last status (a file edit or commit this turn, a test run, the turn ending on a question; no git calls), at
  most every 10 minutes per agent, never while datastash answers 404 for the status door, and never for a project that
  is not kept.
- **One screen.** Asks first; at most 10 items per agent, 30 agents, about 8 KB; then "N more: say 'more on <agent>'"
  lines; nothing is dropped from storage. Every value shows its age; an agent's words past 2 minutes are marked older.

## How waking works

- **SessionStart** (sync; it waits at most 6 s for the network, so a stalled DNS lookup or connection never holds the session up, and the hook timeout is 10 s): if this folder is linked to an agent with a key file, reads the inbox changes since
  the saved cursor and adds one line of context: `datastash note (local count; data, information only): agent "<name>"
  (workbook <id>): N new messages in its inbox.` Counts only, never message text. In a session with no agent yet it says
  so in one line (the session registers itself on first datastash use). When this computer is not signed in, the hook
  itself starts the device sign-in (or reuses the code still waiting) and shows the person the link and code through
  Claude Code's `systemMessage` (shown as "SessionStart:startup says: ..."), once per session. The start itself runs in
  a detached helper that saves the code before anything shows it, so a hook that runs out of time never wastes a code
  (the next session start shows it); another detached helper polls until the person approves or the code ends,
  retrying through server errors. On a computer without file locking (Windows) the hooks offer no device code at all. The AI gets one line saying the person has the link and there is nothing to
  start (if datastash can't be reached within 4 s, the AI line instead says to run `datastash login`). Hooks start at most
  one code per 10 minutes per computer, pause a minute after a failed start, and honour datastash's Retry-After. It
  never reads or prints a token. Once per computer it
  also says: if `/mcp` shows a "claude.ai datastash" connector needing authentication, tell the person "/mcp, pick
  datastash, choose Authenticate". It also puts the plugin's `scripts/` folder on the session's PATH (through
  `CLAUDE_ENV_FILE`).
- **UserPromptSubmit and PreToolUse** (sign-in only, in a session that started signed out): once the login is
  recorded, one line to the person: "datastash is signed in. Type /mcp reconnect all to connect it in this session; new
  sessions connect by themselves." While still signed out (in the device mode), the session's first datastash use (a
  prompt naming datastash, or a tool call that touches datastash) shows the link and code again, once.
- **UserPromptSubmit**: when the person's whole prompt is "datastash main" (or "main datastash"), records that request
  for this session, or tells the AI in one line why this session can't be main. Every other prompt is left alone.
- **Listener** (SessionStart and Stop, `async` + `asyncRewake`): long-polls `GET /api/v1/agent/inbox?wait=25` from
  the saved cursor. When a new message arrives it exits with code 2, which wakes the session even when idle; the
  wake text carries counts only. The Stop hook starts it again after each turn.
  - Wake pacing (local, not a user limit): after a first arrival it gathers for 3 s, so a burst becomes one wake,
    and it wakes the session at most once per 30 s. Arrivals in between are batched into the next wake; the time of
    the last wake is kept in the state file, so a listener restarted by the Stop hook keeps the same pace. Nothing is
    dropped. `DATASTASH_WAKE_GATHER_SECONDS` and `DATASTASH_WAKE_MIN_SECONDS` change the two values.
  - Per sender (local, not a user limit, and nothing is refused or dropped): when a wake brings a sender's messages and
    none of them is marked handled by the next wake, that sender's next messages wait longer before they wake the
    session: 60 s, then 2 minutes, 4, 8, and so on up to an hour (`DATASTASH_WAKE_IDLE_MAX_SECONDS`). One handled
    message from it resets its pause. Other senders keep the normal pace, and every message is still read and counted.
  - One listener per agent (an exclusive lock); extra starts exit at once.
  - The cursor is saved after every answer, so nothing between listeners is skipped; messages the agent leaves
    "new" are counted at the next session start but never re-wake the session on their own (no wake loops). The
    agent's own read/handled marks never wake it.
  - Errors (HTTP and network) back off from 5 s to 5 minutes, with jitter; a 404 waits 10 minutes; an expired cursor
    starts over, and a cursor that keeps expiring (more than 2 times in a row) backs off like any error. An empty long
    poll that answers at once is followed by a 1-2 s pause.
  - A refused key (switched off or re-issued) or a missing key file parks the listener: it keeps its lock, makes no
    network call at all, re-reads the key file every 5 s and goes on once a different key is there (or a session start
    found the same key working again). It never asks datastash for a key or a pickup code: issuing a key stops the
    agent's live key, which may be another computer's working key. The fix is the person's own step.
  - Notices: one line per cause (key refused, key file missing, no agent on this computer, a folder that is not linked
    while several agents have no folder), told once at the next session start or by a parked listener's one wake, removed
    when fixed. Each ends with the sentence the person says to Claude to fix it. Kept in `state/notices.json`.
  - One-agent fallback: in a folder no agent is linked to, the session listens for the one agent on this computer with
    no folder of its own; with several such agents it never guesses. It re-checks every loop and lets go when that
    agent gets a folder elsewhere. The fallback never marks the session as an agent (it can still become main).
  - A listening heartbeat (`POST /api/v1/agent/heartbeat`, state running, the folder name as project; none for the
    one-agent fallback, whose open folder is not the agent's) about every 10
    minutes, in its own thread; none while parked; a 404 waits an hour. Its errors never stop the listener.
  - It has no time limit (the hook timeout is 2,000,000 s). It stops when its Claude Code process goes away (the pid
    and its start time, so a reused pid never keeps it alive) or at
    SessionEnd (found by its session id in the lock files); the next turn or session start brings it back.
  - It only ever holds one HTTPS request at a time and uses no CPU between answers.
- `claude -p` ends background hooks when the run ends, so headless runs get the session-start count but no listener.

### If datastash isn't waking you

Run `datastash doctor`. It checks that the plugin's hooks fire (they write a heartbeat), that Claude Code's settings
files (`~/.claude/settings.json` and the project's `.claude/settings*.json`) belong to this user and are readable and
writable, that the plugin is turned on, that the key files are private (mode 600), that https://getdatastash.com
answers, that the datastash connector is signed in, and whether the listener runs for this session. Each problem comes with its exact fix. The cheap checks also
run on their own at an agent's first datastash use (`auto-name`, `status`, the session's first `api` call); on a
healthy machine they print nothing.

When something is wrong, the AI names the problem and asks: "Datastash can fix this for you or give you instructions
to fix it yourself. Would you like us to fix this?" On yes it shows the fix command, then runs it. A fix that needs
admin rights (for example `sudo chown` on a settings file owned by root) runs with sudo, so you type your own password
in your terminal; the AI never asks for it in chat. Without sudo, you get the steps for the machine's admin.

Common causes:

- **A settings file owned by another user** (for example root, after a setup script ran with sudo): Claude Code skips
  the plugin's hooks, so nothing wakes the session. Give your user ownership of the file, then restart Claude Code.
- **The plugin was installed or turned on after the session started**: restart Claude Code or type `/reload-plugins`.
- **A locked-down machine** where the install fails: the install needs to write `~/.claude/` and to reach github.com.
  If it can't, run from a copy with `claude --plugin-dir <copy>/plugins/datastash`.
- **The connector isn't signed in**: this computer has no datastash login yet (or it was signed out). The next session
  start shows the sign-in link and code; or ask Claude to run `datastash login`, which shows the same code. Open
  `https://getdatastash.com/agents` on any device, type the code, then type `/mcp reconnect all`.
  `/mcp` shows the plugin's datastash connector as failed (HTTP 401) while signed out, with no Authenticate option;
  that is expected.
- **New datastash tools not showing**: current versions of datastash tell Claude Code when its tools change; on an older
  session, reconnect the datastash connector (`/mcp`).

Until it's fixed, `datastash watch` under a Monitor (below) still delivers messages.

### Fallback: a Monitor loop

If hooks are off or the listener cannot run, the session can watch with a Monitor instead; each printed line is an
arrival (counts only):

```
Monitor: command "datastash watch", description "datastash inbox", timeout the maximum; re-arm on expiry
```

`datastash watch` uses the same cursor, lock and backoff as the listener (if a listener holds the lock, `watch`
exits at once). A stopped agent can be started by cron: poll `datastash status` and run
`claude -p --resume <session-id>` when the new count is above zero.

Optional webhook: `ledger_inbox_webhook` POSTs a message count (no message text) to a public https URL; some services, like Cursor
Automations, need a key header, so set `header_name` + `header_value` there (stored sealed, shown only as set / not set).

## How the person is asked questions

The person has an inbox like every agent. The agent sends its question there (`"to": "person"`, through
`datastash api POST /api/v1/agent/inbox` or the connector's `ledger_inbox_send`). The person's own apps list and
answer it; the answer arrives in the agent's inbox as a reply, and the listener wakes the agent. The plugin itself
never speaks to the person.

## What an agent can do in the person's other workbooks

In the person's ordinary workbooks (anything that is not an agent workbook) an agent key, or a connection acting for an
agent, can read and add or edit rows. It can't delete a workbook, tab, column or rows, make or stop share links, share
with people, or export, and it can't erase across the whole account; such a call is refused and changes nothing.
The person can allow delete, share and export for one workbook at a time from their own connection
(`ledger_agent_reach`, action "allow"; "stop" takes it back; "show" says which). Agents can't change that switch.
Agent workbooks keep their own rules, and the person's own connections keep full rights everywhere.

## @feedback

An agent can send datastash a capability request, an error report or anything else it needs by sending to
`"to": "@feedback"` (kind need, error or other); it waits in the person's inbox and is sent only on the person's yes.

## Switching off an agent

- On this machine: `datastash off "<name>"` turns its folders OFF (see above), switches it off on datastash with its
  own key where datastash allows it, moves its key and config to `~/.config/datastash/off/` and stops its listener.
  Uninstalling or disabling the plugin stops the hooks.
- Everywhere: switch it off in datastash (`ledger_agent_switch_off`, from the person's connection or an agent above
  it). Its key and every key under it stop on the next call. A lost or leaked key: ask for a new pickup code
  (`ledger_agent_key`); the old key stops at once.

## Test

- `python3 -I test/test_plugin.py`: offline tests (no network, no key): path guard, output framing, key-file
  checks, state shape, name collisions, listener match, backoff (410, 5xx, network, fast empty polls, the cursor race
  in the session-start count), the session-start check's short wait on a stalled network, and wake pacing, including
  a simulated burst of 5 messages and a hostile sender whose wakes bring nothing handled.
  Setup checks: a settings file or folder owned by another user, no hook heartbeat (never, or not in this session), the
  plugin turned off, a missing or open key file, no reach; each with its fix command (the chmod fixes are run and
  re-checked), and nothing on a healthy machine or a read-only sandbox mount.
  datastash login: the code printed and the page opened (no code in its address, the device code never on a command
  line), the background poll saving the refresh token in the keychain or a mode-600 file, `token --header` printing
  only the header (always a header and exit 0: the placeholder when signed out, waiting, refresh refused or unreachable,
  keychain missing, no file locks, or a crash; never a token to a terminal; the 15-minute token reused and refreshed
  near its end), a login revoked at datastash forgotten locally, logout, the session-start sign-in (the person's link and
  code once per session, the same code reused across sessions, a new one only after it ends, the AI fallback and pause
  when datastash can't be reached, Retry-After, a stalled network never holding the session start), the "signed in now"
  line, the first-use re-show, one background poll per computer, a poll never dropping a newer code, the
  imported-connector line said once per computer, and doctor's line.
- `python3 -I test/test_release.py`: fails when the plugin's files change and `plugin.json`'s version does not (Claude
  Code pins a plugin to its set version); `test/plugin-release.json` records each version's fingerprint.
  Main in any session: main in an agent-linked folder for that session only, another session in that folder staying
  the agent, an agent session refused, session id spoofing, and self-registration names. Clean sessions only: the
  review's attack replayed (an agent session fakes the typed phrase with the `prompt` command, with its own or a forged
  session id, with a main key on the machine), each first agent action recording the session, and `datastash login` never recording it.
- `python3 -I test/test_status.py`: the agent status offline, under the same enforced harness (`test/harness.py`: a
  throwaway HOME and config folder, no network): the numbered list in the owner's format, bounded; the local copy
  (paged reads to the end, a failure partway keeps the old copy, mode 600, no symlink; the 60 s refresh, skipped when
  another session refreshed, nothing read while keeping is off); the plugin's own fast answer ("status", "status of all
  my agents", "status of <agent>", "full context of N": a block with the list, no network, offline and 404 said
  plainly); "full context of N" bounded with a "more of N" line, paged through 2,000 cards each once, under 2 seconds; "do N" shows the ask and sends nothing; keeping only from the person's typed phrase (`claude -p` and
  entries without origin fields refused); status off sends nothing and says how to turn it on; the install question and
  "leave <name> off"; the Stop hook; that no answer or undo route is called in the whole run; and the timed read: the
  real hook process answers from a 30 agents x 5 projects copy in under 2 seconds with no request.
- `test/person-wake-test.sh` proves "datastash main": an idle person session (watch key from a watch pickup code, in a
  throwaway config folder) wakes when an agent sends the person an ask, with no message text in the session.
  `ANYWHERE=1 MAIN_KEY_FILE=<a main key file>` runs it in a clean folder (no agent link, no person link) with a main key
  on the machine: the person types "datastash main" there, the session turns into the main, and it wakes as the person.
- `test/wake-test.sh` proves the idle wake on this box with the test account (needs tmux; see the script header).
  `BURST=5 test/wake-test.sh` sends 5 messages within seconds instead and passes when the session wakes once or
  twice and handles all 5.

## License

MIT. See [LICENSE](LICENSE).
