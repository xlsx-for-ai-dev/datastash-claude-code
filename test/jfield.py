#!/usr/bin/env python3
"""Test helper: read JSON on stdin and print one field, with no eval.
  jfield.py get answer.workbook_id      the value at a dotted path
  jfield.py replies answer.messages ID [ID...]
                                        the ids that answered (reply_to) any of the given message ids, as a JSON list"""
import json
import sys


def at(d, path):
    for k in path.split("."):
        d = d[int(k)] if isinstance(d, list) else d[k]
    return d


d = json.load(sys.stdin)
mode, path, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
if mode == "get":
    print(at(d, path))
elif mode == "replies":
    want = {int(x) for x in rest}
    print(json.dumps(sorted({m.get("reply_to") for m in at(d, path) or [] if m.get("reply_to") in want})))
else:
    sys.exit(f"unknown mode {mode}")
