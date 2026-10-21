"""The runtime helper's argv + stdin grammar (CONTRACT §5 preamble, §5.1, §5.3) and the host half of
T:1/test_runtime_helper_fuzz_tree_equal: every hostile input is refused BY NAME, and the parser touches
no filesystem (every file entry point raises while it runs)."""
from __future__ import annotations

import base64
import builtins
import contextlib
import io
import json
import os
import pathlib
import random

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import runtime_grammar as rg
from levain.slot.runtime_grammar import (DISPATCH, OPERATOR, PrepareInput, RenderBlob, bind_stdin, ok_line,
                                         parse_runtime_argv, parse_stdin, refusal_line)

S = "s-0123456789abcdef"
CONF = {"egress": 8, "none": 2}
NONCE = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
TICKET = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode()


def argv(a, caller=OPERATOR, conf=CONF):
    return parse_runtime_argv(a, configured_slots=conf, caller=caller)


def refused_argv(a, caller=OPERATOR, conf=CONF) -> str:
    with pytest.raises(Refused) as ei:
        argv(a, caller, conf)
    return ei.value.name


def cj(obj) -> bytes:
    return canon.canonical_bytes(obj)


def refused_stdin(verb, raw) -> str:
    with pytest.raises(Refused) as ei:
        parse_stdin(verb, raw)
    return ei.value.name


RENDER = {"format": "levain-session-render/1", "vendor": "claude", "routes": ["anthropic-api", "github"]}


# --- §5.1 argv: every verb parses ----------------------------------------------------------------

def test_every_verb_parses_for_the_operator():
    good = {
        "prepare": ["prepare", "egress", "interactive", "claude", "brokered", "7"],
        "render": ["render", S], "start": ["start", S], "resume": ["resume", S], "teardown": ["teardown", S],
        "confirm-identity": ["confirm-identity", S, "yes"], "adopt-open": ["adopt-open", S],
        "discard": ["discard", S], "restore": ["restore", S], "status": ["status", S],
    }
    assert set(good) == set(rg.VERBS)
    for verb, a in good.items():
        req = argv(a)
        assert req.verb == verb and req.caller == OPERATOR
    p = argv(good["prepare"])
    assert (p.slot_class, p.kind, p.vendor, p.auth, p.slot) == ("egress", "interactive", "claude", "brokered", 7)
    assert argv(["prepare", "egress", "login", "codex", "passthrough"]).slot is None
    assert argv(["status"]).session is None
    assert argv(["confirm-identity", S, "no"]).answer == "no"


def test_lease_column():
    # §5.1 Lease: prepare (nonce), render, start, resume, teardown, confirm-identity; not the other four.
    assert {v for v in rg.VERBS if argv_for(v).needs_lease} == {
        "prepare", "render", "start", "resume", "teardown", "confirm-identity"}


def argv_for(verb):
    if verb == "prepare":
        return argv(["prepare", "egress", "interactive", "claude", "brokered"])
    if verb == "confirm-identity":
        return argv([verb, S, "yes"])
    return argv([verb, S])


# --- §5 preamble patterns --------------------------------------------------------------------------

@pytest.mark.parametrize("tok", ["../", "..", "/etc/passwd", "s-0123456789abcde", "s-0123456789abcdef0",
                                 "s-0123456789ABCDEF", "S-0123456789abcdef", S + "\n", S + " ", " " + S,
                                 S + "/", "./" + S, S.replace("0", "٠"), S.replace("1", "１"),
                                 "s-" + "g" * 16, "s_0123456789abcdef", "-" + S, "--session=" + S])
def test_session_pattern_refuses(tok):
    assert refused_argv(["start", tok]) == "bad-token"


def test_nul_and_empty_token_refused():
    assert refused_argv(["start", S + "\x00"]) == "bad-token"
    assert refused_argv(["start", ""]) == "bad-token"
    assert refused_argv(["start", b"s-0123456789abcdef"]) == "bad-token"
    assert refused_argv("start " + S) == "bad-token"


@pytest.mark.parametrize("tok,name", [("8", "slot-not-configured"), ("9", "slot-not-configured"),
                                      ("63", "slot-not-configured"), ("64", "out-of-range"),
                                      ("99", "out-of-range"), ("100", "bad-token"), ("07", "bad-token"),
                                      ("00", "bad-token"), ("-1", "bad-token"), ("+1", "bad-token"),
                                      ("1 ", "bad-token"), ("1\n", "bad-token"), ("١", "bad-token"),
                                      ("１", "bad-token"), ("¹", "bad-token"), ("0x1", "bad-token"),
                                      ("1.0", "bad-token")])
