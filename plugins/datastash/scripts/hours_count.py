#!/usr/bin/env python3
"""Local estimate of the coding hours datastash would have saved last week, from this computer's own Claude Code
(and Claude Agent SDK) transcripts. Everything is read and judged here; only totals (numbers) ever leave this file.
No transcript text, path, command or name is stored or printed.

Method (a compact port of the research counter; every line is conservative, waiting on the person never counts):
  missed events    notices (background job, CI, agent finished, wake-up, mail) that landed while the agent was idle and
                   waited more than 5 min for its next move; each wait capped at 30 min; not when the agent had just
                   asked the person something
  repeated lookups measured tool time of network lookups (gh, curl, git fetch, issue-tracker reads, web fetch/search)
                   asked again within 10 min; code search and local file reads are left out; each call capped at 5 min
  subagent polling turns spent on "still running" notices and on duplicate "finished" notices (count x median turn)
  lost cards       work in flight that a notice named, then 2+ wake-ups passed with no touch: 5 min per high-confidence
                   episode, minus 5 min per missed event (overlap)
  self-answerable  asks the agent answered itself by a lookup, or the person said "find it yourself" (each capped 10 min)
  lapsed watchers  monitors that expired and were re-armed after more than 5 min: 5 min each

Python 3.8+, standard library only.
CLI: python3 hours_count.py --out PATH [--days 7] [--dirs A:B]
"""
import argparse
import calendar
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import shlex
import statistics
import sys
import tempfile
import time

FLOOR = 300           # seconds: gaps under this are reaction time; also the floor per lost card / lapsed watcher
EVENT_CAP = 1800      # seconds: one missed event counts at most this much (a closed laptop is not a missed event)
CALL_CAP = 300        # seconds: one network lookup counts at most this much
ASK_CAP = 600         # seconds: one self-answerable ask counts at most this much
REPEAT_WINDOW = 600   # seconds: "asked again within 10 minutes"
LOST_MIN_WAKES = 2
MARGIN = 3600         # lines this much older than the window are skipped unread (state older than that is dropped)
INFO_WAIT = 'agent waiting on its own work (info)'
INFO_DONE = 'agent finished, already handed back (info)'


# ---------------------------------------------------------------------------------------------
# Finding the Claude config folders on this computer
# ---------------------------------------------------------------------------------------------
def _has_transcripts(d):
    proj = os.path.join(d, 'projects')
    try:
        for e in os.scandir(proj):
            if e.name.endswith('.jsonl') and e.is_file():
                return True
            if e.is_dir():
                for f in os.scandir(e.path):
                    if f.name.endswith('.jsonl') or f.is_dir():
                        return True
    except OSError:
        pass
    return False


def find_config_dirs(home=None, env=None):
    """Every Claude config folder on the computer (the normal one, $CLAUDE_CONFIG_DIR, and harness folders)."""
    home = home or os.path.expanduser('~')
    env = os.environ if env is None else env
    found, seen = [], set()

    def add(p, need_projects):
        try:
            if not os.path.isdir(p):
                return
            if need_projects and not _has_transcripts(p):
                return
            rp = os.path.realpath(p)
        except OSError:
            return
        if rp not in seen:
            seen.add(rp)
            found.append(p)

    add(os.path.join(home, '.claude'), False)
    for p in (env.get('CLAUDE_CONFIG_DIR') or '').split(os.pathsep):
        if p.strip():
            add(os.path.expanduser(p.strip()), False)
    pats = ['.claude*', '.config/claude*', '*claude-config*', '.*claude-config*', '.config/*/claude*']
    for pat in pats:
        for p in sorted(glob.glob(os.path.join(home, pat))):
            add(p, True)
    try:
        top = [e.path for e in os.scandir(home) if e.is_dir() and not e.is_symlink()]
    except OSError:
        top = []
    for t in sorted(top):
        add(t, True)                       # ~/x and ~/.x that hold a projects/ folder
        try:
            for e in os.scandir(t):        # harness folders named like claude* one level further down
                if 'claude' in e.name.lower() and e.is_dir():
                    add(e.path, True)
        except OSError:
            pass
    return found


