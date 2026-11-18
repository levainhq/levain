"""T10 (levain#102): the worker brief for an item, the launch's recorded close protocol and durable key, the close
predicate by protocol, and the desk brief's board section. Scratch stores and scratch git repos only; no real ``claude``."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys

import pytest

from levain.agents import instantiate as inst
from levain.desk import launch as cl
from levain.desk import lease, seat_close
from levain.desk.store import CoordError, CoordStore
from tests.test_agents_spawn import a_thread, registry, role  # noqa: F401
from tests.test_board_registry import (  # noqa: F401
    A, bare_store, begin, decision_seal, git, home, mkrepo, ok, run, store as board_store_dir, track_ready)


@pytest.fixture
def desk(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


def _spawn(store, token, registry, cwd, **kw):
    _d, t = a_thread(store)
    return inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(cwd), inline=role(), slots={"topic": "x"},
                             agent_store=registry, **kw)


# --- 1: the worker brief for an item -----------------------------------------------------------------------------------
def test_an_attached_launch_brief_carries_the_five_rules_and_an_unattached_one_none(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, item="levain#7")
    text = cl._brief_text(store, ln)
    assert "## Your board item" in text
    assert "board context levain#7` before anything else" in text and "carries no copy of it" in text
    assert "`Board-Item: levain#7`" in text and "`Board-Closes: levain#7`" in text
    assert f"`Board-Seat: {ln.session_key}`" in text and "`repo:levain` and `item:levain#7`" in text
    assert "recorded as `legacy`" in text
    plain = cl._brief_text(store, _spawn(store, token, registry, tmp_path))
    assert "Board-Item" not in plain and "Your board item" not in plain and "board context" not in plain


# --- 2: the launch record's protocol and durable key -----------------------------------------------------------------
def test_a_launch_with_no_registry_is_legacy_and_writes_no_protocol_row(desk, registry, home, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path)                    # no pointer, no registry, not a repo
    assert ln.protocol == "legacy" and ln.migration is None and ln.durable_key == ln.session_key
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM launch_protocol").fetchone()[0] == 0
    conn.close()


def test_every_mode_derives_its_protocol_and_the_lock_refuses_the_rest(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert _spawn(store, token, registry, a).protocol == "legacy"                     # untracked
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    m = [json.loads(x) for x in (board_store_dir / "repos.jsonl").read_text().splitlines()
         if '"begin-track"' in x][-1]["migration"]
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # tracking, no migration id
        _spawn(store, token, registry, a)
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # tracking, another id
        _spawn(store, token, registry, a, migration="not-this-one")
    ln = _spawn(store, token, registry, a, migration=m, item="alpha#1")               # the ceremony's own seat
    assert (ln.protocol, ln.migration) == ("ceremony", m) and ln.board_items == ("alpha#1",)
    assert "recorded as `ceremony`" in cl._brief_text(store, ln)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # aborting
        _spawn(store, token, registry, a)
    b = mkrepo("beta")
    track_ready(capsys, tmp_path, board_store_dir, b, "beta")
    ok(capsys, "track", "beta", "--words", "yes", *A)
    ln = _spawn(store, token, registry, b, item="beta#1", migration="ignored-once-tracked")      # tracked
    assert (ln.protocol, ln.migration) == ("tracked", None)
    assert "recorded as `tracked`" in cl._brief_text(store, ln)
    assert store.launch(ln.launch_id).protocol == "tracked"                           # read back from the record


def test_a_refused_launch_leaves_no_launch(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    conn = sqlite3.connect(store.path)
    before = conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    with pytest.raises(CoordError, match="locked to new work"):
        store.launch_request(token, harness="claude", role="worker", threads=[], cwd=str(a))
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == before
    conn.close()


def test_a_record_made_before_the_protocol_table_reads_legacy(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, item="levain#7")
    conn = sqlite3.connect(store.path)
    conn.execute("DROP TRIGGER IF EXISTS launch_protocol_no_replace")
    conn.executescript("DROP TABLE IF EXISTS launch_protocol")
    conn.commit()
    conn.close()
    again = CoordStore(store.directory).launch(ln.launch_id)       # a reader's open does not recreate the table
    assert again.protocol == "legacy" and again.migration is None
    ro = CoordStore(store.directory)
    c = ro.open_readonly()
    try:
        assert lease.launch(c, ln.launch_id).protocol == "legacy"
    finally:
        c.close()


def test_a_protocol_is_taken_once_and_legacy_is_never_written(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path)
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="close protocol"):
        for _ in range(2):
            conn.execute("INSERT INTO launch_protocol (launch_id, gseq, protocol, migration, at) VALUES (?, ?, 'tracked', NULL, 't')",
                         (ln.launch_id, 9990 + _))
    conn.close()
    c2 = sqlite3.connect(store.path)
    with pytest.raises(Exception, match="tracked or ceremony"):
        lease.record_protocol(c2, "t", ln.launch_id, "legacy")
    c2.close()


# --- 3: the close predicate by protocol ---------------------------------------------------------------------------------
KEY, T0 = "s-abc", "2026-11-17T10:00:00Z"


def _ep(item, *, source=KEY, at="2026-11-17T11:00:00Z"):
    return {"source": source, "at": at, "tags": ["repo:levain", f"item:{item}"]}


def _close(protocol, **kw):
    base = dict(key=KEY, launched_at=T0, episodes=[], closed_items=[], old_close_ok=False, repo_tracked_now=False)
    base.update(kw)
    return seat_close.check_close(protocol, ["levain#1"], **base)


def test_the_tracked_close_wants_an_episode_and_a_trailer_per_item():
    assert not _close("tracked").ok
    both = _close("tracked", episodes=[_ep("levain#1")], closed_items=["levain#1"])
    assert both.ok and both.via == "tracked" and both.missing == ()
    only_ep = _close("tracked", episodes=[_ep("levain#1")])
    assert not only_ep.ok and len(only_ep.missing) == 1 and "Board-Closes" in only_ep.missing[0]
    only_trailer = _close("tracked", closed_items=["levain#1"])
    assert not only_trailer.ok and "no episode" in only_trailer.missing[0]


@pytest.mark.parametrize("ep", [_ep("levain#1", source="another"), _ep("levain#1", at="2026-11-17T09:00:00Z"),
                                _ep("levain#2"), {"source": KEY, "at": "2026-11-17T11:00:00Z", "tags": []}])
def test_an_episode_of_another_source_time_or_item_does_not_count(ep):
    assert not _close("tracked", episodes=[ep], closed_items=["levain#1"]).ok


def test_an_episode_may_carry_its_tags_in_metadata():
    ep = {"source": KEY, "at": "2026-11-17T11:00:00Z", "metadata": {"tags": ["item:levain#1"]}}
    assert _close("tracked", episodes=[ep], closed_items=["levain#1"]).ok


def test_a_legacy_seat_closes_the_old_way_and_a_tracked_repo_accepts_either():
    assert _close("legacy", old_close_ok=True).ok and _close("ceremony", old_close_ok=True).via == "old"
    assert not _close("legacy").ok and not _close("legacy", repo_tracked_now=False, closed_items=["levain#1"]).ok
    became = _close("legacy", repo_tracked_now=True, episodes=[_ep("levain#1")], closed_items=["levain#1"])
    assert became.ok and became.via == "tracked"                       # its pickup file is a stub: the tracked close holds
    assert not _close("legacy", repo_tracked_now=True, episodes=[_ep("levain#1")]).ok
    assert _close("legacy", repo_tracked_now=True, old_close_ok=True).via == "old"
    assert not _close("tracked", old_close_ok=True).ok                 # a tracked seat is never judged by the old file


# --- 4: the desk brief's section ---------------------------------------------------------------------------------------
def test_the_desk_brief_teaches_the_two_modes_and_the_ceremony_in_order(desk):
    store, _ = desk
    from levain import install as inst_
    lv = inst_.desk_levain(sys.executable)
    text = "\n".join(cl._board_modes_section(lv))
    assert "## The work board: two modes" in text and "untracked or tracked" in text
    assert "does not open" not in text
    order = ["\n1. If this install has no item store", "\n2. Read its existing memory", "\n3. Draft ONE review page",
             "\n4. He answers in conversation", f"\n5. Execute, in this order: first `{lv} board begin-track NAME",
             f"for each item, `{lv} board add --repo NAME", "then land `CHARTER.md`",
             f"then `{lv} board track NAME --migration M", "last, copy each old file", "pre-track-<date>/",
             f"`{lv} charter check --repo DIR` reports on the landed charter", f"`{lv} board abandon-track NAME",
             f"then `{lv} board abandon-done NAME", f"leaving tracked mode is `{lv} board untrack NAME",
             "\n6. Write one `decision` episode"]
    steps = text.index("In this order:")                       # the verb list above spells the same forms
    at = [text.index(x, steps) for x in order]
    assert at == sorted(at), dict(zip(order, at))
    assert "--migration M" in text and "Board-Seat" in text


def test_the_desks_door_opens_the_named_board_and_charter_verbs_and_no_other(desk):
    # the door is per-verb (a rule is a command prefix): the ceremony's verbs are opened one by one, in the install's
    # own spelling, and a board or charter verb the brief does not name stays closed
    from levain import install as inst_
    lv = inst_.desk_levain(sys.executable)
    rules = [r.removeprefix("Bash(").removesuffix(":*)") for r in inst_.desk_allow(sys.executable)]
    assert not {f"{lv} board", f"{lv} charter"} & set(rules)           # never the whole command
    opened = {r.removeprefix(lv + " ") for r in rules if r.startswith(lv + " ")}
    assert opened == {*inst_.DESK_VERBS, *inst_.DESK_BOARD_VERBS}
    extra = {v for v in opened if v.split()[0] in ("board", "charter")}
    assert extra == set(inst_.DESK_BOARD_VERBS) and len(extra) == 15
    text = "\n".join(cl._board_modes_section(lv))
    named = {c.removeprefix(lv + " ") for c in re.findall(r"`([^`]+)`", text) if c.startswith(lv + " ")}
    for c in named:                                                    # each verb the brief names is opened
        assert any(c == v or c.startswith(v + " ") for v in opened), c
    for closed in ("board forget", "board export", "charter rewrite", "board"):
        assert not any(closed == v or closed.startswith(v + " ") for v in opened), closed


def test_the_door_in_code_opens_exactly_the_named_board_and_charter_leaves():
    # the code door is default-deny per leaf: only the verbs in DESK_BOARD_VERBS are marked, every other board or
    # charter leaf stays unmarked and so refused inside the desk's workspace
    from levain import install as inst_
    from levain.cli import build_parser
    from levain.desk import door
    leaves = door.declared_leaves(build_parser())
    opened = {" ".join(p) for p, mark in leaves.items() if mark is True and p[0] in ("board", "charter")}
    assert opened == set(inst_.DESK_BOARD_VERBS)
    closed = {" ".join(p) for p, mark in leaves.items() if p[0] in ("board", "charter") and mark is not True}
    assert closed and not closed & opened and "board move" in closed and "board ack-torn" in closed


# --- T10 fix 2 ---------------------------------------------------------------------------------------------------------
def _desk_workspace(tmp_path, monkeypatch):
    from levain.desk import door
    from levain.desk.legacy import JSON_ROLE_DESK, LEGACY_HOME_NAME

    ws = tmp_path / LEGACY_HOME_NAME
    (ws / ".levain").mkdir(parents=True)
    (ws / ".levain" / "config.json").write_text(f'{{"role": {JSON_ROLE_DESK}}}\n')
    door.record(ws)
    monkeypatch.chdir(ws)
    return ws


FILL = {"NAME": "alpha", "NAME#N": "alpha#1", "M": "m1", "M:K": "m1:a", "DECISION": "parked", "SHA256": "a" * 64, "Q-ID": "q-" + "ab" * 12}


def _printed_commands(lines, lv):
    """Every command a brief section prints (each backticked span that starts with the install's command), as argv after
    the install's prefix."""
    import shlex

    cmds = [c for x in lines for c in re.findall(r"`([^`]+)`", x) if c == lv or c.startswith(lv + " ")]
    return [shlex.split(c.removeprefix(lv).strip()) for c in cmds]


def _verb_lines(lv):
    """Every board or charter command the desk brief's board section prints (the verb list AND each ceremony step)."""
    return _printed_commands(cl._board_modes_section(lv), lv)


def _parses(parser, argv):
    """The real parser's reading of ``argv``; a line argparse refuses fails the test, naming it."""
    try:
        return parser.parse_args(argv)
    except SystemExit:
        pytest.fail(f"a printed command does not parse: {argv}")


def test_every_board_and_charter_line_of_the_brief_parses_and_passes_the_door(home, tmp_path, monkeypatch):  # noqa: F811
    # L1 CRITICAL: the door judged --by on verbs whose --by is the record's author; each line the brief spells now parses
    # as written (placeholders filled) and the door lets it run in the desk's workspace
    from levain import install as inst_
    from levain.cli import build_parser
    from levain.desk import door

    _desk_workspace(tmp_path, monkeypatch)
    lv = inst_.desk_levain(sys.executable)
    fill = {**FILL, "DIR": str(tmp_path), "FILE": str(tmp_path / "m.json")}
    parser = build_parser()
    seen = set()
    printed = _verb_lines(lv)
    assert len(printed) > len(inst_.DESK_BOARD_VERBS)          # the ceremony steps are printed commands too, and checked
    for argv in printed:
        argv = [fill.get(a, a) for a in argv]
        args = _parses(parser, argv)
        assert door.refusal(parser, args) is None, argv
        seen.add(" ".join(argv[:2]))
    assert seen == set(inst_.DESK_BOARD_VERBS)                 # the brief spells every opened verb, and only those


def _answered_band(words, question="Track alpha?"):
    """The install's own band (its default consent directory) with one question answered; returns its q-id."""
    from levain.consent.questions import QuestionStore

    qs = QuestionStore()
    qs.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    q = qs.file(agent="claude_code", agent_session_id="s", what=question, options=(), rec="", blocks="b", cwd="/tmp",
                open_ended=True)
    qs.answer(q.id, text=words, channel="cli")
    return q.id


def _ceremony_line(parser_argv, qid):
    """The brief's printed line for a verb (placeholders filled), with the printed ``Q-ID`` replaced by the real row's."""
    return [qid if a == "Q-ID" else a for a in parser_argv]


def _printed(verb, lv, **fill):
    """The brief's one printed line for ``verb`` (a ``_board_forms`` key), as argv, placeholders filled from ``fill``."""
    import shlex

    return [fill.get(a, a) for a in shlex.split(cl._board_forms(lv)[verb].removeprefix(lv).strip())]


def test_the_brief_ceremony_runs_through_the_door_in_a_desk_workspace(home, mkrepo, tmp_path, monkeypatch, capsys):  # noqa: F811
    # the brief's own spellings, run in the desk's workspace: `board use DIR` (no --by), every write `--by desk`, and each
    # decision line ending `--source Q-ID` with a REAL answered band row (P2's writer policy accepts it, relayed)
    from levain import install as inst_
    from levain.cli import main
    from levain.desk import board_registry as breg
    from tests.test_board_registry import CHARTER, land, manifest
    from tests.test_board_sign import Rig

    _desk_workspace(tmp_path, monkeypatch)
    lvc = inst_.desk_levain(sys.executable)
    rig = Rig(tmp_path)
    real_untrack = breg.untrack                                  # untrack takes no --source: his signed write; sealed here
    his_signature = {"on": False}                                # off: the line as the desk would run it (no signature)
    monkeypatch.setattr(breg, "untrack", lambda *a, seal=None, **kw: real_untrack(
        *a, seal=seal or (rig.seal() if his_signature["on"] else None), **kw))
    a, b = mkrepo("alpha"), mkrepo("beta")
    store_dir = tmp_path / "store"
    fill = {"NAME": "alpha", "DIR": str(a), "FILE": "", "M": "", "NAME#N": "", "DECISION": "parked"}

    def lv(*argv):
        rc = main(list(argv))
        out, err = capsys.readouterr()
        assert "desk's workspace" not in err, (argv, err)          # never the door's refusal
        assert rc == 0, (argv, err)
        return out.strip()

    def printed(verb, **kw):
        argv = _printed(verb, lvc, **{**fill, **kw})
        assert argv[0] == "board" or argv[0] == "charter", argv
        return argv

    lv("board", "use", str(store_dir))
    lv("board", "register", "alpha", "--path", str(a), "--words", "his words", "--by", "desk")
    lv("board", "repos")
    lv("charter", "init", "alpha", "--repo", str(a), "--stdout")
    man = manifest(tmp_path)
    sha = hashlib.sha256(man.read_bytes()).hexdigest()
    # the band row: the question carries the manifest's sha256 (the ceremony verbs check it), the answer holds his words
    fill["Q-ID"] = _answered_band("yes, his yes: begin and track it; his words: abandon, park and untrack",
                                  f"Track alpha? manifest sha256 {sha}")

    def refused_without_a_source(verb, **kw):
        """The same printed line with its ``--source Q-ID`` taken off (what the brief used to teach): the writer refuses it
        and writes nothing."""
        argv = printed(verb, **kw)
        k = argv.index("--source") if "--source" in argv else None
        argv = argv[:k] + argv[k + 2:] if k is not None else [x for x in argv if x != "--sign"]
        before = {f.name: f.read_bytes() for f in store_dir.glob("*.jsonl")}
        rc = main(argv)
        err = capsys.readouterr().err
        assert rc == 2 and "decision-class" in err and "--sign" in err, (verb, rc, err)
        assert before == {f.name: f.read_bytes() for f in store_dir.glob("*.jsonl")}, verb

    refused_without_a_source("board begin-track", FILE=str(man))
    lv(*printed("board begin-track", FILE=str(man)))
    m = [json.loads(x) for x in (store_dir / "repos.jsonl").read_text().splitlines() if '"begin-track"' in x][-1]["migration"]
    assert json.loads([x for x in (store_dir / "repos.jsonl").read_text().splitlines() if '"begin-track"' in x][-1])["grade"] == "relayed"
    iid = lv("board", "add", "--repo", "alpha", "--title", "t", "--origin", "desk 1", "--done", "it works",
             "--key", f"{m}:a", "--payload-hash", "a" * 64, "--by", "desk")
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {iid}"))
    lv("charter", "check", "--repo", str(a))
    refused_without_a_source("board track", M=m)
    assert lv(*printed("board track", M=m)).startswith("tracked alpha")
    lv("board", "context", iid)
    lv("board", "amend", "--id", iid, "--why", "clearer", "--title", "t2", "--by", "desk")
    refused_without_a_source("board decide", **{"NAME#N": iid})
    lv(*printed("board decide", **{"NAME#N": iid}))
    refused_without_a_source("board untrack")                       # (its line carries --sign, which the helper drops)
    his_signature["on"] = True
    # a Touch ID prompt cannot run in a test: the printed line's `--sign` is dropped and the seam seals it, throwaway key
    signed_line = [x for x in printed("board untrack") if x != "--sign"]
    assert lv(*signed_line) == "untracked alpha"
    his_signature["on"] = False     # his signed line (--author-kind operator --sign): a signed seal here
    fill["NAME"] = "beta"
    lv("board", "register", "beta", "--path", str(b), "--words", "his words", "--by", "desk")
    lv(*printed("board begin-track", FILE=str(man), NAME="beta"))
    refused_without_a_source("board abandon-track", NAME="beta")
    lv(*printed("board abandon-track", NAME="beta"))
    assert lv("board", "abandon-done", "beta", "--by", "desk") == "untracked beta"
    assert main(["board", "move", "beta", "--path", str(b), "--words", "w", "--by", "desk"]) == 1   # not named: still closed
    assert "is the operator's" in capsys.readouterr().err


def test_the_board_leaves_are_opened_where_defined_and_parse_imports_no_install():
    # W7: the marks ride on the leaves; build_parser no longer reads install.DESK_BOARD_VERBS
    import inspect

    from levain import cli as cli_mod
    from levain import install as inst_
    from levain.cli import build_parser
    from levain.desk import door

    assert "DESK_BOARD_VERBS" not in inspect.getsource(cli_mod)
    parser = build_parser()
    marks = door.declared_leaves(parser)
    for verb in inst_.DESK_BOARD_VERBS:
        assert marks[tuple(verb.split())] is True, verb


def test_a_worker_on_a_tracked_repo_without_an_item_is_refused_with_the_spelling(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    b = mkrepo("beta")
    track_ready(capsys, tmp_path, board_store_dir, b, "beta")
    ok(capsys, "track", "beta", "--words", "yes", *A)
    with pytest.raises(inst.SpawnRefused, match=r"--item beta#N"):
        _spawn(store, token, registry, b)
    assert "--item NAME#N" in "\n".join(cl._board_modes_section("lv"))


def test_a_ceremony_launch_with_no_item_is_told_its_close(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    m, _ = track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    text = cl._brief_text(store, _spawn(store, token, registry, a, migration=m))
    assert "recorded as `ceremony`" in text and "no board item" in text and "Board-Item" not in text


def test_the_items_repo_decides_and_a_directory_outside_it_is_refused(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")                 # tracking: locked
    elsewhere = mkrepo("gamma")                                                  # not registered yet
    before = (board_store_dir / "repos.jsonl").read_bytes()
    with pytest.raises(inst.SpawnRefused, match="item of alpha"):
        _spawn(store, token, registry, elsewhere, item="alpha#1")
    with pytest.raises(inst.SpawnRefused, match="item of alpha"):
        _spawn(store, token, registry, tmp_path, item="alpha#1")               # not a repository at all
    assert (board_store_dir / "repos.jsonl").read_bytes() == before            # a refused spawn wrote nothing


def test_a_lease_refusal_registers_nothing(desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    g = mkrepo("gamma")
    before = (board_store_dir / "repos.jsonl").read_bytes()
    with pytest.raises(inst.SpawnRefused, match="lease token"):
        _spawn(store, token + 1, registry, g)
    assert (board_store_dir / "repos.jsonl").read_bytes() == before
    _spawn(store, token, registry, g)                                            # the same request, passing: registers
    assert (board_store_dir / "repos.jsonl").read_bytes() != before


def test_git_failing_inside_a_registered_repo_refuses_and_outside_every_one_is_legacy(desk, registry, mkrepo, board_store_dir, tmp_path, capsys, monkeypatch):  # noqa: F811
    from levain.desk import board_registry as br

    store, token = desk
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    (a / "sub").mkdir()
    monkeypatch.setattr(br, "_git", lambda *_a, **_k: (127, "git: TimeoutExpired"))
    with pytest.raises(inst.SpawnRefused, match="git cannot say"):
        _spawn(store, token, registry, a / "sub")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _spawn(store, token, registry, outside).protocol == "legacy"


def test_a_registry_that_moves_between_the_decision_and_the_record_refuses(desk, registry, mkrepo, board_store_dir, tmp_path, capsys, monkeypatch):  # noqa: F811
    from levain.desk import board_registry as br

    store, token = desk
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    real, calls = br.registry_stamp, []

    def stamp(*args, **kw):
        calls.append(1)
        got = real(*args, **kw)
        return got if len(calls) < 3 else (*got, "moved")                     # the third read is the in-transaction one
    monkeypatch.setattr(br, "registry_stamp", stamp)
    conn = sqlite3.connect(store.path)
    before = conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    with pytest.raises(inst.SpawnRefused, match="registry changed"):
        _spawn(store, token, registry, a)
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == before
    conn.close()


def test_an_exact_path_match_wins_over_a_clone_of_another_mode(mkrepo, tmp_path):  # noqa: F811
    from levain.desk import board_registry as br
    from levain.desk import repos_fold as rf

    a = mkrepo("alpha")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(a), str(clone))
    ia, ic = br.identity(a), br.identity(clone)
    git(clone, "remote", "set-url", "origin", ia["origin"] or "")
    ic = br.identity(clone)
    locked = rf.Repo("alpha", ia["path"], ia["origin"], tuple(ia["root"]), mode="tracking", migration="m1")
    mine = rf.Repo("clone", ic["path"], ic["origin"], tuple(ic["root"]))
    assert br._launch_repo(clone, {"alpha": locked, "clone": mine}) is mine
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(a), str(other))
    git(other, "remote", "set-url", "origin", ia["origin"] or "")
    with pytest.raises(br.BoardError, match="different modes"):
        br._launch_repo(other, {"alpha": locked, "clone": mine})


def test_a_tracked_launch_with_no_readable_claim_is_not_closed():
    got = seat_close.check_close("tracked", [], key=KEY, launched_at=T0, episodes=[], closed_items=[], old_close_ok=False,
                                 repo_tracked_now=True)
    assert not got.ok and "no readable board item claim" in got.missing[0]


def test_a_legacy_seat_with_no_item_on_a_repo_that_became_tracked_is_not_stranded():
    got = seat_close.check_close("legacy", [], key=KEY, launched_at=T0, episodes=[], closed_items=[], old_close_ok=False,
                                 repo_tracked_now=True)
    assert got.ok and got.via == "tracked" and "dual-accept" in got.reason
    assert not seat_close.check_close("legacy", [], key=KEY, launched_at=T0, episodes=[], closed_items=[],
                                      old_close_ok=False, repo_tracked_now=False).ok


@pytest.mark.parametrize("launched, at, counts", [
    ("2026-11-17T10:00:00Z", "2026-11-17T14:00:00+05:30", False),           # 08:30Z: before, though it sorts after
    ("2026-11-17T10:00:00Z", "2026-11-17T06:00:00-05:00", True),            # 11:00Z: after, though it sorts before
    ("2026-11-17T10:00:00.500000Z", "2026-11-17T10:00:00Z", True),          # whole seconds: the launch's own second counts
    ("2026-11-17T10:00:00.500000Z", "2026-11-17T09:59:59Z", False),
    ("2026-11-17T10:00:00.500000Z", "2026-11-17T10:00:00.400000Z", False),  # the same precision: compared exactly
    ("2026-11-17T10:00:00Z", "2026-11-17T11:00:00", False),                 # no offset: unknown zone, never counts
])
def test_times_are_compared_as_instants(launched, at, counts):
    got = _close("tracked", launched_at=launched, episodes=[_ep("levain#1", at=at)], closed_items=["levain#1"])
    assert got.ok is counts


def test_an_empty_top_level_tag_list_reads_the_metadata_tags():
    ep = {"source": KEY, "at": "2026-11-17T11:00:00Z", "tags": [], "metadata": {"tags": ["item:levain#1"]}}
    assert _close("tracked", episodes=[ep], closed_items=["levain#1"]).ok


# --- T10 fix 3 ---------------------------------------------------------------------------------------------------------
def test_every_command_the_worker_board_section_prints_parses():
    # codex OPEN 10: the worker's close names `board amend` in its complete form, not a bare `amend --done`
    from types import SimpleNamespace

    from levain.cli import build_parser
    from levain.install import desk_levain

    lv = desk_levain(sys.executable)
    parser = build_parser()
    for items in (("alpha#1",), ("alpha#1", "beta#2")):
        ln = SimpleNamespace(board_items=items, protocol="tracked", session_key=KEY)
        printed = _printed_commands(cl._board_item_section(ln), lv)
        assert sum(a[:2] == ["board", "amend"] for a in printed) == len(items)
        for argv in printed:
            _parses(parser, argv)                                   # concrete: no placeholder left to fill


def test_the_desk_brief_scopes_the_token_to_coord_drops_and_thread_writes():
    src = __import__("inspect").getsource(cl._brief_text)
    assert "Every write goes" not in src
    assert "Every `drops`, `thread` and `coord` write takes your token" in src
    assert "they take no token, and their complete forms are in" in src
    text = "\n".join(cl._board_modes_section("lv"))
    assert "`lv coord spawn" not in text                        # a spawn shape is never printed as a command here


def test_an_item_of_an_unregistered_repo_is_refused_wherever_the_launch_runs(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    # codex OPEN 6 / gemini MED 1: `--item typo#1` names no registered repository, so it cannot be a board item
    store, token = desk
    a, b = mkrepo("alpha"), mkrepo("beta")
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "yes", *A)                        # alpha tracked
    ok(capsys, "register", "beta", "--path", str(b), "--words", "w", *A)      # beta untracked
    outside = tmp_path / "outside"
    outside.mkdir()
    before = (board_store_dir / "repos.jsonl").read_bytes()
    for cwd in (a, b, outside):
        with pytest.raises(inst.SpawnRefused, match="'typo', which is not a registered repository"):
            _spawn(store, token, registry, cwd, item="typo#1")
    assert (board_store_dir / "repos.jsonl").read_bytes() == before            # nothing registered by a refusal
    ln = _spawn(store, token, registry, a, item="alpha#1")                      # the right item in its own repo
    assert (ln.protocol, ln.board_items) == ("tracked", ("alpha#1",))
    with pytest.raises(inst.SpawnRefused, match="item of alpha"):              # a registered repo other than cwd's
        _spawn(store, token, registry, b, item="alpha#1")


def test_an_unreadable_launch_time_strands_no_seat_that_has_no_item():
    # gemini LOW 2: with no item nothing is judged against the launch time, so the dual-accept close holds
    for protocol in ("legacy", "ceremony"):
        got = seat_close.check_close(protocol, [], key=KEY, launched_at="2026-11-17 10:00:00", episodes=[], closed_items=[],
                                     old_close_ok=False, repo_tracked_now=True)
        assert got.ok and got.via == "tracked" and got.missing == (), protocol
    got = seat_close.check_close("legacy", ["levain#1"], key=KEY, launched_at="2026-11-17 10:00:00", episodes=[],
                                 closed_items=["levain#1"], old_close_ok=False, repo_tracked_now=True)
    assert not got.ok and "unreadable" in got.missing[0]                       # with an item, it still reports it


# --- T10 fix 4 ---------------------------------------------------------------------------------------------------------
def _count_launches(store):
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    finally:
        conn.close()


def test_an_item_of_an_unregistered_repo_is_refused_under_an_inactive_registry_and_nothing_is_written(desk, registry, bare_store, mkrepo, tmp_path):  # noqa: F811
    # codex MED (r4): the item is judged at the FIRST decision whatever the registry's state, before anything is written
    store, token = desk
    g = mkrepo("gamma")
    files = sorted(p.name for p in bare_store.iterdir())
    n = _count_launches(store)
    with pytest.raises(inst.SpawnRefused, match="'typo', which is not a registered repository.*not active yet"):
        _spawn(store, token, registry, g, item="typo#1")
    assert sorted(p.name for p in bare_store.iterdir()) == files and "repos.jsonl" not in files   # no registry written
    assert _count_launches(store) == n
    assert _spawn(store, token, registry, g).protocol == "legacy"               # with no item: as before (W6)
    assert not (bare_store / "repos.jsonl").exists()


def test_an_item_of_an_unregistered_repo_registers_nothing_under_an_active_registry(desk, registry, board_store_dir, mkrepo, tmp_path):  # noqa: F811
    store, token = desk
    g = mkrepo("gamma")                                                          # not registered: the launch would register it
    before = (board_store_dir / "repos.jsonl").read_bytes()
    with pytest.raises(inst.SpawnRefused, match="'typo', which is not a registered repository"):
        _spawn(store, token, registry, g, item="typo#1")
    assert (board_store_dir / "repos.jsonl").read_bytes() == before


@pytest.mark.parametrize("pointered", [False, True])
def test_a_worker_launch_with_an_item_and_no_directory_is_refused_by_name(pointered, desk, home, tmp_path, capsys):  # noqa: F811
    # codex MED (r4): an item claim always takes the protocol decision; with no cwd it is refused, never `legacy`
    store, token = desk
    if pointered:
        ok(capsys, "use", str(tmp_path / "store"))
        (tmp_path / "store" / "repos.jsonl").touch()
    _d, t = a_thread(store)
    n = _count_launches(store)
    with pytest.raises(CoordError, match="a worker launch with a board item needs its working directory"):
        store.launch_request(token, harness="claude", role="worker", threads=[t.thread_id], cwd=None, attach_item="alpha#1")
    assert _count_launches(store) == n
    assert store.launch_request(token, harness="claude", role="worker", threads=[t.thread_id], cwd=None).protocol == "legacy"


def test_a_worker_naming_several_threads_on_a_locked_repo_hears_the_several_threads_refusal(decision_seal, desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    # deepseek LOW (r4): both refusals write nothing; the several-threads one is the message it gets first
    store, token = desk
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")                  # tracking: locked to new work
    (_d1, t1), (_d2, t2) = a_thread(store), a_thread(store, "another")
    n = _count_launches(store)
    with pytest.raises(lease.LeaseRefused, match="a worker holds one thread"):
        store.launch_request(token, harness="claude", role="worker", threads=[t1.thread_id, t2.thread_id], cwd=str(a))
    assert _count_launches(store) == n


def _worker_text(items, protocol="tracked"):
    from types import SimpleNamespace
    return "\n".join(cl._board_item_section(SimpleNamespace(board_items=items, protocol=protocol, session_key=KEY)))


def test_the_worker_amend_close_is_the_one_form_and_sets_the_field_the_close_reads(home, tmp_path, capsys):  # noqa: F811
    # docs MED/HIGH 1: the close reads an amend whose `done` is set by a record carrying the seat's launch key (flow's
    # fanin_log._closed_items); the brief prints that form from _board_forms, filled, and says it replaces the DONE line
    import shlex

    from levain.cli import main
    from levain.install import desk_levain

    lv = desk_levain(sys.executable)
    text = _worker_text(("alpha#1",))
    want = cl._board_forms(lv)["board amend --done"].replace("NAME#N", "alpha#1").replace("KEY", KEY)
    assert f"`{want}`" in text and "REPLACES the item's DONE line" in text
    src = __import__("inspect").getsource(cl._board_item_section)
    assert "--launch-key" not in src and "--by worker" not in src  # no hand-rolled spelling
    ok(capsys, "use", str(tmp_path / "store"))
    iid = ok(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "desk 1", "--done", "it works", "--by", "desk")
    argv = shlex.split(want.replace("alpha#1", iid).removeprefix(lv).strip())
    assert main(argv) == 0, capsys.readouterr().err
    rec = json.loads((tmp_path / "store" / "items.jsonl").read_text().splitlines()[-1])
    assert rec["op"] == "amend" and rec["fields"]["done"] == "what remains" and rec["author"]["launch_key"] == KEY


def test_a_tracked_launch_with_no_item_is_told_it_cannot_close_by_the_board():
    # docs MED 3: no close it can never meet; and no placeholder id, no amend line
    text = _worker_text(())
    assert "cannot close by the board" in text and "ask the desk" in text
    assert "amend" not in text and "NAME#N" not in text and "Board-Closes" not in text
    assert "cannot close by the board" not in _worker_text((), "ceremony")


def test_a_multi_item_brief_prints_each_item_and_never_a_placeholder():
    # docs MED 2: every command and trailer carries a real id
    text = _worker_text(("alpha#1", "beta#2"))
    assert "NAME#N" not in text and "NAME" not in text.replace("CURRENT", "")
    for i, r in (("alpha#1", "alpha"), ("beta#2", "beta")):
        assert f"board context {i}`" in text and f"`Board-Item: {i}`" in text and f"`Board-Closes: {i}`" in text
        assert f"`repo:{r}` and `item:{i}`" in text and f"amend --id {i} " in text


def test_the_desk_brief_names_the_manifest_verbs_and_the_operator_signed_form():
    # docs MED 1 (anansi) / LOW 2 (complement): begin-track and track check the manifest, the same sha256; abandon-track
    # does not. LOW 4, 5: the signed form swaps --by too, and untrack is his to run
    text = " ".join(cl._board_modes_section("lv"))
    assert "For `begin-track` and `track` the question" in text and "the SAME sha256 for both" in text
    assert "`decide` and `abandon-track` carry none" in text and "three ceremony verbs" not in text
    assert "`--by operator --author-kind operator --sign` in place of `--by desk ... --source Q-ID`" in text
    assert "all but `untrack`: he runs that one at the laptop" in text
    assert "prints `tracking NAME (migration M)`" in text


def test_the_manifest_rule_the_brief_states_is_the_writers():
    # the brief's verb set for the manifest check, read off the code: begin-track and track pass require_manifest, and
    # abandon-track reads its relay without one
    import inspect

    from levain.desk import board_registry as br
    assert "require_manifest=True" in inspect.getsource(br.begin_track)
    assert "require_manifest=True" in inspect.getsource(br.track)
    assert "require_manifest" not in inspect.getsource(br.abandon_track)