def test_slot_pattern_and_bound(tok, name):
    # §5 preamble: slot ^[0-9]{1,2}$ < configured; §1.1: the index space is 64.
    assert refused_argv(["prepare", "egress", "interactive", "claude", "brokered", tok]) == name


def test_slot_bound_is_per_class_and_follows_an_index_set():
    assert argv(["prepare", "egress", "interactive", "claude", "brokered", "7"]).slot == 7
    assert refused_argv(["prepare", "none", "verifier", "claude", "none", "2"], caller=DISPATCH) == \
        "slot-not-configured"
    assert argv(["prepare", "none", "verifier", "claude", "none", "1"], caller=DISPATCH).slot == 1
    # a replenished slot takes a new index (§1.1): the configured set is not a prefix
    conf = {"egress": frozenset({0, 1, 9}), "none": 0}
    assert argv(["prepare", "egress", "interactive", "claude", "brokered", "9"], conf=conf).slot == 9
    assert refused_argv(["prepare", "egress", "interactive", "claude", "brokered", "2"], conf=conf) == \
        "slot-not-configured"
    for bad in ({"egress": 65, "none": 0}, {"egress": [64], "none": 0}, {"egress": "8"}, {}, {"egress": True}):
        assert refused_argv(["prepare", "egress", "interactive", "claude", "brokered", "0"], conf=bad) == \
            "bad-config"


@pytest.mark.parametrize("a", [["prepare", "Egress", "interactive", "claude", "brokered"],
                               ["prepare", "egress", "INTERACTIVE", "claude", "brokered"],
                               ["prepare", "egress", "interactive", "claude ", "brokered"],
                               ["prepare", "egress", "interactive", "gemini", "brokered"],
                               ["prepare", "egress", "interactive", "claude", "--auth=brokered"],
                               ["confirm-identity", S, "YES"], ["confirm-identity", S, "y"]])
def test_enums_are_exact(a):
    assert refused_argv(a) == "bad-token"


@pytest.mark.parametrize("a,name", [
    ([], "missing-argv"), (["start"], "missing-argv"), (["start", S, S], "extra-argv"),
    (["start", S, "--force"], "extra-argv"), (["status", S, S], "extra-argv"),
    (["confirm-identity", S], "missing-argv"), (["confirm-identity", S, "yes", "yes"], "extra-argv"),
    (["prepare", "egress", "interactive", "claude"], "missing-argv"),
    (["prepare", "egress", "interactive", "claude", "brokered", "1", "2"], "extra-argv"),
    (["discard"], "missing-argv"), (["restore"], "missing-argv"),
    (["Start", S], "unknown-verb"), (["START", S], "unknown-verb"), (["start\n", S], "unknown-verb"),
    (["--help"], "unknown-verb"), (["-c", "start"], "unknown-verb"), (["setup"], "unknown-verb"),
    (["purge", S], "unknown-verb"), (["../start", S], "unknown-verb"),
])
def test_argv_shape(a, name):
    assert refused_argv(a) == name


def test_class_kind_auth_rows():
    # §5.6: verifier is none class; §5.3 + §5.1 (5b): a none-class session has no front, so auth none.
    assert refused_argv(["prepare", "egress", "verifier", "claude", "none"], caller=DISPATCH) == \
        "class-kind-mismatch"
    for auth in ("brokered", "passthrough"):
        assert refused_argv(["prepare", "none", "verifier", "claude", auth], caller=DISPATCH) == "class-auth-mismatch"
    # F11, the other direction: none class is verifier ONLY, so no other kind is handed a verifier pair
    assert refused_argv(["prepare", "none", "interactive", "codex", "none"]) == "class-kind-mismatch"
    for kind in ("dispatch", "planner"):
        assert refused_argv(["prepare", "none", kind, "codex", "none"], caller=DISPATCH) == "class-kind-mismatch"
    assert refused_argv(["prepare", "none", "login", "claude", "none"]) == "class-kind-mismatch"
    assert argv(["prepare", "none", "verifier", "codex", "none"], caller=DISPATCH).kind == "verifier"
    assert argv(["prepare", "egress", "interactive", "claude", "none"]).auth == "none"


# --- §5.1 the _levain_dispatch rule ----------------------------------------------------------------

