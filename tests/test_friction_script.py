import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    'friction_script', Path(__file__).resolve().parent.parent / 'scripts' / 'friction.py')
friction = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(friction)


def _line(sid, tid, iso, name, inp=None):
    return json.dumps({'type': 'assistant', 'sessionId': sid, 'timestamp': iso, 'message': {
        'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp or {}}]}})


def _fixture(tmp_path):
    # 2026-10-16 12:00 UTC = 08:00 EDT, inside the 10-16 local window.
    a = [
        _line('s1', 't1', '2026-10-16T12:00:00Z', 'Bash', {'command': 'ls -la'}),
        _line('s1', 't2', '2026-10-16T12:01:00Z', 'Bash', {'command': 'make build'}),
        _line('s1', 't3', '2026-10-16T12:02:00Z', 'Edit'),
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
    assert out['by_class'] == {'bash-ro': 1, 'bash-other': 1, 'grantable': 1, 'silent': 1,
                               'bash-git-ro': 1, 'other-tool': 1, 'mcp': 1}
    assert out['tool_calls'] == 7
    # s1: 60+60+60+600+60 = 840s ; s2: 30+60 = 90s
    assert out['active_session_hours'] == round((840 + 90) / 3600, 1)
    assert out['prompts_no_pack'] == 5  # 3 bash + mcp + other-tool
    assert out['prompts_with_ro_pack'] == 3  # bash-other + mcp + other-tool


def test_until_and_silent_extra(tmp_path):
    _fixture(tmp_path)
    wide = friction.compute(str(tmp_path), '2026-10-16', '2026-10-18')
    assert wide['by_class']['bash-ro'] == 2  # the 10-17 call now counts
    quiet = friction.compute(str(tmp_path), '2026-10-16', None, ['Agent'])
    assert 'other-tool' not in quiet['by_class'] and quiet['by_class']['silent'] == 2
    empty = friction.compute(str(tmp_path), '2026-01-01')
    assert empty['sessions'] == 0 and empty['prompts_per_active_hour_aggregate_no_pack'] is None
