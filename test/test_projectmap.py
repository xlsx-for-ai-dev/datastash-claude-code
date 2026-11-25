#!/usr/bin/env python3
"""Offline tests for XLI-307 (plugin 0.7.0): the project map.
- The listener builds a code map of each linked git folder in plain code (no AI): files with a one-line purpose,
  exported functions and classes, HTTP routes (express, fastify, flask, fastapi, rails, next), env var reads, MCP tool
  names and migrations, each with file:line. It rebuilds only when HEAD's sha changes.
- The map goes into the agent workbook as the keyed tab "Project map: <repo>" (key path|kind|name) and a local copy;
  another agent on the same project reads the tab and does not rebuild when the sha matches.
- `datastash where <words>` and `datastash file <path>` answer from the local copy.
- Never mapped: .env*, *.key, *.pem, secrets, anything .gitignore names, node_modules, vendor, build output, files over
  about 1 MB. Nothing of a file is kept beyond its one-line purpose and the names.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_projectmap.py"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds, WB, BIN, row  # noqa: E402  (the same harness, plugin copy and fixtures)
import harness  # noqa: E402  (tmpdir: a test's own temp folders go when it ends)

BODY_MARKER = "BODY_ONLY_TEXT_7f3a"  # appears only inside function bodies and values: must never reach the map
ENV_VALUE = "sk_live_ENVVALUE_should_never_leave"

FILES = {
    "src/auth.ts": (
        "/**\n"
        " * src/auth.ts — the login check for every request. Second sentence is dropped.\n"
        " */\n"
        "import x from 'y';\n"
        "\n"
        "/** Checks the session cookie. */\n"
        "export async function checkLogin(req) {\n"
        f"  const s = '{BODY_MARKER}';\n"
        "  return process.env.SESSION_SECRET_NAME && process.env['AUTH_TTL'];\n"
        "}\n"
        "\n"
        "export class SessionStore {}\n"
        "export const LOGIN_PATH = '/login';\n"
        "app.post('/login', checkLogin);\n"
        "router.get(\"/users/:id\", show);\n"
        "fastify.route({ method: 'DELETE', url: '/sessions/:id', handler: h });\n"
        "app.get(LOGIN_PATH, page);\n"
        "const n = intEnv('LEDGER_FREE_ROWS', 5000);\n"
        "axios.get('/not/a/route');\n"
    ),
    "src/mcp.ts": (
        "// MCP server: the search tools.\n"
        "server.tool('search_docs', {}, h);\n"
        "server.registerTool(\"open_doc\", {}, h);\n"
        "export const tools = [{ name: 'list_items', inputSchema: {} }];\n"
        "export const FETCH_TOOL_NAME = 'fetch_page';\n"
    ),
    "app/main.py": (
        '#!/usr/bin/env python3\n'
        '"""Flask and FastAPI entry points. More text here."""\n'
        'import os\n'
        '\n'
        '@app.route("/health", methods=["GET", "POST"])\n'
        'def health():\n'
        f'    return "{BODY_MARKER}"\n'
        '\n'
        '@api.get("/items/{item_id}")\n'
        'async def read_item(item_id):\n'
        '    """Reads one item."""\n'
        '    return os.environ.get("ITEMS_DB") or os.getenv("CACHE_URL") or os.environ["REGION"]\n'
        '\n'
        'class Order:\n'
        '    pass\n'
        '\n'
        'def _private():\n'
        '    pass\n'
        '\n'
        '@mcp.tool()\n'
        'def lookup_order(order_id):\n'
        '    pass\n'
    ),
    "config/routes.rb": (
        "# Rails routes.\n"
        "Rails.application.routes.draw do\n"
        "  get 'about', to: 'pages#about'\n"
        "  resources :orders\n"
        "  post '/webhooks/stripe', to: 'hooks#stripe'\n"
        "end\n"
    ),
    "app/models/user.rb": "# A user account.\nclass User < ApplicationRecord\n  def full_name\n    ENV['NAME_FORMAT']\n  end\nend\n",
    "app/api/users/route.ts": "// Users API.\nexport async function GET(req) {}\nexport const POST = async () => {};\n",
    "pages/api/hello.ts": "// Says hello.\nexport default function handler(req, res) {}\n",
    "migrations/001_init.sql": "-- Creates the users table.\nCREATE TABLE users (id int);\n",
    "db/migrate/20240101_create_orders.rb": "# Orders table.\nclass CreateOrders < ActiveRecord::Migration[7.0]\nend\n",
    "go/server.go": "// Package server serves HTTP.\npackage server\n\n// Start runs it.\nfunc Start() {}\nfunc helper() {}\ntype Config struct{}\nvar p = os.Getenv(\"PORT\")\n",
    "README.md": "# Demo project\n\nSome words.\n",
    "src/token.ts": "// key " + "A" * 40 + " lives here.\nexport const x = 1;\n",
    # never mapped
    ".env": f"API_KEY={ENV_VALUE}\n",
    ".env.local": f"OTHER={ENV_VALUE}\n",
    "certs/server.key": f"{ENV_VALUE}\n",
    "certs/server.pem": f"{ENV_VALUE}\n",
    "config/secrets.yml": f"# secret store\nkey: {ENV_VALUE}\n",
    "secrets/loader.ts": f"// loads {ENV_VALUE}\nexport const a = 1;\n",
    "node_modules/lib/index.js": "// a dependency\nexport function dep() {}\n",
    "vendor/gem/x.rb": "# vendored\nclass Vendored; end\n",
    "dist/bundle.js": "// built\nexport function built() {}\n",
    "build/out.js": "// built\nexport function built2() {}\n",
    "ignored/local.ts": "// force-added but ignored\nexport function ignoredFn() {}\n",
}
NEVER = (".env", ".env.local", "certs/server.key", "certs/server.pem", "config/secrets.yml", "secrets/loader.ts",
         "node_modules/lib/index.js", "vendor/gem/x.rb", "dist/bundle.js", "build/out.js", "ignored/local.ts",
         "big/huge.ts")


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           "-C", cwd] + list(args), capture_output=True, text=True, check=True).stdout.strip()


def make_repo(folder, files=FILES):
    git(folder, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    os.makedirs(os.path.join(folder, "big"), exist_ok=True)
    with open(os.path.join(folder, "big", "huge.ts"), "w") as f:
        f.write("// huge\nexport function huge() {}\n" + "x" * 1_200_000)
    with open(os.path.join(folder, ".gitignore"), "w") as f:
        f.write("ignored/\n")
    git(folder, "add", "-A")
    git(folder, "add", "-f", "ignored/local.ts", ".env", ".env.local")
    git(folder, "commit", "-q", "-m", "one")
    return git(folder, "rev-parse", "HEAD")


def by(entries, kind, name=None, path=None):
    return [e for e in entries if e[2] == kind and (name is None or e[3] == name) and (path is None or e[0] == path)]


class MapBase(Base):
    def setUp(self):
        super().setUp()
        self.head = make_repo(self.folder)
        self.tab = {}  # the fake server tab: key -> cells
        self.tab_name = None
        self.mcp_calls = []
        self.reads = 0
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            if path == ds.MCP_PATH:
                name, args = body["params"]["name"], body["params"]["arguments"]
                self.mcp_calls.append((name, args))
                self.tab_name = args["tab"]
                if name == "ledger_rows_upsert":
                    self.assertEqual(args.get("key"), "key")
                    for r in args["rows"]:
                        self.tab[r["key"]] = dict(r)
                    out = {"workbook_id": WB, "rows_written": len(args["rows"])}
                elif name == "ledger_agent_workbook":
                    self.assertEqual((args["action"], args["access"]), ("share", "write"))
                    out = {"shared": args["agents"]}
                elif name == "ledger_rows_delete":
                    for k in args["keys"]:
                        self.tab.pop(k, None)
                    out = {"deleted": len(args["keys"])}
                else:
                    raise AssertionError(name)
                return 200, "application/json", json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                    "content": [{"type": "text", "text": json.dumps(out)}]}})
            if method == "GET" and path.startswith(ds.ROWS_PATH + "?"):
                self.reads += 1
                q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
                if self.tab_name is not None:
                    self.assertEqual(q["tab"], self.tab_name)
                if not self.tab:
                    raise ds.ApiError(404, {"code": "not_found"})
                rows = [{"row_id": i + 1, "cells": dict(c), "row_version": 1} for i, c in enumerate(self.tab.values())]
                return 200, "application/json", json.dumps({"rows": rows, "cursor": "9", "more": False})
            return base(method, path, key, body, form, timeout)
        ds.http = http
        self.agent = ds.all_agents()[0]

    def local(self):
        return ds.map_read_local(self.folder)


class Build(MapBase):
    def test_build_finds_purposes_exports_routes_env_tools_and_migrations(self):
        m = ds.map_build(self.folder, self.head)
        e = m["entries"]
        self.assertEqual(m["sha"], self.head)
        f = by(e, "file", path="src/auth.ts")[0]
        self.assertEqual((f[1], f[4]), (1, "the login check for every request."))
        self.assertEqual(by(e, "file", path="app/main.py")[0][4], "Flask and FastAPI entry points.")
        self.assertEqual(by(e, "file", path="README.md")[0][4], "Demo project")
        self.assertEqual(by(e, "export", "checkLogin")[0][:2], ["src/auth.ts", 7])
        self.assertEqual(by(e, "export", "checkLogin")[0][4], "Checks the session cookie.")
        self.assertEqual(by(e, "export", "SessionStore")[0][1], 12)
        self.assertEqual(by(e, "export", "read_item")[0][4], "Reads one item.")
        self.assertTrue(by(e, "export", "Order", "app/main.py"))
        self.assertFalse(by(e, "export", "_private"))
        self.assertTrue(by(e, "export", "User", "app/models/user.rb"))
        self.assertEqual(by(e, "export", "Start")[0][4], "Start runs it.")
        self.assertFalse(by(e, "export", "helper"))
        routes = {(r[3], r[0], r[1]) for r in by(e, "route")}
        for want in [("POST /login", "src/auth.ts", 14), ("GET /users/:id", "src/auth.ts", 15),
                     ("DELETE /sessions/:id", "src/auth.ts", 16), ("GET /login", "src/auth.ts", 17),
                     ("GET /health", "app/main.py", 5), ("POST /health", "app/main.py", 5),
                     ("GET /items/{item_id}", "app/main.py", 9), ("GET /about", "config/routes.rb", 3),
                     ("RESOURCES /orders", "config/routes.rb", 4), ("POST /webhooks/stripe", "config/routes.rb", 5),
                     ("GET /api/users", "app/api/users/route.ts", 2), ("POST /api/users", "app/api/users/route.ts", 3),
                     ("ANY /api/hello", "pages/api/hello.ts", 1)]:
            self.assertIn(want, routes)
        self.assertFalse([r for r in routes if "/not/a/route" in r[0]])
        envs = {(r[3], r[0]) for r in by(e, "env")}
        for want in [("SESSION_SECRET_NAME", "src/auth.ts"), ("AUTH_TTL", "src/auth.ts"), ("LEDGER_FREE_ROWS", "src/auth.ts"),
                     ("ITEMS_DB", "app/main.py"), ("CACHE_URL", "app/main.py"), ("REGION", "app/main.py"),
                     ("NAME_FORMAT", "app/models/user.rb"), ("PORT", "go/server.go")]:
            self.assertIn(want, envs)
        self.assertEqual(by(e, "env", "AUTH_TTL")[0][1], 9)
        tools = {(r[3], r[0]) for r in by(e, "tool")}
        for want in [("search_docs", "src/mcp.ts"), ("open_doc", "src/mcp.ts"), ("list_items", "src/mcp.ts"),
                     ("fetch_page", "src/mcp.ts"), ("lookup_order", "app/main.py")]:
            self.assertIn(want, tools)
        migs = {(r[3], r[0]) for r in by(e, "migration")}
        self.assertIn(("001_init.sql", "migrations/001_init.sql"), migs)
        self.assertIn(("20240101_create_orders.rb", "db/migrate/20240101_create_orders.rb"), migs)
        self.assertEqual(by(e, "migration", "001_init.sql")[0][4], "Creates the users table.")

    def test_never_maps_secrets_ignored_dependencies_build_output_or_big_files(self):
        m = ds.map_build(self.folder, self.head)
        paths = {x[0] for x in m["entries"]}
        for p in NEVER:
            self.assertNotIn(p, paths)
        self.assertFalse(by(m["entries"], "export", "ignoredFn"))
        text = json.dumps(m)
        self.assertNotIn(ENV_VALUE, text)
        self.assertNotIn(BODY_MARKER, text)
        for p in (".env.production", "a/.env", "x.key", "home/.aws/credential_store", ".npmrc", "id_ed25519",
                  "deep/cert.pem", "config/secrets.json", "my_secret.py",
                  "node_modules/a.js", "a/vendor/b.go", "dist/x.js", "build/y.js", "out/z.js", "target/debug/q.rs",
                  ".next/s.js", "coverage/c.js", "__pycache__/m.py"):
            self.assertTrue(ds.map_skip(p), p)
        for p in ("src/env.ts", "src/keys.ts", "src/builder.ts", "lib/environment.py"):
            self.assertFalse(ds.map_skip(p), p)

    def test_a_one_megabyte_line_with_no_period_maps_in_under_a_second(self):
        line = "envenv" + "word " * 199_000  # about 1 MB, one line, no sentence end
        for rel in ("notes.txt", "long.md", "src/long.ts", "app/long.py"):
            with open(os.path.join(self.folder, rel), "w") as f:
                f.write(("# " if rel.endswith((".md", ".py")) else "// " if rel.endswith(".ts") else "") + line + "\n"
                        + ("export function after() {}\n" if rel.endswith(".ts") else ""))
        git(self.folder, "add", "-A")
        git(self.folder, "commit", "-q", "-m", "long lines")
        head = git(self.folder, "rev-parse", "HEAD")
        for rel in ("notes.txt", "src/long.ts"):
            t = time.time()
            with open(os.path.join(self.folder, rel)) as f:
                ds._map_file(rel, f.read(), [], {}, [])
            self.assertLess(time.time() - t, 1, rel)
        t = time.time()
        self.assertLessEqual(len(ds._map_sentence(line)), 200)
        self.assertLess(time.time() - t, 1)
        t = time.time()
        m = ds.map_build(self.folder, head)
        self.assertLess(time.time() - t, 5)
        self.assertEqual(by(m["entries"], "export", "after")[0][:2], ["src/long.ts", 2])  # line numbers kept
        self.assertTrue(all(len(e[4]) <= 200 for e in m["entries"]))

    def test_contents_are_read_in_small_batches_with_the_same_map(self):
        whole = ds.map_build(self.folder, self.head)["entries"]
        calls, real = [], ds._map_git
        ds._map_git = lambda root, args, inp=None, timeout=120: calls.append(args[0]) or real(root, args, inp, timeout)
        old = ds.MAP_BATCH_FILES
        ds.MAP_BATCH_FILES = 3
        try:
            small = ds.map_build(self.folder, self.head)["entries"]
        finally:
            ds.MAP_BATCH_FILES, ds._map_git = old, real
        self.assertGreater(calls.count("cat-file"), 3)
        self.assertEqual(small, whole)
        self.assertEqual([len(b) for b in ds._map_batches([("a", "x", 5_000_000)] * 3)], [1, 1, 1])

    def test_a_secret_looking_word_in_a_purpose_is_cut(self):
        m = ds.map_build(self.folder, self.head)
        purpose = by(m["entries"], "file", path="src/token.ts")[0][4]
        self.assertNotIn("A" * 40, purpose)
        self.assertIn("lives here", purpose)


class Refresh(MapBase):
    def test_builds_once_per_head_and_again_after_a_commit(self):
        built = []
        real = ds.map_build
        ds.map_build = lambda root, sha: built.append(sha) or real(root, sha)
        try:
            self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
            self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "fresh")
            self.assertEqual(built, [self.head])
            with open(os.path.join(self.folder, "src", "new.ts"), "w") as f:
                f.write("// A new file.\nexport function brandNew() {}\n")
            git(self.folder, "add", "-A")
            git(self.folder, "commit", "-q", "-m", "two")
            head2 = git(self.folder, "rev-parse", "HEAD")
            self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
            self.assertEqual(built, [self.head, head2])
        finally:
            ds.map_build = real
        self.assertEqual(self.local()["sha"], head2)
        self.assertTrue(by(self.local()["entries"], "export", "brandNew"))

    def test_the_tab_gets_keyed_rows_and_the_head_row_last(self):
        ds.map_refresh(self.agent, self.folder, "ldgr_ag_test")
        repo = os.path.basename(self.folder)
        self.assertEqual(self.tab_name, f"Project map: {repo}")
        self.assertEqual(self.mcp_calls[-1][1]["rows"][-1]["key"], ds.MAP_META_KEY)
        self.assertEqual(self.tab[ds.MAP_META_KEY]["sha"], self.head)
        row = self.tab["src/auth.ts|export|checkLogin"]
        self.assertEqual((row["path"], row["line"], row["kind"], row["name"]), ("src/auth.ts", 7, "export", "checkLogin"))
        self.assertEqual(set(row), {"key", "path", "line", "kind", "name", "purpose"})
        self.assertIn("src/auth.ts|file|auth.ts", self.tab)
        self.assertNotIn(ENV_VALUE, json.dumps(self.tab))
        self.assertTrue(self.local()["shared"])

    def test_a_second_agent_reads_the_shared_tab_and_does_not_rebuild(self):
        ds.map_refresh(self.agent, self.folder, "ldgr_ag_test")
        os.replace(ds.map_local_path(self.folder), os.path.join(ds.CONF, f"old-map-{time.time()}"))
        n_calls = len(self.mcp_calls)
        real = ds.map_build
        ds.map_build = lambda root, sha: self.fail("rebuilt although the tab has this sha")
        try:
            self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_other"), "tab")
        finally:
            ds.map_build = real
        self.assertEqual(len(self.mcp_calls), n_calls)  # nothing written
        loc = self.local()
        self.assertEqual(loc["sha"], self.head)
        self.assertEqual(by(loc["entries"], "export", "checkLogin")[0][:2], ["src/auth.ts", 7])
        self.assertFalse(by(loc["entries"], "meta"))

    def test_a_new_commit_sends_only_changed_rows_and_removes_gone_ones(self):
        ds.map_refresh(self.agent, self.folder, "ldgr_ag_test")
        self.mcp_calls.clear()
        os.replace(os.path.join(self.folder, "go", "server.go"), os.path.join(ds.CONF, f"old-go-{time.time()}"))
        git(self.folder, "add", "-A")
        git(self.folder, "commit", "-q", "-m", "drop go")
        self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
        deleted = [k for n, a in self.mcp_calls if n == "ledger_rows_delete" for k in a["keys"]]
        self.assertIn("go/server.go|export|Start", deleted)
        sent = [r["key"] for n, a in self.mcp_calls if n == "ledger_rows_upsert" for r in a["rows"]]
        self.assertEqual(sent, [ds.MAP_META_KEY])
        self.assertNotIn("go/server.go|export|Start", self.tab)

    def test_the_maker_shares_the_tab_with_the_other_agents_on_the_project_once(self):
        repo = os.path.basename(self.folder)
        ds.save_status_cache(WB, [row("api", "x", project=repo), row("docs", "y", project="other")], time.time(), merge=False)
        ds.map_refresh(self.agent, self.folder, "ldgr_ag_test")
        shares = [a for n, a in self.mcp_calls if n == "ledger_agent_workbook"]
        self.assertEqual(len(shares), 1)
        self.assertEqual((shares[0]["agents"], shares[0]["tab"]), (["api"], f"Project map: {repo}"))
        self.assertEqual(self.local()["shared_with"], ["api"])
        with open(os.path.join(self.folder, "README.md"), "a") as f:
            f.write("more\n")
        git(self.folder, "commit", "-q", "-am", "three")
        ds.map_refresh(self.agent, self.folder, "ldgr_ag_test")
        self.assertEqual(len([1 for n, _ in self.mcp_calls if n == "ledger_agent_workbook"]), 1)

    def test_no_upload_for_a_folder_the_person_did_not_keep(self):
        ds.record_left_off([self.folder])
        self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
        self.assertEqual(self.mcp_calls, [])
        self.assertEqual(self.reads, 0)
        self.assertFalse(self.local()["shared"])

    def test_no_key_builds_locally_only(self):
        self.assertEqual(ds.map_refresh(self.agent, self.folder, None), "built")
        self.assertEqual(self.mcp_calls, [])
        self.assertEqual(self.local()["sha"], self.head)

    def test_not_a_git_folder_does_nothing(self):
        plain = os.path.realpath(harness.tmpdir(self, prefix="plain-", dir=ds.CONF))
        self.assertEqual(ds.map_refresh(self.agent, plain, "ldgr_ag_test"), "none")

    def test_the_listener_thread_maps_each_linked_folder_and_never_for_the_person(self):
        import threading
        stop = threading.Event()
        stop.set()  # one pass, then it ends
        ds.map_refresher(self.agent, {"key": None}, stop)
        self.assertEqual(self.local()["sha"], self.head)
        self.assertIsNone(ds.start_map_refresher({"slug": ds.PERSON_SLUG, "name": "person", "workbook_id": WB}, {}))

    def test_local_copy_is_private(self):
        ds.map_refresh(self.agent, self.folder, None)
        self.assertEqual(os.stat(ds.map_local_path(self.folder)).st_mode & 0o777, 0o600)


class MapPaging(MapBase):
    """XLI-407: the map tab is read to its last page; no page count stops it."""
    def test_more_than_10000_pages_are_all_read(self):
        n = 10_005

        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
            page = int(q.get("after", "0"))
            rows = [{"row_id": page + 1, "cells": {"key": f"k{page}", "v": page}, "row_version": 1}]
            return 200, "application/json", json.dumps({"rows": rows, "cursor": str(page + 1), "more": page + 1 < n})
        ds.http = http
        out = ds.map_read_tab(self.agent, "ldgr_ag_test", "Map")
        self.assertEqual(len(out), n)
        self.assertEqual(out[f"k{n - 1}"]["v"], n - 1)

    def test_a_cursor_the_server_already_gave_raises_instead_of_looping(self):
        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            return 200, "application/json", json.dumps({"rows": [], "cursor": "7", "more": True})
        ds.http = http
        with self.assertRaises(ds.ApiError):
            ds.map_read_tab(self.agent, "ldgr_ag_test", "Map")


class Where(MapBase):
    def setUp(self):
        super().setUp()
        cwd = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, cwd)

    def run_bin(self, *args, cwd=None):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1")
        t = time.time()
        r = subprocess.run([sys.executable, "-I", BIN] + list(args), capture_output=True, text=True, env=env,
                           cwd=cwd or self.folder, timeout=20)
        return r, time.time() - t

    def test_where_answers_with_file_line_and_purpose(self):
        ds.map_refresh(self.agent, self.folder, None)
        rc, out, _ = self.run_cmd(ds.cmd_where, ["login", "check"])
        self.assertEqual(rc, 0)
        first = out.splitlines()[0]
        self.assertIn("src/auth.ts:7", first)
        self.assertIn("checkLogin", first)
        self.assertIn("Checks the session cookie.", first)
        rc, out, _ = self.run_cmd(ds.cmd_where, ["ITEMS_DB"])
        self.assertIn("app/main.py:12", out)
        rc, out, _ = self.run_cmd(ds.cmd_where, ["zzz-nothing-like-this"])
        self.assertEqual(rc, 1)
        self.assertIn("grep", out)

    def test_where_from_a_subfolder_and_as_a_command(self):
        ds.map_refresh(self.agent, self.folder, None)
        r, took = self.run_bin("where", "search_docs", cwd=os.path.join(self.folder, "src"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("src/mcp.ts:2", r.stdout)
        self.assertLess(took, 3)

    def test_where_with_no_copy_never_waits_and_maps_in_the_background(self):
        real = ds.map_build
        ds.map_build = lambda root, sha: self.fail("where mapped in the foreground")
        try:
            t = time.time()
            rc, out, _ = self.run_cmd(ds.cmd_where, ["Order"])
            self.assertLess(time.time() - t, 1)
            self.assertEqual(rc, 1)
            self.assertIn("being built now", out)
            rc, out, _ = self.run_cmd(ds.cmd_file, ["app/main.py"])
            self.assertIn("being built now", out)
        finally:
            ds.map_build = real
        self.assertEqual(self.spawned[0][-1], "map-refresh")
        self.assertEqual(self.run_cmd(ds.cmd_map_refresh, [])[0], 0)  # what the spawned helper runs
        self.assertEqual(self.mcp_calls, [])  # it never sends the map
        rc, out, _ = self.run_cmd(ds.cmd_where, ["Order"])
        self.assertEqual(rc, 0)
        self.assertIn("app/main.py:14", out)
        self.assertEqual(self.local()["sha"], self.head)

    def test_where_outside_a_git_folder(self):
        plain = os.path.realpath(harness.tmpdir(self, prefix="plain-", dir=ds.CONF))
        os.environ["CLAUDE_PROJECT_DIR"] = plain
        cwd = os.getcwd()
        os.chdir(plain)
        try:
            rc, out, _ = self.run_cmd(ds.cmd_where, ["x"])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 1)
        self.assertIn("not in a git project", out)

    def test_file_lists_the_purpose_and_what_is_in_it(self):
        ds.map_refresh(self.agent, self.folder, None)
        rc, out, _ = self.run_cmd(ds.cmd_file, ["src/auth.ts"])
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertIn("the login check for every request.", lines[0])
        self.assertTrue(any("src/auth.ts:7" in x and "checkLogin" in x for x in lines))
        self.assertTrue(any("POST /login" in x for x in lines))
        rc, out, _ = self.run_cmd(ds.cmd_file, ["nope.ts"])
        self.assertEqual(rc, 1)


class Launcher(MapBase):
    CLI = os.path.join(HERE, "..", "plugins", "datastash", "scripts", "cli", "datastash")

    def test_the_path_command_runs_the_same_helper(self):
        ds.map_refresh(self.agent, self.folder, None)
        env = dict(os.environ, DATASTASH_NO_NETWORK="1")
        r = subprocess.run([sys.executable, "-I", self.CLI, "where", "checkLogin"], capture_output=True, text=True,
                           env=env, cwd=self.folder, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("src/auth.ts:7", r.stdout)
        r = subprocess.run([sys.executable, "-I", self.CLI, "help"], capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("where WORDS", r.stdout)
        self.assertTrue(os.access(self.CLI, os.X_OK))

    def test_the_session_path_is_the_launcher_folder(self):
        env = os.path.join(ds.CONF, f"env-{time.time()}")
        os.environ["CLAUDE_ENV_FILE"] = env
        try:
            ds.put_on_path()
            with open(env) as f:
                line = f.read()
        finally:
            os.environ.pop("CLAUDE_ENV_FILE", None)
        self.assertIn(os.path.dirname(os.path.realpath(self.CLI)), line)


class Skill(unittest.TestCase):
    def test_skill_says_where_before_grep(self):
        with open(os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")) as f:
            text = f.read()
        self.assertIn("datastash where", text)
        self.assertIn("datastash file", text)
        i = text.index("datastash where")
        self.assertIn("grep", text[i - 400:i + 600])


if __name__ == "__main__":
    unittest.main()
