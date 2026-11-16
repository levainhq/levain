"""levain#66 slice S1: the host-grant format (``levain/slot/egress_grants.py``), its signed-message namespace
(``levain-egress-host`` in ``levain/contracts/namespaces.py``) and the choice support for it in
``levain/consent/sign.py``. Trust-boundary code: a signed message grammar is read by root, so every case here is an
adversary's shape, and every refusal must be a fixed sentence that never echoes the input."""
import ast
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.broker import policy
from levain.consent import sign
from levain.contracts import namespaces as NS
from levain.slot import egress_grants as eg

ROOT = Path(__file__).resolve().parent.parent
ISSUED = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
STORE = "store-" + "ab" * 16
GRANT = "eh-" + "0123456789abcdef01234567"
SESSION = "s-" + "0123456789abcdef"
MARK = "EVIL-MARK-9f3"


def message(host="files.example.org", **over):
    f = {"store": STORE, "grant": GRANT, "session": SESSION, "slot": "3", "host": host, "port": "443",
         "issued": eg.stamp(ISSUED), "expires-session": eg.stamp(ISSUED + timedelta(hours=1)),
         "expires-day": eg.stamp(ISSUED + timedelta(hours=24)), "rp": "phone.example.net",
         "writable": "yes" if eg.writable_host(host) else "no"}
    f.update(over)
    return eg.FORMAT_LINE + "\n" + "".join(f"{k}: {f[k]}\n" for k in eg._FIELDS)


def signed(msg=None, key="s"):
    return eg.signed_bytes(msg if msg is not None else message(), key)


def refusal(blob):
    with pytest.raises(eg.GrantRefused) as info:
        eg.parse_signed(blob)
    exc = info.value
    assert exc.args == (eg.REASONS[exc.name],) and str(exc) == eg.REASONS[exc.name]
    assert MARK not in str(exc) and MARK not in repr(exc) and exc.__cause__ is None
    return exc.name


# --- the good path ----------------------------------------------------------------------------------------------------
def test_build_parse_roundtrip_and_the_choice_picks_the_expiry():
    msg = eg.build_message(STORE, GRANT, SESSION, 3, "files.example.org", ISSUED, ISSUED + timedelta(hours=1),
                           "phone.example.net")
    assert re.fullmatch(NS.LEVAIN_SIGN_REQUESTS[NS.EGRESS_HOST_NAMESPACE], msg) and msg == message()
    s, d = eg.parse_signed(eg.signed_bytes(msg, "s")), eg.parse_signed(eg.signed_bytes(msg, "d"))
    assert (s.choice, d.choice) == ("s", "d")
    assert s.expires == ISSUED + timedelta(hours=1) and d.expires == ISSUED + timedelta(hours=24)
    assert (s.store, s.grant, s.session, s.slot, s.host, s.port, s.rp, s.writable) == (
        STORE, GRANT, SESSION, 3, "files.example.org", 443, "phone.example.net", False)


def test_the_session_expiry_is_the_earlier_of_the_deadline_and_eight_hours():
    far = eg.build_message(STORE, GRANT, SESSION, 0, "a.example.org", ISSUED, ISSUED + timedelta(days=3), "p.example")
    assert eg.parse_signed(signed(far)).expires_session == ISSUED + timedelta(hours=8)
    near = eg.build_message(STORE, GRANT, SESSION, 0, "a.example.org", ISSUED, ISSUED + timedelta(minutes=5), "p.example")
    assert eg.parse_signed(signed(near)).expires_session == ISSUED + timedelta(minutes=5)


def test_a_punycode_a_label_host_is_a_host():
    assert eg.parse_signed(signed(message("xn--e1afmkfd.example.org"))).host == "xn--e1afmkfd.example.org"