# ---------------------------------------------------------------------------------------------
# Network-lookup classifier (reduced): shape of a repeated question, never kept past this process
# ---------------------------------------------------------------------------------------------
_LOCAL_HEADS = {'cd', 'ls', 'pwd', 'echo', 'printf', 'sleep', 'true', 'false', 'test', '[', '[[', 'export', 'source', '.',
                'date', 'set', 'read', 'exit', 'cat', 'head', 'tail', 'jq', 'grep', 'egrep', 'rg', 'sed', 'awk', 'sort',
                'uniq', 'wc', 'cut', 'tr', 'tee', 'xargs', 'column', 'python3', 'python', 'node', 'base64', 'mktemp',
                'basename', 'dirname', 'wait', 'unset', 'local', 'then', 'do', 'done', 'fi', 'if', 'for', 'while', 'else'}
_WRAP = {'sudo', 'env', 'nohup', 'time', 'nice', 'timeout', 'command', 'exec', 'stdbuf', 'unbuffer'}
_GH_READ = {'view', 'list', 'ls', 'checks', 'diff', 'status', 'search'}
_LINEAR_READ = {'get', 'list', 'comments', 'attachments', 'teams', 'project', 'search', 'show', 'view'}
_LOCALHOST = re.compile(r'(?i)^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[?::1\]?)(:\d+)?$')


def _ph(tok):
    if re.match(r'https?://', tok or ''):
        return _url_shape(tok)
    if re.fullmatch(r'[A-Z][A-Z0-9]{1,6}-\d+', tok or ''):
        return '<card>'
    if re.fullmatch(r'#?\d+', tok or ''):
        return '<n>'
    if re.fullmatch(r'[0-9a-f]{7,64}', tok or ''):
        return '<sha>'
    return '<path>' if '/' in (tok or '') else '…'


def _safe_word(w):
    return bool(re.fullmatch(r'[a-z][a-z0-9_.-]{0,23}', w)) and not re.search(r'\d{3,}', w)


def _shape(tool, args, nverbs=2):
    verbs, pos, flags, after = [], [], set(), False
    for a in args:
        if a.startswith('-') and len(a) > 1:
            flags.add(a.split('=', 1)[0])
            after = '=' not in a
        elif after:
            after = False
        elif len(verbs) < nverbs and not pos and _safe_word(a):
            verbs.append(a)
        else:
            pos.append(_ph(a))
    return ' '.join([tool] + verbs + pos[:4] + sorted(flags)[:6])


def _url_shape(url):
    m = re.match(r'(?i)(https?)://([^/?#\s]+)([^?#\s]*)', url or '')
    if not m:
        return '<url>'
    host = m.group(2).split('@')[-1].lower()
    segs = []
    for s in m.group(3).split('/'):
        if s:
            ok = re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,29}', s) and len(re.findall(r'\d', s)) <= 2
            segs.append(s if ok else '<id>')
    return host + ('/' + '/'.join(segs[:8]) if segs else '')


def _host_local(url):
    m = re.match(r'(?i)https?://([^/?#\s]+)', url)
    return bool(m and _LOCALHOST.match(m.group(1).split('@')[-1]))


def _gh(a):
    if not a:
        return None
    if a[0] == 'api':
        method, path, body, i = 'GET', None, False, 1
        while i < len(a):
            x = a[i]
            if x in ('-X', '--method') and i + 1 < len(a):
                method, i = a[i + 1].upper(), i + 2
                continue
            if x in ('-f', '-F', '--field', '--raw-field', '--input'):
                body, i = True, i + 2
                continue
            if x in ('--jq', '-q', '-H', '--header', '--template', '-t', '--hostname', '--cache'):
                i += 2
                continue
            if not x.startswith('-') and path is None:
                path = x
            i += 1
        if method != 'GET' or body or path is None:
            return None
        segs = [s if re.fullmatch(r'[a-z][a-z_-]{0,29}', s) else '<id>' for s in path.split('?')[0].lstrip('/').split('/')]
        return 'gh api ' + '/'.join(segs[:8])
    verb = a[1] if len(a) > 1 and not a[1].startswith('-') else ''
    if '--watch' in a or '-w' in a or '--web' in a:
        return None
    if verb in _GH_READ or a[0] in ('search', 'status'):
        keep = [x for x in a if x not in ('--json', '-q', '--jq', '-R', '--repo', '--limit', '-L')]
        return _shape('gh', keep)
    return None


