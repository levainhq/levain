import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    'friction_script', Path(__file__).resolve().parent.parent / 'scripts' / 'friction.py')
friction = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(friction)

NATIVE, UNLISTED, OTHER = 'bash-native-ro', 'bash-ro-unlisted', 'bash-other'


def _line(sid, tid, iso, name, inp=None, cwd=None):
    o = {'type': 'assistant', 'sessionId': sid, 'timestamp': iso, 'message': {
        'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp or {}}]}}
    if cwd:
        o['cwd'] = cwd
    return json.dumps(o)


def _fixture(tmp_path):
    # 2026-10-16 12:00 UTC = 08:00 EDT, inside the 10-16 local window.
    a = [
        _line('s1', 't1', '2026-10-16T12:00:00Z', 'Bash', {'command': 'ls -la'}),
        _line('s1', 't2', '2026-10-16T12:01:00Z', 'Bash', {'command': 'make build'}),
        _line('s1', 't3', '2026-10-16T12:02:00Z', 'Edit', {'file_path': '/w/a.py'}, '/w'),
        _line('s1', 't4', '2026-10-16T12:03:00Z', 'Read'),
        # idle gap of 1h: capped at 600s
        _line('s1', 't5', '2026-10-16T13:03:00Z', 'Bash', {'command': 'git status'}),
        _line('s1', 't5', '2026-10-16T13:03:00Z', 'Bash', {'command': 'git status'}),  # duplicate id
    ]
    b = [
        _line('s2', 'u1', '2026-10-16T14:00:00Z', 'Agent'),
        _line('s2', 'u2', '2026-10-16T14:00:30Z', 'mcp__x__y'),
        _line('s2', 'u3', '2026-10-17T12:00:00Z', 'Bash', {'command': 'ls'}),  # outside window (10-17 local)
        _line('s2', 'u4', '2026-10-16T03:00:00Z', 'Bash', {'command': 'ls'}),  # 10-15 23:00 local, outside
    ]
    (tmp_path / 'a.jsonl').write_text('\n'.join(a) + '\n')
    (tmp_path / 'b.jsonl').write_text('\n'.join(b) + '\n')


def test_classes_hours_and_window(tmp_path):
    _fixture(tmp_path)
    out = friction.compute(str(tmp_path), '2026-10-16')
    assert out['window']['since'] == '2026-10-16' and out['window']['until'] == '2026-10-17'
    assert out['sessions'] == 2
    assert out['by_class'] == {NATIVE: 2, OTHER: 1, 'grantable': 1, 'silent': 1, 'other-tool': 1, 'mcp': 1}
    assert out['tool_calls'] == 7
    # s1: 60+60+60+600+60 = 840s ; s2: 30+60 = 90s
    assert out['active_session_hours'] == round((840 + 90) / 3600, 1)
    assert out['prompts'] == 3  # bash-other + mcp + other-tool
    assert 'prompts_with_ro_pack' not in out and 'prompts_no_pack' not in out


def test_until_and_silent_extra(tmp_path):
    _fixture(tmp_path)
    wide = friction.compute(str(tmp_path), '2026-10-16', '2026-10-18')
    assert wide['by_class'][NATIVE] == 3  # the 10-17 call now counts
    quiet = friction.compute(str(tmp_path), '2026-10-16', None, ['Agent'])
    assert 'other-tool' not in quiet['by_class'] and quiet['by_class']['silent'] == 2
    empty = friction.compute(str(tmp_path), '2026-01-01')
    assert empty['sessions'] == 0 and empty['prompts_per_active_hour_aggregate'] is None


def test_f1_native_vs_unlisted():
    assert friction.classify_bash('ls -la') == NATIVE
    assert friction.classify_bash('git status') == NATIVE
    assert friction.classify_bash('rg foo src') == UNLISTED
    assert friction.classify_bash('ls && rg foo') == UNLISTED
    assert friction.classify_bash('make build') == OTHER


def test_f2_compound_redirect_substitution(tmp_path):
    c = friction.classify_bash
    assert c('ls && cat x | wc -l') == NATIVE
    assert c('ls; echo hi\npwd') == NATIVE
    assert c('ls && make') == OTHER
    assert c('ls > out.txt') == OTHER
    assert c('echo hi >> out.txt') == OTHER
    assert c('grep -r x . 2>/dev/null') == NATIVE
    assert c('ls 2>&1') == NATIVE
    assert c('cat $(echo f)') == OTHER
    assert c('echo `date`') == OTHER
    assert c('echo "unterminated') == OTHER
    assert c('ls | tee out.txt') == OTHER
    cwd = str(tmp_path)
    (tmp_path / 'sub').mkdir()
    assert c('cd sub && ls', cwd) == NATIVE
    assert c('cd /etc && ls', cwd) == OTHER
    assert c('cd sub && git status', cwd) == OTHER
    assert c('cd . && git status', cwd) == NATIVE


def test_f3_write_capable_forms():
    c = friction.classify_bash
    for cmd in ['find . -delete', 'find . -name x -exec rm {} ;', 'find . -fprint out', 'sed -i s/a/b/ f', 'sed -i.bak s/a/b/ f',
                'sort -o out f', 'sort f --output=out', 'git branch -D x', 'git branch newbranch', 'git tag v1',
                'git tag -d v1', 'git remote add o u', 'git worktree add p', 'git fetch', 'git -C x branch -m a b',
                'git diff --output=out', 'find *.py -name x', 'git log *.py', 'sed s/a/b/ *.txt']:
        assert c(cmd) == OTHER, cmd
    for cmd in ['find . -name "*.py"', 'git branch -a', 'git branch --list "x*"', 'git tag -l', 'git remote -v',
                'git -C /tmp/x --no-pager log -3', 'git worktree list', 'ls *.py', "sed -n '1,5p' f"]:
        assert c(cmd) in (NATIVE, UNLISTED), cmd


def test_f4_malformed_tool_use_blocks(tmp_path):
    good = _line('s1', 'g1', '2026-10-16T12:00:00Z', 'Bash', {'command': 'ls'})
    bad = json.dumps({'type': 'assistant', 'sessionId': 's1', 'timestamp': '2026-10-16T12:00:10Z', 'message': {'content': [
        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'ls'}},          # no id
        {'type': 'tool_use', 'id': 'x1', 'input': {}},                              # no name
        {'type': 'tool_use', 'id': 5, 'name': 'Bash', 'input': 'oops'},             # bad types
        {'type': 'tool_use', 'id': 'x2', 'name': 'Bash', 'input': 'not-a-dict'},    # bad input
    ]}})
    (tmp_path / 'a.jsonl').write_text(good + '\n' + bad + '\n')
    out = friction.compute(str(tmp_path), '2026-10-16')
    assert out['tool_calls'] == 2 and out['by_class'] == {NATIVE: 1, OTHER: 1}