def test_the_signed_bytes_are_what_consent_sign_signs_for_the_request_levain_files(tmp_path):
    rid = sign.file_levain_request(tmp_path, NS.EGRESS_HOST_NAMESPACE, message(), title="t", ttl_s=600)
    req = sign.read_request(tmp_path, rid)
    assert req.refused is None and req.options == NS.LEVAIN_SIGN_CHOICES[NS.EGRESS_HOST_NAMESPACE] and not req.write_in
    for key in ("s", "d"):
        assert sign.signed_bytes(req, key) == eg.signed_bytes(message(), key)
        assert sign.parse_choice(sign.signed_bytes(req, key))[1:] == (key, eg.option_text(key))
    assert sign.signed_bytes(req, "s") != sign.signed_bytes(req, "d")
    assert json.loads((tmp_path / "sign" / f"{rid}.json").read_text())["format"] == sign.CHOICE_FORMAT


def test_the_copied_constants_match_the_modules_root_cannot_import():
    assert eg.CHOICE_MARK == sign.CHOICE_MARK
    from levain.cockpit.consent import _STAMP_FORMAT
    from levain.slot.grammar import MAX_SLOTS
    assert eg._STAMP_FORMAT == _STAMP_FORMAT and eg.SLOTS_MAX == MAX_SLOTS


# --- the grammar refuses every field out of shape ----------------------------------------------------------------------
def lines(**over):
    return message(**over)


@pytest.mark.parametrize("field, value", [
    ("store", "short"), ("store", "has space-xxxxxxxx"), ("store", "x" * 65), ("store", ""),
    ("grant", "eh-" + "0123456789abcdef0123456"), ("grant", "eh-" + "0123456789ABCDEF01234567"),
    ("grant", "xx-" + "0123456789abcdef01234567"), ("grant", "eh-" + "0123456789abcdef012345678"), ("grant", ""),
    ("session", "s-" + "0123456789abcde"), ("session", "S-" + "0123456789abcdef"), ("session", "s-" + "g" * 16),
    ("slot", "01"), ("slot", "-1"), ("slot", "٣"), ("slot", "100"), ("slot", "32"), ("slot", "3.0"), ("slot", ""),
    ("host", "Files.Example.org"), ("host", "files.example.org."), ("host", "files.example.org:443"),
    ("host", "1.2.3.4"), ("host", "0x7f.0x1"), ("host", "127.1"), ("host", "2130706433"), ("host", "localhost"),
    ("host", "exa mple.org"), ("host", "-a.example.org"), ("host", "a-.example.org"), ("host", "a..example.org"),
    ("host", "*.example.org"), ("host", "münchen.example.org"), ("host", "a.example.org/x"), ("host", ""),
    ("host", "[::1]"), ("host", "::1"), ("host", "a" * 64 + ".example.org"), ("host", ("a." * 130) + "org"),
    ("port", "22"), ("port", "8443"), ("port", "4430"), ("port", "0443"), ("port", "443 "), ("port", "+443"),
    ("port", "٤٤٣"), ("port", "４４３"), ("port", ""), ("port", "443.0"), ("port", "0x1bb"),
    ("issued", "2026-11-16T12:00:00Z"), ("issued", "2026-11-16 12:00:00.000000Z"),
    ("issued", "2026-11-16T12:00:00.000000+00:00"), ("issued", "2026-02-30T12:00:00.000000Z"),
    ("issued", "2026-11-16T25:00:00.000000Z"), ("issued", "0000-11-16T12:00:00.000000Z"), ("issued", ""),
    ("expires-session", "2026-11-16T13:00:00.0Z"), ("expires-day", "tomorrow"),
    ("rp", ""), ("rp", "Phone.example.net"), ("rp", "-phone.example"), ("rp", "phone.example."), ("rp", "p q"),
    ("writable", "maybe"), ("writable", "YES"), ("writable", ""), ("writable", "yes "),
])
def test_a_field_out_of_shape_is_refused_by_a_fixed_sentence(field, value):
    assert refusal(signed(lines(**{field: value}))) in eg.REASONS
    # the same value with attacker text after a newline: still refused, and the text is nowhere in the refusal
    assert refusal(signed(lines(**{field: value + "\n" + MARK}))) in eg.REASONS