def _curl(a):
    method, url, body, i = None, None, False, 0
    while i < len(a):
        x = a[i]
        if x in ('-X', '--request') and i + 1 < len(a):
            method, i = a[i + 1].upper(), i + 2
            continue
        if x in ('-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '-F', '--form', '-T',
                 '--upload-file', '--json'):
            body, i = True, i + 2
            continue
        if x in ('-H', '--header', '-o', '--output', '-u', '--user', '-m', '--max-time', '--connect-timeout', '-A',
                 '-w', '-e', '-b', '-c'):
            i += 2
            continue
        if not x.startswith('-') and url is None and re.match(r'(?i)https?://', x):
            url = x
        i += 1
    if url is None or _host_local(url) or (method or ('POST' if body else 'GET')) not in ('GET', 'HEAD'):
        return None
    return 'curl ' + _url_shape(url)


def _stage_shape(toks):
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.match(r'[A-Za-z_]\w*=', t):
            i += 1
        elif t in _WRAP:
            i += 1
            while i < len(toks) and (toks[i].startswith('-') or re.fullmatch(r'\d+[smhd]?', toks[i])):
                i += 1
        else:
            break
    toks = toks[i:]
    if not toks:
        return None, False
    head, a = os.path.basename(toks[0]), toks[1:]
    local = head in _LOCAL_HEADS
    if head == 'gh':
        return _gh(a), local
    if head == 'curl':
        return _curl(a), local
    if head in ('linear-card', 'linear'):
        return (_shape(head, a, 1) if a and a[0] in _LINEAR_READ else None), local
    if head == 'git':
        while a and a[0].startswith('-'):
            a = a[2:] if a[0] in ('-C', '-c') else a[1:]
        return (_shape('git', a, 1) if a and a[0] in ('fetch', 'ls-remote') else None), local
    return None, local


