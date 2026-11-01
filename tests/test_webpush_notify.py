"""The notify core (N1): tokens, the sanitiser, the notices store and the policy decision. No HTTP route here. Built from
the hand runs of the N1 brief."""
from __future__ import annotations

import json
import os
import threading

import pytest

from levain.webpush import notify, notify_tokens as nt, presence
from levain.webpush.notify import NotifyRefused
from levain.webpush.presence import DEFAULT_CELLS
from levain.webpush.store import NOTICES_KEEP, PushStore

NOW = 1_000_000.0
PHILL = {"quiet": dict(DEFAULT_CELLS["quiet"]), "sleep": {"approval": "push", "attention": "push", "other": "hold"}}
DEFAULT = {m: dict(c) for m, c in DEFAULT_CELLS.items()}


@pytest.fixture(autouse=True)
def _fresh_minute_window():
    nt.MINUTE.clear()
    yield
    nt.MINUTE.clear()


def _refused(**kw):
    with pytest.raises(NotifyRefused) as e:
        nt.admit(**kw)
    return e.value


def _mint(q, name="Backup", **kw):
    rec, tok = nt.create(q, name, now=NOW, **kw)
    return rec, tok


# --- tokens: one of each shape -------------------------------------------------------------------------

def test_create_shows_the_token_once_and_keeps_only_its_hash(tmp_path):
    rec, tok = _mint(tmp_path)
    assert tok.startswith(nt.TOKEN_PREFIX) and len(tok) > 40
    f = tmp_path / "webpush" / nt.FILE_NAME
    assert tok not in f.read_text() and nt._hash(tok) in f.read_text()
    assert (f.stat().st_mode & 0o777) == 0o600
    assert "sha256" not in rec and tok not in json.dumps(nt.list_public(tmp_path))
    assert rec["source"] == "backup" and rec["max_priority"] == "normal" and rec["urgent"] is False
    assert (rec["rate_per_min"], rec["rate_per_day"], rec["urgent_per_hour"]) == (6, 200, 3)


def test_one_token_of_each_shape(tmp_path):
    _, low = _mint(tmp_path, "Lowly", source="lowly", max_priority="low")
    _, norm = _mint(tmp_path, "Normal", source="norm")
    rec, urg = _mint(tmp_path, "Alarm", source="alarm", urgent=True)
    assert rec["max_priority"] == "urgent" and rec["urgent"] is True
    assert nt.admit(tmp_path, low, "t", "b", "low", now=NOW).kind == "notify"
    assert nt.admit(tmp_path, norm, "t", "b", None, now=NOW).priority == "normal"
    a = nt.admit(tmp_path, urg, "t", "b", "urgent", now=NOW)
    assert a.kind == "notify_urgent" and a.display == "[alarm] t" and a.source == "alarm"
    assert _refused(queue_dir=tmp_path, supplied=low, title="t", body="b", priority="normal", now=NOW).error == "priority_above_token_max"


def test_verify_accepts_only_the_live_token(tmp_path):
    _, tok = _mint(tmp_path)
    assert nt.verify(tmp_path, tok)["name"] == "Backup"
    for bad in ("", "x", tok + "x", tok[:-1], None, 7):
        assert nt.verify(tmp_path, bad) is None


def test_verify_compares_every_record(tmp_path, monkeypatch):
    for n in ("a", "b", "c"):
        _mint(tmp_path, n)
    _, tok = _mint(tmp_path, "d")
    seen = []
    real = nt.hmac.compare_digest
    monkeypatch.setattr(nt.hmac, "compare_digest", lambda x, y: (seen.append(1), real(x, y))[1])
    assert nt.verify(tmp_path, tok) is not None
    assert len(seen) == 4


@pytest.mark.parametrize("kw", [dict(name=""), dict(name=" x"), dict(name="a/b"), dict(name="x" * 41),
                                dict(name="ok", source="Upper"), dict(name="ok", source="a b"), dict(name="ok", source="x" * 25),
                                dict(name="ok", source="-lead"), dict(name="ok", max_priority="high"),
                                dict(name="ok", max_priority="urgent"), dict(name="ok", urgent="yes"),
                                dict(name="ok", rate_per_min=0), dict(name="ok", rate_per_day=True),
                                dict(name="Has Space")])
