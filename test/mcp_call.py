#!/usr/bin/env python3
"""Test helper: one MCP tools/call on https://getdatastash.com/mcp signed with a key FILE. Prints the tool's answer
with any key redacted. Usage: mcp_call.py KEY_FILE TOOL 'JSON_ARGS'"""
import json, sys, urllib.request
key_file, tool, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")
key = open(key_file).read().strip()
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}).encode()
req = urllib.request.Request("https://getdatastash.com/mcp", data=body, method="POST", headers={
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + key})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
except urllib.error.HTTPError as e:
    text = json.dumps({"http_status": e.code})
text = text.replace(key, "***")
try:
    msg = json.loads(text)
    res = msg.get("result") or {}
    blocks = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
    out = {"isError": bool(res.get("isError")), "answer": json.loads(blocks[0]) if blocks and blocks[0].startswith("{") else (blocks[0] if blocks else msg)}
    print(json.dumps(out))
except ValueError:
    print(text[:2000])
