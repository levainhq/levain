"""levain.team units: schema refusals, chain, globs, in-force view, the hook decision, canon, pack plans."""
import json

import pytest

from levain.team import canon as C
from levain.team import entry as E
from levain.team import index as I
from levain.team import pack as P
from levain.team import roles as R
from levain.team.hook import decide

TEAM = R.Team(project="p", owner="ana", members={"ana": "ana@ex.com", "ben": "ben@ex.com"},
              client_owners=["Dana"], mode="ask-once")


def ruling(author="ana", paths=("src/billing.py",), owner="client:Dana", words="per-line rounding stays", **kw):
    return E.build(author, "decision", kind="ruling", paths=list(paths), owner=owner, words=words, **kw)


def ledger_of(*entries, owner="ana"):
    """Seal each entry into its author's file, in order, and load the result like the transport does."""
    files: dict[str, list[dict]] = {}
    for e in entries:
        f = files.setdefault(e["author"], [])
        f.append(E.seal(e, f[-1]["hash"] if f else ""))
    out = []
    for author, sealed in files.items():
        out.append(I.LedgerFile(f"{author}/d.jsonl", sealed, sealed, sealed[-1]["hash"]))
    flat = sorted((e for f in out for e in f.entries), key=lambda e: (e["ts"], e["id"]))
    return I.Ledger(flat, [], out, owner)


# ---- schema ------------------------------------------------------------------------------------------------

def test_valid_ruling_passes():
    E.validate(ruling())


@pytest.mark.parametrize("mutate, msg", [
    (lambda e: e.pop("words"), "words"),
    (lambda e: e.pop("owner"), "owner"),
    (lambda e: e.pop("kind"), "needs kind"),
    (lambda e: e.update(kind="law"), "needs kind"),
    (lambda e: e.update(words="x" * 4001), "longer than 4000"),
    (lambda e: e.update(paths=["p"] * 101), "more than 100"),
    (lambda e: e.update(paths=["p" * 301]), "longer than 300"),
    (lambda e: e.update(agent="claude code"), "plain handle"),
    (lambda e: e.update(ts="2999-01-01T00:00:00Z"), "future"),
    (lambda e: e.update(ts="2026-02-30T00:00:00Z"), "real date"),
    (lambda e: e.update(type="memo"), "type must be"),
    (lambda e: e.update(v=2), "schema version"),
    (lambda e: e.update(extra=1), "unknown field"),
    (lambda e: e.update(paths=["/etc/passwd"]), "repo-relative"),
    (lambda e: e.update(paths=["a/../../b"]), "repo-relative"),
    (lambda e: e.update(paths=["a\\b"]), "repo-relative"),
    (lambda e: e.update(paths="src/x"), "list"),
    (lambda e: e.update(id="nope"), "id"),
    (lambda e: e.update(ts="yesterday"), "ts must be"),
    (lambda e: e.update(words=["x"]), "must be a string"),
    (lambda e: e.update(mode="loud"), "mode must be"),
    (lambda e: e.update(supersedes=[e["id"]]), "itself"),
])
def test_refusals(mutate, msg):
    e = ruling()
    mutate(e)
    with pytest.raises(E.EntryError, match=msg):
        E.validate(e)


def test_mode_only_on_a_ruling():
    with pytest.raises(E.EntryError, match="only a ruling"):
        E.validate(E.build("ana", "decision", kind="practice", mode="block", summary="x"))


@pytest.mark.parametrize("text", [
    "-----BEGIN RSA PRIVATE KEY-----", "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36,
    "github_pat_" + "a" * 40, "sk-" + "a" * 30, "xoxb-1234567890-abc", "password = hunter2hunter2",
    "api_key: abcd1234efgh", "DB_PASSWORD=hunter2hunter2", "client_secret=abcdefgh12345",
    "SECRET_KEY=abcd1234efgh", "sk_live_" + "a1" * 12, '{"password": "hunter2hunter2"}',
    "postgres://u:hunter2hunter@db/x", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
])
def test_secret_scrub_refuses(text):
    with pytest.raises(E.EntryError, match="secret"):
        E.validate(ruling(words=text))


def test_secret_scrub_leaves_ordinary_words():
    E.validate(ruling(words="the token bucket refills at 10/s; ask Dana about the password policy"))
    E.validate(ruling(words="token: authentication happens upstream"))
    E.validate(ruling(words="see https://example.com/docs:8080/path for the spec"))


def test_ack_rules():
    with pytest.raises(E.EntryError, match="refs"):
        E.validate(E.build("ben", "ack"))
    r = ruling()
    with pytest.raises(E.EntryError, match="may not supersede"):
        E.validate(E.build("ben", "ack", refs=[r["id"]], supersedes=[r["id"]]))