def _split_stages(cmd):
    cmd = re.split(r'<<-?\s*[\'"]?\w+', cmd or '', maxsplit=1)[0]
    out, cur, q, n = [], [], None, len(cmd)
    for i, ch in enumerate(cmd):
        if q:
            if ch == q:
                q = None
        elif ch in '\'"':
            q = ch
        elif ch in '\n;|' or (ch == '&' and not (i and cmd[i - 1] in '>&') and not (i + 1 < n and cmd[i + 1] in '>')):
            out.append(''.join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append(''.join(cur))
    return [s.strip() for s in out if s.strip()]


def lookup_shapes(name, inp):
    """(network-lookup shapes of one tool call, number of non-trivial parts the call's time is shared over)."""
    if name == 'WebFetch':
        return ['webfetch ' + _url_shape(str(inp.get('url') or ''))], 1
    if name == 'WebSearch':
        return ['websearch ' + str(inp.get('query') or '').strip().lower()], 1
    if name != 'Bash':
        return [], 0
    shapes, weight = [], 0
    for st in _split_stages(str(inp.get('command') or '')):
        try:
            toks = shlex.split(st)
        except ValueError:
            toks = st.split()
        s, local = _stage_shape(toks)
        if s:
            shapes.append(s)
        if s or not local:
            weight += 1
    return shapes, max(weight, 1)


# ---------------------------------------------------------------------------------------------
# Turn-end judgement (reduced): did the agent ask the person?
# ---------------------------------------------------------------------------------------------
_CODE = re.compile(r'```.*?(?:```|$)|`[^`\n]*`|https?://\S+', re.S)
_SENT = re.compile(r'(?<=[.!?])\s+|\n+')
_ENUM = re.compile(r'(?im)^\s*(?:\(?[1-9a-d][.):]|option [a-d1-9]\b)')
_OPTION = re.compile(r'(?i)\b(options?|which|pick|choose|prefer|either|vote)\b')
_ASK_RX = re.compile(
    r"(?i)\b(waiting (for|on) (your|you\b|the owner|owner)|your (ok|okay|go[- ]ahead|approval|answer|decision|call|yes|"
    r"confirmation|input|choice|pick)\b|let me know\b|(shall|should) i\b|do you want\b|want me to\b|"
    r"would you (like|prefer|rather)\b|if you('d| would)? (like|want|prefer)\b|"
    r"(once|when|after) you (confirm|approve|reply|say|decide|pick|choose|answer)\b|needs? (your|you to)\b|"
    r"awaiting your\b|please (confirm|approve|choose|pick|reply|decide|answer|tell me)\b|up to you\b|yes\s*/\s*no\b)")
_SELF_REPLY = re.compile(
    r"(?i)(\b(find|look|check|figure|work) (it|that|this|them)?\s*(out|up)?\s*(yourself|on your own)\b|"
    r"\byou can (find|check|look|see|get) (it|that|this)\b|\b(don'?t|stop|no need to) ask(ing)? me\b|"
    r"\b(decide|answer) (it )?yourself\b|\byou know (this|that)\b)")
_TICKET = re.compile(r'\b([A-Z]{2,5}-\d{1,6})\b')
_NOT_TICKET = {'UTF', 'SHA', 'ISO', 'RFC', 'HTTP', 'TLS', 'AES', 'GPT', 'MD', 'CVE', 'ECMA', 'IEEE', 'TCP', 'UDP', 'SSL',
               'RSA', 'ASCII', 'LATIN', 'BASE'}
_PR = re.compile(r'(?i)(?:\bPR\s*#?\s*|\bpull/|\bpulls/|\bland-pr\s+)(\d{1,6})\b')
_PATHTOK = re.compile(r'[\w.~/-]*/[\w.-]+|\b[\w-]+\.(?:py|ts|js|md|json|sh|txt|log|yml|yaml|sql|toml)\b')
_FLIGHT = re.compile(r"(?i)\b(in[- ]flight|building|rebuilding|in progress|underway|in review|under review|queued|"
                     r"spawned|launched|landing|deploying|(builders?|forks?|agents?|helpers?|ci|checks|reviews?|"
                     r"reviewer|gate|lander|tests?)\b[^.\n]{0,40}\b(running|started|on it|working|pending))\b")
_CLOSE = re.compile(r"(?i)\b(merged|landed|deployed|done|closed|parked|dropped|cancell?ed|killed|abandoned|shipped|"
                    r"is live|now live|superseded)\b")
_CI = re.compile(r'(?i)\b(ci|checks?|workflow|actions|gh run|gate|lander|landing|pipeline)\b')
_SUMMARY = re.compile(r'<summary>(.*?)</summary>', re.S)
_EVENT = re.compile(r'<event>(.*?)</event>', re.S)
_TASKID = re.compile(r'<task-id>([^<]{1,64})</task-id>')
_QUOTED = re.compile(r'"([^"]{0,200})"')


def _h(s):
    return hashlib.blake2b(s.strip().encode('utf-8', 'replace'), digest_size=8).hexdigest()


def asked_the_person(text):
    t = _CODE.sub(' ', text or '').strip()
    if not t:
        return False
    sents = [x.strip().rstrip('*_"\')] ') for x in _SENT.split(t) if x.strip().strip('*_-#>')]
    return (any(x.endswith('?') for x in sents[-2:]) or bool(_ASK_RX.search(t))
            or (len(_ENUM.findall(t)) >= 2 and bool(_OPTION.search(t[-500:]))))


def _ids(text):
    out = {_h('id:' + x) for x in _TICKET.findall(text or '') if x.split('-')[0] not in _NOT_TICKET}
    return out | {_h('pr:' + x) for x in _PR.findall(text or '')}


def _keys(text):
    out = set(_ids(text))
    for x in _PATHTOK.findall(text or ''):
        base = x.rstrip('/.').split('/')[-1]
        if len(base) >= 4 and not base.isdigit():
            out.add(_h('f:' + base.lower()))
    return out


def _flight_marks(text):
    fl, cl = set(), set()
    for sent in _SENT.split(text or ''):
        ids = _ids(sent)
        if ids:
            if _FLIGHT.search(sent):
                fl |= ids
            elif _CLOSE.search(sent):
                cl |= ids
    return fl, cl


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


def _event_kind(text, okind):
    if okind == 'peer':
        return 'agent message (peer)'
    m = _SUMMARY.search(text)
    s = m.group(1) if m else ''
    m = _EVENT.search(text)
    e = m.group(1) if m else None
    if e is not None or s.startswith('Monitor event'):
        e = e or ''
        if e.lstrip().startswith('[Monitor expired'):
            return 'monitor expired'
        if _CI.search(e) or _CI.search(s):
            return 'CI / checks / gate'
        return 'monitor event'
    if s.startswith('Monitor') and 'stream ended' in s:
        return 'monitor stream ended'
    if 'waiting on its own background work' in s:
        return INFO_WAIT
    if s.startswith('Agent'):
        return 'agent finished or stopped'
    if s.startswith('Background command'):
        return 'CI / checks / gate' if _CI.search(s) else 'background job finished'
    return 'other notification'


# ---------------------------------------------------------------------------------------------
# One pass over one transcript file
# ---------------------------------------------------------------------------------------------
_TS = re.compile(r'"timestamp":"([^"]{19,40})"')
_TUID = re.compile(r'"tool_use_id":"([^"]+)"')


def _epoch(s):
    try:
        t = calendar.timegm((int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19])))
    except ValueError:
        return None
    if len(s) > 20 and s[19] == '.':
        digits = re.match(r'\d+', s[20:])
        if digits:
            t += float('0.' + digits.group(0))
    return float(t)