@pytest.mark.parametrize("a", [["adopt-open", S], ["discard", S], ["restore", S], ["confirm-identity", S, "yes"],
                               ["adopt-open", "../x"], ["confirm-identity"]])
def test_dispatch_caller_refused_its_four_verbs_before_argv(a):
    assert refused_argv(a, caller=DISPATCH) == "verb-not-allowed-for-caller"


def test_dispatch_caller_has_the_rest():
    for verb in ("render", "start", "resume", "teardown", "status"):
        assert argv([verb, S], caller=DISPATCH).caller == DISPATCH
    assert argv(["status"], caller=DISPATCH).session is None
    assert argv(["prepare", "egress", "dispatch", "codex", "brokered"], caller=DISPATCH).kind == "dispatch"
    assert argv(["prepare", "egress", "planner", "claude", "brokered"], caller=DISPATCH).kind == "planner"


def test_kinds_split_by_caller():
    for kind in ("dispatch", "planner"):
        assert refused_argv(["prepare", "egress", kind, "claude", "brokered"]) == "kind-not-allowed-for-caller"
    assert refused_argv(["prepare", "none", "verifier", "claude", "none"]) == "kind-not-allowed-for-caller"
    for kind in ("interactive", "login"):
        assert refused_argv(["prepare", "egress", kind, "claude", "brokered"], caller=DISPATCH) == \
            "kind-not-allowed-for-caller"


@pytest.mark.parametrize("caller", ["root", "Operator", "", None, "_levain_dispatch", 0])
def test_unknown_caller(caller):
    assert refused_argv(["status"], caller=caller) == "unknown-caller"


# --- stdin: prepare --------------------------------------------------------------------------------

def test_prepare_stdin():
    blob = parse_stdin("prepare", cj({"lease_nonce": NONCE}))
    assert blob == PrepareInput(NONCE, None)
    assert parse_stdin("prepare", cj({"lease_nonce": NONCE, "ticket": TICKET})).ticket == TICKET


@pytest.mark.parametrize("obj,name", [
    ({}, "missing-key"), ({"ticket": TICKET}, "missing-key"),
    ({"lease_nonce": NONCE, "extra": 1}, "unknown-key"), ({"lease_nonce": NONCE, "format": "x"}, "unknown-key"),
    ({"lease_nonce": 1}, "wrong-type"), ({"lease_nonce": None}, "wrong-type"),
    ({"lease_nonce": NONCE + "="}, "bad-handle"), ({"lease_nonce": NONCE[:-1]}, "bad-handle"),
    ({"lease_nonce": NONCE + "A"}, "bad-handle"), ({"lease_nonce": NONCE.replace("A", "+")}, "bad-handle"),
    ({"lease_nonce": NONCE[:-1] + "B"}, "bad-handle"),   # non-zero spare bits: a second spelling
    ({"lease_nonce": "../../etc/passwd" + "A" * 27}, "bad-handle"),
    ({"lease_nonce": NONCE, "ticket": ""}, "bad-handle"),
])
def test_prepare_stdin_refusals(obj, name):
    assert refused_stdin("prepare", cj(obj)) == name


def test_ticket_binding():
    op = argv(["prepare", "egress", "interactive", "claude", "brokered"])
    dp = argv(["prepare", "egress", "dispatch", "claude", "brokered"], caller=DISPATCH)
    bind_stdin(op, PrepareInput(NONCE))
    bind_stdin(dp, PrepareInput(NONCE, TICKET))
    with pytest.raises(Refused) as ei:
        bind_stdin(dp, PrepareInput(NONCE))
    assert ei.value.name == "ticket-required"
    with pytest.raises(Refused) as ei:
        bind_stdin(op, PrepareInput(NONCE, TICKET))
    assert ei.value.name == "ticket-not-allowed"
    with pytest.raises(Refused) as ei:
        bind_stdin(argv(["start", S]), PrepareInput(NONCE))
    assert ei.value.name == "unexpected-stdin"
    with pytest.raises(Refused) as ei:
        bind_stdin(argv(["render", S]), None)
    assert ei.value.name == "missing-stdin"


# --- stdin: shared encodings + size ----------------------------------------------------------------

