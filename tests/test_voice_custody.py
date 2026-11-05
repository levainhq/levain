"""Voice key custody (voice V1-1, design 1104-voice/V1_DESIGN.md §3.1 to §3.4).

A ``voice-key`` record is a laptop-presence-signed link naming a keychain item; ``custody.read_key`` is the only
reader of the value. Every log here is real (an ssh-keygen laptop key through the replay, as
``tests/chat_egress/keys_rig.py`` does it) and the keychain is a FAKE ``security`` executable that keeps its items
in a file under ``tmp_path`` and records every call: no test reaches a person's keychain (``custody.checked_security``
refuses the real binary under pytest, and a test below proves it). The key VALUE is a string no code in the repo
spells, so a hit is this test's leak.

What each refusal here guards is named in its test; each was removed once and its test seen to fail (the slice's
RETURN lists them). There is no HTTP surface in this slice, so "never in an HTTP response" is asserted here for the
data a route would be built from (``list_keys`` rows, every exception, every repr) and is V1-4's to assert on the
route itself."""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import stat
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.chat_egress.key_setup import KeychainItems, SetupDeclined
from levain.chat_egress.keys import KeychainStore
from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.voice import custody
from levain.voice.custody import (KeychainWriteFailed, Secret, SecurityStdinWriter, VoiceKeyRefused,
                                  VoiceKeyUnavailable)
from tests.chat_egress.keys_rig import Cockpit

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import T0, World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

# a value no code in the repo spells; letters, digits and '-' are in the alphabet custody writes
VALUE = "vkv-S11-HELD-KEY-7f3c9a2e5b1d4068"
OTHER = "vkv-S11-OTHER-KEY-0a1b2c3d4e5f6789"
SOURCE = "levain-voice-openai"
ACCOUNT = "levain-voice"
VK = "vk-0123456789ab"
VK2 = "vk-ba9876543210"

_SCRIPT = '''#!{py}
import json, os, shlex, sys
ROOT = {root!r}
def items():
    try:
        return json.load(open(ROOT + "/items.json"))
    except FileNotFoundError:
        return {{}}
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv[:1] == ["-i"] else ""
with open(ROOT + "/calls.jsonl", "a") as f:
    f.write(json.dumps({{"argv": argv, "env": dict(os.environ), "stdin_lines": len(stdin.splitlines())}}) + "\\n")
def flag(name):
    return os.path.exists(ROOT + "/" + name)
def opt(args, name):
    return args[args.index(name) + 1] if name in args else None
def run(args):
    cmd = args[0]
    if cmd == "add-generic-password":
        if flag("fail_write"):
            sys.stderr.write("security: cannot add " + " ".join(args) + "\\n")
            return 1
        if flag("drop_writes"):
            return 0
        it = items()
        it[opt(args, "-s") + "\\t" + opt(args, "-a")] = opt(args, "-w")
        json.dump(it, open(ROOT + "/items.json", "w"))
        return 0
    if cmd == "find-generic-password":
        if flag("locked"):
            sys.stderr.write("STORE-REPLY-MARKER the keychain is locked\\n")
            return 36
        v = items().get(opt(args, "-s") + "\\t" + opt(args, "-a"))
        if v is None:
            return 44
        if "-w" in args:
            print(v)
        return 0
    if cmd == "list-keychains":
        print('    "/fake/login.keychain-db"')
        return 0
    return 1
if argv[:1] == ["-i"]:
    code = 0
    for line in stdin.splitlines():
        parts = shlex.split(line)
        if parts and parts[0] != "exit":
            code = code or run(parts)
    sys.exit(code)
sys.exit(run(argv))
'''


