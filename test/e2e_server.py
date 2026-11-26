#!/usr/bin/env python3
"""e2e_server.py: the server side of test/e2e-real.sh. Reads back from https://getdatastash.com what each step did, for
the selftest test account only, and finishes a Claude Code connector sign-in through the selftest door (x-live XLI-314).
Run with python3 -I. Never prints a secret, a code or a token.

  browse <authorize-url>   the browser's part of /mcp Authenticate: the selftest sign-in step, then the code to the
                           client's own loopback redirect (as x-live scripts/selftest-oauth-signin.sh does)
  signin                   this script's own connection to the same account (a native throwaway app); its tokens go to
                           E2E_SERVER_TOKEN_FILE (mode 600)
  agents                   the account's agents that are on, as a JSON list of names (ledger_agents_list)
  on NAME                  switch that agent back on (ledger_agent_switch_off action "on", the person's own switch-on):
                           only to recover a name a plain switch-off left off (an older harness did that), so the typed
                           uninstall can remove it and the name installs again (x-live #510). This script never switches
                           an agent off: a plain switch-off is not an uninstall, and a reinstall does not bring it back.
  apps                     how many app connections the account has (sync apps), as a number
  other-apps               the names of the account's app connections other than this script's own, as JSON (Claude
                           Code's sign-ins share one client, so they show as one app)
  disconnect-all           disconnect every app connection on the account, this script's own last (cleanup)
  disconnect-others        disconnect every app connection but this script's own (the clean start of a run)
  chat-count TEXT [MUST]    (1.0.18) how many saved exchanges start "User: TEXT" across the account's saved chats, and
                           whether one has an answer after it (holding MUST, when given), as JSON {"count", "answered"}
  timer-find WB NOTE        (1.0.18) whether a timer with NOTE is set in agent workbook WB; then cancels it, as JSON
  email-code OUTFILE [SECS]  (1.0.20, XLI-417) the sign-in code datastash made for the test account's typed
                           `datastash sync <email>`, read once through the selftest door (x-live
                           POST /api/v1/selftest/email-code: the secret, from the build box, selftest accounts only), into
                           OUTFILE (mode 600); waits up to SECS (default 90) for it. The code is never printed.
  inbox-marks WB INBOX MARK  (XLI-408 step 9) every message in that inbox of workbook WB whose text holds "MARK #<n>",
                           as a JSON list of the n's in the order datastash added them (a repeat shows twice)

Env: E2E_SELFTEST_SECRET_FILE (the bare secret, mode 600), E2E_ACCOUNT (the selftest label), E2E_SERVER_TOKEN_FILE,
E2E_SIGNIN_LEDGER (optional: each selftest sign-in appends its time there). A refused selftest sign-in names the
server's reason (rate_limited, too_many_accounts, ...) and its text.
Exit 0 ok, 1 refused or failed (one line on stderr), 2 can't run.
"""
import base64, hashlib, json, os, secrets, stat, sys, time, urllib.error, urllib.parse, urllib.request

HOST = "getdatastash.com"
ORIGIN = f"https://{HOST}"
MCP = ORIGIN + "/mcp"
REDIRECT = "http://127.0.0.1:9/callback"  # this script's own app: the code is read from the Location only


def die(msg, rc=1):
    print(f"e2e_server: {msg}", file=sys.stderr)
    sys.exit(rc)


def secret():
    sf = os.environ.get("E2E_SELFTEST_SECRET_FILE", "")
    try:
        st = os.stat(sf)
    except OSError:
        die("the selftest secret file is not readable", 2)
    if stat.S_IMODE(st.st_mode) & 0o077 or st.st_uid != os.getuid():
        die("the selftest secret file must be this user's own, mode 600", 2)
    with open(sf) as f:
        s = f.read().strip()
    if len(s) < 32:
        die("the selftest secret file holds no secret of 32+ characters", 2)
    return s


LABEL = os.environ.get("E2E_ACCOUNT", "plugin-e2e")