def test_size_cap_before_parse():
    pad = "x" * (rg.STDIN_MAX - len(cj({"lease_nonce": NONCE, "z": ""})))
    at_cap = cj({"lease_nonce": NONCE, "z": pad})
    assert len(at_cap) == rg.STDIN_MAX
    assert refused_stdin("prepare", at_cap) == "unknown-key"           # parsed: the cap is ≤, not <
    assert refused_stdin("prepare", at_cap[:-1] + b"x\"}") == "stdin-too-large"
    assert refused_stdin("prepare", b"\xff" * (rg.STDIN_MAX + 1)) == "stdin-too-large"   # before decode
    assert refused_stdin("start", b" " * (rg.STDIN_MAX + 1)) == "stdin-too-large"


@pytest.mark.parametrize("raw,name", [
    (b"", "missing-stdin"),
    (b'{"lease_nonce":"%s","lease_nonce":"%s"}' % (NONCE.encode(), NONCE.encode()), "duplicate-key"),
    (b'{"lease_nonce":1.0}', "float-refused"), (b'{"lease_nonce":1e3}', "float-refused"),
    (b'{"lease_nonce":NaN}', "float-refused"), (b'{"lease_nonce":Infinity}', "float-refused"),
    (b'{"lease_nonce":-Infinity}', "float-refused"),
    (b'{"lease_nonce":9007199254740992}', "int-out-of-range"),
    (b'{"lease_nonce":"\xff"}', "stdin-not-utf8"), (b'{"lease_nonce":"\xc0\xaf"}', "stdin-not-utf8"),
    (b"\xef\xbb\xbf" + cj({"lease_nonce": NONCE}), "stdin-bom"),
    (b"[" * 33 + b"]" * 33, "stdin-too-deep"), (b"[" * 60000, "stdin-too-deep"),
    (b'{"lease_nonce":"\\ud800"}', "stdin-not-json"),
    (b"{", "stdin-not-json"), (b"null", "stdin-not-object"), (b"[]", "stdin-not-object"),
    (b'"x"', "stdin-not-object"), (b"1", "stdin-not-object"),
    (cj({"lease_nonce": NONCE}) + b"\n", "stdin-not-canonical"),
    (b" " + cj({"lease_nonce": NONCE}), "stdin-not-canonical"),
    (('{"ticket":"%s","lease_nonce":"%s"}' % (TICKET, NONCE)).encode(), "stdin-not-canonical"),
    (b'{"lease_nonce": "%s"}' % NONCE.encode(), "stdin-not-canonical"),
    (b'{"lease_nonce":"\\u0041%s"}' % NONCE[1:].encode(), "stdin-not-canonical"),
    (cj({"lease_nonce": NONCE}) + b"\x00", "stdin-not-json"),
])
def test_stdin_encoding_refusals(raw, name):
    assert refused_stdin("prepare", raw) == name


def test_no_stdin_verbs():
    for verb in ("start", "resume", "teardown", "confirm-identity", "adopt-open", "discard", "restore", "status"):
        assert parse_stdin(verb, b"") is None
        assert refused_stdin(verb, b"{}") == "unexpected-stdin"
    assert refused_stdin("PREPARE", b"{}") == "unknown-verb"
    assert refused_stdin("prepare", "{}") == "wrong-type"


# --- stdin: §5.3 render blob -----------------------------------------------------------------------

def test_render_blob_full():
    full = dict(RENDER, model="anthropic/claude-x:1.0", git_identity={"name": "Alice Ö", "email": "a@b.c"},
                hooks=["levain_queue", "levain_gate"], mcp=[], allow_own_loopback=False,
                repo_label="levain — main", verifier_steps=[{"argv": ["pytest", "-q"], "wall_s": 900}],
                driver="pytest-report")
    blob = parse_stdin("render", cj(full))
    assert isinstance(blob, RenderBlob)
    assert blob.routes == ("anthropic-api", "github") and blob.hooks == {"levain_gate", "levain_queue"}
    assert blob.verifier_steps[0].argv == ("pytest", "-q") and blob.driver == "pytest-report"
    minimal = parse_stdin("render", cj(dict(RENDER, routes=[])))
    assert minimal.routes == () and minimal.verifier_steps is None and minimal.model is None


