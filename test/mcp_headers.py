#!/usr/bin/env python3
"""Test-only headersHelper for Claude Code's MCP config: prints the Authorization header from a key file.
Claude Code reads this output; never run it where the output is shown."""
import json, os, sys
print(json.dumps({"Authorization": "Bearer " + open(os.path.expanduser(sys.argv[1])).read().strip()}))