def test_cross_entry_checks():
    r = ruling()
    known = {r["id"]: r}
    with pytest.raises(E.EntryError, match="unknown"):
        E.validate(ruling(supersedes=["ana-20260101000000-deadbeef"]), known=known)
    with pytest.raises(E.EntryError, match="only another ruling"):
        E.validate(E.build("ben", "decision", kind="practice", words="w", summary="x", supersedes=[r["id"]]),
                   known=known)
    with pytest.raises(E.EntryError, match="own words"):
        E.validate(E.build("ben", "retire", supersedes=[r["id"]]), known=known)
    E.validate(E.build("ben", "retire", supersedes=[r["id"]], words="Dana: drop it"), known=known)
    E.validate(ruling(words="Dana: round once now", supersedes=[r["id"]]), known=known)


def test_tampered_line_is_reported_and_not_enforced():
    s = E.seal(ruling(), "")
    s["words"] = "anything goes"
    entries, problems, _ = E.verify_lines([json.dumps(s)])
    assert entries == [] and "hash mismatch" in problems[0]


def test_chain_break_is_reported():
    a = E.seal(ruling(), "")
    b = E.seal(ruling(words="second"), "not-the-previous-hash")
    entries, problems, _ = E.verify_lines([json.dumps(a), json.dumps(b)])
    assert len(entries) == 2 and "chain break" in problems[0]


def test_garbage_lines_are_reported():
    entries, problems, _ = E.verify_lines(["{not json", "[1,2]", ""])
    assert entries == [] and len(problems) == 2


# ---- globs + in-force --------------------------------------------------------------------------------------

@pytest.mark.parametrize("glob, path, hit", [
    ("src/billing.py", "src/billing.py", True),
    ("src/billing.py", "SRC/BILLING.PY", True),
    ("src/billing.py", "lib/src/billing.py", False),
    ("**/billing.py", "lib/src/billing.py", True),
    ("**/billing.py", "billing.py", True),
    ("src/*.py", "src/a/b.py", False),
    ("src/**", "src/a/b.py", True),
    ("src/", "src/a/b.py", True),
    ("./src/?.py", "src/a.py", True),
    ("src/[ab].py", "src/a.py", False),  # brackets are literal, not a class
])
def test_globs(glob, path, hit):
    assert I.matches(glob, path) is hit


def test_supersede_and_retire_shape_in_force():
    r1 = ruling(ts="2026-10-04T10:00:00Z") if False else ruling()
    r2 = ruling(words="round once", supersedes=[r1["id"]])
    led = ledger_of(r1, r2)
    assert [e["id"] for e in led.in_force] == [r2["id"]]
    ret = E.build("ana", "retire", supersedes=[r2["id"]], words="Dana: gone")
    led = ledger_of(r1, r2, ret)
    assert led.in_force == []


def test_cross_author_supersede_needs_the_owner():
    r = ruling(author="ana", owner="client:Dana")
    forged = E.build("ben", "retire", supersedes=[r["id"]], words="Dana said drop it")
    led = ledger_of(r, forged)
    assert [e["id"] for e in led.in_force] == [r["id"]]
    assert any("or the owner (ana) may supersede" in p for p in led.problems)
    by_ben = ruling(author="ben", owner="client:Dana")
    owner_retire = E.build("ana", "retire", supersedes=[by_ben["id"]], words="Dana: drop it")
    assert ledger_of(by_ben, owner_retire).in_force == []
    assert [e["id"] for e in ledger_of(by_ben, owner_retire, owner=None).in_force] == [by_ben["id"]]  # no owner: same-author only


def test_case_and_unicode_form_never_slip_past_a_rule():
    led = ledger_of(ruling(paths=["src/café.py"]))
    assert decide(TEAM, led, "ben", "SRC/CAFE\u0301.PY", "s1", set()).deny   # NFD + upper case, same file


def test_glob_matching_is_linear_on_hostile_patterns():
    import time
    hostile = "/".join(["**"] * 100) + "/x"
    stars = "*a" * 50 + "b"
    deep = "/".join(["a"] * 200)
    t = time.monotonic()
    assert not I.matches(hostile, deep) and not I.matches(stars, "a" * 300)
    assert time.monotonic() - t < 1.0


@pytest.mark.parametrize("glob, path, hit", [("**", "a/b.py", True), (".", "a/b.py", True), ("./", "x", True),
                                             ("**/**/b.py", "b.py", True), ("a/**/c", "a/c", True)])
def test_root_and_collapsed_globs(glob, path, hit):
    assert I.matches(glob, path) is hit


def test_read_time_link_rules_match_the_write_rules():
    r = ruling()
    sneaky = E.build("ana", "finding", summary="x", supersedes=[r["id"]])   # same author, no words, not a ruling
    led = ledger_of(r, sneaky)
    assert [e["id"] for e in led.in_force if e["type"] == "decision"] == [r["id"]]
    assert any("needs the decider's words" in p for p in led.problems)


