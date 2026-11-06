"""A consent grant's root refusal ("/ or the home directory or above it"), with the home or the workspace spelled in
another letter case (key 1106-1, casesweep 1105-49 rows 1 to 1e; the helper is ``levain.pathid``).

On this Mac's case-insensitive APFS ``/USERS/me`` and ``/Users/me`` are one directory and ``os.path.realpath`` keeps the
spelling it was given, so a lexical "is the home under the workspace" test let a grant whose workspace was the home (or the
parent of home) through at append, and then answered every Read under home. The refusal is DENY-style:
``pathid.maybe_inside(workspace, home)`` (True when the home is surely or possibly under the workspace).

Every alias test builds its own alias at run time (``_case_alias``) and SKIPS with "this filesystem is case-sensitive: no
case alias of a directory exists" where none exists, so a Linux CI job can never red it; only a macOS run can. The test at
the end replaces ``pathid._fs_spelling`` (the seam ``tests/test_pathid.py`` uses) with a case-insensitive walk, so the
same refusal logic runs on Linux as well.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import authority, pathid
from levain.authority_sig import AuthorityError
from levain.cockpit.consent import utc_stamp
from levain.consent import policy
from levain.consent.record import ExternalGateRequest, tool_input_digest

REFUSED = "is / or the home directory or above it"


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
    """``top/{home/{proj,secret/k.txt}, homex}``: ``homex`` is a sibling whose name starts with the home's."""
    top = tmp_path.resolve() / "top"
    for d in ("home/proj", "home/secret", "homex/proj"):
        (top / d).mkdir(parents=True)
    (top / "home/secret/k.txt").write_text("k")
    return top


# ------------------------------------------------------------------ consent_root_problem (rows 1, 1b, 1c)

def test_a_workspace_that_is_the_home_or_above_it_is_refused_in_the_canonical_spelling(tree):
    """The canonical behaviour this change must not move."""
    home = tree / "home"
    assert REFUSED in authority.consent_root_problem(str(home), home=home)
    assert REFUSED in authority.consent_root_problem(str(tree), home=home)
    assert REFUSED in authority.consent_root_problem("/", home=home)


def test_a_sibling_named_like_the_home_and_a_project_below_it_are_still_accepted(tree):
    """A signed consent grant is a trust boundary in BOTH directions: `<home>x` is not the home or above it, so a grant
    for it is taken, in the canonical spelling and (macOS) in an alias spelling of it or of the home."""
    home = tree / "home"
    assert authority.consent_root_problem(str(tree / "homex"), home=home) is None
    assert authority.consent_root_problem(str(tree / "homex/proj"), home=home) is None
    assert authority.consent_root_problem(str(home / "proj"), home=home) is None


def test_the_same_acceptance_holds_when_the_spellings_are_aliases(tree):
    home = tree / "home"
    assert authority.consent_root_problem(str(_case_alias(tree / "homex")), home=home) is None
    assert authority.consent_root_problem(str(_case_alias(tree / "homex/proj")), home=_case_alias(home)) is None
    assert authority.consent_root_problem(str(_case_alias(home / "proj")), home=home) is None
    assert authority.consent_root_problem(str(home / "proj"), home=_case_alias(home)) is None


@pytest.mark.parametrize("which", ["home", "above_home"])
def test_an_alias_of_the_home_or_of_its_parent_as_the_workspace_is_refused(tree, which):
    """casesweep rows 1 and 1b: the lexical test said the alias was neither the home nor above it."""
    home = tree / "home"
    ws = _case_alias(home if which == "home" else tree)
    assert REFUSED in authority.consent_root_problem(str(ws), home=home)


@pytest.mark.parametrize("which", ["home", "above_home"])
def test_a_home_argument_in_another_case_is_refused_against_a_canonical_workspace(tree, which):
    """casesweep row 1c: the `home=` handed in (a different HOME on another machine's log, or the env's own spelling)."""
    ws = tree / "home" if which == "home" else tree
    assert REFUSED in authority.consent_root_problem(str(ws), home=_case_alias(tree / "home"))


def test_both_in_alias_spellings_is_refused(tree):
    assert REFUSED in authority.consent_root_problem(str(_case_alias(tree / "home")), home=_case_alias(tree / "home"))


# ------------------------------------------------------------------ _consent_home_check (row 1d)

