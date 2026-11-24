#!/usr/bin/env python3
"""XLI-388 end-to-end driver (used by test/e2e/typed-kinds.e2e.test.ts against x-live's real server, see
test/test_typed_kinds_server.py). It runs the plugin's own code from a real person status reply to the request the
plugin sends: the local copy, the numbered list, the person's typed prompt (the UserPromptSubmit hook, then the typed
transcript entry as Claude Code writes it), the transcript checker and the proof. Only the transport is replaced: the
request is written out for the server test to send through the real door.
Input (argv[1], a JSON file of this user): {"workbook_id", "status": <the GET /api/v1/person/status reply>, "answer_key",
"grant", "token", "typed": [{"instance", "say"}]} where say has "{n}" for the number the plugin gave that instance.
Output (argv[2]): {"requests": [{"instance", "typed", "method", "path", "bearer", "body"}], "shown": <the list>}.
Run: python3 -I test/e2e_typed_kinds_driver.py IN OUT"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401  (a throwaway HOME and datastash folder; no network)
import test_status  # noqa: E402
from test_status import ds  # noqa: E402


def main(argv):
    with open(argv[1]) as f:
        cfg = json.load(f)
    test_status.WB = cfg["workbook_id"]  # the folder's agent workbook is the server's
    out = {"requests": [], "shown": None}

    class Drive(test_status.Base):
        def runTest(self):
            pass

    t = Drive()
    t.setUp()
    try:
        ds.read_answer_key = lambda: (cfg["answer_key"], cfg["grant"])
        ds.answer_key_kept = lambda: True
        ds.login_token = lambda force=False: cfg["token"]
        ds._open_browser = lambda url: True
        sent = []

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if method == "GET" and path.startswith(ds.PERSON_STATUS_PATH + "?"):
                return 200, "application/json", json.dumps(cfg["status"])
            sent.append({"method": method, "path": path, "bearer": key, "body": body})
            return 200, "application/json", json.dumps({"ok": True})
        ds.http = http
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": cfg["workbook_id"]})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        for item in cfg["typed"]:
            out["shown"] = t.prompt("status of all my agents")
            nums = ds.read_view("sess1")["numbers"]
            # datastash's own ask or the default taken (a default's instance is also its agent's ask, listed there too)
            hits = [int(k) for k, v in nums.items() if isinstance(v, dict) and v.get("instance") == item["instance"]
                    and v.get("person_ask")]
            if len(hits) != 1:
                raise SystemExit(f"instance {item['instance']} is not numbered once in the list: {hits}")
            typed = item["say"].format(n=hits[0])
            before = len(sent)
            t.prompt(typed)
            for r in sent[before:]:
                out["requests"].append(dict(r, instance=item["instance"], typed=typed))
    finally:
        t.tearDown()
    fd = os.open(argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