@pytest.mark.parametrize("mutate", [
    lambda m: m.replace("levain-egress-host/1", "levain-egress-host/2", 1),
    lambda m: m.replace("levain-egress-host/1", "levain-egress-host/1 ", 1),
    lambda m: "x" + m, lambda m: m + "extra: " + MARK + "\n", lambda m: m + "\n",
    lambda m: m.replace("\n", "\r\n"), lambda m: m.replace("slot: 3\n", ""),
    lambda m: m.replace("slot: 3\n", "slot: 3\nslot: 3\n"),
    lambda m: m.replace("host: ", "Host: ", 1), lambda m: m.replace("port: 443", "port:  443"),
    lambda m: m.replace(": ", ":", 1), lambda m: m.replace("grant: ", "grant:  ", 1),
    lambda m: m.replace("\nport: 443\n", "\n").replace("\nhost:", "\nport: 443\nhost:"),   # reordered
    lambda m: m.replace("rp: phone.example.net\n", "rp: phone.example.net\0\n"),
    lambda m: m.replace("host: files", "host: fi​les"), lambda m: m.rstrip("\n"), lambda m: "",
])
def test_a_message_out_of_its_grammar_is_refused(mutate):
    assert refusal(signed(mutate(message()))) in {"shape", "choice"}
    assert refusal(signed(mutate(message()).replace("example", MARK))) in {"shape", "choice"}


def test_a_message_that_matches_the_fence_but_not_the_rules_is_refused_by_the_parser():
    # ip-looking digit labels pass the fence's character class and are refused by the host grammar
    assert refusal(signed(lines(host="999.999.999.999"))) == "host"
    assert refusal(signed(lines(slot="40"))) == "host"        # past the slot count (the fence allows two digits)


def test_non_bytes_and_oversized_signed_input_is_refused():
    for blob in (None, "str", 5, [], b"", b"\xff\xfe" + b"x" * 50, b"x" * (eg.SIGNED_MAX + 1),
                 bytes(signed()).replace(b"files", b"fi\xffles")):
        assert refusal(blob) in {"shape", "choice"}
    assert eg.parse_signed(bytearray(signed())).host == "files.example.org"


def test_build_refuses_what_it_would_never_file_and_echoes_nothing():
    good = dict(store=STORE, grant=GRANT, session=SESSION, slot=3, host="files.example.org", issued=ISSUED,
                session_deadline=ISSUED + timedelta(hours=1), rp="phone.example.net")
    assert eg.build_message(**good)
    for over in ({"host": "a.local"}, {"host": "metadata." + MARK + ".org"}, {"grant": MARK}, {"session": MARK},
                 {"slot": True}, {"slot": 32}, {"slot": -1}, {"slot": MARK}, {"host": 5}, {"store": MARK + " x"},
                 {"rp": MARK + "\nx"}, {"rp": "Bad"}, {"issued": datetime(2026, 1, 1)},
                 {"session_deadline": datetime(2026, 1, 1)}, {"host": "1.2.3.4"}, {"host": "a.example.org\nport: 22"}):
        with pytest.raises(eg.GrantRefused) as info:
            eg.build_message(**{**good, **over})
        assert info.value.name == "build" and MARK not in str(info.value) and info.value.__cause__ is None


# --- never-grant and writable ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["foo.local", "a.b.local", "box.internal", "metadata.google.internal",
                                  "metadata.example.org", "a.localhost", "x.home.arpa", "printer.localdomain"])
def test_a_never_grant_host_is_refused_by_the_parser_though_it_passes_the_fence(host):
    assert eg.never_grant(host)
    assert re.fullmatch(NS.LEVAIN_SIGN_REQUESTS[NS.EGRESS_HOST_NAMESPACE], message(host))
    assert refusal(signed(message(host))) == "never-grant"