# The server's refusals (x-live src/auth/selftest-oauth.ts) are plain text "selftest sign-in refused: <text>"; the
# code is told apart by its text. Neither text holds a secret.
REFUSALS = (("selftest sign-ins an hour", "rate_limited (the hourly sign-in limit; wait, then run again)"),
            ("selftest accounts; use an existing label", "too_many_accounts (no new label can be made; use an existing one)"),
            ("not a selftest test account", "not_a_test_account"),
            ("being deleted", "closing"),
            ("test account label is", "bad_label"))


def refusal_code(status, body):
    for text, code in REFUSALS:
        if text in (body or ""):
            return code
    return "no known code"


def refusal_text(body):
    t = " ".join((body or "").split())[:200]
    return t.removeprefix("selftest sign-in refused: ") or "(no text)"


def note_signin():
    """One line per selftest sign-in this harness made (a time, nothing else): the run's budget check reads it."""
    p = os.environ.get("E2E_SIGNIN_LEDGER", "")
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{int(time.time())}\n")
    except OSError:
        pass


class Keep(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Keep())


def call(step, method, url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        die(f"{step} did not answer ({type(e).__name__})")


def selftest_code(auth_url, redirect):
    """The selftest sign-in from an authorize URL to the redirect's Location (with the code), never followed here."""
    jar = {}

    def take(h):
        for c in (h.get_all("Set-Cookie") or []) if h else []:
            name, _, value = c.split(";", 1)[0].partition("=")
            low = c.lower()
            if value == "" or "max-age=0" in low or "expires=thu, 01 jan 1970" in low:
                jar.pop(name.strip(), None)
            else:
                jar[name.strip()] = value

    def on_host(loc):
        full = urllib.parse.urljoin(ORIGIN + "/", loc)
        if urllib.parse.urlsplit(full).netloc != HOST:
            die("a sign-in redirect points off the datastash host")
        return full

    cookie = lambda: "; ".join(f"{k}={v}" for k, v in jar.items())  # noqa: E731
    status, h, _ = call("/oauth/auth", "GET", auth_url)
    take(h)
    loc = (h.get("Location") if h else "") or ""
    if status not in (302, 303) or "/oauth-interaction/" not in loc:
        die(f"/oauth/auth answered HTTP {status}, not the sign-in step")
    step = on_host(loc).split("?", 1)[0]
    status, h, body = call("the selftest step", "POST", step + "/selftest-login",
                           {"Cookie": cookie(), "x-datastash-selftest-secret": secret(), "x-datastash-selftest-account": LABEL}, b"")
    take(h)
    if status == 404:
        die("selftest sign-in refused (404): off on the server, a wrong secret, or not the build box")
    if status not in (302, 303):
        die(f"selftest sign-in refused (HTTP {status}, {refusal_code(status, body)}): {refusal_text(body)}")
    note_signin()
    loc = (h.get("Location") if h else "") or ""
    r = urllib.parse.urlsplit(redirect)
    for _ in range(8):
        back = urllib.parse.urlsplit(loc)
        if back.hostname in ("127.0.0.1", "localhost", "::1"):
            if (back.hostname, back.port, back.path) != (r.hostname, r.port, r.path):
                die("the sign-in ended on another address than the client's redirect")
            if not urllib.parse.parse_qs(back.query).get("code"):
                die("the sign-in ended without a code")
            return loc
        status, h, page = call("a sign-in step", "GET", on_host(loc), {"Cookie": cookie()})
        take(h)
        if "Sign in with Google" in page:
            die("the sign-in page came back after the selftest step")
        loc = (h.get("Location") if h else "") or ""
        if status not in (302, 303) or not loc:
            die(f"a sign-in step answered HTTP {status} with no redirect")
    die("no redirect to the client after 8 steps")


def cmd_browse(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or (u.hostname or "").lower() != HOST or u.path != "/oauth/auth":
        die(f"the address must be {ORIGIN}/oauth/auth?...", 2)
    redirect = (urllib.parse.parse_qs(u.query).get("redirect_uri") or [""])[0]
    r = urllib.parse.urlsplit(redirect)
    if r.scheme != "http" or r.hostname not in ("127.0.0.1", "localhost", "::1"):
        die("the redirect must be the client's own loopback address", 2)
    loc = selftest_code(url, redirect)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(loc, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError:
        pass
    except (urllib.error.URLError, OSError) as e:
        die(f"the client's loopback redirect did not answer ({type(e).__name__})")
    print("selftest sign-in ok")


def token_file():
    p = os.environ.get("E2E_SERVER_TOKEN_FILE", "")
    if not p:
        die("E2E_SERVER_TOKEN_FILE is not set", 2)
    return p


def save(tok):
    p = token_file()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def cmd_signin():
    reg = json.dumps({"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", "application_type": "native",
                      "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                      "client_name": "datastash plugin e2e-real (server read-back)"}).encode()
    status, _, body = call("POST /oauth/reg", "POST", ORIGIN + "/oauth/reg", {"Content-Type": "application/json"}, reg)
    if status != 201:
        die(f"registering the read-back app answered HTTP {status}")
    client_id = json.loads(body).get("client_id")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(12)
    q = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
                                "scope": "openid mcp:tools offline_access", "code_challenge": challenge,
                                "code_challenge_method": "S256", "state": state})
    loc = selftest_code(ORIGIN + "/oauth/auth?" + q, REDIRECT)
    got = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
    if (got.get("state") or [""])[0] != state:
        die("the code came back with another state")
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": got["code"][0], "redirect_uri": REDIRECT,
                                   "client_id": client_id, "code_verifier": verifier}).encode()
    status, _, body = call("POST /oauth/token", "POST", ORIGIN + "/oauth/token",
                           {"Content-Type": "application/x-www-form-urlencoded"}, form)
    if status != 200:
        die(f"the code exchange answered HTTP {status}")
    tok = json.loads(body)
    if not tok.get("access_token") or not tok.get("refresh_token"):
        die("the code exchange answered no tokens")
    save({"client_id": client_id, "access": tok["access_token"], "refresh": tok["refresh_token"],
          "until": time.time() + int(tok.get("expires_in") or 600) - 60})
    print("read-back signed in")


def load():
    try:
        with open(token_file()) as f:
            t = json.load(f)
    except (OSError, ValueError):
        die("not signed in (run signin first)", 2)
    if time.time() >= t.get("until", 0):
        form = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": t["refresh"],
                                       "client_id": t["client_id"]}).encode()
        status, _, body = call("refresh", "POST", ORIGIN + "/oauth/token", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        if status != 200:
            die(f"the token refresh answered HTTP {status}")
        tok = json.loads(body)
        t.update(access=tok["access_token"], refresh=tok.get("refresh_token") or t["refresh"],
                 until=time.time() + int(tok.get("expires_in") or 600) - 60)
        save(t)
    return t["access"]


def tool(name, args):
    access = load()
    for attempt in range(4):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode()
        status, h, text = call(name, "POST", MCP, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                                   "Authorization": "Bearer " + access}, body)
        if status != 200:
            die(f"{name} answered HTTP {status}")
        if text.lstrip().startswith("event:") or text.lstrip().startswith("data:"):
            text = "\n".join(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
        res = json.loads(text).get("result") or {}
        blocks = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        try:
            out = json.loads(blocks[0]) if blocks else {}
        except ValueError:
            out = blocks[0]
        code = out.get("code") if isinstance(out, dict) else None
        if res.get("isError") and code in ("rate_limit_exceeded", "rate_limited") and attempt < 3:
            time.sleep(min(int(out.get("retry_after_seconds") or 30), 90) + 1)
            continue
        return bool(res.get("isError")), out
    return True, {"code": "rate_limit_exceeded"}


def cmd_agents():
    err, out = tool("ledger_agents_list", {})
    if err or not isinstance(out, dict) or not isinstance(out.get("agents"), list):
        die(f"ledger_agents_list refused ({out.get('code') if isinstance(out, dict) else 'no code'})")
    print(json.dumps(sorted(a.get("name") for a in out["agents"])))


def cmd_on(name):
    err, out = tool("ledger_agent_switch_off", {"name": name, "action": "on"})
    code = out.get("code") if isinstance(out, dict) else None
    if err and code in ("agent_already_on", "agent_not_found"):
        print(f"{name}: {'already on' if code == 'agent_already_on' else 'no such agent'} (nothing switched on)")
        return
    if err:
        die(f"switch on {name!r} refused ({code or 'no code'})")
    print(f"switched on {', '.join(out.get('switched_on') or [name])} (the person's switch-on)")


def apps():
    err, out = tool("sync", {"action": "apps"})
    lst = out.get("apps") if isinstance(out, dict) else None
    if err or not isinstance(lst, list):
        die("sync apps refused")
    return lst


def cmd_disconnect_all(keep_mine=False):
    lst = [a for a in apps() if isinstance(a, dict)]
    mine = [a for a in lst if a.get("this_connection") is True]
    todo = [a for a in lst if a.get("this_connection") is not True] + ([] if keep_mine else mine)
    bad = 0
    for a in todo:
        err, _ = tool("sync", {"action": "disconnect", "connection": a.get("connection")})
        bad += bool(err)
    print(f"disconnected {len(todo) - bad} of {len(todo)} app connections" + (" (the read-back's own kept)" if keep_mine else ""))
    if bad:
        sys.exit(1)


def cmd_chats(state):
    """1.0.18: the account's chat saving (chatstash), set by the person's own read-back connection: "off" before and
    around the onboarding (its datastash sync turns it on), so the other steps' sessions don't save every exchange."""
    err, out = tool("sync", {"action": "stop_chatstash" if state == "off" else "start_chatstash"})
    if err:
        die(f"chatstash {state} refused ({out.get('code') if isinstance(out, dict) else 'no code'})")
    print(f"chat saving is {state} for the test account")


def cmd_inbox_marks(wb, inbox, mark):
    """XLI-408: the read-back of the offline outbox step: the person's connection reads the agent's inbox from the start."""
    import re
    found, after, seen_ids = [], 0, set()
    for _page in range(200):
        err, out = tool("ledger_inbox_read", {"workbook_id": wb, "inbox": inbox, "after": after, "limit": 100})
        if err or not isinstance(out, dict) or not isinstance(out.get("messages"), list):
            die(f"ledger_inbox_read refused ({out.get('code') if isinstance(out, dict) else 'no code'})")
        for m in out["messages"]:
            mid = m.get("message_id") if isinstance(m, dict) else None
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            for n in set(re.findall(re.escape(mark) + r" #(\d+)\b", json.dumps(m))):  # once per message
                found.append((mid if isinstance(mid, int) else 0, int(n)))
        if not out.get("more") or str(out.get("cursor")) == str(after):
            break
        after = out.get("cursor")
    print(json.dumps([n for _mid, n in sorted(found)]))


def cmd_timer_find(wb, note):
    """1.0.18 (x-live #519): the agent's timer with this note in its workbook (set with no confirm after datastash sync):
    found or not, then cancelled so nothing fires later. Prints {"found": bool, "set_by": ...}."""
    err, out = tool("ledger_timer_list", {"workbook_id": wb, "limit": 100})
    if err or not isinstance(out, dict):
        print(json.dumps({"found": False, "why": out.get("code") if isinstance(out, dict) else "no answer"}))
        return
    hit = [t for t in out.get("timers") or [] if isinstance(t, dict) and note in str(t.get("note") or "")]
    for t in hit:
        tool("ledger_timer_cancel", {"workbook_id": wb, "timer_id": t.get("timer_id")})
    print(json.dumps({"found": bool(hit), "count": len(hit), "set_by": hit[0].get("set_by") if hit else None}))


def cmd_chat_count(text, must=None):
    """1.0.18 (XLI-413): how many times the exchange line "User: <text>" is saved across the account's saved chats (the
    Chats tab, one row per chat, each read with sync read_chat), and whether one of them carries an answer after it.
    Prints {"count": n, "answered": bool}."""
    err, out = tool("ledger_workbook_list", {})
    # ledger_workbook_list answers its page under "items" (x-live src/mcp/manifest.ts executeLedgerWorkbookList).
    wbs = (out.get("items") or out.get("workbooks")) if isinstance(out, dict) else None
    wb = next((w.get("workbook_id") for w in wbs or [] if isinstance(w, dict) and w.get("kind") == "chats"), None)
    if err or wb is None:
        print(json.dumps({"count": 0, "answered": False, "why": "no Chats workbook"}))
        return
    err, out = tool("ledger_rows_filter", {"workbook_id": wb, "tab": "Chats", "limit": 200})
    rows = out.get("rows") if isinstance(out, dict) else None
    if err or not isinstance(rows, list):
        die(f"ledger_rows_filter Chats refused ({out.get('code') if isinstance(out, dict) else 'no code'})")
    want, n, answered = f"User: {text}\n", 0, False
    for r in rows:
        cells = next((r[k] for k in ("cells", "values", "data") if isinstance(r, dict) and isinstance(r.get(k), dict)), r)
        cid = cells.get("chat_id") if isinstance(cells, dict) else None
        if not isinstance(cid, str):
            continue
        cursor, whole = None, ""
        for _page in range(100):
            err, got = tool("sync", {"action": "read_chat", "chat_id": cid, **({"cursor": cursor} if cursor else {})})
            if err or not isinstance(got, dict):
                break
            whole += str(got.get("transcript") or "")
            cursor = got.get("next_cursor")
            if not cursor:
                break
        n += whole.count(want)
        for piece in whole.split(want)[1:]:
            line = piece.split("\n", 1)[0]
            reply = piece.split("\nUser: ", 1)[0]
            answered = answered or (len(line.split(": ", 1)[-1].strip()) > 0 and (must is None or must in reply))
    print(json.dumps({"count": n, "answered": answered}))


EMAIL_CODE_URL = ORIGIN + "/api/v1/selftest/email-code"


def cmd_email_code(out, secs=90):
    """1.0.20: the test account's emailed sign-in code, read once through the selftest door, into OUT (mode 600)."""
    headers = {"x-datastash-selftest-secret": secret(), "x-datastash-selftest-account": LABEL,
               "Content-Type": "application/json"}
    until = time.time() + secs
    while True:
        status, _, body = call("POST /api/v1/selftest/email-code", "POST", EMAIL_CODE_URL, headers, b"{}")
        if status == 404:
            die("the selftest code door answered 404 (off, not deployed, or not from the build box)")
        if status != 200:
            die(f"the selftest code door answered HTTP {status}")
        try:
            code = json.loads(body).get("code")
        except ValueError:
            die("the selftest code door answered no JSON")
        if isinstance(code, str) and code.isdigit() and len(code) == 6:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(code)
            print("sign-in code read (not shown)")
            return None
        if time.time() > until:
            die(f"no sign-in code for the test account within {secs}s (was the typed datastash sync <email> sent?)")
        time.sleep(3)


def main(argv):
    if not argv:
        die(__doc__.strip().splitlines()[0], 2)
    c = argv[0]
    if c == "browse" and len(argv) == 2:
        return cmd_browse(argv[1])
    if c == "signin":
        return cmd_signin()
    if c == "agents":
        return cmd_agents()
    if c == "on" and len(argv) == 2:
        return cmd_on(argv[1])
    if c == "apps":
        print(len(apps()))
        return None
    if c == "other-apps":
        print(json.dumps(sorted(str(a.get("name")) for a in apps() if isinstance(a, dict) and a.get("this_connection") is not True)))
        return None
    if c == "disconnect-all":
        return cmd_disconnect_all()
    if c == "disconnect-others":
        return cmd_disconnect_all(keep_mine=True)
    if c == "chats" and len(argv) == 2 and argv[1] in ("on", "off"):
        return cmd_chats(argv[1])
    if c == "chat-count" and len(argv) in (2, 3):
        return cmd_chat_count(argv[1], argv[2] if len(argv) == 3 else None)
    if c == "timer-find" and len(argv) == 3:
        return cmd_timer_find(argv[1], argv[2])
    if c == "email-code" and len(argv) in (2, 3) and (len(argv) == 2 or argv[2].isdigit()):
        return cmd_email_code(argv[1], int(argv[2]) if len(argv) == 3 else 90)
    if c == "inbox-marks" and len(argv) == 4:
        return cmd_inbox_marks(argv[1], argv[2], argv[3])
    die("unknown command", 2)


if __name__ == "__main__":
    main(sys.argv[1:])
