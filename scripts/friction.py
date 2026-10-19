#!/usr/bin/env python3
"""Friction estimate for a Levain-on, auto-off Claude Code session, from transcripts. Aggregate counts only.

Model: default permission mode, no user Bash allow rule. Read/Glob/Grep/LS never prompt in default mode. Edit/Write/
MultiEdit/NotebookEdit and WebFetch/WebSearch are answerable by a today-shape consent grant (workspace edits, assumed
granted). Bash is never grant-answered (ruling 1) -> every Bash call is a prompt, unless a curated read-only CC
allow-rule pack covers it (estimated by the command's first word; CC's own prefix matching, never Levain's).

Release tool, not a package module. Ported from project_memory/1017-authorityF/friction.py (10-16 run).
Prints JSON counts only; never prompt text, commands or paths from transcripts.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import shlex
import statistics
import sys
import zoneinfo

TZ = zoneinfo.ZoneInfo('America/New_York')
IDLE = 600  # a gap over 10 min between tool calls is not active time
READONLY = {'ls', 'cat', 'head', 'tail', 'wc', 'grep', 'rg', 'find', 'pwd', 'echo', 'stat', 'file', 'which', 'date', 'diff',
            'sort', 'uniq', 'cut', 'tr', 'jq', 'du', 'df', 'uptime', 'ps', 'sed', 'awk', 'basename', 'dirname', 'realpath', 'tree'}
GIT_RO = {'status', 'log', 'diff', 'show', 'branch', 'rev-parse', 'ls-files', 'grep', 'merge-base', 'ls-remote', 'cherry',
          'blame', 'fetch', 'worktree', 'remote', 'describe', 'tag', 'cat-file', 'check-ignore'}
GRANTABLE = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'WebFetch', 'WebSearch'}
SILENT = {'Read', 'Glob', 'Grep', 'LS', 'NotebookRead', 'TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet', 'ToolSearch'}


def _ts(o):
    return datetime.datetime.fromisoformat(o['timestamp'].replace('Z', '+00:00')).timestamp()


def first_word(cmd):
    c = cmd.strip()
    for pre in ('cd ',):
        if c.startswith(pre) and '&&' in c:
            c = c.split('&&', 1)[1].strip()
    try:
        toks = shlex.split(c.split('|')[0].split('&&')[0].split(';')[0])
    except ValueError:
        toks = c.split()
    toks = [t for t in toks if '=' not in t or t.startswith('-')] or ['?']
    w = os.path.basename(toks[0])
    if w == 'git' and len(toks) > 1:
        sub = [t for t in toks[1:] if not t.startswith('-')]
        return 'git-ro' if sub and sub[0] in GIT_RO else 'git-other'
    return 'ro' if w in READONLY else 'other'


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
        with open(p, errors='replace') as fh:
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
                    k = (o.get('sessionId'), c['id'])
                    if k in seen:
                        continue
                    seen.add(k)
                    n = c['name']
                    if n == 'Bash':
                        cls = 'bash-' + first_word(str((c.get('input') or {}).get('command', '')))
                    elif n in GRANTABLE:
                        cls = 'grantable'
                    elif n in silent:
                        cls = 'silent'
                    elif n.startswith('mcp__'):
                        cls = 'mcp'
                    else:
                        cls = 'other-tool'
                    per_sess[o.get('sessionId')].append((t, cls, bool(o.get('isSidechain'))))

    tot = collections.Counter()
    active_h = 0.0
    rates = []
    rates_pack = []
    for sid, ev in per_sess.items():
        ev.sort()
        act = sum(min(b[0] - a[0], IDLE) for a, b in zip(ev, ev[1:])) + 60
        h = act / 3600
        active_h += h
        c = collections.Counter(x[1] for x in ev)
        tot.update(c)
        bash = sum(v for k, v in c.items() if k.startswith('bash-'))
        bash_after_pack = c['bash-other'] + c['bash-git-other']
        prompts = bash + c['mcp'] + c['other-tool']
        if h >= 0.25:
            rates.append(prompts / h)
            rates_pack.append((bash_after_pack + c['mcp'] + c['other-tool']) / h)
    bash_all = sum(v for k, v in tot.items() if k.startswith('bash-'))
    with_pack = tot['bash-other'] + tot['bash-git-other'] + tot['mcp'] + tot['other-tool']
    no_pack = bash_all + tot['mcp'] + tot['other-tool']
    return {
        'window': {'since': s.isoformat(), 'until': u.isoformat(), 'tz': 'America/New_York',
                   'silent_extra': sorted(silent_extra)},
        'sessions': len(per_sess), 'tool_calls': sum(tot.values()), 'active_session_hours': round(active_h, 1),
        'by_class': dict(tot.most_common()),
        'prompts_no_pack': no_pack,
        'prompts_with_ro_pack': with_pack,
        'prompts_per_active_hour_aggregate_no_pack': round(no_pack / active_h, 1) if active_h else None,
        'prompts_per_active_hour_aggregate_with_pack': round(with_pack / active_h, 1) if active_h else None,
        'per_session_rate_median_no_pack': round(statistics.median(rates), 1) if rates else None,
        'per_session_rate_median_with_pack': round(statistics.median(rates_pack), 1) if rates_pack else None,
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
