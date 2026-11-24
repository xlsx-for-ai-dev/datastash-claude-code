#!/usr/bin/env python3
"""XLI-388 end to end against x-live's own server code: runs test/e2e/typed-kinds.e2e.test.ts inside an x-live checkout
(DATASTASH_SERVER_DIR: a checkout of senoff/x-live at #493 or later, with node_modules), on PGlite through the real
doors, where the plugin's own code (test/e2e_typed_kinds_driver.py) turns the person's real status reply and typed
prompts into the requests it sends. The test file is copied into the checkout's test/cases for the run and moved out
after. Skipped when DATASTASH_SERVER_DIR is not set (no server code here); never touches the network.
Run: DATASTASH_SERVER_DIR=/path/to/x-live python3 -I test/test_typed_kinds_server.py"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "e2e", "typed-kinds.e2e.test.ts")
NAME = "zz-datastash-plugin-typed-kinds.test.ts"


class ServerEndToEnd(unittest.TestCase):
    def test_plugin_requests_pass_the_real_verifier(self):
        server = os.environ.get("DATASTASH_SERVER_DIR", "").strip()
        if not server:
            self.skipTest("DATASTASH_SERVER_DIR is not set")
        answers = os.path.join(server, "src", "agents", "answers.ts")
        with open(answers) as f:
            self.assertIn("datastash-typed-undo-default/v1", f.read(), "the checkout is older than x-live #493")
        npx = shutil.which("npx")
        if npx is None:
            self.skipTest("no npx here")
        dest = os.path.join(server, "test", "cases", NAME)
        self.assertFalse(os.path.exists(dest), dest)
        shutil.copyfile(SRC, dest)
        try:
            env = dict(os.environ, DATASTASH_PLUGIN_DIR=os.path.dirname(HERE))
            r = subprocess.run([npx, "vitest", "run", f"test/cases/{NAME}"], cwd=server, env=env, capture_output=True,
                               text=True, timeout=900)
        finally:
            os.replace(dest, os.path.join(tempfile.mkdtemp(prefix="ds-e2e-done-"), NAME))  # moved out, never deleted
        tail = (r.stdout + r.stderr)[-6000:]
        self.assertEqual(r.returncode, 0, tail)
        self.assertIn("2 passed", r.stdout, tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