@pytest.mark.parametrize("host", ["localhost", "local", "internal", "metadata", "", None, 5, b"a.local", ["a.local"]])
def test_never_grant_is_true_for_the_bare_names_and_for_anything_not_a_string(host):
    assert eg.never_grant(host) is True


@pytest.mark.parametrize("host", ["example.org", "localhost.example.org", "metadata2.example.org",
                                  "notlocal.example.org", "internal.example.org", "my-local.example.org"])
def test_never_grant_does_not_catch_lookalikes_that_are_ordinary_names(host):
    assert eg.never_grant(host) is False


@pytest.mark.parametrize("host", ["github.com", "gist.github.com", "api.github.com", "gitlab.com", "bitbucket.org",
                                  "pastebin.com", "bkt.s3.amazonaws.com", "s3.us-east-1.amazonaws.com",
                                  "bkt.s3-us-west-2.amazonaws.com", "storage.googleapis.com", "drive.google.com",
                                  "dropbox.com", "dl.dropbox.com", "transfer.sh", "webhook.site", "x.requestbin.com"])
def test_the_writable_list_matches_hosts_and_their_subdomains(host):
    assert eg.writable_host(host) is True


@pytest.mark.parametrize("host", ["example.org", "notgithub.com", "github.com.evil.example", "amazonaws.com",
                                  "ec2.us-east-1.amazonaws.com", "mys3.amazonaws.com", "google.com", None, 5])
def test_the_writable_list_does_not_match_lookalikes_or_other_amazon_hosts(host):
    assert eg.writable_host(host) is False


def test_the_message_must_carry_levains_own_writable_answer():
    assert eg.parse_signed(signed(message("github.com"))).writable is True
    assert refusal(signed(message("github.com", writable="no"))) == "writable"
    assert refusal(signed(message("example.org", writable="yes"))) == "writable"


# --- bounds -----------------------------------------------------------------------------------------------------------
def st(delta):
    return eg.stamp(ISSUED + delta)


def test_the_bounds_are_exact():
    ok = message(**{"expires-session": st(timedelta(hours=8)), "expires-day": st(timedelta(hours=24))})
    assert eg.parse_signed(signed(ok)).expires_day == ISSUED + timedelta(hours=24)
    us = timedelta(microseconds=1)
    for over in ({"expires-session": st(timedelta(hours=8) + us)},
                 {"expires-day": st(timedelta(hours=24) + us)},
                 {"expires-session": st(timedelta(days=30)), "expires-day": st(timedelta(days=30))},
                 {"expires-session": st(timedelta(0))}, {"expires-session": st(-us)},
                 {"expires-session": st(timedelta(hours=3)), "expires-day": st(timedelta(hours=2))},
                 {"expires-day": st(timedelta(hours=1) - us)}):
        assert refusal(signed(message(**over))) == "bound", over


def test_a_stamp_at_the_calendar_edge_is_refused_not_an_overflow():
    top = "9999-12-31T23:59:59.999999Z"
    assert refusal(signed(message(issued=top, **{"expires-session": top, "expires-day": top}))) == "bound"
    assert refusal(signed(message(issued="0001-01-01T00:00:00.000000Z"))) == "bound"


def test_freshness_is_checked_against_roots_clock_at_its_edges():
    g = eg.parse_signed(signed())
    for age, ok in ((0, True), (eg.FRESH_S, True), (eg.FRESH_S + 0.000001, False), (-eg.SKEW_S, True),
                    (-eg.SKEW_S - 0.000001, False), (86400, False), (-86400, False)):
        now = ISSUED + timedelta(seconds=age)
        if ok:
            eg.check_fresh(g, now)
        else:
            with pytest.raises(eg.GrantRefused) as info:
                eg.check_fresh(g, now)
            assert info.value.name == "fresh"