def _iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t))


def scan_file(path, since):
    """Partial result for one file (numbers only), or None when nothing in it falls inside the window."""
    sub = (os.sep + 'subagents' + os.sep) in path
    cutoff = _iso(since - MARGIN)
    r = {'sub': sub, 'person': False, 'hits': [], 'missed_n': 0, 'missed_s': 0.0, 'info': {INFO_WAIT: [], INFO_DONE: []},
         'lost_high': 0, 'lapses': 0, 'asks': []}
    try:
        fh = open(path, 'r', encoding='utf-8', errors='replace')
    except OSError:
        return None
    active, eps = False, set()
    calls = {}                      # tool_use id -> (ts, background, [hit], weight)
    queued, delivered_recent, awaiting, events = {}, {}, [], []
    pending, buf, asks = set(), [], []
    items, expired, handed_back = {}, [], set()
    last_out = last_input = None

    def touch(idset, ts, closing=()):
        for k in idset:
            it = items.get(k)
            if it is None:
                continue
            if it['flight'] and it['wakes'] >= LOST_MIN_WAKES and ts >= since:
                if it['id_wakes'] is not None and it['id_wakes'] >= LOST_MIN_WAKES:
                    r['lost_high'] += 1
            it['last'], it['wakes'], it['id_wakes'] = ts, 0, None
            if k in closing:
                it['flight'] = False

    def deliver(text, okind, ts, handback_from=None, mid_turn=False):
        if okind not in ('task-notification', 'peer'):
            return
        if handback_from:
            handed_back.add(handback_from)
        i = text.find('<agent-message') if okind == 'peer' else -1
        hk = _h(text[i:] if i >= 0 else text)
        if queued.get(hk):
            enq = queued[hk].pop(0)
        elif hk in delivered_recent and ts - delivered_recent[hk] < 3600:
            return
        else:
            enq = ts
        delivered_recent[hk] = ts
        kind = _event_kind(text, okind)
        if kind == 'agent finished or stopped':
            tid = _TASKID.search(text)
            if tid and tid.group(1) in handed_back:
                kind = INFO_DONE
        if kind == 'monitor expired':
            m = _SUMMARY.search(text)
            q = _QUOTED.search(m.group(1)) if m else None
            expired.append({'ts': enq, 'name': _h(q.group(1)) if q else None})
        elif enq >= since:
            asked = bool(not pending and buf and asked_the_person('\n'.join(buf)))
            ev = {'kind': kind, 'start': enq, 'delivered': ts, 'action': None, 'mid': mid_turn, 'person': asked}
            events.append(ev)
            awaiting.append(ev)
        if okind == 'task-notification':
            ids = _ids(text)
            for k, it in items.items():
                if it['flight']:
                    it['wakes'] += 1
                    if it['id_wakes'] is not None:
                        it['id_wakes'] += 1
                    elif k in ids:
                        it['id_wakes'] = 0

    with fh:
        for line in fh:
            is_res = '"tool_result"' in line
            if not (is_res or '"assistant"' in line or '"user"' in line or '"queue-operation"' in line
                    or '"queued_command"' in line):
                continue
            m = _TS.search(line)
            if not m or m.group(1)[:19] < cutoff:
                continue
            ts = _epoch(m.group(1))
            if ts is None:
                continue
            if ts >= since:
                active = True
            if is_res and '"type":"user"' in line:       # tool results: only timing and the pending set matter
                for tid in _TUID.findall(line):
                    pending.discard(tid)
                    c = calls.pop(tid, None)
                    if c is not None:
                        share = min(CALL_CAP, max(0.0, ts - c[0]) / c[3])
                        for h in c[2]:
                            h[2] = share
                continue
            if sub and '"tool_use"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            typ = d.get('type')
            if d.get('entrypoint'):
                eps.add(d['entrypoint'])
            side = bool(d.get('isSidechain'))
            if typ == 'queue-operation':
                c = d.get('content')
                if side or sub or not (isinstance(c, str) and c.lstrip().startswith(('<task-notification', '<agent-message'))):
                    continue
                if d.get('operation') == 'enqueue':
                    queued.setdefault(_h(c), []).append(ts)
                elif d.get('operation') == 'remove' and d.get('reason') in ('absorbed_mid_turn', 'delivered_to_agent'):
                    deliver(c, 'peer' if c.lstrip().startswith('<agent-message') else 'task-notification', ts,
                            mid_turn=d.get('reason') == 'absorbed_mid_turn')
                continue
            if typ == 'attachment':
                a = d.get('attachment')
                if not side and not sub and isinstance(a, dict) and a.get('type') == 'queued_command':
                    o, pr = a.get('origin'), a.get('prompt')
                    hb = o.get('senderTaskId') if isinstance(o, dict) and o.get('handback') else None
                    deliver(pr if isinstance(pr, str) else _text_of(pr), o.get('kind') if isinstance(o, dict) else o,
                            ts, handback_from=hb, mid_turn=True)
                continue
            msg = d.get('message') if isinstance(d.get('message'), dict) else {}
            content = msg.get('content')
            if typ == 'assistant':
                acted, texts = False, []
                for c in content if isinstance(content, list) else ():
                    if not isinstance(c, dict):
                        continue
                    if c.get('type') == 'text' and c.get('text'):
                        buf.append(c['text'])
                        texts.append(c['text'])
                        acted = True
                    elif c.get('type') == 'tool_use':
                        acted = True
                        pending.add(c.get('id'))
                        buf = []
                        name = c.get('name') or ''
                        inp = c.get('input') if isinstance(c.get('input'), dict) else {}
                        shapes = []
                        if name in ('Bash', 'WebFetch', 'WebSearch'):
                            shapes, weight = lookup_shapes(name, inp)
                        if ts >= since and shapes and c.get('id'):
                            hs = [[ts, _h(s), None] for s in shapes]
                            r['hits'].extend(hs)
                            if not inp.get('run_in_background'):
                                calls[c['id']] = (ts, False, hs, weight)
                        if side or sub:
                            continue
                        try:
                            raw = json.dumps(inp)
                        except (TypeError, ValueError):
                            raw = ''
                        ids = _ids(raw)
                        if name in ('Agent', 'Task') or (name == 'Skill' and 'build' in str(inp.get('skill', ''))):
                            for k in ids:
                                items[k] = {'flight': True, 'last': ts, 'wakes': 0, 'id_wakes': None}
                        touch(ids, ts)
                        if name == 'Monitor':
                            nh = _h(str(inp.get('description') or ''))
                            for x in list(expired):
                                if x['name'] in (nh, None):
                                    if x['ts'] >= since and ts - x['ts'] > FLOOR:
                                        r['lapses'] += 1
                                    expired.remove(x)
                        if asks and (name in ('Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch') or shapes):
                            keys = _keys(raw)
                            for a in asks:
                                a['looked'] = a['looked'] or ts
                                if keys & a['keys']:
                                    a['match'] = a['match'] or ts
                if side or sub:
                    continue
                if texts:
                    t = '\n'.join(texts)
                    fl, cl = _flight_marks(t)
                    touch(_ids(t), ts, closing=cl - fl)
                    for k in fl:
                        items[k] = {'flight': True, 'last': ts, 'wakes': 0, 'id_wakes': None}
                if acted:
                    for ev in awaiting:
                        if ev['action'] is None and ts >= ev['delivered']:
                            ev['action'] = ts
                    awaiting = [ev for ev in awaiting if ev['action'] is None]
                    last_out = ts
                continue
            if typ != 'user' or side or sub:
                continue
            if last_out is not None and (last_input is None or last_out > last_input) and not pending:
                raw_end = '\n'.join(buf)
                if asked_the_person(raw_end):
                    asks.append({'ts': last_out, 'keys': _keys(raw_end[-500:]), 'looked': None, 'match': None})
                else:
                    for a in list(asks):
                        if a['looked']:
                            if a['match'] and a['ts'] >= since:
                                r['asks'].append(min(ASK_CAP, max(0.0, last_out - a['ts'])))
                            asks.remove(a)
                buf = []
            last_input = ts
            origin = d.get('origin')
            okind = origin.get('kind') if isinstance(origin, dict) else origin
            text = _text_of(content)
            if okind in ('task-notification', 'peer'):
                deliver(text, okind, ts, origin.get('senderTaskId') if okind == 'peer' and origin.get('handback') else None)
                continue
            if 'origin' in d or 'promptSource' in d:
                typed = okind == 'human'
            else:
                typed = (isinstance(content, str) and not d.get('isMeta') and not d.get('isCompactSummary')
                         and not d.get('toolUseResult') and not text.lstrip().startswith('<'))
            if typed and not text.lstrip().startswith('[Request interrupted'):
                if asks and _SELF_REPLY.search(text):
                    for a in asks:
                        if a['ts'] >= since:
                            r['asks'].append(min(ASK_CAP, max(0.0, ts - a['ts'])))
                asks = []
    if not active:
        return None
    r['person'] = bool(eps) and not any(e.startswith('sdk') for e in eps)
    if not r['person']:
        r['asks'] = []
    for ev in events:
        if ev['action'] is None:
            continue
        gap = max(0.0, ev['action'] - ev['start'])
        if ev['kind'] in r['info']:
            r['info'][ev['kind']].append(gap)
        elif not ev['mid'] and not ev['person'] and gap > FLOOR:
            r['missed_n'] += 1
            r['missed_s'] += min(gap, EVENT_CAP)
    r['hits'] = [(h[0], h[1], h[2] or 0.0) for h in r['hits']]
    return r


