#!/usr/bin/env python3
"""Friction estimate for a Levain-on, auto-off Claude Code session, from transcripts. Aggregate counts only.

Model: default permission mode, no user Bash allow rule. Claude Code's built-in read-only Bash set runs with no prompt
in every mode (docs: permissions -> "Read-only commands"); a Bash call is `bash-native-ro` (silent) only when EVERY
subcommand is in that set and none carries a write/exec-capable form. The extra read-only-looking commands this script
used to assume (rg sort uniq ...) are not named by the docs: they are `bash-ro-unlisted`, counted as a prompt and reported
separately. Read/Glob/Grep/LS/NotebookRead never prompt inside the session cwd. Edit/Write/MultiEdit/NotebookEdit and
WebFetch/WebSearch are answerable by a today-shape consent grant (workspace edits, assumed granted), except an edit of
agent config (Levain tier T3, never grantable) or of a file outside the session cwd, which prompt.

Release tool, not a package module. Ported from project_memory/1017-authorityF/friction.py (10-16 run).
Prints JSON counts only; never prompt text, commands or paths from transcripts.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import pathlib
import re
import shlex
import statistics
import sys
import zoneinfo

TZ = zoneinfo.ZoneInfo('America/New_York')
IDLE = 600  # a gap over 10 min between tool calls is not active time
# Claude Code's built-in read-only set: the names its permissions page lists ("The set includes ...") plus the ones the
# same section names with flag conditions (sed, sort, file, ps); the flag conditions are applied below
NATIVE_RO = {'ls', 'cat', 'echo', 'pwd', 'head', 'tail', 'grep', 'find', 'wc', 'which', 'diff', 'stat', 'du', 'sed',
             'sort', 'file', 'ps'}
UNLISTED_RO = {'rg', 'uniq', 'cut', 'tr', 'jq', 'df', 'uptime', 'awk', 'basename', 'dirname', 'realpath', 'tree',
               'date'}
GIT_RO = {'status', 'log', 'diff', 'show', 'rev-parse', 'ls-files', 'grep', 'merge-base', 'ls-remote', 'cherry', 'blame',
          'describe', 'cat-file', 'check-ignore', 'rev-list', 'ls-tree', 'show-ref', 'shortlog', 'name-rev', 'for-each-ref'}
GIT_OPT_WITH_VALUE = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env'}
GIT_RISKY_CONFIG = ('pager', 'editor', 'fsmonitor', 'external', 'sshcommand', 'hookspath', 'alias.', 'askpass', 'textconv')
FIND_WRITE = {'-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprint0', '-fprintf', '-fls'}
GLOB_RISK = {'find', 'sort', 'sed', 'git'}
GRANTABLE = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'WebFetch', 'WebSearch'}
FILE_EDIT = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
PATH_READ = {'Read', 'Glob', 'Grep', 'LS', 'NotebookRead'}
SILENT = {'Read', 'Glob', 'Grep', 'LS', 'NotebookRead', 'TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet', 'ToolSearch'}
PROMPT_CLASSES = {'bash-other', 'bash-ro-unlisted', 'mcp', 'other-tool', 'agent-config', 'edit-outside-cwd',
                  'read-outside-cwd'}

_agent_config = None


def _targets_agent_config():
    """Levain's own T3 classifier (levain.consent.adapters.claude_code.targets_agent_config), imported from the repo this
    script sits in; never a hand copy of its path list."""
    global _agent_config
    if _agent_config is None:
        try:
            from levain.consent.adapters.claude_code import targets_agent_config
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from levain.consent.adapters.claude_code import targets_agent_config
        _agent_config = targets_agent_config
    return _agent_config


def _ts(o):
    d = datetime.datetime.fromisoformat(o['timestamp'].replace('Z', '+00:00'))
    if d.tzinfo is None:  # no offset: UTC
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


# ---- Bash classification -------------------------------------------------------------------------------------

def _scan(cmd):
    """Split a command on unquoted && || ; | & newline. Returns [(segment_text, has_unquoted_glob)] or None when the
    command is unparseable, substitutes ($( or backtick), groups ( ), or redirects anywhere but /dev/null."""
    segs, buf, glob_, q, i, n = [], [], False, None, 0, len(cmd)

    def flush():
        nonlocal buf, glob_
        t = ''.join(buf).strip()
        if t:
            segs.append((t, glob_))
        buf, glob_ = [], False

    while i < n:
        ch = cmd[i]
        if q == "'":
            buf.append(ch)
            q = None if ch == "'" else q
            i += 1
            continue
        if q == '"':
            if ch == '\\' and i + 1 < n:
                buf.append(cmd[i:i + 2])
                i += 2
                continue
            if ch == '`' or cmd.startswith('$(', i):
                return None
            buf.append(ch)
            q = None if ch == '"' else q
            i += 1
            continue
        if ch == '\\':
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        if ch in '\'"':
            q = ch
            buf.append(ch)
        elif ch == '`' or cmd.startswith('$(', i) or ch in '()':
            return None
        elif ch in '*?[':
            glob_ = True
            buf.append(ch)
        elif ch in ';\n':
            flush()
        elif ch == '|':
            i += 1 if cmd[i:i + 2] in ('||', '|&') else 0
            flush()
        elif ch == '&' and cmd[i + 1:i + 2] != '>':
            i += 1 if cmd[i + 1:i + 2] == '&' else 0
            flush()
        elif ch in '<>&':  # redirect (including &> and &>>)
            m = re.search(r'(^|\s)\d+$', ''.join(buf))
            if m:
                buf = list(''.join(buf)[:m.start() + len(m.group(1))])
            j = i
            while j < n and cmd[j] in '<>&|' and j - i < 3:
                j += 1
            op = cmd[i:j]
            if '<' in op:
                return None
            if op.endswith('&') and cmd[j:j + 1].isdigit() or (op.endswith('&') and cmd[j:j + 1] == '-'):
                while j < n and (cmd[j].isdigit() or cmd[j] == '-'):
                    j += 1
                i = j
                continue
            while j < n and cmd[j] in ' \t':
                j += 1
            k, tq, tgt = j, None, []
            while k < n:
                c = cmd[k]
                if tq:
                    if c == tq:
                        tq = None
                    else:
                        tgt.append(c)
                elif c in '\'"':
                    tq = c
                elif c in ' \t\n;|&<>()':
                    break
                else:
                    tgt.append(c)
                k += 1
            if tq or ''.join(tgt) != '/dev/null':
                return None
            i = k
            continue
        else:
            buf.append(ch)
        i += 1
    if q:
        return None
    flush()
    return segs


def _inside(path, cwd):
    try:
        p, c = os.path.realpath(path), os.path.realpath(cwd)
        return os.path.commonpath([p, c]) == c
    except ValueError:
        return False


def _git_class(words):
    """'ro' | 'prompt' for a git invocation (words[0] == 'git')."""
    i = 1
    while i < len(words) and words[i].startswith('-'):
        o = words[i]
        if o =='--exec-path' or o.startswith('--exec-path=') or o in ('-p', '--paginate'):
            return 'prompt'          # -p / --paginate run the configured pager
        if o in GIT_OPT_WITH_VALUE:
            if o in ('-c', '--config-env'):
                val = words[i + 1] if i + 1 < len(words) else ''
                if any(r in val.lower() for r in GIT_RISKY_CONFIG):
                    return 'prompt'
            i += 2
            continue
        if o.startswith('-c') and len(o) > 2 and o[2] != '-':  # -ckey=val
            return 'prompt'
        if o.startswith('--config-env=') or o.startswith('--git-dir=') or o.startswith('--work-tree=') \
                or o.startswith('--namespace=') or o.startswith('--super-prefix='):
            i += 1
            continue
        i += 1
    if i >= len(words):
        return 'prompt'
    sub, rest = words[i], words[i + 1:]
    if any(a == '--output' or a.startswith('--output=') for a in rest):
        return 'prompt'
    if sub in GIT_RO:
        return 'ro'
    flags = [a for a in rest if a.startswith('-')]
    pos = [a for a in rest if not a.startswith('-')]
    if sub == 'branch':
        bad = {'-d', '-D', '-m', '-M', '-c', '-C', '-u', '-f', '--delete', '--move', '--copy', '--force', '--edit-description',
               '--set-upstream-to', '--unset-upstream', '--track', '--no-track'}
        if any(f in bad or f.startswith('--set-upstream-to=') for f in flags):
            return 'prompt'
        listing = any(f in ('-l', '--list', '--contains', '--merged', '--no-merged', '--points-at', '--show-current')
                      for f in flags)
        return 'ro' if (not pos or listing) else 'prompt'
    if sub == 'tag':
        bad = {'-d', '-a', '-s', '-f', '-m', '-F', '-u', '--delete', '--annotate', '--sign', '--force', '--message'}
        if any(f in bad for f in flags):
            return 'prompt'
        return 'ro' if (not pos or any(f in ('-l', '--list') for f in flags)) else 'prompt'
    if sub == 'remote':
        return 'ro' if (not pos or pos[0] in ('show', 'get-url')) else 'prompt'
    if sub == 'worktree':
        return 'ro' if pos[:1] == ['list'] else 'prompt'
    return 'prompt'  # fetch, config, stash, anything else


def classify_bash(cmd, cwd=None):
    """'bash-native-ro' | 'bash-ro-unlisted' | 'bash-other' for one Bash command."""
    if len(cmd) > 10000:
        return 'bash-other'
    segs = _scan(cmd)
    if not segs:
        return 'bash-other'
    unlisted, cd_changed, has_git = False, False, False
    for text, glob_ in segs:
        try:
            words = shlex.split(text)
        except ValueError:
            return 'bash-other'
        if not words:
            continue
        name, args = words[0], words[1:]
        if '=' in name or '/' in name:
            return 'bash-other'
        if glob_ and name in GLOB_RISK:
            return 'bash-other'
        if name == 'cd':
            a = [x for x in args if x != '--']
            if len(a) != 1 or a[0] == '-' or '$' in a[0] or glob_:
                return 'bash-other'
            t = os.path.expanduser(a[0])
            if cwd:
                t = os.path.normpath(os.path.join(cwd, t))
                if not _inside(t, cwd):
                    return 'bash-other'
                cd_changed = cd_changed or os.path.realpath(t) != os.path.realpath(cwd)
            elif os.path.isabs(t) or a[0].startswith('~'):
                return 'bash-other'
            else:
                cd_changed = cd_changed or os.path.normpath(t) != '.'
            continue
        if name == 'git':
            has_git = True
            if _git_class(words) != 'ro':
                return 'bash-other'
            continue
        if name not in NATIVE_RO and name not in UNLISTED_RO:
            return 'bash-other'
        if name == 'find' and any(a in FIND_WRITE or a.startswith('-fprint') for a in args):
            return 'bash-other'
        if name == 'sed' and any(a == '--in-place' or a.startswith('--in-place=') or
                                 (a.startswith('-') and not a.startswith('--') and 'i' in a) for a in args):
            return 'bash-other'
        if name == 'sort' and any(a == '--output' or a.startswith('--output=') or
                                  (a.startswith('-') and not a.startswith('--') and 'o' in a) for a in args):
            return 'bash-other'
        if name == 'rg' and any(a == '--pre' or a.startswith('--pre=') for a in args):
            return 'bash-other'
        if name == 'file' and any(a in ('-m', '-f') or a.startswith(('--magic-file', '--files-from')) or
                                  (a.startswith('-') and not a.startswith('--') and ('m' in a or 'f' in a))
                                  for a in args):
            return 'bash-other'
        if name == 'ps' and any((a.isalpha() and 'e' in a) or a == '-e' for a in args):   # BSD 'e' prints env
            return 'bash-other'
        if name in UNLISTED_RO:
            unlisted = True
    if has_git and cd_changed:
        return 'bash-other'
    return 'bash-ro-unlisted' if unlisted else 'bash-native-ro'


# ---- path-targeting tools ------------------------------------------------------------------------------------

def _target(inp):
    for k in ('file_path', 'notebook_path', 'path'):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _outside(inp, cwd):
    """True when the tool input names a path outside the entry's cwd (for Glob, an absolute ``pattern`` counts as its
    path). No path: not outside. Unknown cwd: outside only for an absolute or ~ path (conservative)."""
    t = _target(inp)
    if t is None:
        pat = inp.get('pattern')
        if isinstance(pat, str) and pat.startswith(('/', '~')):
            t = pat.split('*', 1)[0] or '/'
    if t is None:
        return False
    t = os.path.expanduser(t)
    if not isinstance(cwd, str) or not cwd:
        return os.path.isabs(t)
    return not _inside(os.path.join(cwd, t), cwd)


def classify_tool(name, inp, cwd, silent):
    if name == 'Bash':
        return classify_bash(str(inp.get('command', '')), cwd if isinstance(cwd, str) and cwd else None)
    if name in FILE_EDIT:
        if _targets_agent_config()(name, inp, cwd if isinstance(cwd, str) else '',
                                    home=pathlib.Path.home(), environ=os.environ):
            return 'agent-config'
        return 'edit-outside-cwd' if _outside(inp, cwd) else 'grantable'
    if name in GRANTABLE:
        return 'grantable'
    if name in PATH_READ and name in silent:
        return 'read-outside-cwd' if _outside(inp, cwd) else 'silent'
    if name in silent:
        return 'silent'
    if name.startswith('mcp__'):
        return 'mcp'
    return 'other-tool'


def window_bounds(since, until=None):
    """Local-date (America/New_York) window [lo, hi) as epoch seconds; until exclusive, default since+1 day."""
    s = datetime.date.fromisoformat(since)
    u = datetime.date.fromisoformat(until) if until else s + datetime.timedelta(days=1)
    if u <= s:
        raise ValueError('--until must be after --since')
    lo = datetime.datetime(s.year, s.month, s.day, tzinfo=TZ).timestamp()
    hi = datetime.datetime(u.year, u.month, u.day, tzinfo=TZ).timestamp()
    return lo, hi, s, u


def compute(transcripts, since, until=None, silent_extra=()):
    lo, hi, s, u = window_bounds(since, until)
    silent = SILENT | set(silent_extra)
    per_sess = collections.defaultdict(list)  # sid -> [(t, cls, sidechain)]
    files = [p for p in glob.glob(os.path.join(os.path.expanduser(transcripts), '**', '*.jsonl'), recursive=True)
             if os.path.getmtime(p) >= lo]
    seen = set()
    for p in files:
        with open(p, encoding='utf-8', errors='replace') as fh:
            for l in fh:
                if '"tool_use"' not in l:
                    continue
                try:
                    o = json.loads(l)
                    t = _ts(o)
                except Exception:
                    continue
                if not (lo <= t < hi) or o.get('type') != 'assistant':
                    continue
                m = o.get('message') or {}
                for c in m.get('content', []) if isinstance(m.get('content'), list) else []:
                    if not (isinstance(c, dict) and c.get('type') == 'tool_use'):
                        continue
                    tid, n = c.get('id'), c.get('name')
                    if not (isinstance(tid, str) and tid and isinstance(n, str) and n):
                        continue  # malformed block
                    if tid in seen:  # tool_use ids are globally unique
                        continue
                    seen.add(tid)
                    inp = c.get('input') if isinstance(c.get('input'), dict) else {}
                    cls = classify_tool(n, inp, o.get('cwd'), silent)
                    per_sess[o.get('sessionId')].append((t, cls, bool(o.get('isSidechain'))))

    tot = collections.Counter()
    active_h = 0.0
    rates = []
    for sid, ev in per_sess.items():
        ev.sort()
        act = sum(min(b[0] - a[0], IDLE) for a, b in zip(ev, ev[1:])) + 60
        h = act / 3600
        active_h += h
        c = collections.Counter(x[1] for x in ev)
        tot.update(c)
        if h >= 0.25:
            rates.append(sum(v for k, v in c.items() if k in PROMPT_CLASSES) / h)
    prompts = sum(v for k, v in tot.items() if k in PROMPT_CLASSES)
    return {
        'window': {'since': s.isoformat(), 'until': u.isoformat(), 'tz': 'America/New_York',
                   'silent_extra': sorted(silent_extra)},
        'sessions': len(per_sess), 'tool_calls': sum(tot.values()), 'active_session_hours': round(active_h, 1),
        'by_class': dict(tot.most_common()),
        'prompts': prompts,
        'prompts_per_active_hour_aggregate': round(prompts / active_h, 1) if active_h else None,
        'per_session_rate_median': round(statistics.median(rates), 1) if rates else None,
        'sessions_rated (>=15 min active)': len(rates),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='Estimate Claude Code permission-prompt friction from transcripts (counts only).')
    ap.add_argument('--since', required=True, help='YYYY-MM-DD local (America/New_York), inclusive')
    ap.add_argument('--until', help='YYYY-MM-DD local, exclusive (default: since + 1 day)')
    ap.add_argument('--transcripts', default='~/.claude/projects', help='transcripts root (default ~/.claude/projects)')
    ap.add_argument('--silent-extra', action='append', default=[], metavar='NAME',
                    help='treat tool NAME as silent (never prompts); repeatable, e.g. Agent')
    a = ap.parse_args(argv)
    try:
        out = compute(a.transcripts, a.since, a.until, a.silent_extra)
    except ValueError as e:
        ap.error(str(e))
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