def test_bad_mint_is_refused(tmp_path, kw):
    name = kw.pop("name")
    if name == "Has Space":                        # a name with a space is fine; the derived source is the slug
        assert nt.create(tmp_path, name)[0]["source"] == "has-space"
        return
    with pytest.raises(nt.NotifyTokenError):
        nt.create(tmp_path, name, **kw)


def test_name_and_source_are_unique_among_live_and_a_revoked_one_can_be_reused(tmp_path):
    _mint(tmp_path, "one", source="src")
    with pytest.raises(nt.NotifyTokenError):
        _mint(tmp_path, "one", source="other")
    with pytest.raises(nt.NotifyTokenError):
        _mint(tmp_path, "two", source="src")
    assert nt.revoke(tmp_path, "one") is True
    _mint(tmp_path, "one", source="src")


def test_list_never_carries_a_secret_or_a_hash(tmp_path):
    _, tok = _mint(tmp_path)
    blob = json.dumps(nt.list_public(tmp_path))
    assert tok not in blob and nt._hash(tok) not in blob and "sha256" not in blob


def test_source_is_immutable_there_is_no_way_to_change_it(tmp_path):
    _, tok = _mint(tmp_path, source="first")
    a = nt.admit(tmp_path, tok, "t", "b", now=NOW)
    assert a.source == "first"
    assert not hasattr(nt, "rename") and not hasattr(nt, "set_source")


def test_last_used_is_stamped_only_on_an_admitted_post(tmp_path):
    _, tok = _mint(tmp_path)
    _refused(queue_dir=tmp_path, supplied=tok, title="", body="b", now=NOW + 5)
    assert nt.list_public(tmp_path)[0]["last_used"] is None
    nt.admit(tmp_path, tok, "t", "b", now=NOW + 6)
    assert nt.list_public(tmp_path)[0]["last_used"] == NOW + 6


# --- revoke ---------------------------------------------------------------------------------------------

def test_revoke_is_instant_and_unknown_or_revoked_read_the_same(tmp_path):
    _, tok = _mint(tmp_path)
    nt.admit(tmp_path, tok, "t", "b", now=NOW)
    assert nt.revoke(tmp_path, "Backup") is True and nt.revoke(tmp_path, "Backup") is False
    assert nt.verify(tmp_path, tok) is None
    gone = _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b", now=NOW + 1)
    unknown = _refused(queue_dir=tmp_path, supplied="nope", title="t", body="b", now=NOW + 1)
    assert (gone.error, gone.status) == (unknown.error, unknown.status) == ("forbidden", 403)
    assert str(gone) == str(unknown)


def test_a_revoke_between_the_first_look_and_the_post_refuses_it(tmp_path, monkeypatch):
    # the revoke fires at the last seam before admit takes its lock: the post must not be admitted afterwards
    _, tok = _mint(tmp_path)
    assert nt.verify(tmp_path, tok) is not None          # the "first check" of a caller
    real = nt._locked
    fired = []

    def revoke_first(q, wait=3.0):
        if not fired:
            fired.append(None)
            fired[0] = nt.revoke(q, "Backup")
        return real(q, wait)

    monkeypatch.setattr(nt, "_locked", revoke_first)
    assert _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b", now=NOW).error == "forbidden"
    assert fired == [True]


def test_a_revoke_during_the_post_waits_for_it_and_the_next_post_fails(tmp_path):
    _, tok = _mint(tmp_path)
    inside, go, order = threading.Event(), threading.Event(), []
    real_write = nt._write

    def slow_write(q, recs):
        if not inside.is_set():                          # the admitted post's write; the revoke's own write passes through
            inside.set()
            go.wait(2)
            order.append("write")
        return real_write(q, recs)

    nt._write = slow_write
    try:
        post = threading.Thread(target=lambda: order.append(("admitted", nt.admit(tmp_path, tok, "t", "b", now=NOW).title)))
        post.start()
        assert inside.wait(2)
        rv = threading.Thread(target=lambda: order.append(("revoked", nt.revoke(tmp_path, "Backup"))))
        rv.start()
        go.set()
        post.join(3)
        rv.join(3)
    finally:
        nt._write = real_write
    assert order == ["write", ("admitted", "t"), ("revoked", True)]      # the revoke came after the admitted post, never inside it
    assert _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b", now=NOW + 1).error == "forbidden"