@pytest.mark.parametrize("patch,name", [
    ({"driver_input": {}}, "unknown-key"),               # §5.3: REMOVED in c5
    ({"Vendor": "claude"}, "unknown-key"), ({"path": "/etc/passwd"}, "unknown-key"),
    ({"format": "levain-session-render/2"}, "bad-format"), ({"format": 1}, "bad-format"),
    ({"vendor": "Claude"}, "bad-value"), ({"vendor": ["claude"]}, "wrong-type"),
    ({"routes": "github"}, "wrong-type"), ({"routes": ["GitHub"]}, "bad-value"),
    ({"routes": ["../x"]}, "bad-value"), ({"routes": ["a" * 49]}, "bad-value"), ({"routes": [""]}, "bad-value"),
    ({"routes": ["github", "github"]}, "duplicate-value"), ({"routes": [1]}, "wrong-type"),
    ({"routes": [f"r{i}" for i in range(65)]}, "too-many"),
    ({"model": ""}, "bad-value"), ({"model": "m" * 129}, "bad-value"), ({"model": "a b"}, "bad-value"),
    ({"model": None}, "wrong-type"),
    ({"git_identity": {"name": "a"}}, "missing-key"),
    ({"git_identity": {"name": "a", "email": "a@b.c", "x": 1}}, "unknown-key"),
    ({"git_identity": {"name": "a<b", "email": "a@b.c"}}, "bad-value"),
    ({"git_identity": {"name": "a\nb", "email": "a@b.c"}}, "bad-value"),
    ({"git_identity": {"name": "a", "email": "a b@c"}}, "bad-value"),
    ({"git_identity": {"name": "", "email": "a@b.c"}}, "bad-value"),
    ({"git_identity": "alice"}, "wrong-type"),
    ({"hooks": ["levain_gate", "levain_gate"]}, "duplicate-value"), ({"hooks": ["other"]}, "bad-value"),
    ({"hooks": {"levain_gate": True}}, "wrong-type"),
    ({"mcp": ["github"]}, "bad-value"), ({"mcp": "x"}, "wrong-type"),
    ({"allow_own_loopback": 1}, "wrong-type"), ({"allow_own_loopback": "false"}, "wrong-type"),
    ({"repo_label": "a‮b"}, "bad-value"), ({"repo_label": "a\x1bb"}, "bad-value"),
    ({"repo_label": "a\u0085b"}, "bad-value"), ({"repo_label": "a\tb"}, "bad-value"),
    ({"repo_label": ""}, "bad-value"), ({"repo_label": "x" * 257}, "bad-value"),
    ({"driver": "d"}, "verifier-keys-unpaired"),
    ({"verifier_steps": [{"argv": ["x"], "wall_s": 1}]}, "verifier-keys-unpaired"),
    ({"driver": "../d", "verifier_steps": [{"argv": ["x"], "wall_s": 1}]}, "bad-value"),
    ({"driver": "D", "verifier_steps": [{"argv": ["x"], "wall_s": 1}]}, "bad-value"),
    ({"driver": "d", "verifier_steps": []}, "bad-value"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"], "wall_s": 1}] * 17}, "too-many"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"] * 65, "wall_s": 1}]}, "too-many"),
    ({"driver": "d", "verifier_steps": [{"argv": [], "wall_s": 1}]}, "bad-value"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x" * 4097], "wall_s": 1}]}, "bad-value"),
    ({"driver": "d", "verifier_steps": [{"argv": ["é" * 2049], "wall_s": 1}]}, "bad-value"),  # bytes, not chars
    ({"driver": "d", "verifier_steps": [{"argv": ["a\x00b"], "wall_s": 1}]}, "bad-value"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"], "wall_s": 901}]}, "out-of-range"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"], "wall_s": 0}]}, "out-of-range"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"], "wall_s": True}]}, "wrong-type"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"]}]}, "missing-key"),
    ({"driver": "d", "verifier_steps": [{"argv": ["x"], "wall_s": 1, "cwd": "/"}]}, "unknown-key"),
    ({"driver": "d", "verifier_steps": [["x"]]}, "wrong-type"),
])
def test_render_blob_refusals(patch, name):
    assert refused_stdin("render", cj(dict(RENDER, **patch))) == name


def test_verifier_steps_huge_wall_s_is_refused_not_valueerror():
    # reproduced: a >4300-digit wall_s made the refusal detail raise ValueError
    with pytest.raises(rg.Refused) as e:
        rg._verifier_steps([{"argv": ["x"], "wall_s": 10 ** 5000}])
    assert e.value.name == "out-of-range"


def test_render_required_keys():
    for k in ("format", "vendor", "routes"):
        assert refused_stdin("render", cj({x: v for x, v in RENDER.items() if x != k})) == "missing-key"