# --- the choice: a key never reads as the other ----------------------------------------------------------------------
def test_a_choice_signed_for_s_never_parses_as_d_and_the_reverse():
    s_bytes, d_bytes = signed(key="s"), signed(key="d")
    assert eg.parse_signed(s_bytes).choice == "s" and eg.parse_signed(d_bytes).choice == "d"
    assert s_bytes != d_bytes
    swapped_key = s_bytes.replace(b"chosen (s):", b"chosen (d):")
    assert refusal(swapped_key) == "choice"
    assert refusal(d_bytes.replace(b"chosen (d):", b"chosen (s):")) == "choice"
    # the other key's words under the right key, and the right words under no key at all
    assert refusal(s_bytes.replace(eg.option_text("s").encode(), eg.option_text("d").encode())) == "choice"
    for key in ("x", "a", "*", "S", "ds", "dd", "", "1"):
        blob = (message() + "\n" + eg.CHOICE_MARK + "(" + key + "): " + eg.option_text("d")).encode()
        assert refusal(blob) == "choice"


def test_the_choice_line_must_be_exactly_one_line_with_levains_words():
    msg = message()
    line = eg.CHOICE_MARK + "(s): " + eg.option_text("s")
    for blob in (msg.encode(), (msg + "\n").encode(), (msg + "\n" + line + "\n").encode(),
                 (msg + "\n" + line + "\n" + line).encode(), (msg + "\n" + line + " ").encode(),
                 (msg + "\n" + line + "x").encode(), (msg + "\n" + line + MARK).encode(),
                 (msg + "\n" + line.replace("chosen", "Chosen")).encode(),
                 (msg + "\n" + line.replace("/1", "/2")).encode(), (msg + "\n(s): " + eg.option_text("s")).encode(),
                 (msg + "\n" + eg.CHOICE_MARK + "(*): " + MARK).encode(), (msg + line).encode(),
                 (msg + "\r\n" + line).encode(), line.encode(), (line + "\n" + msg).encode()):
        assert refusal(blob) in {"choice", "shape"}


def test_a_message_naming_the_choice_mark_in_a_field_is_refused():
    assert refusal(signed(message(rp="p.example\n" + eg.CHOICE_MARK + "(d): x"))) in {"shape", "choice"}


# --- forgery across namespaces ----------------------------------------------------------------------------------------
def test_other_levain_messages_are_never_a_host_grant_and_this_one_is_never_theirs():
    other = [ns for ns in NS.LEVAIN_SIGN_REQUESTS if ns != NS.EGRESS_HOST_NAMESPACE]
    assert other                      # the walk reaches the other grammars
    for blob in (signed(key="s"), signed(key="d"), message().encode()):
        for ns in other:
            assert re.fullmatch(NS.LEVAIN_SIGN_REQUESTS[ns], blob.decode()) is None, ns
    samples = [
        "levain review clear rv-" + "ab" * 16 + " open none",
        "levain-dispatch-config-approve/1\nstore: " + STORE + "\nkind: verifier\ngeneration: 1\nprev: none\n"
        "item-sha256: " + "a" * 64 + "\nexpires: 2026-11-16T13:00:00.000000Z\nrp: p.example\nchange: x\nsummary: y\n",
        "levain-terminal-answer/1\nstore: levain-questions:x\n" + "h\n" * 7 + "countersign-of: x\ne\n",
        "levain-envelope-revoke/1\nstore: " + STORE + "\nenvelope: e\nat: 2026\nnonce: n\n",
    ]
    for s in samples:
        for key in ("s", "d"):
            assert refusal((s + "\n" + eg.CHOICE_MARK + "(" + key + "): " + eg.option_text(key)).encode()) in {
                "shape", "choice"}
        assert refusal(s.encode()) in {"shape", "choice"}