def test_the_append_check_refuses_an_alias_home_workspace(tree, monkeypatch):
    """casesweep row 1d: `realpath(alias) == alias` holds (realpath keeps the case), so the append check reached the
    lexical home test and returned. HOME is the canonical home."""
    monkeypatch.setenv("HOME", str(tree / "home"))
    for ws in (_case_alias(tree / "home"), _case_alias(tree)):
        with pytest.raises(AuthorityError, match="home directory or above it"):
            authority._consent_home_check([{"workspace": str(ws)}])
    authority._consent_home_check([{"workspace": str(tree / "homex")}])                  # the sibling is taken
    authority._consent_home_check([{"workspace": str(_case_alias(tree / "homex"))}])
    authority._consent_home_check([{"workspace": str(tree / "home" / "proj")}])


# ------------------------------------------------------------------ grant_refusal end to end (row 1e)

NOW = datetime.now(timezone.utc)


def _read_refusal(ws, cwd, target, *, home):
    ti = {"file_path": str(target)}
    grant = {"id": "g1", "kind": "consent", "agent": "claude_code", "tools": ["Read"], "workspace": str(ws),
             "postures": ["default"], "expires_at": utc_stamp(NOW + timedelta(hours=1))}
    req = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s", event_id="e", tool_name="Read",
        tool_input_digest=tool_input_digest(ti), display="Read", cwd=str(cwd), tier="T1",
        received_at=utc_stamp(NOW - timedelta(seconds=5)), expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    return policy.grant_refusal(grant, req, tool_input=ti, posture="default", now=NOW, home=str(home))


def test_a_home_grant_answers_nothing_in_the_canonical_spelling(tree):
    home = tree / "home"
    why = _read_refusal(home, home, home / "secret/k.txt", home=home)
    assert why is not None and "not, here and now, the directory the grant was signed for" in why


def test_a_home_grant_spelled_in_another_case_answers_nothing_either(tree):
    """casesweep row 1e: the grant's workspace, the request's cwd and the Read target all in the alias spelling were
    ANSWERED (allow) for a Read of `home/secret/k.txt`."""
    home = tree / "home"
    a_home = _case_alias(home)
    why = _read_refusal(a_home, a_home, a_home / "secret/k.txt", home=home)
    assert why is not None and "not, here and now, the directory the grant was signed for" in why
    why = _read_refusal(a_home, home, a_home / "secret/k.txt", home=home)                # cwd canonical, as the hook has it
    assert why is not None and "not, here and now, the directory the grant was signed for" in why


def test_a_project_grant_still_answers_in_both_spellings_of_the_home(tree):
    """The other direction at the same site: the grant for `home/proj` is not refused as a root, whatever case the home
    is handed in."""
    proj = tree / "home" / "proj"
    assert _read_refusal(proj, proj, proj / "f", home=tree / "home") is None
    assert _read_refusal(proj, proj, proj / "f", home=_case_alias(tree / "home")) is None


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


def test_the_root_refusal_with_an_injected_case_fold_runs_on_any_filesystem(tree, monkeypatch):
    """Runs on a case-sensitive filesystem too: the seam stands in for the volume that makes `.../HOME` the home."""
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    home = tree / "home"
    upper = tree / "HOME"
    assert REFUSED in authority.consent_root_problem(str(upper), home=home)
    assert REFUSED in authority.consent_root_problem(str(home), home=upper)
    assert REFUSED in authority.consent_root_problem(str(tree.parent / "TOP"), home=home)
    assert authority.consent_root_problem(str(tree / "HOMEX"), home=home) is None          # the prefix sibling is taken
    assert authority.consent_root_problem(str(tree / "HOME" / "proj"), home=home) is None


# ------------------------------------------------------------------ the cockpit's proposal path (casesweep row 1, callers)

def test_a_proposal_is_never_made_for_an_alias_of_the_home_or_above_it(tree, monkeypatch):
    """`cockpit.proposals._proposable` takes its workspace from the cwd recorded on past requests and asks
    `consent_root_problem` (no home test of its own), so the alias fix reaches it: no card offers the home as a root."""
    from levain.cockpit import proposals
    monkeypatch.setenv("HOME", str(tree / "home"))
    for ws in (_case_alias(tree / "home"), _case_alias(tree)):
        assert proposals._proposable("claude_code", "Read", str(ws)) is None
    assert proposals._proposable("claude_code", "Read", str(tree / "home" / "proj")) is not None
    assert proposals._proposable("claude_code", "Read", str(_case_alias(tree / "homex"))) is not None