def test_step_arg_at_the_byte_cap_passes():
    blob = dict(RENDER, driver="d", verifier_steps=[{"argv": ["x" * 4096], "wall_s": 900}] * 15)
    assert len(cj(blob)) <= rg.STDIN_MAX
    assert len(parse_stdin("render", cj(blob)).verifier_steps) == 15
    blob = dict(RENDER, driver="d", verifier_steps=[{"argv": ["x"] * 64, "wall_s": 1}] * 16)
    assert len(parse_stdin("render", cj(blob)).verifier_steps) == 16


# --- output lines ----------------------------------------------------------------------------------

def test_output_lines():
    assert ok_line({}) == '{"ok":true}\n'
    assert ok_line({"session": S, "slot": {"class": "egress", "index": 3}}) == \
        '{"ok":true,"session":"%s","slot":{"class":"egress","index":3}}\n' % S
    assert ok_line({"repo": "a\nb "}).count("\n") == 1
    assert refusal_line(Refused("bad-token", "x\n")) == '{"ok":false,"refused":"bad-token"}\n'
    assert refusal_line(Refused("no-baseline:3")) == '{"ok":false,"refused":"no-baseline:3"}\n'
    for hostile in ("bad\ntoken", "../x", "", "A", "x" * 65, '"}{"ok":true'):
        assert refusal_line(Refused(hostile)) == '{"ok":false,"refused":"internal"}\n'
    with pytest.raises(ValueError):
        ok_line({"ok": False})
    with pytest.raises(ValueError):          # canonical JSON refuses floats (ContractError is a ValueError)
        ok_line({"x": 1.5})
    with pytest.raises(ValueError):
        ok_line({"x": float("nan")})
    with pytest.raises(TypeError):
        ok_line({1: "x"})
    for line in (ok_line({"a": [1, None, True]}), refusal_line(Refused("x"))):
        body = line[:-1].encode()
        assert canon.canonical_bytes(canon.parse(body)) == body and line.endswith("\n")


# --- the fuzz harness (host half of T:1/test_runtime_helper_fuzz_tree_equal) -----------------------

class FileTouched(BaseException):
    """A BaseException so no ``except Exception`` in the parser can swallow it."""


@contextlib.contextmanager
def no_filesystem():
    """Every file entry point raises while the parser runs."""
    def boom(*a, **k):
        raise FileTouched(repr(a)[:80])
    with pytest.MonkeyPatch.context() as mp:
        for mod, names in ((builtins, ("open",)), (io, ("open", "open_code")),
                           (os, ("open", "stat", "lstat", "listdir", "scandir", "readlink", "access", "walk",
                                 "chdir", "mkdir", "makedirs", "remove", "unlink", "rename", "replace",
                                 "rmdir", "symlink", "link", "chmod", "chown", "getcwd", "fspath", "getenv",
                                 "putenv")),
                           (os.path, ("exists", "lexists", "isdir", "isfile", "islink", "realpath",
                                      "abspath", "getsize", "samefile"))):
            for n in names:
                if hasattr(mod, n):
                    mp.setattr(mod, n, boom)
        for n in ("open", "stat", "lstat", "exists", "is_dir", "is_file", "is_symlink", "iterdir", "resolve",
                  "read_bytes", "read_text", "write_bytes", "write_text", "touch", "mkdir", "unlink",
                  "readlink", "absolute", "cwd", "home", "glob", "rglob"):
            if hasattr(pathlib.Path, n):
                mp.setattr(pathlib.Path, n, boom)
        mp.setattr(os, "environ", None)
        yield


def _guarded(fn, *a, **k):
    """Run ``fn`` with no filesystem; return ('ok', value) or ('refused', name). A FileTouched or any other
    exception propagates and fails the test."""
    with no_filesystem():
        try:
            return "ok", fn(*a, **k)
        except Refused as e:
            return "refused", e.name


