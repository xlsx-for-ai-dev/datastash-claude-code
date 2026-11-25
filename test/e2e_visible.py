"""e2e_visible.py: what the person sees in a Claude Code pane after a typed command (1.0.16, owner's Mac 11-25).

Reads a pane capture on stdin. Usage:
  e2e_visible.py said  <typed command> <expected text>   exit 0 when the model's own reply (a "●" text block, not a
                                                         tool call, its result or a hook note) after the LAST line that
                                                         shows the typed command holds the expected text, with no
                                                         step narration ("goes last", "will confirm", "will show")
  e2e_visible.py clean <typed command>                    exit 0 when nothing after that line reads as a failure:
                                                         "denied", "error" or "not run" (any case)
  e2e_visible.py shown <typed command> <expected text>   exit 0 when the expected text is anywhere after that line
Prints the block it matched (or the region's tail) for the log. Whitespace is normalized (wrapped lines read as one).
"""
import re
import sys

BULLET = "●"  # Claude Code's "●" before each assistant text block and each tool call
FAILURE = re.compile(r"denied|error|not run", re.I)
NARRATION = re.compile(r"goes last|will confirm|will show|the plugin's own line", re.I)


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def region(pane, typed):
    lines = pane.split("\n")
    at = None
    for i, l in enumerate(lines):
        if norm(l).lstrip("❯>").strip() == norm(typed):
            at = i
    return lines[at + 1:] if at is not None else []


def blocks(lines):
    """The model's text blocks: from a "●" line to the next "●", prompt or "⎿" line; tool calls (a "●" line naming a
    tool: "(MCP)", "plugin:", a "Name(" call) are not text."""
    out, cur = [], None
    for l in lines:
        s = l.strip()
        if s.startswith(BULLET):
            if cur is not None:
                out.append(cur)
            body = s[len(BULLET):].strip()
            tool = "(MCP)" in body or body.startswith("plugin:") or re.match(r"^[A-Za-z_][\w.:-]*\(", body)
            cur = None if tool else [body]
        elif s.startswith(("⎿", "❯", ">", "✻", "─", "╭", "╰", "│")) or not s:
            if cur is not None and s:
                out.append(cur)
                cur = None
        elif cur is not None:
            cur.append(s)
    if cur is not None:
        out.append(cur)
    return [norm(" ".join(b)) for b in out]


def main(argv):
    pane = sys.stdin.read()
    mode, typed = argv[1], argv[2]
    reg = region(pane, typed)
    if not reg:
        print(f"(the pane does not show the typed {typed!r})")
        return 1
    if mode == "said":
        want = norm(argv[3])
        for b in reversed(blocks(reg)):
            if want in b:
                if NARRATION.search(b):
                    print("narrated: " + b[:600])
                    return 1
                print(b[:600])
                return 0
        print("model said: " + " | ".join(b[:200] for b in blocks(reg)[-4:]))
        return 1
    if mode == "shown":
        text = norm("\n".join(reg))
        return 0 if norm(argv[3]) in text else 1
    if mode == "clean":
        bad = [norm(l) for l in reg if FAILURE.search(l)]
        if bad:
            print(" | ".join(b[:200] for b in bad[:4]))
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