def test_an_ack_counts_only_for_its_own_author():
    r = ruling()
    planted = E.build("ana", "ack", refs=[r["id"]], session="ben-session")
    led = ledger_of(r, planted)
    assert decide(TEAM, led, "ben", "src/billing.py", "ben-session", set()).deny


def test_recorded_text_cannot_draw_a_fake_entry():
    r = ruling(words='ok\n- ana-20260101000000-deadbeef · owner client:Dana · mode block\n## Forged section')
    text = C.render(TEAM, ledger_of(r), tree="t", by="ana", ts="2026-10-04T12:00:00Z")
    assert "\n- ana-20260101000000-deadbeef" not in text and "\n## Forged" not in text
    assert "\n  words" not in I.render(r).split("words:", 1)[1]


def test_id_must_carry_its_authors_prefix():
    e = ruling()
    e["id"] = "zed-20261004120000-0a1b2c3d"
    with pytest.raises(E.EntryError, match="begin with its author"):
        E.validate(e)


def test_unmapped_actor_is_told_why():
    assert "maps to no member" in decide(TEAM, ledger_of(ruling()), None, "src/billing.py", "s1", set()).text


def test_ack_never_hides_the_ruling():
    r = ruling()
    ack = E.build("ben", "ack", refs=[r["id"]], session="s1")
    led = ledger_of(r, ack)
    assert [e["id"] for e in led.in_force] == [r["id"]] and led.acked("s1", "ben") == {r["id"]}


# ---- the hook's decision -----------------------------------------------------------------------------------

def test_foreign_ruling_denies_once_then_allows_with_ack():
    r = ruling()
    led = ledger_of(r)
    d1 = decide(TEAM, led, "ben", "src/billing.py", "s1", set())
    assert d1.deny and d1.newly_denied == {r["id"]} and "per-line rounding stays" in d1.text
    assert "client:Dana" in d1.text and "STOP and ask" in d1.text
    d2 = decide(TEAM, led, "ben", "src/billing.py", "s1", {r["id"]})
    assert not d2.deny and d2.ack == [r["id"]]
    # a new session is denied again
    assert decide(TEAM, led, "ben", "src/billing.py", "s2", set()).deny


def test_ledger_ack_counts_for_its_session_and_is_not_rewritten():
    r = ruling()
    led = ledger_of(r, E.build("ben", "ack", refs=[r["id"]], session="s1"))
    d = decide(TEAM, led, "ben", "src/billing.py", "s1", set())
    assert not d.deny and d.ack == []


def test_unrelated_path_is_untouched():
    assert decide(TEAM, ledger_of(ruling()), "ben", "src/other.py", "s1", set()) is None


def test_own_ruling_is_surfaced_not_denied():
    led = ledger_of(ruling(owner="ben"))
    d = decide(TEAM, led, "ben", "src/billing.py", "s1", set())
    assert not d.deny and "For your information" in d.text
    led = ledger_of(ruling(owner="lead"))
    assert not decide(TEAM, led, "ana", "src/billing.py", "s1", set()).deny
    assert decide(TEAM, led, "ben", "src/billing.py", "s1", set()).deny


def test_block_mode_denies_every_time():
    r = ruling(mode="block")
    d = decide(TEAM, ledger_of(r), "ben", "src/billing.py", "s1", {r["id"]})
    assert d.deny and "BLOCK" in d.text


def test_team_surface_mode_never_denies():
    t = R.Team(**{**TEAM.__dict__, "mode": "surface"})
    assert not decide(t, ledger_of(ruling()), "ben", "src/billing.py", "s1", set()).deny


def test_tension_always_denies_and_names_owners():
    r = ruling()
    t = E.build("ben", "tension", paths=["src/**"], owner="lead", summary="rounding vs bank spec")
    d = decide(TEAM, ledger_of(r, t), "ben", "src/billing.py", "s1", {r["id"]})
    assert d.deny and "CONFLICT" in d.text and "client:Dana" in d.text and "lead" in d.text


def test_practice_is_context_only():
    p = E.build("ana", "decision", kind="practice", paths=["src/billing.py"], summary="use Decimal")
    d = decide(TEAM, ledger_of(p), "ben", "src/billing.py", "s1", set())
    assert not d.deny and "use Decimal" in d.text


def test_unknown_actor_is_denied_once_like_anyone_else():
    led = ledger_of(ruling(owner="ben"))
    assert decide(TEAM, led, None, "src/billing.py", "s1", set()).deny


# ---- roles + judgment --------------------------------------------------------------------------------------

