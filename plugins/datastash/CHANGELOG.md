# What's new in datastash for Claude Code

One line per release, newest first.

- **1.0.36**: Asking any command for help (`--help` or `-h`) only shows help and never saves or sends anything; `datastash --help` lists every command; status and doctor agree on whether the listener runs; old versions no longer pile up on PATH, and unused old version folders are moved to a trash folder instead of piling up; the sign-in guard no longer refuses a plain file just because it names a datastash command; "Check your email" shows once.
- **1.0.35**: Type **DDS update** (or **morning update**) to see each agent's open list and every agent's together; for the first week it also arrives at 6 AM.
- **1.0.34**: With clean sessions on, a wake watcher you allow by typing **datastash allow watcher PATH** can run in your main session, in the background only.
- **1.0.33**: Agents can set timers without the connector: `datastash timer set`, `timer list` and `timer delete`.
- **1.0.32**: Not released.
- **1.0.31**: After the email-code sign-in, new and resumed sessions connect to datastash by themselves; a session running an older version says once to start a new session.
- **1.0.30**: Signing in is just **datastash sync you@email.com** and then **datastash sync CODE**, with no connector step; a change to your setup is told once; times on a UTC computer say UTC.
- **1.0.29**: Time estimates for a list of cards match each card's own estimate, done-around times are in your own time, and **DDS estimate recalc** starts estimates fresh.
- **1.0.28**: **datastash sync** lines typed while the session is busy still go through.
- **1.0.27**: The daily update check finishes even after short runs, and doctor shows which version this session runs, which is installed and which is newest.
- **1.0.26**: Lines you type in the Claude desktop app's Code tab count as typed by you.