def test_the_consent_sign_filer_refuses_this_namespace_unless_it_is_levains_own_choice_request(tmp_path):
    ns = NS.EGRESS_HOST_NAMESPACE
    for bad in ("x", message().replace("port: 443", "port: 22"), message() + MARK):
        with pytest.raises(ValueError):
            sign.file_levain_request(tmp_path, ns, bad, title="t", ttl_s=600)
    assert not (tmp_path / "sign").exists() or not list((tmp_path / "sign").iterdir())
    # a CALLER's request may never name it (the namespace begins "levain")
    assert sign.namespace_problem(ns) is not None
    # a levain namespace without options stays plain: a choice request in it is still refused
    plain = sign.file_levain_request(tmp_path, NS.REVIEW_CLEAR_NAMESPACE, "levain review clear rv-" + "ab" * 16 + " open x",
                                     title="t", ttl_s=600)
    assert sign.read_request(tmp_path, plain).refused is None


def _hand(tmp_path, **over):
    rid = sign.file_levain_request(tmp_path, NS.EGRESS_HOST_NAMESPACE, message(), title="t", ttl_s=600)
    path = tmp_path / "sign" / f"{rid}.json"
    body = json.loads(path.read_text())
    body.update(over)
    new = "lv-" + "cd" * 16
    body["id"] = new
    (tmp_path / "sign" / f"{new}.json").write_text(json.dumps(body))
    return sign.read_request(tmp_path, new)


def test_a_request_file_in_this_namespace_with_anything_but_levains_options_is_refused(tmp_path):
    opts = [{"key": k, "text": t} for k, t in NS.LEVAIN_SIGN_CHOICES[NS.EGRESS_HOST_NAMESPACE]]
    assert _hand(tmp_path).refused is None
    for over in ({"options": opts[::-1]}, {"options": opts[:1] + [{"key": "x", "text": "Allow forever"}]},
                 {"options": opts + [{"key": "f", "text": "Allow forever"}]},
                 {"options": [{"key": "s", "text": opts[1]["text"]}, {"key": "d", "text": opts[0]["text"]}]},
                 {"options": [{"key": "s", "text": opts[0]["text"] + " "}, opts[1]]},
                 {"write_in": True}, {"message": message().replace("port: 443", "port: 22")}):
        got = _hand(tmp_path, **over)
        assert got.refused and "levain's own" in got.refused, over
        for f in (tmp_path / "sign").glob("lv-" + "cd" * 16 + ".json"):
            f.unlink()
    plain_body = _hand(tmp_path)
    assert plain_body.refused is None
    (tmp_path / "sign" / ("lv-" + "cd" * 16 + ".json")).unlink()
    # a plain-format request (no options) in the namespace is refused
    rid = sign.file_levain_request(tmp_path, NS.EGRESS_HOST_NAMESPACE, message(), title="t", ttl_s=600)
    body = json.loads((tmp_path / "sign" / f"{rid}.json").read_text())
    del body["options"]
    body["format"] = sign.REQUEST_FORMAT
    body["id"] = "lv-" + "ef" * 16
    (tmp_path / "sign" / ("lv-" + "ef" * 16 + ".json")).write_text(json.dumps(body))
    assert "levain's own" in sign.read_request(tmp_path, body["id"]).refused


def test_a_write_in_never_signs_in_this_namespace(tmp_path):
    req = _hand(tmp_path)
    with pytest.raises(sign.SignRefused):
        sign.signed_bytes(req, "*", "anything")
    with pytest.raises(sign.SignRefused):
        sign.signed_bytes(req, "f")


# --- grants.json ----------------------------------------------------------------------------------------------------
def entry(host="files.example.org", grant=GRANT, expires=None, choice="s"):
    return eg.Entry(grant=grant, host=host, port=443, expires=expires or ISSUED + timedelta(hours=1), choice=choice)


def test_the_grants_file_roundtrips_in_one_canonical_form():
    raw = eg.dump_grants(SESSION, 3, [entry(), entry("b.example.org", "eh-" + "1" * 24, choice="d")])
    got = eg.parse_grants(raw)
    assert got.session == SESSION and got.slot == 3 and [e.host for e in got.entries] == ["files.example.org", "b.example.org"]
    assert eg.dump_grants(got.session, got.slot, got.entries) == raw and raw.endswith(b"\n") and raw.isascii()
    assert eg.parse_grants(eg.dump_grants(SESSION, 0, [])).entries == ()


