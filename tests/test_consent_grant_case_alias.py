"""The MATCH half of a consent grant (``policy._within``, used by ``grant_refusal`` and ``contained_shell_refusal``), with the
request's cwd or a target spelled in another letter case (key 1106-1, casesweep 1105-49 rows 2, 2b, 2c; the helper is
``levain.pathid``).

On this Mac's case-insensitive APFS ``/USERS/me/proj`` and ``/Users/me/proj`` are one directory and ``os.path.realpath``
keeps the spelling it was given, so the lexical containment test said "outside" for an alias of a path that IS inside the
grant, and the human was asked again for what the grant already covers. A GRANTS-style fix: ``pathid.inside`` (True only
when sure). An alias of a path OUTSIDE the grant must stay outside, and ``_resolved``'s no-symlink test is untouched.

Every alias test builds its own alias at run time (``_case_alias``) and SKIPS with "this filesystem is case-sensitive: no
case alias of a directory exists" where none exists, so a Linux CI job can never red it; only a macOS run can. The last
test replaces ``pathid._fs_spelling`` (the seam ``tests/test_pathid.py`` uses) with a case-insensitive walk, so the
containment logic of ``_within`` itself runs on Linux as well.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import authority, pathid
from levain.cockpit.consent import utc_stamp
from levain.consent import policy
from levain.consent.contained import ContainedLaunchProof
from levain.consent.record import ExternalGateRequest, tool_input_digest

NOW = datetime.now(timezone.utc)
OUTSIDE_TARGET = "a target is outside the grant's workspace"
OUTSIDE_CWD = "the working directory is outside the grant's workspace"


def _case_alias(p):
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same directory (a
    case-insensitive one: macOS APFS by default); else the test is skipped."""
    p = Path(p)
    alias = Path(str(p.resolve()).swapcase())
    if str(alias) == str(p.resolve()) or not alias.exists() or not os.path.samefile(alias, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return alias


@pytest.fixture
def tree(tmp_path):
    """``top/{home/{proj/src, secret}, other}``: the grant is for ``home/proj``; ``home/secret`` and ``other`` are outside it."""
    top = tmp_path.resolve() / "top"
    for d in ("home/proj/src", "home/secret", "other"):
        (top / d).mkdir(parents=True)
    return top


def _read_refusal(ws, cwd, target, *, home):
    ti = {"file_path": str(target)}
    grant = {"id": "g1", "kind": "consent", "agent": "claude_code", "tools": ["Read"], "workspace": str(ws),
             "postures": ["default"], "expires_at": utc_stamp(NOW + timedelta(hours=1))}
    req = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s", event_id="e", tool_name="Read",
        tool_input_digest=tool_input_digest(ti), display="Read", cwd=str(cwd), tier="T1",
        received_at=utc_stamp(NOW - timedelta(seconds=5)), expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    return policy.grant_refusal(grant, req, tool_input=ti, posture="default", now=NOW, home=str(home))


# ------------------------------------------------------------------ grant_refusal (rows 2, 2b, 2c)

def test_a_read_inside_the_grant_is_answered_in_the_canonical_spelling(tree):
    """The canonical behaviour this change must not move: inside is answered, outside and a prefix sibling are not."""
    ws, home = tree / "home/proj", tree / "home"
    assert _read_refusal(ws, ws, ws / "src/a.txt", home=home) is None
    assert _read_refusal(ws, ws / "src", "a.txt", home=home) is None                         # relative to the cwd
    assert _read_refusal(ws, ws, tree / "other/x.txt", home=home) == OUTSIDE_TARGET
    assert _read_refusal(ws, ws, tree / "home/secret/k.txt", home=home) == OUTSIDE_TARGET
    assert _read_refusal(ws, tree / "other", tree / "other/x.txt", home=home) == OUTSIDE_CWD
    assert _read_refusal(ws, tree / "home/projx", tree / "home/projx/x", home=home) == OUTSIDE_CWD


def test_a_target_spelled_in_another_case_is_answered_when_it_is_inside_the_grant(tree):
    """casesweep row 2: the grant covers `home/proj`; the Read's target is `<alias>/src/a.txt`."""
    ws, home = tree / "home/proj", tree / "home"
    a_ws = _case_alias(ws)
    assert _read_refusal(ws, ws, a_ws / "src/a.txt", home=home) is None
    assert _read_refusal(ws, ws, a_ws / "src/NEW.TXT", home=home) is None                    # a file that does not exist yet


def test_a_cwd_spelled_in_another_case_is_answered_when_it_is_inside_the_grant(tree):
    """casesweep row 2b: the request's cwd is the alias; the grant's signed workspace stays the canonical string."""
    ws, home = tree / "home/proj", tree / "home"
    a_ws = _case_alias(ws)
    assert _read_refusal(ws, a_ws, a_ws / "src/a.txt", home=home) is None
    assert _read_refusal(ws, a_ws, ws / "src/a.txt", home=home) is None
    assert _read_refusal(ws, a_ws / "src", "a.txt", home=home) is None


def test_the_same_with_the_workspace_signed_as_an_alias_string(tree):
    """A grant signed with the alias spelling is the directory's real path as `realpath` spells it (it keeps the case), so
    it passes the `root != workspace` check; the canonical cwd and targets are inside it."""
    ws, home = tree / "home/proj", tree / "home"
    a_ws = _case_alias(ws)
    assert _read_refusal(a_ws, ws, ws / "src/a.txt", home=home) is None
    assert _read_refusal(a_ws, a_ws, a_ws / "src/a.txt", home=home) is None


def test_an_alias_of_a_path_outside_the_grant_is_still_outside(tree):
    """casesweep row 2c: the alias spelling must not widen the grant."""
    ws, home = tree / "home/proj", tree / "home"
    a_other, a_secret = _case_alias(tree / "other"), _case_alias(tree / "home/secret")
    assert _read_refusal(ws, ws, a_other / "x.txt", home=home) == OUTSIDE_TARGET
    assert _read_refusal(ws, ws, a_secret / "k.txt", home=home) == OUTSIDE_TARGET
    assert _read_refusal(ws, a_other, a_other / "x.txt", home=home) == OUTSIDE_CWD
    assert _read_refusal(ws, _case_alias(tree / "home"), "proj/../secret/k.txt", home=home) == OUTSIDE_CWD


def test_a_symlink_on_the_target_path_is_still_refused_even_in_an_alias_spelling(tree):
    """`_resolved`'s no-symlink test stays: a link inside the workspace pointing out of it matches nothing."""
    ws, home = tree / "home/proj", tree / "home"
    (ws / "link").symlink_to(tree / "other")
    a_ws = _case_alias(ws)
    assert _read_refusal(ws, ws, ws / "link/x.txt", home=home) == OUTSIDE_TARGET
    assert _read_refusal(ws, ws, a_ws / "link/x.txt", home=home) == OUTSIDE_TARGET


def test_a_dotdot_target_is_still_refused_in_an_alias_spelling(tree):
    ws, home = tree / "home/proj", tree / "home"
    a_ws = _case_alias(ws)
    assert _read_refusal(ws, ws, f"{a_ws}/src/../../secret/k.txt", home=home) == OUTSIDE_TARGET


# ------------------------------------------------------------------ contained_shell_refusal (the same _within)

def _shell_refusal(ws, cwd, launch_cwd, *, home):
    ti = {"command": "pytest -q"}
    grant = {"id": "sh1", "kind": authority.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
             "workspace": str(ws), "postures": ["default"], "containment_digest": "d" * 64,
             "expires_at": utc_stamp(NOW + timedelta(hours=1))}
    req = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s", event_id="chat:toolu_1", tool_name="Bash",
        tool_input_digest=tool_input_digest(ti), display="d", cwd=str(cwd), tier="T2",
        received_at=utc_stamp(NOW - timedelta(seconds=5)), expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    proof = ContainedLaunchProof(launch_id="L1", spawn_nonce="a" * 32, profile_digest="d" * 64,
                                 canary_verdict_id="c" * 64, cwd=str(launch_cwd))
    return policy.contained_shell_refusal(grant, req, tool_input=ti, posture="default", now=NOW, home=str(home),
                                          contained_launch=proof)


def test_a_contained_shell_launch_in_the_canonical_spelling_is_answered_only_inside_the_grant(tree):
    ws, home = tree / "home/proj", tree / "home"
    assert _shell_refusal(ws, ws / "src", ws / "src", home=home) is None
    assert _shell_refusal(ws, tree / "other", tree / "other", home=home) == "outside the grant's workspace"


def test_a_contained_shell_launch_whose_directory_is_an_alias_inside_the_grant_is_answered(tree):
    """The request's cwd and the launch's cwd are the same string (the proof says so); both are the alias."""
    ws, home = tree / "home/proj", tree / "home"
    a_src = _case_alias(ws) / "src"
    assert _shell_refusal(ws, a_src, a_src, home=home) is None


def test_a_contained_shell_launch_in_an_alias_of_a_directory_outside_the_grant_is_refused(tree):
    ws, home = tree / "home/proj", tree / "home"
    a_other = _case_alias(tree / "other")
    assert _shell_refusal(ws, a_other, a_other, home=home) == "outside the grant's workspace"


# ------------------------------------------------------------------ the same logic with no alias (Linux CI)

def _case_insensitive_spelling(path: str):
    """The filesystem's own spelling over the real tree, matching each component without case; None where nothing matches."""
    cur = Path(path).anchor or "/"
    for part in Path(path).parts[1:]:
        names = os.listdir(cur)
        hit = part if part in names else next((n for n in names if n.lower() == part.lower()), None)
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur


def test_within_with_an_injected_case_fold_runs_on_any_filesystem(tree, monkeypatch):
    """Runs on a case-sensitive filesystem too: the seam stands in for the volume that makes `.../PROJ` the workspace."""
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    root = str(tree / "home/proj")
    assert policy._within(str(tree / "HOME/PROJ/Src/a.txt"), root)
    assert policy._within(str(tree / "HOME/PROJ"), root)
    assert policy._within(root + "/src", str(tree / "HOME/PROJ"))
    assert not policy._within(str(tree / "HOME/PROJX/a"), root)
    assert not policy._within(str(tree / "OTHER/x"), root)
    assert not policy._within(str(tree / "home"), root)                                       # the parent is not inside