def test_f5_agent_config_and_outside_edit(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    cwd = str(ws)
    t = friction.classify_tool
    assert t('Edit', {'file_path': str(ws / 'a.py')}, cwd, friction.SILENT) == 'grantable'
    assert t('Edit', {'file_path': str(ws / '.claude' / 'settings.json')}, cwd, friction.SILENT) == 'agent-config'
    assert t('Write', {'file_path': str(ws / '.claude' / 'hooks' / 'h.sh')}, cwd, friction.SILENT) == 'agent-config'
    assert t('Write', {'file_path': str(ws / '.mcp.json')}, cwd, friction.SILENT) == 'agent-config'
    assert t('Edit', {'file_path': str(tmp_path / 'other.py')}, cwd, friction.SILENT) == 'edit-outside-cwd'
    assert t('Edit', {'file_path': 'rel.py'}, cwd, friction.SILENT) == 'grantable'
    assert t('WebFetch', {'url': 'https://x'}, cwd, friction.SILENT) == 'grantable'
    # through compute: the agent-config edit is a counted prompt
    (tmp_path / 't.jsonl').write_text(_line('s1', 'e1', '2026-10-16T12:00:00Z', 'Edit',
                                            {'file_path': str(ws / '.claude' / 'settings.json')}, cwd) + '\n')
    out = friction.compute(str(tmp_path), '2026-10-16')
    assert out['by_class'] == {'agent-config': 1} and out['prompts'] == 1


def test_f6_read_outside_cwd(tmp_path):
    cwd = str(tmp_path)
    t = friction.classify_tool
    s = friction.SILENT
    assert t('Read', {'file_path': str(tmp_path / 'a')}, cwd, s) == 'silent'
    assert t('Read', {'file_path': '/etc/hosts'}, cwd, s) == 'read-outside-cwd'
    assert t('Glob', {'pattern': '*.py', 'path': '/usr'}, cwd, s) == 'read-outside-cwd'
    assert t('Grep', {'pattern': 'x'}, cwd, s) == 'silent'
    assert t('LS', {'path': '/'}, cwd, s) == 'read-outside-cwd'
    assert t('Read', {'file_path': '/etc/hosts'}, cwd, s | {'Read'}) == 'read-outside-cwd'


def test_f7_dedupe_on_id_alone(tmp_path):
    a = _line('sA', 'same', '2026-10-16T12:00:00Z', 'Bash', {'command': 'ls'})
    b = _line('sB', 'same', '2026-10-16T12:00:05Z', 'Bash', {'command': 'ls'})
    (tmp_path / 'a.jsonl').write_text(a + '\n' + b + '\n')
    assert friction.compute(str(tmp_path), '2026-10-16')['tool_calls'] == 1


def test_f8_naive_timestamp_is_utc(tmp_path):
    # 2026-10-17T03:30 naive = UTC = 10-16 23:30 EDT -> inside the 10-16 window
    (tmp_path / 'a.jsonl').write_text(_line('s1', 'n1', '2026-10-17T03:30:00', 'Bash', {'command': 'ls'}) + '\n')
    assert friction.compute(str(tmp_path), '2026-10-16')['tool_calls'] == 1
    # non-ASCII transcript reads fine
    (tmp_path / 'b.jsonl').write_text(_line('s2', 'n2', '2026-10-16T12:00:00Z', 'Bash', {'command': 'echo é'}) + '\n',
                                      encoding='utf-8')
    assert friction.compute(str(tmp_path), '2026-10-16')['tool_calls'] == 2


def test_flagged_members_of_the_native_set():
    # Claude Code's permissions page names sed, sort, file and ps in its read-only set, with flag conditions
    cases = {"sed -n 1,20p a.py": "bash-native-ro", "sed -i s/a/b/ a.py": "bash-other",
             "sort a.txt": "bash-native-ro", "sort -o out a": "bash-other",
             "file a.bin": "bash-native-ro", "file -m magic a": "bash-other",
             "ps aux": "bash-native-ro", "ps -ef": "bash-native-ro", "ps -Ao pid,etime,ucomm": "bash-native-ro",
             "ps auxe": "bash-other", "ps aux -e": "bash-other"}
    for cmd, want in cases.items():
        assert friction.classify_bash(cmd, "/w") == want, cmd