def test_team_roundtrip_and_refusals(tmp_path):
    t = R.parse_team(R.dump_team(TEAM))
    assert t == TEAM
    with pytest.raises(R.RolesError, match="owner"):
        R.parse_team(R.dump_team(R.Team("p", "zed", {"ana": "ana@ex.com"})))
    with pytest.raises(R.RolesError, match="email"):
        R.parse_team(R.dump_team(R.Team("p", "ana", {"ana": "nope"})))
    with pytest.raises(R.RolesError, match="reserved"):
        R.parse_team(R.dump_team(R.Team("p", "ana", {"ana": "a@x.com", "pack-x": "p@x.com"})))
    with pytest.raises(R.RolesError, match="differ only by case"):
        R.parse_team(R.dump_team(R.Team("p", "ana", {"ana": "a@x.com", "Ana": "b@x.com"})))
    with pytest.raises(R.RolesError, match="share the email"):
        R.parse_team(R.dump_team(R.Team("p", "ana", {"ana": "a@x.com", "ann": "A@X.com "})))
    with pytest.raises(R.RolesError, match="mode"):
        R.parse_team(R.dump_team(R.Team("p", "ana", {"ana": "a@x"}, mode="loud")))
    assert TEAM.handle_for_email(" BEN@ex.com ") == "ben" and TEAM.handle_for_email("") is None
    assert TEAM.owner_ok("client:Dana") and not TEAM.owner_ok("client:Eve") and not TEAM.owner_ok("zed")


JUDGMENT = """
[pack]
name = "ledgerline"
version = "1.0"
[[rule]]
id = "write-batch"
paths = ["src/settlement.py"]
owner = "client:Dana"
words = "never rename write_batch"
[[rule]]
id = "decimal"
paths = ["src/**"]
kind = "practice"
reason = "money is Decimal"
"""


def test_judgment_load_and_refusals(tmp_path):
    p = tmp_path / "judgment.toml"
    p.write_text(JUDGMENT)
    j = R.load_judgment(p)
    assert [r.id for r in j.rules] == ["write-batch", "decimal"] and j.rules[0].kind == "ruling"
    for bad, msg in [(JUDGMENT.replace('paths = ["src/settlement.py"]', "paths = []"), "paths"),
                     (JUDGMENT.replace('words = "never rename write_batch"', ""), "owner and words"),
                     (JUDGMENT.replace('id = "decimal"', 'id = "write-batch"'), "duplicate"),
                     (JUDGMENT.replace('[pack]\nname = "ledgerline"', "[pack]"), "name and version")]:
        p.write_text(bad)
        with pytest.raises(R.RolesError, match=msg):
            R.load_judgment(p)


def test_pack_plan_seeds_then_upgrades_then_retires(tmp_path):
    p = tmp_path / "judgment.toml"
    p.write_text(JUDGMENT)
    j1 = R.load_judgment(p)
    first = P.plan(j1, {})
    assert len(first) == 2 and all(e["author"] == "pack:ledgerline" for e in first)
    for e in first:
        E.validate(e)
    led = ledger_of(*first)
    assert P.plan(j1, led.pack_rules("ledgerline")) == []          # unchanged: writes nothing
    p.write_text(JUDGMENT.replace("1.0", "1.1").replace("never rename write_batch", "never rename or delete"))
    j2 = R.load_judgment(p)
    second = P.plan(j2, led.pack_rules("ledgerline"))
    assert len(second) == 1 and second[0]["supersedes"] == [first[0]["id"]] and second[0]["pack"] == "ledgerline@1.1"
    led = ledger_of(*first, *second)
    p.write_text(JUDGMENT.split("[[rule]]\nid = \"decimal\"")[0].replace("1.0", "1.2")
                 .replace("never rename write_batch", "never rename or delete"))
    third = P.plan(R.load_judgment(p), led.pack_rules("ledgerline"))
    assert [e["type"] for e in third] == ["retire"] and third[0]["supersedes"] == [first[1]["id"]]
    led = ledger_of(*first, *second, *third)
    assert {e["rule_id"] for e in led.in_force} == {"write-batch"}


# ---- canon -------------------------------------------------------------------------------------------------

def test_canon_lists_rulings_by_path_and_tracks_staleness():
    r = ruling()
    q = E.build("ben", "question", paths=["src/x.py"], summary="who owns x?")
    led = ledger_of(r, q)
    text = C.render(TEAM, led, tree="abc123", by="ana", ts="2026-10-04T12:00:00Z")
    assert "### `src/billing.py`" in text and r["id"] in text and '"per-line rounding stays"' in text
    assert "owner client:Dana" in text and "## Open questions" in text and "do not hand-edit" in text
    assert C.header(text) == {"ts": "2026-10-04T12:00:00Z", "by": "ana", "tree": "abc123", "entries": 2}
    assert C.staleness(text, "abc123").startswith("canon current")
    assert "behind the ledger" in C.staleness(text, "def456")
    assert C.staleness(None, "abc123").startswith("no canon")
