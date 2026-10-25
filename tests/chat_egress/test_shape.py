"""CH-D2 S3b: R-10's path shape (the decider's ONE path-shape function) and the unkeyed high-entropy redaction.

Rows: R15-19-5 (the shape defined; `/dl/SECRET123SHORT` on a keyed host), R15-18-Q3 (path shape everywhere: keyed ->
shape, unkeyed -> B15's high-entropy redaction, no query value). The composed halves (no byte in events, frames, obs
or `chat_egress_events`) are S9's runs: `xfail(strict=True)` at the bottom.
"""

from __future__ import annotations

import pytest

from levain.chat_egress import keys, parse, shape

AUDIT = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"
TOKEN = r"<(?:0|1-4|5-8|9-16|17-32|33-64|65\+):[aA9\-%*]+>"


def FakeRoute(record_id, canonical_host, credential_source, declared_repos, attach):  # noqa: N802
    """S6's real `keys.RouteSnapshot` with a fixed account and incarnation (both required since the integration
    merge); the call shape S3a's fake had."""
    return keys.RouteSnapshot(record_id=record_id, canonical_host=canonical_host, credential_source=credential_source,
                              declared_repos=frozenset(declared_repos), attach=attach, credential_account="acct",
                              incarnation="1:" + "0" * 64)


def route(declared=("/o/r.git",), host="git.example"):
    return FakeRoute("rec-1", host, "keychain:x", frozenset(declared), True)


def frame(path, query=None, method="GET", host="git.example"):
    f = {"t": "req", "id": 1, "method": method, "host": host, "port": 443, "sni": host, "path": path,
         "headers": {"x-levain-audit": [AUDIT]}}
    if query is not None:
        f["query"] = query
    return f


def keyed(path, declared=("/o/r.git",), query=None):
    return shape.request_path_shape(parse.parse(frame(path, query)), route(declared))


def unkeyed(path, query=None):
    return shape.request_path_shape(parse.parse(frame(path, query)), None)


# --- R15-19-5: a keyed host -----------------------------------------------------------------------------------------

def test_r15_19_5_a_short_secret_segment_on_a_keyed_host_leaves_no_byte():
    out = keyed("/dl/SECRET123SHORT")
    assert "SECRET123SHORT" not in out and "SECRET" not in out
    assert out == "/<1-4:a>/<9-16:A9>"


def test_r15_19_5_a_declared_repos_info_refs_keeps_every_segment():
    assert keyed("/o/r.git/info/refs") == "/o/r.git/info/refs"
    assert keyed("/o/r.git/git-upload-pack") == "/o/r.git/git-upload-pack"


def test_r15_19_5_the_git_vocabulary_is_literal_and_exact():
    assert keyed("/x/info/refs", declared=()) == "/<1-4:a>/info/refs"
    assert keyed("/HEAD/objects/git-receive-pack", declared=()) == "/HEAD/objects/git-receive-pack"
    assert keyed("/head/Objects/INFO", declared=()) == "/<1-4:a>/<5-8:aA>/<1-4:A>"
    assert shape.GIT_VOCABULARY == frozenset({"info", "refs", "git-upload-pack", "git-receive-pack", "HEAD",
                                              "objects"})


def test_r15_19_5_undeclared_segments_are_tokens():
    out = keyed("/o/secret-repo.git/info/refs")
    assert out == "/o/<9-16:a->/info/refs"
    assert "secret" not in out


def test_r15_19_5_declared_repos_compare_in_r2s_canonical_form():
    assert keyed("/o/%72.git/info/refs", declared=("o/r.git",)) == "/o/r.git/info/refs"


def test_r15_19_5_a_token_never_carries_the_exact_length():
    assert keyed("/AAAAAAAAA1", declared=()) == keyed("/AAAAAAAAAAAAAAA1", declared=())


def test_the_root_and_trailing_slash_shapes():
    assert keyed("/", declared=()) == "/"
    assert keyed("/o/r.git/", declared=("/o/r.git",)) == "/o/r.git/"


# --- R15-18-Q3: an unkeyed host keeps B15's high-entropy redaction ------------------------------------------------

LONG = "SECRET123LONG" + "a" * 29          # ROUND4 probe 6's 42-character segment


@pytest.mark.parametrize("seg", [
    LONG,
    "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11",                    # a UUID
    "4b825dc642cb6eb9a060e54bf8d69288fbee4904",                # a git object id
    "a1b2c3d4e5f6",                                            # a short hex id
    "AbC9xYz12QwErTy0LpMn",                                    # a random base64-ish token
    "1234567890123456",                                        # a long number
])
def test_r15_18_q3_unkeyed_high_entropy_segments_are_redacted(seg):
    out = unkeyed(f"/dl/{seg}/f")
    assert seg not in out
    assert out.startswith("/dl/<") and out.endswith(">/f")


