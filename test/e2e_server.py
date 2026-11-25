#!/usr/bin/env python3
"""e2e_server.py: the server side of test/e2e-real.sh. Reads back from https://getdatastash.com what each step did, for
the selftest test account only, and finishes a Claude Code connector sign-in through the selftest door (x-live XLI-314).
Run with python3 -I. Never prints a secret, a code or a token.

  browse <authorize-url>   the browser's part of /mcp Authenticate: the selftest sign-in step, then the code to the
                           client's own loopback redirect (as x-live scripts/selftest-oauth-signin.sh does)
  signin                   this script's own connection to the same account (a native throwaway app); its tokens go to
                           E2E_SERVER_TOKEN_FILE (mode 600)
  agents                   the account's agents that are on, as a JSON list of names (ledger_agents_list)
  off NAME                 switch that agent off (ledger_agent_switch_off, the person's connection): cleanup only
  apps                     how many app connections the account has (sync apps), as a number
  other-apps               the names of the account's app connections other than this script's own, as JSON (Claude
                           Code's sign-ins share one client, so they show as one app)
  disconnect-all           disconnect every app connection on the account, this script's own last (cleanup)

Env: E2E_SELFTEST_SECRET_FILE (the bare secret, mode 600), E2E_ACCOUNT (the selftest label), E2E_SERVER_TOKEN_FILE.
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
        die(f"selftest sign-in refused (HTTP {status})")
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


def cmd_off(name):
    err, out = tool("ledger_agent_switch_off", {"name": name, "action": "off"})
    if err:
        die(f"switch off {name!r} refused ({out.get('code') if isinstance(out, dict) else 'no code'})")
    print(f"switched off {name}")


def apps():
    err, out = tool("sync", {"action": "apps"})
    lst = out.get("apps") if isinstance(out, dict) else None
    if err or not isinstance(lst, list):
        die("sync apps refused")
    return lst


def cmd_disconnect_all():
    lst = apps()
    mine = [a for a in lst if isinstance(a, dict) and a.get("this_connection") is True]
    bad = 0
    for a in [a for a in lst if isinstance(a, dict) and a.get("this_connection") is not True] + mine:
        err, _ = tool("sync", {"action": "disconnect", "connection": a.get("connection")})
        bad += bool(err)
    print(f"disconnected {len(lst) - bad} of {len(lst)} app connections")
    if bad:
        sys.exit(1)


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
    if c == "off" and len(argv) == 2:
        return cmd_off(argv[1])
    if c == "apps":
        print(len(apps()))
        return None
    if c == "other-apps":
        print(json.dumps(sorted(str(a.get("name")) for a in apps() if isinstance(a, dict) and a.get("this_connection") is not True)))
        return None
    if c == "disconnect-all":
        return cmd_disconnect_all()
    die("unknown command", 2)


if __name__ == "__main__":
    main(sys.argv[1:])