ARGV_CORPUS = [
    # paths and path-ish names in every session slot
    ["start", "../"], ["start", ".."], ["start", "/etc/passwd"], ["render", "/etc/passwd"],
    ["status", "../" + S], ["teardown", S + "/../../etc"], ["resume", "~root"], ["start", "s-0123456789abcde\\"],
    ["start", "$(id)"], ["start", "`id`"], ["start", "*"], ["start", "s-0123456789abcd??"],
    # NUL, newline, whitespace
    ["start", S + "\x00"], ["start\x00", S], ["start", S + "\n"], ["start", "\n" + S], ["start", S + " "],
    ["start", S + "\t"], ["start", S + "\r"], ["start", " " + S], ["start", S + "​"],
    # unicode digits and case
    ["start", "s-" + "٠" * 16], ["start", "s-" + "０" * 16], ["start", "s-" + "०" * 16],
    ["start", S.upper()], ["start", "ｓ-0123456789abcdef"],
    ["prepare", "egress", "interactive", "claude", "brokered", "٣"],
    ["prepare", "egress", "interactive", "claude", "brokered", "３"],
    ["prepare", "egress", "interactive", "claude", "brokered", "³"],
    # slot bounds
    ["prepare", "egress", "interactive", "claude", "brokered", "8"],
    ["prepare", "none", "interactive", "claude", "none", "2"],
    ["prepare", "egress", "interactive", "claude", "brokered", "64"],
    ["prepare", "egress", "interactive", "claude", "brokered", "99"],
    ["prepare", "egress", "interactive", "claude", "brokered", "100"],
    ["prepare", "egress", "interactive", "claude", "brokered", "-0"],
    # extra / missing argv, flags
    [], ["start"], ["start", S, S], ["status", S, "--all"], ["prepare"], ["prepare", "egress"],
    ["prepare", "egress", "interactive", "claude", "brokered", "1", "--slot=2"],
    ["confirm-identity", S], ["confirm-identity", S, "yes", "--force"],
    # unknown verbs, case variants, flag injection
    ["frobnicate"], ["Prepare"], ["PREPARE"], ["sTaRt", S], ["start ", S], ["--verb=start", S], ["-h"],
    ["--", "start", S], ["start=", S], ["setup", "--root", "/"], ["purge", S], ["force-retire", "egress", "0"],
    ["prepare", "--class", "egress", "interactive", "claude", "brokered"],
    ["prepare", "egress", "interactive", "claude", "brokered", "--root", "/"],
    # enums
    ["prepare", "EGRESS", "interactive", "claude", "brokered"], ["prepare", "egress", "dispatch ", "claude",
                                                                  "brokered"],
    ["prepare", "egress", "interactive", "codex", "Brokered"], ["confirm-identity", S, "Yes"],
    ["confirm-identity", S, "1"], ["confirm-identity", S, "true"],
]

DISPATCH_CORPUS = [["adopt-open", S], ["discard", S], ["restore", S], ["confirm-identity", S, "yes"],
                   ["confirm-identity", S, "no"],
                   ["prepare", "egress", "interactive", "claude", "brokered"],
                   ["prepare", "egress", "login", "claude", "passthrough"]]