class FakeSecurity:
    """A stand-in ``security``: items in ``items.json`` (this IS the keychain), every call in ``calls.jsonl``."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "fakesec"
        self.root.mkdir()
        self.path = str(self.root / "security")
        Path(self.path).write_text(_SCRIPT.format(py=sys.executable, root=str(self.root)))
        Path(self.path).chmod(Path(self.path).stat().st_mode | stat.S_IXUSR)

    def calls(self) -> list[dict]:
        log = self.root / "calls.jsonl"
        return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []

    def items(self) -> dict[str, str]:
        f = self.root / "items.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def seed(self, source: str, account: str, value: str) -> None:
        (self.root / "items.json").write_text(json.dumps({**self.items(), f"{source}\t{account}": value}))

    def flag(self, name: str) -> None:
        (self.root / name).write_text("")

    def clear(self, name: str) -> None:
        (self.root / name).unlink()


@pytest.fixture
def ck(tmp_path):
    return Cockpit(tmp_path)


@pytest.fixture
def sec(tmp_path):
    return FakeSecurity(tmp_path)


def add(ck, sec, *, value=VALUE, provider="openai", uses=("stt", "tts"), source=SOURCE, account=ACCOUNT, **kw):
    args = {"provider": provider, "uses": uses, "source": source, "account": account, "value": Secret(value),
            "confirm": lambda preview: True, "writer": SecurityStdinWriter(sec.path),
            "items": KeychainItems(sec.path), "store": KeychainStore(sec.path)}
    return custody.add_key(ck.root, **{**ck.kw(), **args, **kw})


def read(ck, sec, provider="openai", use="tts"):
    return custody.read_key(provider, use, authority=ck.source(), store=KeychainStore(sec.path))


def hits(root: Path, value: str = VALUE, *, skip: tuple[Path, ...] = ()) -> list[str]:
    """Every file under ``root`` holding ``value`` (raw or base64'd), but the fake keychain itself."""
    needles = [value.encode(), base64.b64encode(value.encode()).rstrip(b"=")]
    out = []
    for p in Path(root).rglob("*"):
        if p.is_file() and not p.is_symlink() and p not in skip:
            data = p.read_bytes()
            if any(n in data for n in needles):
                out.append(str(p))
    return out


def vkey(gid=VK, **over) -> dict:
    g = {"id": gid, "kind": auth.VOICE_KEY_KIND, "provider": "openai", "uses": ["stt", "tts"], "source": SOURCE,
         "account": ACCOUNT}
    g.update(over)
    return g


# --- the record kind (the log's rules) ---------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_voice_key_is_a_laptop_signed_create_written_v5_and_replays_as_its_kind(world):
    snap = world.link({"op": "create_grant", "grant": vkey()})
    g = snap.policy.grants[VK]
    assert world.record(snap.seq)["format"] == auth.FORMAT_V5 == "levain-authority/5"
    assert snap.link_signers[snap.seq - 1] == world.fp("laptop")
    assert auth.voice_key_record(g) and not auth.consent_grant(g) and not auth.inert_grant(g)
    assert "expires_at" not in g                                      # a key stands until revoked (Q1(a))
    assert auth.voice_key_for(snap.policy.grants, "openai")["id"] == VK
    assert auth.voice_key_for(snap.policy.grants, "deepgram") is None


def test_an_older_replay_stops_at_the_v5_link_rather_than_holding_it_inert(world, monkeypatch):
    """A voice key has no ``expires_at``, which the envelope an older replay holds an unknown kind to requires; so the
    link is /5 and a replay that knows only /1 to /4 stops at it (a hand-made /1 link is refused here)."""
    world.link({"op": "create_grant", "grant": vkey()})
    monkeypatch.setattr(auth, "_KNOWN_FORMATS", (auth.FORMAT, auth.FORMAT_V2, auth.FORMAT_V3, auth.FORMAT_V4))
    assert "format" in world.replay_verdict(world.db)
    monkeypatch.undo()
    op = {"op": "create_grant", "grant": vkey(VK2, provider="deepgram")}
    assert "is written levain-authority/5" in world.replay_verdict(world.plant(op, fmt=auth.FORMAT))


@pytest.mark.parametrize("over, said", [
    ({"provider": "azure"}, "provider"), ({"provider": None}, "provider"),
    ({"uses": []}, "uses"), ({"uses": ["tts", "stt"]}, "sorted"), ({"uses": ["tts", "tts"]}, "sorted"),
    ({"uses": ["speak"]}, "uses"), ({"uses": "tts"}, "uses"),
    ({"id": "vk-XYZ"}, "id"), ({"id": "ek-0123456789ab"}, "id"),
    ({"source": ""}, "source"), ({"account": "has\nnewline"}, "account"), ({"source": 7}, "source"),
])
def test_the_log_refuses_a_malformed_voice_key(world, over, said):
    assert said in world.refused({"op": "create_grant", "grant": vkey(**over)})


@pytest.mark.parametrize("extra", ["expires_at", "value", "key", "host"])
def test_the_log_refuses_a_voice_key_with_a_field_it_does_not_define(world, extra):
    """No ``expires_at`` and, above all, no ``value``: a record carries names only."""
    assert "exactly" in world.refused({"op": "create_grant", "grant": {**vkey(), extra: "x"}})


def test_the_log_refuses_a_voice_key_missing_a_field(world):
    g = vkey()
    del g["account"]
    assert "exactly" in world.refused({"op": "create_grant", "grant": g})


def test_one_voice_key_per_provider_and_a_replacement_is_one_link(world):
    world.link({"op": "create_grant", "grant": vkey()})
    assert "one voice key per provider" in world.refused({"op": "create_grant", "grant": vkey(VK2)})
    # another provider is another key
    world.link({"op": "create_grant", "grant": vkey(VK2, provider="deepgram")})
    # a replacement that keeps the old one is refused; one that drops it is a loosen the log takes
    pol = auth._thaw(world.state().policy.as_dict())
    pol["grants"]["vk-111111111111"] = vkey("vk-111111111111")
    assert "one voice key per provider" in world.refused({"op": "loosen", "policy": pol})
    del pol["grants"][VK]
    snap = world.link({"op": "loosen", "policy": pol})
    assert VK not in snap.policy.grants and "vk-111111111111" in snap.policy.grants


def test_a_voice_keys_terms_are_fixed_under_its_id(world):
    world.link({"op": "create_grant", "grant": vkey()})
    pol = auth._thaw(world.state().policy.as_dict())
    pol["grants"][VK]["source"] = "another-item"
    assert "terms are fixed" in world.refused({"op": "loosen", "policy": pol})
    pol["grants"][VK] = vkey(uses=["tts"])
    assert "terms are fixed" in world.refused({"op": "loosen", "policy": pol})


def test_a_voice_key_has_no_renew(world):
    world.link({"op": "create_grant", "grant": vkey()})
    refusal = world.refused({"op": "renew", "grant_id": VK, "expires_at": "2027-01-01T00:00:00Z",
                             "signed_at": utc_stamp(world.clock.now + timedelta(minutes=1))})
    assert "no expiry" in refusal


def test_it_stands_until_revoked_whatever_the_date(world):
    snap = world.link({"op": "create_grant", "grant": vkey()})
    assert auth.voice_key_record(snap.policy.grants[VK])
    world.clock.now += timedelta(days=4000)
    later = world.link({"op": "timeouts", "timeouts": {"x": 1}})
    assert VK in later.policy.grants and auth.voice_key_for(later.policy.grants, "openai")


def test_only_a_laptop_presence_key_creates_one_and_a_passkey_creates_nothing(world):
    """T3: a key swap points the room's words at another account. A display-phone ssh key and a passkey (the
    phone) cannot create or replace a voice key; the laptop can."""
    op = {"op": "create_grant", "grant": vkey()}
    assert "cannot sign" in world.refused(op, "phone")
    assert "consent answers and grant revokes only" in world.refused(op, "pk")
    assert VK not in world.state().policy.grants
    world.link(op, "laptop")
    pol = auth._thaw(world.state().policy.as_dict())
    del pol["grants"][VK]
    pol["grants"][VK2] = vkey(VK2)
    assert "cannot sign" in world.refused({"op": "loosen", "policy": pol}, "phone")
    assert "consent answers and grant revokes only" in world.refused({"op": "loosen", "policy": pol}, "pk")
    assert VK in world.state().policy.grants and VK2 not in world.state().policy.grants


@pytest.mark.parametrize("signer", ["laptop", "phone", "pk"])
def test_any_enrolled_key_revokes_a_voice_key_the_phone_passkey_included(world, signer):
    """A revoke only narrows (revoke beside every grant, 10-21): the laptop, a display-phone key and the phone's
    passkey may each sign it."""
    world.link({"op": "create_grant", "grant": vkey()})
    snap = world.link({"op": "revoke_grant", "grant_id": VK}, signer)
    assert VK not in snap.policy.grants and auth.voice_key_for(snap.policy.grants, "openai") is None


# --- the shapes of a value custody will write -----------------------------------------------------------------

def test_a_secret_is_closed_in_repr_str_format_and_pickle():
    import pickle

    s = Secret(VALUE)
    for shown in (repr(s), str(s), f"{s}", f"{s!r}", "%s" % s, repr([s]), repr({"v": s})):
        assert VALUE not in shown
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(AttributeError):
        s._value = "x"
    assert s.reveal() == VALUE


@pytest.mark.parametrize("bad", ["", "short", "has space in it", "quo'te-key-1234", 'dq"uote-key-1234',
                                 "new\nline-key-1234", "$(touch-x)-key-1234", "semi;colon-key-1234",
                                 "back\\slash-key-1234", "tab\tkey-1234567", "ünicode-key-123456", "x" * 513])
def test_a_value_that_would_need_quoting_is_refused_before_anything_runs(ck, sec, bad):
    with pytest.raises(VoiceKeyRefused) as exc:
        add(ck, sec, value=bad)
    assert bad not in str(exc.value) or bad == ""
    assert sec.calls() == [] and ck.state().policy.grants == {}


@pytest.mark.parametrize("name", ["has space", "quo'te", 'a"b', "semi;colon", "$(x)", "new\nline", ""])
def test_the_writer_refuses_an_item_name_it_cannot_place_unquoted(sec, name):
    with pytest.raises(KeychainWriteFailed):
        SecurityStdinWriter(sec.path).write(name, ACCOUNT, Secret(VALUE))
    with pytest.raises(KeychainWriteFailed):
        SecurityStdinWriter(sec.path).write(SOURCE, name, Secret(VALUE))
    assert sec.calls() == []


# --- add: the ceremony -------------------------------------------------------------------------------------------

def test_add_signs_a_record_naming_the_item_and_stores_the_value_through_stdin(ck, sec):
    rec, warning = add(ck, sec)
    assert warning is None
    g = ck.state().policy.grants[rec["id"]]
    assert auth._thaw(g) == {"id": rec["id"], "kind": "voice-key", "provider": "openai", "uses": ["stt", "tts"],
                             "source": SOURCE, "account": ACCOUNT}
    assert VALUE not in json.dumps(auth._thaw(g)) and VALUE not in repr(rec)
    assert sec.items() == {f"{SOURCE}\t{ACCOUNT}": VALUE}
    state = ck.state()
    assert state.link_signers[state.seq - 1] == ck.fp                  # the laptop-presence key signed it
    writes = [c for c in sec.calls() if c["argv"] == ["-i"]]
    assert len(writes) == 1 and writes[0]["stdin_lines"] == 2          # the add line and `exit`, on stdin only


def test_the_value_is_in_no_argv_env_record_log_repr_or_output(ck, sec, tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    rec, _ = add(ck, sec)
    got = read(ck, sec)
    assert got == VALUE
    rows = custody.list_keys(ck.root, now=datetime.now(timezone.utc))
    custody.revoke_key(ck.root, rec["id"], **ck.kw())
    for call in sec.calls():
        assert VALUE not in " ".join(call["argv"])                    # argv of every `security` run
        assert VALUE not in "".join(call["env"].values())             # and its whole environment
        assert call["env"]["PATH"] == "/usr/bin:/bin"
    assert VALUE not in "".join(os.environ.values())                  # not parked in this process's environment
    assert hits(tmp_path, skip=(sec.root / "items.json",)) == []     # the journal, the anchor, the call log, the keys
    assert VALUE not in json.dumps(rows) and VALUE not in repr(rows)
    out = capsys.readouterr()
    assert VALUE not in out.out + out.err and VALUE not in caplog.text
    for shown in (repr(rec), repr(Secret(VALUE)), repr(custody.VoiceKeyPreview(rec, ())), repr(SecurityStdinWriter(sec.path)),
                  repr(KeychainStore(sec.path))):
        assert VALUE not in shown


def test_every_failure_speaks_fixed_words_never_the_store_the_value_or_the_record(ck, sec):
    """Each exception custody raises, with a store that echoes the whole command line and prints markers."""
    def words(exc) -> str:
        return str(exc.value) + repr(exc.value) + "".join(traceback.format_exception(exc.value))

    sec.flag("fail_write")
    with pytest.raises(KeychainWriteFailed) as e1:
        add(ck, sec)
    sec.clear("fail_write")
    sec.flag("drop_writes")
    with pytest.raises(KeychainWriteFailed) as e2:
        add(ck, sec)
    sec.clear("drop_writes")
    add(ck, sec)
    sec.flag("locked")
    with pytest.raises(VoiceKeyUnavailable) as e3:
        read(ck, sec)
    sec.clear("locked")
    sec.seed(SOURCE, ACCOUNT, "not a usable key (has spaces)")
    with pytest.raises(VoiceKeyUnavailable) as e4:
        read(ck, sec)
    for exc in (e1, e2, e3, e4):
        text = words(exc)
        assert VALUE not in text and "STORE-REPLY-MARKER" not in text and "cannot add" not in text
        assert "has spaces" not in text
    assert (e3.value.code, e4.value.code) == ("keychain_unreadable", "unusable")


def test_a_declined_add_stores_nothing_and_signs_nothing(ck, sec):
    with pytest.raises(SetupDeclined):
        add(ck, sec, confirm=lambda preview: False)
    assert [c for c in sec.calls() if c["argv"] == ["-i"]] == [] and sec.items() == {}
    assert ck.state().policy.grants == {}


def test_the_confirm_is_shown_the_record_exactly_and_never_the_value(ck, sec):
    seen = []
    rec, _ = add(ck, sec, confirm=lambda p: seen.append(p) or True)
    assert seen[0].record == rec and seen[0].replaces == ()
    assert VALUE not in repr(seen[0])


def test_a_write_the_keychain_silently_dropped_is_caught_by_the_read_back_and_signs_nothing(ck, sec):
    """`security -i` was not measured against the real binary: a write that exited 0 and stored nothing must not
    produce a record that names an item holding nothing."""
    sec.flag("drop_writes")
    with pytest.raises(KeychainWriteFailed, match="nothing was signed"):
        add(ck, sec)
    assert ck.state().policy.grants == {}


def test_a_write_that_stored_a_different_value_is_caught_by_the_read_back(ck, sec):
    class Stale(custody.KeychainWriter):
        def write(self, source, account, value):
            sec.seed(source, account, OTHER)

    with pytest.raises(KeychainWriteFailed, match="nothing was signed"):
        add(ck, sec, writer=Stale())
    assert ck.state().policy.grants == {}


def test_a_failed_signature_leaves_an_item_no_record_names_and_nothing_reads_it(ck, sec):
    def refusing_sign(message):
        raise RuntimeError("the touch was declined")

    with pytest.raises(RuntimeError):
        add(ck, sec, **{**ck.kw(), "sign": refusing_sign})
    assert ck.state().policy.grants == {}
    with pytest.raises(VoiceKeyUnavailable) as e:
        read(ck, sec)
    assert e.value.code == "not_set"


def test_a_second_key_for_a_provider_needs_replace_and_is_refused_before_anything_is_written(ck, sec):
    add(ck, sec)
    n = len(sec.calls())
    with pytest.raises(VoiceKeyRefused, match="--replace"):
        add(ck, sec, value=OTHER)
    assert len(sec.calls()) == n and sec.items() == {f"{SOURCE}\t{ACCOUNT}": VALUE}
    with pytest.raises(VoiceKeyRefused, match="nothing to replace"):
        add(ck, sec, provider="deepgram", replace=True, source="levain-voice-deepgram", value=OTHER)
    assert len(sec.calls()) == n


def test_replace_is_one_link_and_the_next_read_gets_the_new_item(ck, sec):
    old, _ = add(ck, sec)
    seq = ck.state().seq
    new, _ = add(ck, sec, replace=True, value=OTHER, source="levain-voice-openai-2")
    state = ck.state()
    assert state.seq == seq + 1 and old["id"] not in state.policy.grants and new["id"] in state.policy.grants
    assert read(ck, sec) == OTHER
    assert [c["argv"][2] for c in sec.calls() if c["argv"][0] == "find-generic-password"][-1] == "levain-voice-openai-2"


def test_an_item_that_matches_twice_across_the_search_list_is_refused(ck, sec):
    class Twice:
        def count(self, service, account):
            return 2

    with pytest.raises(VoiceKeyRefused, match="search list"):
        add(ck, sec, items=Twice())
    assert [c for c in sec.calls() if c["argv"] == ["-i"]] == []


@pytest.mark.parametrize("provider, uses", [("azure", ("tts",)), ("openai", ()), ("openai", ("speak",)),
                                            ("", ("tts",))])
def test_add_refuses_an_unknown_provider_or_use(ck, sec, provider, uses):
    with pytest.raises(VoiceKeyRefused):
        add(ck, sec, provider=provider, uses=uses)
    assert sec.calls() == []


def test_a_secret_is_required_not_a_bare_string(ck, sec):
    with pytest.raises(VoiceKeyRefused):
        custody.add_key(ck.root, **{**ck.kw(), "provider": "openai", "uses": ("tts",), "source": SOURCE,
                                    "account": ACCOUNT, "value": VALUE, "confirm": lambda p: True})
    assert sec.calls() == []


# --- read: the only reader ---------------------------------------------------------------------------------------

def test_read_key_reads_the_named_item_once_per_call_and_holds_nothing(ck, sec):
    add(ck, sec)
    before = len(sec.calls())
    assert read(ck, sec) == VALUE and read(ck, sec, use="stt") == VALUE
    finds = [c["argv"] for c in sec.calls()[before:]]
    assert finds == [["find-generic-password", "-s", SOURCE, "-a", ACCOUNT, "-w"]] * 2


def test_a_revoke_stops_the_next_read_before_the_keychain_is_asked(ck, sec):
    rec, _ = add(ck, sec)
    assert read(ck, sec) == VALUE
    seq, _ = custody.revoke_key(ck.root, rec["id"], **ck.kw())
    n = len(sec.calls())
    with pytest.raises(VoiceKeyUnavailable) as e:
        read(ck, sec)
    assert e.value.code == "not_set" and len(sec.calls()) == n         # the keychain was not touched
    assert ck.state().seq == seq and rec["id"] not in ck.state().policy.grants
    assert sec.items()                                               # the item stays: a revoke withdraws authority


def test_a_revoke_by_a_passkey_class_signer_is_the_logs_to_allow_and_a_missing_record_is_refused(ck, sec):
    with pytest.raises(VoiceKeyRefused, match="no voice key"):
        custody.revoke_key(ck.root, VK, **ck.kw())
    ek = {"id": "ek-000000000001", "kind": auth.EGRESS_KEY_KIND, "entity": "e", "host": "example.com",
          "source": SOURCE, "account": ACCOUNT, "placement": {"scheme": "bearer"}, "repos": [],
          "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=7))}
    ck.link({"op": "create_grant", "grant": ek})
    with pytest.raises(VoiceKeyRefused, match="no voice key"):                 # another kind's record is not a voice key
        custody.revoke_key(ck.root, "ek-000000000001", **ck.kw())
    assert "ek-000000000001" in ck.state().policy.grants


def test_a_use_the_record_does_not_allow_is_refused_before_the_keychain_is_asked(ck, sec):
    add(ck, sec, uses=("tts",))
    n = len(sec.calls())
    with pytest.raises(VoiceKeyUnavailable) as e:
        read(ck, sec, use="stt")
    assert e.value.code == "use_not_granted" and len(sec.calls()) == n
    assert read(ck, sec, use="tts") == VALUE


def test_a_provider_with_no_key_reads_nothing(ck, sec):
    add(ck, sec)
    n = len(sec.calls())
    for provider in ("deepgram", "elevenlabs"):
        with pytest.raises(VoiceKeyUnavailable) as e:
            read(ck, sec, provider=provider)
        assert e.value.code == "not_set"
    with pytest.raises(VoiceKeyUnavailable):
        custody.read_key("azure", "tts", authority=ck.source(), store=KeychainStore(sec.path))
    with pytest.raises(VoiceKeyUnavailable):
        custody.read_key("openai", "dictate", authority=ck.source(), store=KeychainStore(sec.path))
    assert len(sec.calls()) == n


def test_the_authority_must_be_a_fresh_source_not_a_kept_snapshot(ck, sec):
    add(ck, sec)
    kept = ck.state()
    with pytest.raises(TypeError):
        custody.read_key("openai", "tts", authority=lambda: kept, store=KeychainStore(sec.path))


def test_a_log_that_cannot_be_read_is_unreadable_never_a_key(ck, sec, tmp_path):
    add(ck, sec)
    dead = custody.authority_for(tmp_path / "no-such-cockpit")
    with pytest.raises(VoiceKeyUnavailable) as e:
        custody.read_key("openai", "tts", authority=dead, store=KeychainStore(sec.path))
    assert e.value.code == "authority_unreadable"
    anchor = auth.anchor_path(ck.root)
    anchor.write_text(anchor.read_text().replace("0", "1", 1))        # an anchor the log no longer matches
    with pytest.raises(VoiceKeyUnavailable) as e2:
        read(ck, sec)
    assert e2.value.code == "authority_unreadable"


def test_a_missing_item_is_unreadable(ck, sec):
    add(ck, sec)
    (sec.root / "items.json").write_text("{}")
    with pytest.raises(VoiceKeyUnavailable) as e:
        read(ck, sec)
    assert e.value.code == "keychain_unreadable"


# --- the real binary is unreachable under a test -------------------------------------------------------------

def test_no_test_reaches_the_real_security_binary(ck, sec, tmp_path):
    link = tmp_path / "sec-link"
    link.symlink_to("/usr/bin/security")
    for spelled in (None, "/usr/bin/security", str(link), "/usr/bin/../bin/security"):
        with pytest.raises(VoiceKeyRefused, match="real `security`"):
            custody.checked_security(spelled)
    assert custody.checked_security(sec.path) == sec.path
    with pytest.raises(VoiceKeyRefused):
        SecurityStdinWriter()
    add(ck, sec)
    with pytest.raises(VoiceKeyRefused):                              # read_key's default store is the real one
        custody.read_key("openai", "tts", authority=ck.source())
    with pytest.raises(VoiceKeyRefused):                              # and so is add's, when none is handed in
        add(ck, sec, replace=True, value=OTHER, writer=SecurityStdinWriter(sec.path), items=None)


# --- the CLI ---------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(ck, sec, monkeypatch, capsys):
    monkeypatch.setattr(custody, "DEFAULT_SECURITY", sec.path)
    typed = {"value": VALUE}
    monkeypatch.setattr("getpass.getpass", lambda prompt="": typed["value"])

    def run(*argv):
        code = main(["voice", "key", *argv, "--data-root", str(ck.root)])
        out = capsys.readouterr()
        return code, out.out, out.err

    run.typed = typed
    return run


def test_cli_add_list_revoke_end_to_end_and_the_value_is_never_printed(ck, sec, cli):
    code, out, err = cli("add", "openai", "--yes", "--signing-key", str(ck.key))
    assert code == 0, err
    assert VALUE not in out + err and "stands until revoked" in out
    code, out, err = cli("list")
    assert code == 0 and VALUE not in out + err
    row = out.split()
    assert row[1] == "openai" and SOURCE in out
    assert read(ck, sec) == VALUE
    code, out, err = cli("revoke", row[0], "--signing-key", str(ck.key))
    assert code == 0 and VALUE not in out + err
    with pytest.raises(VoiceKeyUnavailable):
        read(ck, sec)
    assert cli("list")[1].strip() == "no voice keys"


def test_cli_add_without_a_terminal_and_without_yes_stores_nothing(ck, sec, cli, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    code, out, err = cli("add", "openai", "--signing-key", str(ck.key))
    assert code == 1 and "pass --yes" in err and VALUE not in out + err
    assert sec.items() == {} and ck.state().policy.grants == {}


def test_cli_a_value_it_cannot_place_is_refused_in_fixed_words(ck, sec, cli):
    cli.typed["value"] = "has a space in the key"
    code, out, err = cli("add", "openai", "--yes", "--signing-key", str(ck.key))
    assert code == 1 and "add refused" in err and "has a space" not in out + err
    assert sec.calls() == []


def test_cli_takes_no_value_argument_and_demands_a_signing_key(ck, sec, cli):
    with pytest.raises(SystemExit) as exc:
        cli("add", "openai", "--yes")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc2:
        cli("add", "openai", VALUE, "--yes", "--signing-key", str(ck.key))
    assert exc2.value.code == 2
    with pytest.raises(SystemExit):
        cli("add", "azure", "--yes", "--signing-key", str(ck.key))


def test_cli_replace_and_a_second_add_without_it(ck, sec, cli):
    assert cli("add", "openai", "--use", "tts", "--yes", "--signing-key", str(ck.key))[0] == 0
    code, out, err = cli("add", "openai", "--yes", "--signing-key", str(ck.key))
    assert code == 1 and "--replace" in err
    cli.typed["value"] = OTHER
    assert cli("add", "openai", "--replace", "--yes", "--signing-key", str(ck.key))[0] == 0
    assert read(ck, sec, use="stt") == OTHER                         # the replacement allows both uses (the default)