def _file(**over):
    body = {"format": eg.GRANTS_FORMAT, "session": SESSION, "slot": 3,
            "grants": [{"grant": GRANT, "host": "files.example.org", "port": 443, "expires": eg.stamp(ISSUED),
                        "choice": "s"}]}
    body.update(over)
    return json.dumps(body).encode()


def _entry(**over):
    e = {"grant": GRANT, "host": "files.example.org", "port": 443, "expires": eg.stamp(ISSUED), "choice": "s"}
    e.update(over)
    return _file(grants=[e])


def test_the_strict_reader_accepts_the_plain_file_and_refuses_every_departure():
    assert eg.parse_grants(_file()).entries[0].host == "files.example.org"
    bad = [
        b"", b"{}", b"[]", b"null", b"not json", b"\xff\xfe", _file() + b"x", b"x" + _file(), _file() + _file(),
        _file(format="levain-egress-grants/2"), _file(session="s-" + "g" * 16), _file(session=5), _file(slot=True),
        _file(slot=32), _file(slot=-1), _file(slot=3.0), _file(slot="3"), _file(grants={}), _file(grants="x"),
        _file(extra=1), _file(grants=[None]), _file(grants=["x"]), _file(grants=[{}]),
        _entry(extra=1), _entry(host="1.2.3.4"), _entry(host="a.local"), _entry(host="Files.example.org"),
        _entry(host="metadata.example.org"), _entry(port=22), _entry(port="443"), _entry(port=443.0),
        _entry(port=True), _entry(choice="x"), _entry(choice="*"), _entry(choice=None), _entry(grant="eh-x"),
        _entry(expires="2026-11-16T12:00:00Z"), _entry(expires=5), _entry(expires="2026-02-30T12:00:00.000000Z"),
        _file(grants=[json.loads(_entry())["grants"][0]] * 2),
        _file(grants=[{**json.loads(_entry())["grants"][0], "grant": "eh-" + "2" * 24}] +
              [json.loads(_entry())["grants"][0]]),       # same host twice
        _file(grants=[{**json.loads(_entry())["grants"][0], "host": f"h{i}.example.org", "grant": "eh-" + f"{i:024x}"}
                      for i in range(eg.GRANTS_MAX + 1)]),
        b"x" * (eg.GRANTS_FILE_MAX + 1), None, "str", 5,
        b'{"format":"levain-egress-grants/1","format":"levain-egress-grants/1","session":"' + SESSION.encode() +
        b'","slot":3,"grants":[]}',
        b'{"format":"levain-egress-grants/1","session":"' + SESSION.encode() + b'","slot":NaN,"grants":[]}',
        b'{"format":"levain-egress-grants/1","session":"' + SESSION.encode() + b'","slot":3,"grants":[]' + b"[" * 9000,
    ]
    for raw in bad:
        with pytest.raises(eg.GrantRefused) as info:
            eg.parse_grants(raw)
        assert info.value.name == "grants-file" and info.value.args == (eg.REASONS["grants-file"],)


def test_the_writer_refuses_what_the_reader_would_not_read():
    for bad in (entry(host="a.local"), entry(host="1.2.3.4"), entry(choice="x"), eg.Entry(GRANT, "a.example.org", 22, ISSUED, "s")):
        with pytest.raises(eg.GrantRefused):
            eg.dump_grants(SESSION, 3, [bad])
    with pytest.raises(eg.GrantRefused):
        eg.dump_grants("s-bad", 3, [])
    with pytest.raises(eg.GrantRefused):
        eg.dump_grants(SESSION, 3, [entry(), entry()])       # one grant id twice