# ---------------------------------------------------------------------------------------------
# Files, parallel run, merge
# ---------------------------------------------------------------------------------------------
def _scan_job(job):
    try:
        return job[0], scan_file(job[1], job[2])
    except Exception:  # one odd file must never stop the count
        return job[0], None


def _list_files(config_dirs, since):
    files, seen = [], set()
    for di, d in enumerate(config_dirs):
        for root, _, names in os.walk(os.path.join(d, 'projects')):
            for n in names:
                if not n.endswith('.jsonl'):
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if st.st_mtime < since or key in seen:
                    continue
                seen.add(key)
                files.append((p, st.st_size, di))
    return files


def _run(jobs, workers):
    if workers > 1 and len(jobs) > 1:
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
                return list(ex.map(_scan_job, jobs, chunksize=4))
        except Exception:
            pass
    return [_scan_job(j) for j in jobs]


def _med(xs):
    return statistics.median(xs) if xs else 0.0


def estimate(config_dirs=None, days=7, now=None, workers=None):
    """Hours saved over the last `days` days from the local transcripts, or None when there are none in the window."""
    t0 = time.time()
    if now is not None and hasattr(now, 'timestamp'):
        now = now.timestamp()
    now = float(now) if now is not None else t0
    since = now - days * 86400
    dirs = list(config_dirs) if config_dirs is not None else find_config_dirs()
    files = _list_files(dirs, since)
    if not files:
        return None
    files.sort(key=lambda f: -f[1])
    workers = workers if workers is not None else min(8, os.cpu_count() or 1)
    jobs = [(i, f[0], since) for i, f in enumerate(files)]
    parts = [(files[i], r) for i, r in _run(jobs, workers) if r is not None]
    if not parts:
        return None
    plans, agents, subs, hits = set(), 0, 0, []
    missed_n, missed_s, lost_high, lapses, asks = 0, 0.0, 0, 0, []
    info = {INFO_WAIT: [], INFO_DONE: []}
    for (path, size, di), r in parts:
        plans.add(di)
        agents += 0 if r['sub'] else 1
        subs += 1 if r['sub'] else 0
        hits.extend(r['hits'])
        missed_n += r['missed_n']
        missed_s += r['missed_s']
        lost_high += r['lost_high']
        lapses += r['lapses']
        asks.extend(r['asks'])
        for k in info:
            info[k].extend(r['info'][k])
    last, repeated = {}, 0.0
    for ts, shape, secs in sorted(hits):
        if shape in last and ts - last[shape] <= REPEAT_WINDOW:
            repeated += secs
        last[shape] = ts
    polling = sum(len(v) * _med(v) for v in info.values())
    lost = max(0, lost_high - missed_n) * FLOOR
    total = missed_s + repeated + polling + lost + sum(asks) + lapses * FLOOR
    return {'hours': round(total / 3600.0, 1), 'plans': len(plans), 'agents': agents, 'subagents': subs,
            'files': len(parts), 'bytes': sum(p[0][1] for p in parts), 'seconds': round(time.time() - t0, 2),
            'days': days,
            # card 8b ("show dds stats"): the same total split by category, in hours (numbers only)
            'by': {'missed': round(missed_s / 3600.0, 1), 'lookups': round(repeated / 3600.0, 1),
                   'polling': round(polling / 3600.0, 1), 'lost': round(lost / 3600.0, 1),
                   'asks': round(sum(asks) / 3600.0, 1), 'watchers': round(lapses * FLOOR / 3600.0, 1)}}


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def _write_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.hours-', suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description='Local estimate of coding hours datastash would have saved.')
    ap.add_argument('--out', required=True)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--dirs', default=None, help='config folders, separated by %r' % os.pathsep)
    a = ap.parse_args(argv)
    try:
        dirs = [p for p in a.dirs.split(os.pathsep) if p] if a.dirs else None
        res = estimate(dirs, days=a.days)
        _write_atomic(a.out, res if res is not None else {'none': True})
    except Exception as e:  # ordinary failure: no text, exit 0
        print('hours_count: failed (%s)' % type(e).__name__, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