_deep = b"[" * 40 + b"]" * 40
STDIN_CORPUS = [
    ("prepare", b"x" * (rg.STDIN_MAX + 1)), ("render", b"{" + b" " * rg.STDIN_MAX + b"}"),
    ("prepare", b'{"lease_nonce":' + _deep + b"}"), ("render", b"[" * (rg.STDIN_MAX // 2)),
    ("render", b'{"a":' * 100 + b"1" + b"}" * 100),
    ("prepare", b'{"lease_nonce":"%s","lease_nonce":"%s"}' % (NONCE.encode(), TICKET.encode())),
    ("prepare", b'{"lease_nonce":0.5}'), ("prepare", b'{"lease_nonce":NaN}'), ("prepare", b'{"lease_nonce":-0.0}'),
    ("prepare", b'{"lease_nonce":"\xfe"}'), ("prepare", b"\xef\xbb\xbf{}"), ("prepare", b"\xff\xfe{\x00}\x00"),
    ("render", cj(dict(RENDER, driver_input={"x": 1}))), ("render", cj(dict(RENDER, unknown=1))),
    ("render", cj(dict(RENDER, routes=["/etc/passwd"]))), ("render", cj(dict(RENDER, repo_label="‮")))
    , ("prepare", cj({"lease_nonce": "/etc/passwd"})), ("prepare", cj({"lease_nonce": NONCE, "ticket": "../"})),
    ("start", b"{}"), ("status", b"\n"), ("prepare", b""), ("render", b"null"),
    ("prepare", cj({"lease_nonce": NONCE}) + b"\n"),
]


def test_fuzz_corpus_refused_by_name_with_no_filesystem():
    for a in ARGV_CORPUS:
        kind, out = _guarded(parse_runtime_argv, a, configured_slots=CONF, caller=OPERATOR)
        assert kind == "refused", (a, out)
        assert refusal_line(Refused(out)) != '{"ok":false,"refused":"internal"}\n', (a, out)
    for a in DISPATCH_CORPUS:
        assert _guarded(parse_runtime_argv, a, configured_slots=CONF, caller=DISPATCH)[0] == "refused", a
    for verb, raw in STDIN_CORPUS:
        kind, out = _guarded(parse_stdin, verb, raw)
        assert kind == "refused", (verb, raw[:40], out)
        assert refusal_line(Refused(out)) != '{"ok":false,"refused":"internal"}\n', (verb, out)


def test_the_guard_bites():
    """The harness would catch a file touch (a guard that cannot fail is not evidence)."""
    for probe in (lambda: open("/etc/hosts"), lambda: os.stat("/"), lambda: pathlib.Path("/").exists(),
                  lambda: os.path.realpath("/tmp"), lambda: os.environ.get("SUDO_UID")):
        with pytest.raises((FileTouched, TypeError, AttributeError)):
            _guarded(probe)


def test_valid_inputs_also_touch_no_filesystem():
    assert _guarded(parse_runtime_argv, ["prepare", "egress", "interactive", "claude", "brokered", "3"],
                    configured_slots=CONF, caller=OPERATOR)[0] == "ok"
    assert _guarded(parse_stdin, "prepare", cj({"lease_nonce": NONCE}))[0] == "ok"
    assert _guarded(parse_stdin, "render", cj(dict(RENDER, repo_label="x", driver="d",
                                                   verifier_steps=[{"argv": ["x"], "wall_s": 1}])))[0] == "ok"
    assert _guarded(ok_line, {"a": 1})[0] == "ok"
    assert _guarded(refusal_line, Refused("x"))[0] == "ok"


_ALPHABET = ["start", "prepare", "status", "render", "egress", "none", "interactive", "dispatch", "claude",
             "brokered", "none", "yes", S, "7", "64", "..", "/", "\x00", "\n", " ", "٠", "--x", ""]


def test_random_argv_never_crashes_and_never_touches_files():
    rnd = random.Random(1019)
    for _ in range(3000):
        n = rnd.randrange(0, 8)
        a = []
        for _ in range(n):
            if rnd.random() < 0.7:
                a.append(rnd.choice(_ALPHABET))
            else:
                a.append("".join(chr(rnd.randrange(0, 0x3000)) for _ in range(rnd.randrange(0, 20))))
        if rnd.random() < 0.3:          # mutate a valid request by one character
            a = ["prepare", "egress", "interactive", "claude", "brokered", "3"]
            i = rnd.randrange(len(a))
            t = list(a[i])
            t[rnd.randrange(len(t))] = chr(rnd.randrange(0, 0x3000))
            a[i] = "".join(t)
        caller = rnd.choice([OPERATOR, DISPATCH])
        kind, out = _guarded(parse_runtime_argv, a, configured_slots=CONF, caller=caller)
        if kind == "ok":                 # whatever passes is exactly a grammar sentence
            assert out.verb in rg.VERBS and (out.session is None or rg.SESSION_RE.fullmatch(out.session))
            assert all(t.isascii() and t.isprintable() and t.strip() == t for t in a)


def test_random_stdin_never_crashes_and_never_touches_files():
    rnd = random.Random(1020)
    seeds = [cj({"lease_nonce": NONCE}), cj({"lease_nonce": NONCE, "ticket": TICKET}), cj(RENDER),
             cj(dict(RENDER, driver="d", verifier_steps=[{"argv": ["x"], "wall_s": 1}]))]
    for _ in range(3000):
        raw = bytearray(rnd.choice(seeds))
        for _ in range(rnd.randrange(1, 4)):
            op = rnd.randrange(3)
            i = rnd.randrange(len(raw) + 1)
            if op == 0 and raw:
                raw[min(i, len(raw) - 1)] = rnd.randrange(256)
            elif op == 1:
                raw[i:i] = bytes([rnd.randrange(256)])
            elif raw:
                del raw[min(i, len(raw) - 1)]
        verb = rnd.choice(["prepare", "render", "start"])
        kind, out = _guarded(parse_stdin, verb, bytes(raw))
        if kind == "ok" and out is not None:      # whatever passes is canonical JSON, byte for byte
            assert canon.canonical_bytes(canon.parse(bytes(raw))) == bytes(raw)


def test_corpus_is_json_clean():
    """The argv corpus is plain strings (no bytes smuggled in by accident)."""
    json.dumps(ARGV_CORPUS + DISPATCH_CORPUS)