def test_the_file_read_is_bounded_and_checks_owner_mode_and_type(tmp_path):
    me = os.getuid()
    p = tmp_path / "grants.json"
    p.write_bytes(_file())
    os.chmod(p, 0o440)
    assert eg.read_grants_file(str(p), owner_uid=me).session == SESSION
    assert eg.read_grants_file(str(p), owner_uid=me, group_gid=os.getgid()).session == SESSION
    assert eg.read_grants_file(str(p), owner_uid=me, group_gid=os.getgid() + 1) is None
    assert eg.read_grants_file(str(p), owner_uid=me + 1) is None            # not root's (the default owner is root)
    if me != 0:
        assert eg.read_grants_file(str(p)) is None
    for mode in (0o660, 0o640 | 0o020, 0o444, 0o404, 0o640 | 0o002, 0o666):
        os.chmod(p, mode)
        assert eg.read_grants_file(str(p), owner_uid=me) is None, oct(mode)
    os.chmod(p, 0o440)
    link = tmp_path / "link.json"
    link.symlink_to(p)
    assert eg.read_grants_file(str(link), owner_uid=me) is None
    assert eg.read_grants_file(str(tmp_path), owner_uid=me) is None
    assert eg.read_grants_file(str(tmp_path / "missing.json"), owner_uid=me) is None
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o440)
    assert eg.read_grants_file(str(fifo), owner_uid=me) is None
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (eg.GRANTS_FILE_MAX + 10))
    os.chmod(big, 0o440)
    assert eg.read_grants_file(str(big), owner_uid=me) is None
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{")
    os.chmod(bad, 0o440)
    assert eg.read_grants_file(str(bad), owner_uid=me) is None
    assert stat.S_ISREG(os.stat(p).st_mode)


def test_only_a_live_entry_for_this_session_host_and_port_admits():
    f = eg.parse_grants(_file())
    now = ISSUED - timedelta(minutes=1)
    ok = eg.live_entry(f, SESSION, "files.example.org", 443, now)
    assert ok is not None and ok.grant == GRANT
    other_session = "s-" + "f" * 16
    cases = [(f, other_session, "files.example.org", 443, now), (f, SESSION, "other.example.org", 443, now),
             (f, SESSION, "sub.files.example.org", 443, now), (f, SESSION, "example.org", 443, now),
             (f, SESSION, "files.example.org", 22, now), (f, SESSION, "files.example.org", 8443, now),
             (f, SESSION, "files.example.org", 0, now), (f, SESSION, "files.example.org", None, now),
             (f, SESSION, "1.2.3.4", 443, now), (f, SESSION, "FILES.example.org", 443, now),
             (f, SESSION, None, 443, now), (f, SESSION, "files.example.org", 443, ISSUED),       # expires > now, strict
             (f, SESSION, "files.example.org", 443, ISSUED + timedelta(seconds=1)), (None, SESSION, "files.example.org", 443, now)]
    for args in cases:
        assert eg.live_entry(*args) is None, args


# --- python 3.9 and the import surface root relies on ---------------------------------------------------------------
def test_the_module_is_python_39_and_imports_only_what_root_can():
    src = (ROOT / "levain/slot/egress_grants.py").read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 9))
    assert not [n for n in ast.walk(tree) if type(n).__name__ in ("Match", "TryStar")]
    annotations = set()
    for n in ast.walk(tree):
        for a in (getattr(n, "annotation", None), getattr(n, "returns", None)):
            if a is not None:
                annotations.update(id(x) for x in ast.walk(a))
    flags = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("O_")]
    runtime_unions = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                      and id(n) not in annotations and n.lineno not in flags]   # os.open's int flags are not a type union
    assert runtime_unions == []
    assert "from __future__ import annotations" in src
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module if n.module.startswith("levain") else n.module.split(".")[0])
    assert mods == {"__future__", "json", "os", "re", "secrets", "stat", "dataclasses", "datetime", "typing",
                    "levain.broker.policy", "levain.contracts.namespaces"}


def test_the_host_grammar_is_the_policys_own():
    assert eg.is_host_name is policy.is_host_name