def test_a_failed_read_refuses_a_write_and_leaves_the_file(tmp_path, monkeypatch):
    import errno

    _mint(tmp_path, "door")
    _mint(tmp_path, "bed")
    f = tmp_path / "webpush" / nt.FILE_NAME
    before, ino = f.read_bytes(), f.stat().st_ino
    real = os.open

    def emfile(path, *a, **kw):
        if os.fspath(path).endswith(nt.FILE_NAME):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, *a, **kw)

    monkeypatch.setattr(nt.os, "open", emfile)
    with pytest.raises(nt.NotifyTokenError):
        _mint(tmp_path, "hall")
    with pytest.raises(nt.NotifyTokenError):
        nt.revoke(tmp_path, "door")
    assert nt.verify(tmp_path, "x") is None and nt.list_public(tmp_path) == []
    monkeypatch.setattr(nt.os, "open", real)
    assert f.read_bytes() == before and f.stat().st_ino == ino


def test_a_malformed_or_symlinked_file_is_not_overwritten(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir()
    (d / nt.FILE_NAME).write_text("not json")
    with pytest.raises(nt.NotifyTokenError):
        _mint(tmp_path)
    assert (d / nt.FILE_NAME).read_text() == "not json"
    (d / nt.FILE_NAME).unlink()
    (d / "real.json").write_text("{}")
    os.symlink(d / "real.json", d / nt.FILE_NAME)
    with pytest.raises(nt.NotifyTokenError):
        _mint(tmp_path)
    assert (d / "real.json").read_text() == "{}"


# --- the sanitiser: every refusal ----------------------------------------------------------------------

@pytest.fixture
def tok(tmp_path):
    return _mint(tmp_path, "Backup")[1]


def test_oversize_is_refused_never_truncated(tmp_path, tok):
    assert nt.admit(tmp_path, tok, "t" * 80, "b" * 1000, now=NOW).title == "t" * 80
    e = _refused(queue_dir=tmp_path, supplied=tok, title="t" * 81, body="b", now=NOW + 1)
    assert (e.error, e.status) == ("title_too_long", 413)
    e = _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b" * 1001, now=NOW + 1)
    assert (e.error, e.status) == ("body_too_long", 413)


@pytest.mark.parametrize("bad", ["a\x00b", "a\x1bb", "a\x7fb", "a\x85b", "a\u202eb", "a\u2066b", "a\u200bb", "a\u200db",
                                 "a\ufeffb", "a\U000e0041b", "a\u2028b", "a\u2029b", "a\ue000b"])
def test_control_bidi_zero_width_and_tag_characters_are_refused(tmp_path, tok, bad):
    assert _refused(queue_dir=tmp_path, supplied=tok, title=bad, body="b", now=NOW).error == "bad_characters"
    assert _refused(queue_dir=tmp_path, supplied=tok, title="t", body=bad, now=NOW + 1).error == "bad_characters"


def test_newline_is_body_only_and_crlf_is_normalised(tmp_path, tok):
    assert nt.admit(tmp_path, tok, "t", "a\r\nb\n\n\n\nc", now=NOW).body == "a\nb\n\nc"
    assert _refused(queue_dir=tmp_path, supplied=tok, title="a\nb", body="b", now=NOW + 1).error == "bad_characters"
    assert _refused(queue_dir=tmp_path, supplied=tok, title="t", body="a\tb", now=NOW + 1).error == "bad_characters"


def test_markup_is_plain_text_and_a_url_stays_a_string(tmp_path, tok):
    a = nt.admit(tmp_path, tok, "<b>x</b> & more", "<script>1</script> https://example.test/a?b=c", now=NOW)
    assert a.title == "<b>x</b> & more" and a.body == "<script>1</script> https://example.test/a?b=c"


@pytest.mark.parametrize("title", ["[Seat 4] needs you", "  [NEEDS YOU] approve", "\uff3bNeeds you\uff3d x", "\u3010seat\u3011 x", "[a]"])
def test_a_title_that_opens_with_a_bracketed_label_is_refused(tmp_path, tok, title):
    assert _refused(queue_dir=tmp_path, supplied=tok, title=title, body="b", now=NOW).error == "spoofed_label"


def test_a_bracket_later_in_the_title_is_fine_and_the_server_builds_the_label(tmp_path, tok):
    a = nt.admit(tmp_path, tok, "Needs you: disk [90%]", "b", now=NOW)
    assert a.display == "[backup] Needs you: disk [90%]"


def test_wrong_types_and_empties_are_refused(tmp_path, tok):
    for t, b in ((None, "b"), (5, "b"), ("t", None), ("t", ["x"]), ("  ", "b"), ("t", "   ")):
        assert _refused(queue_dir=tmp_path, supplied=tok, title=t, body=b, now=NOW).error in ("bad_shape", "empty")
    for p in ("high", "URGENT", 3, "", ["low"]):
        assert _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b", priority=p, now=NOW).error == "bad_priority"


# --- priority ceiling ----------------------------------------------------------------------------------

def test_urgent_from_a_non_urgent_token_is_refused_with_a_named_reason_never_downgraded(tmp_path, tok):
    e = _refused(queue_dir=tmp_path, supplied=tok, title="t", body="b", priority="urgent", now=NOW)
    assert (e.error, e.status) == ("urgent_not_permitted", 403)
    assert nt.list_public(tmp_path)[0]["last_used"] is None            # nothing admitted, nothing counted


def test_a_non_urgent_token_can_never_produce_an_urgent_kind(tmp_path):
    _, t = _mint(tmp_path, "Plain", source="plain", max_priority="normal")
    for p in (None, "low", "normal"):
        assert nt.admit(tmp_path, t, "t", "b", p, now=NOW + (0 if p is None else 70 * (1 + len(str(p))))).kind == "notify"
    # the stored record cannot be talked into it either: a hand-edited max_priority without the urgent flag still refuses
    f = tmp_path / "webpush" / nt.FILE_NAME
    doc = json.loads(f.read_text())
    doc["tokens"][0]["max_priority"] = "urgent"
    f.write_text(json.dumps(doc))
    assert _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", priority="urgent", now=NOW + 999).error == "urgent_not_permitted"


# --- rate caps -----------------------------------------------------------------------------------------

def test_per_minute_cap(tmp_path):
    _, t = _mint(tmp_path, "m", rate_per_min=2)
    nt.admit(tmp_path, t, "t", "b", now=NOW)
    nt.admit(tmp_path, t, "t", "b", now=NOW + 1)
    e = _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", now=NOW + 2)
    assert (e.error, e.status) == ("rate_limited", 429) and 57 <= e.retry_after <= 59
    nt.admit(tmp_path, t, "t", "b", now=NOW + 61)


def test_per_day_cap_and_the_next_utc_day_resets_it(tmp_path):
    day0 = 20_000 * 86400.0
    _, t = _mint(tmp_path, "d", rate_per_day=3, rate_per_min=100)
    for i in range(3):
        nt.admit(tmp_path, t, "t", "b", now=day0 + 100 + i)
    e = _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", now=day0 + 500)
    assert (e.error, e.status) == ("rate_limited_day", 429) and e.retry_after == pytest.approx(86400 - 500)
    nt.admit(tmp_path, t, "t", "b", now=day0 + 86400 + 1)


def test_urgent_hour_cap(tmp_path):
    _, t = _mint(tmp_path, "u", urgent=True, rate_per_min=100)
    for i in range(3):
        nt.admit(tmp_path, t, "t", "b", "urgent", now=NOW + i * 10)
    e = _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", priority="urgent", now=NOW + 40)
    assert (e.error, e.status) == ("rate_limited_urgent", 429)
    nt.admit(tmp_path, t, "t", "b", "normal", now=NOW + 41)           # an ordinary notice from the same token is not capped by it
    nt.admit(tmp_path, t, "t", "b", "urgent", now=NOW + 3601)


def test_a_refused_post_does_not_spend_the_cap(tmp_path):
    _, t = _mint(tmp_path, "r", rate_per_min=1)
    for _ in range(5):
        _refused(queue_dir=tmp_path, supplied=t, title="x" * 99, body="b", now=NOW)
    nt.admit(tmp_path, t, "t", "b", now=NOW)


def test_restart_keeps_the_day_counter_and_drops_the_minute_window(tmp_path):
    day0 = 20_000 * 86400.0
    _, t = _mint(tmp_path, "s", rate_per_day=2, rate_per_min=1)
    nt.admit(tmp_path, t, "t", "b", now=day0 + 10)
    assert _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", now=day0 + 11).error == "rate_limited"
    nt.MINUTE.clear()                                                 # a restart: the in-memory window is gone
    nt.admit(tmp_path, t, "t", "b", now=day0 + 12)                    # the minute counter reset
    nt.MINUTE.clear()
    e = _refused(queue_dir=tmp_path, supplied=t, title="t", body="b", now=day0 + 13)
    assert e.error == "rate_limited_day"                              # the day counter did not
    assert nt.list_public(tmp_path)[0]["sent_today"] >= 0


def test_caps_are_per_token(tmp_path):
    _, a = _mint(tmp_path, "a", rate_per_min=1)
    _, b = _mint(tmp_path, "b", rate_per_min=1)
    nt.admit(tmp_path, a, "t", "b", now=NOW)
    nt.admit(tmp_path, b, "t", "b", now=NOW)


# --- the policy decision -------------------------------------------------------------------------------

def _sleep_word():
    return {"word": "sleep", "ts": NOW - 100}


def test_kinds_and_classes():
    assert presence.kind_class("notify") == "other" and presence.kind_class("notify_urgent") == "attention"
    assert "notify_urgent" in presence.URGENT_KINDS and "notify" not in presence.URGENT_KINDS
    assert presence.URGENT_KINDS == {k for k, c in presence.KIND_CLASSES.items() if c != "other"}
    assert notify.kind_for("low") == notify.kind_for("normal") == "notify" and notify.kind_for("urgent") == "notify_urgent"
    with pytest.raises(ValueError):
        notify.kind_for("high")


def test_a_sleep_notice_under_the_default_table_every_priority_is_held_or_recorded():
    # idle 1000 < now - word.ts: no input since the word, so sleep still holds
    got = {p: notify.notify_decision(p, NOW, _sleep_word(), 1000.0, policy=DEFAULT) for p in notify.PRIORITIES}
    assert got == {"low": "recorded", "normal": "held", "urgent": "held"}


def test_a_sleep_notice_under_phills_table_only_an_urgent_one_rings():
    got = {p: notify.notify_decision(p, NOW, _sleep_word(), 1000.0, policy=PHILL) for p in notify.PRIORITIES}
    assert got == {"low": "recorded", "normal": "held", "urgent": "pushed"}


def test_quiet_and_the_open_desk(tmp_path):
    quiet = {"word": "quiet", "ts": NOW - 10, "until": NOW + 600}
    assert notify.notify_decision("normal", NOW, quiet, 1000.0, policy=DEFAULT) == "held"
    assert notify.notify_decision("urgent", NOW, quiet, 1000.0, policy=DEFAULT) == "pushed"      # the default quiet attention cell
    # no mute: an ordinary notice follows the desk (active holds, idle pushes), an urgent one pushes
    assert notify.notify_decision("normal", NOW, None, 5.0) == "held"
    assert notify.notify_decision("normal", NOW, None, 99999.0) == "pushed"
    assert notify.notify_decision("urgent", NOW, None, 5.0) == "pushed"
    loud = {"word": "loud", "ts": NOW - 10, "until": NOW + 600}
    assert notify.notify_decision("normal", NOW, loud, 5.0, policy=DEFAULT) == "pushed"
    assert notify.notify_decision("low", NOW, loud, 5.0) == "recorded"


def test_the_decision_is_the_operators_tables_not_the_senders(tmp_path):
    # the same urgent notice moves with the table alone
    assert (notify.notify_decision("urgent", NOW, _sleep_word(), 1000.0, policy=DEFAULT),
            notify.notify_decision("urgent", NOW, _sleep_word(), 1000.0, policy=PHILL)) == ("held", "pushed")


# --- the notices store ---------------------------------------------------------------------------------

def test_notice_roundtrip_dismiss_and_bounded_read(tmp_path):
    s = PushStore(tmp_path)
    a = s.add_notice("backup", "t1", "b1", "normal", "Backup", "held", now=NOW)
    b = s.add_notice("alarm", "t2", "b2", "urgent", "Alarm", "pushed", now=NOW + 1)
    got = s.notices()
    assert [n["id"] for n in got] == [b, a] and got[0]["source"] == "alarm" and got[0]["dismissed_at"] is None
    assert set(got[0]) == {"id", "at", "source", "title", "body", "priority", "token", "decision", "ping_id", "dismissed_at"}
    assert s.dismiss_notice(a, now=NOW + 2) is True and s.dismiss_notice(a) is False and s.dismiss_notice(999) is False
    assert [n["id"] for n in s.notices()] == [b]
    assert [n["id"] for n in s.notices(include_dismissed=True)] == [b, a]
    assert len(s.notices(limit=1)) == 1 and s.notices(limit=0) == [] and len(s.notices(limit=10**9, include_dismissed=True)) == 2


def test_the_store_refuses_a_bad_priority_or_decision(tmp_path):
    import sqlite3

    s = PushStore(tmp_path)
    for pr, dec in (("high", "held"), ("low", "sent")):
        with pytest.raises(sqlite3.IntegrityError):
            s.add_notice("x", "t", "b", pr, "T", dec, now=NOW)


def test_retention_prunes_at_501_rows(tmp_path):
    assert NOTICES_KEEP == 500                            # the ruled number, not whatever the constant says
    s = PushStore(tmp_path)
    ids = [s.add_notice("x", f"t{i}", "b", "normal", "T", "recorded", now=NOW + i) for i in range(NOTICES_KEEP)]
    assert len(s.notices(limit=10**6, include_dismissed=True)) == NOTICES_KEEP
    last = s.add_notice("x", "newest", "b", "normal", "T", "recorded", now=NOW + 600)
    rows = s.notices(limit=10**6, include_dismissed=True)
    assert len(rows) == NOTICES_KEEP and rows[0]["id"] == last and ids[0] not in {r["id"] for r in rows} and ids[1] in {r["id"] for r in rows}


def test_retention_prunes_past_30_days_and_a_dismissed_row_follows_the_same_rule(tmp_path):
    s = PushStore(tmp_path)
    old = s.add_notice("x", "old", "b", "normal", "T", "recorded", now=NOW)
    edge = s.add_notice("x", "edge", "b", "normal", "T", "recorded", now=NOW + 86400)
    s.dismiss_notice(old)
    s.add_notice("x", "fresh", "b", "normal", "T", "recorded", now=NOW + 30 * 86400 + 60)     # 'old' is now 30 days and a minute old
    ids = {r["id"] for r in s.notices(limit=100, include_dismissed=True)}
    assert old not in ids and edge in ids
    s.add_notice("x", "later", "b", "normal", "T", "recorded", now=NOW + 31 * 86400 + 86400 + 60)
    assert edge not in {r["id"] for r in s.notices(limit=100, include_dismissed=True)}


def test_notices_do_not_touch_the_ping_record(tmp_path):
    s = PushStore(tmp_path)
    s.add_notice("x", "t", "b", "normal", "T", "recorded", now=NOW)
    assert s.record() == []