@pytest.mark.parametrize("seg", ["repos", "v1", "octocat", "Hello-World.git", "README.md", "issues", "2024",
                                 "my-long-project-name-here", "SECRET123SHORT"])
def test_r15_18_q3_unkeyed_ordinary_segments_stay_literal(seg):
    """`SECRET123SHORT` is PINNED literal on an unkeyed host: the known edge (§15; R15-19-5), the operator's own log."""
    assert unkeyed(f"/{seg}") == f"/{seg}"


def test_r15_18_q3_unkeyed_segments_outside_the_pchar_set_are_tokens():
    """A literal segment that looks like a token (or carries `"`, `<`, `>`, `\\``) is itself shaped."""
    assert unkeyed("/a/<1-4:a>/b") == "/a/<5-8:a9-*>/b"
    assert unkeyed('/a/x"y/b') == "/a/<1-4:a*>/b"


def test_r15_18_q3_no_query_value_in_either_shape():
    for fn in (keyed, unkeyed):
        out = fn("/dl/x", query="token=SECRETVALUE&SECRETNAME=v")
        assert "SECRETVALUE" not in out and "SECRETNAME" not in out and "?" not in out


def test_a_walled_path_is_all_tokens():
    p = parse.parse(frame("/o/../r.git/info/refs"))
    assert p.canonical_path is None
    for rt in (None, route()):
        out = shape.request_path_shape(p, rt)
        assert "r.git" not in out and "info" not in out
        segs = out.split("/")[1:]
        assert all(s.startswith("<") and s.endswith(">") for s in segs), out


def test_the_shape_uses_r2s_canonical_form():
    assert unkeyed("/%72epos") == "/repos"


# --- the token alphabet -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("n,bucket", [(0, "0"), (1, "1-4"), (4, "1-4"), (5, "5-8"), (8, "5-8"), (9, "9-16"),
                                      (16, "9-16"), (17, "17-32"), (32, "17-32"), (33, "33-64"), (64, "33-64"),
                                      (65, "65+"), (10_000, "65+")])
def test_len_buckets(n, bucket):
    assert shape.len_bucket(n) == bucket


@pytest.mark.parametrize("seg,cls", [("abc", "a"), ("ABC", "A"), ("123", "9"), ("a-b_c.d~e", "a-"), ("%2F", "A9%"),
                                     ("a$b", "a*"), ("aA9-%$", "aA9-%*"), ("é", "*")])
def test_charset_classes(seg, cls):
    assert shape.charset_class(seg) == cls


def test_a_token_matches_the_token_grammar():
    import re
    assert re.fullmatch(TOKEN, shape.segment_token("SECRET123SHORT"))


# --- R-10's query names (R15-21-2's rule; the function lives with the path shape: one owner) -----------------------

def test_query_names_literal_only_when_listed_or_pagination():
    p = parse.parse(frame("/x", query="token=1&page-token=2&X-Amz-Signature=3&SECRETNAME=4&foo=5&my_auth=6"))
    out = shape.query_name_shapes(p.params)
    assert out[0] == "token" and out[1] == "page-token" and out[2] == "x-amz-signature"
    assert out[3].endswith(":backstop>") and "secret" not in out[3].lower()
    assert out[4].startswith("<") and "foo" not in out[4] and not out[4].endswith(":backstop>")
    assert out[5].endswith(":backstop>")
    assert "SECRETNAME" not in " ".join(out) and "4" not in " ".join(out).replace("1-4", "")


# --- the composed halves: S9 ----------------------------------------------------------------------------------------

S9 = pytest.mark.xfail(strict=True, reason="S9 composed run")


@S9
def test_r15_19_5_keyed_short_secret_leaves_no_byte_in_events_frames_obs():
    """`/dl/SECRET123SHORT` on a keyed host through the CH-D2 glue + decider: no `SECRET123SHORT` byte in events,
    frames, obs or `chat_egress_events`."""
    raise NotImplementedError("needs the composed glue + decider + records (S9)")


@S9
def test_r15_18_q3_rows_carry_the_shape_and_events_no_path():
    """A keyed host's request -> its `chat_egress_events` row carries the shape and its glue event no path; an
    unkeyed host's high-entropy segment -> redacted in its row, no path in its event; no query value in either."""
    raise NotImplementedError("needs the composed glue + decider + records (S9)")
