"""Row 177: the `anthropic-beta` header on a dispatch route.

A dispatch route admits only the beta names levain judged (`dispatch_betas`); the operator's signed header pattern
is applied beside that list and can only narrow it. The captured set is what a real Claude Code sent a local
listener (`tests/fixtures/client_bodies/claude_code_anthropic_beta_headers.json`).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace

import pytest

from levain.broker import codec, gwwire, norm
from levain.broker.norm import digest_of_token

AM = codec.CODECS["anthropic-messages"]
OC = codec.CODECS["openai-chat"]
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "client_bodies")
SHIPPED = re.compile(r"[a-z0-9-]{1,64}(,\s?[a-z0-9-]{1,64}){0,31}")


def _captured():
    with open(os.path.join(FIX, "claude_code_anthropic_beta_headers.json"), encoding="utf-8") as f:
        return json.load(f)["requests"]


def _refused(headers, c=AM):
    with pytest.raises(norm.Refused) as e:
        c.check_betas(headers, dispatch=True)
    return e.value


# --- the outage test: what a real client sends is admitted -----------------------------------------------------------

@pytest.mark.parametrize("value", _captured())
def test_each_captured_claude_code_beta_set_is_admitted_on_dispatch(value):
    AM.check_betas([("anthropic-beta", value)], dispatch=True)


def test_the_captured_sets_hold_nine_distinct_betas_all_listed():
    names = {t for v in _captured() for t in v.split(",")}
    assert len(names) == 9 and names <= set(AM.dispatch_betas)


# --- the probe rows: refused on dispatch, admitted interactive -------------------------------------------------------

@pytest.mark.parametrize("beta,what", [
    ("mcp-client-2025-11-20", "mcp_client_2025_11_20"),
    ("advisor-tool-2026-03-01", "advisor_tool_2026_03_01"),
    ("inline-tools-2026-09-15", "inline_tools_2026_09_15"),
    ("dreaming-2026-04-21", "dreaming_2026_04_21"),
])
def test_an_unlisted_beta_is_refused_on_dispatch_and_admitted_interactive(beta, what):
    e = _refused([("anthropic-beta", beta)])
    assert e.name == "beta-unlisted:" + what and norm.status_of(e.name) == 403
    assert beta not in AM.dispatch_betas
    AM.check_betas([("anthropic-beta", beta)], dispatch=False)


def test_one_bad_token_in_a_comma_separated_value_refuses_the_whole_header():
    e = _refused([("anthropic-beta", "claude-code-20250219,mcp-client-2025-11-20,effort-2025-11-24")])
    assert e.name == "beta-unlisted:mcp_client_2025_11_20"
    e = _refused([("anthropic-beta", "claude-code-20250219, advisor-tool-2026-03-01")])   # a space after the comma
    assert e.name == "beta-unlisted:advisor_tool_2026_03_01"


def test_a_repeated_header_is_read_in_every_pair():
    e = _refused([("anthropic-beta", "effort-2025-11-24"), ("content-type", "application/json"),
                  ("anthropic-beta", "mcp-client-2025-11-20")])
    assert e.name == "beta-unlisted:mcp_client_2025_11_20"
    AM.check_betas([("anthropic-beta", "effort-2025-11-24"), ("anthropic-beta", "claude-code-20250219")],
                   dispatch=True)


def test_the_header_name_is_read_whatever_its_case_and_other_headers_are_ignored():
    assert _refused([("Anthropic-Beta", "mcp-client-2025-11-20")]).name == "beta-unlisted:mcp_client_2025_11_20"
    AM.check_betas([("x-other", "mcp-client-2025-11-20")], dispatch=True)


# --- name grammar ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1x", "claude-code-20250219,,effort-2025-11-24", "", " ", "claude-code-20250219,",
                                   "Effort-2025-11-24", "x" * 200, "a;b"])
def test_a_token_outside_the_wire_grammar_gets_a_name_status_of_takes(value):
    e = _refused([("anthropic-beta", value)])
    assert norm.status_of(e.name) == 403 and e.name.startswith("beta-unlisted")


def test_a_digit_led_or_empty_token_is_the_fixed_suffix():
    assert _refused([("anthropic-beta", "1x")]).name == "beta-unlisted:beta"
    assert _refused([("anthropic-beta", "effort-2025-11-24,,x")]).name == "beta-unlisted:beta"


def test_a_long_token_is_cut_to_the_grammar():
    e = _refused([("anthropic-beta", "z" + "-y" * 100)])
    assert e.name == "beta-unlisted:z" + "_y" * 23 + "_" and norm.status_of(e.name) == 403


# --- narrowing, never widening ---------------------------------------------------------------------------------------

def test_an_operator_pattern_narrows_a_listed_beta_out():
    only_effort = {"anthropic-beta": re.compile(r"effort-2025-11-24")}
    assert norm.check_headers([("anthropic-beta", "effort-2025-11-24")], only_effort, False, strict=True)
    with pytest.raises(norm.Refused) as e:
        norm.check_headers([("anthropic-beta", "claude-code-20250219")], only_effort, False, strict=True)
    assert e.value.name == "policy-header"
    assert "claude-code-20250219" in AM.dispatch_betas   # listed, and still outside the operator's own pattern


def test_an_operator_pattern_that_would_widen_the_list_does_not():
    wide = {"anthropic-beta": re.compile(r".*")}
    fwd = norm.check_headers([("anthropic-beta", "mcp-client-2025-11-20")], wide, False, strict=True)
    assert fwd == [("anthropic-beta", "mcp-client-2025-11-20")]   # the pattern admits it ...
    assert _refused(fwd).name == "beta-unlisted:mcp_client_2025_11_20"   # ... the list still refuses it
    # the pattern shipped for the interactive Claude route also spells it validly
    assert SHIPPED.fullmatch("mcp-client-2025-11-20,advisor-tool-2026-03-01")


def test_a_codec_that_never_sees_the_header_admits_no_beta_on_dispatch():
    assert OC.dispatch_betas == ()
    assert _refused([("anthropic-beta", "effort-2025-11-24")], OC).name == "beta-unlisted:effort_2025_11_24"
    OC.check_betas([("content-type", "application/json")], dispatch=True)
    OC.check_betas([("anthropic-beta", "effort-2025-11-24")], dispatch=False)


# --- the lists ---------------------------------------------------------------------------------------------------------

def test_the_beta_lists_are_disjoint_and_hold_valid_names():
    groups = (AM.dispatch_betas, AM.unbounded_betas, AM.unjudged_betas, AM.other_route_betas)
    flat = [b for g in groups for b in g]
    assert len(flat) == len(set(flat)), [b for b in flat if flat.count(b) > 1]
    assert all(re.fullmatch(r"[a-z0-9-]{1,64}", b) for b in flat)


def test_the_unbounded_and_unjudged_betas_are_refused_on_dispatch():
    for b in AM.unbounded_betas + AM.unjudged_betas + AM.other_route_betas:
        assert _refused([("anthropic-beta", b)]).name.startswith("beta-unlisted:")


# --- the keys path -----------------------------------------------------------------------------------------------------

def _beta_pol(pattern: str = SHIPPED.pattern):
    from tests.test_tickets_keys import dispatch_pol
    pol = dispatch_pol()
    return replace(pol, headers={**(pol.headers or {}), "anthropic-beta": re.compile(pattern)})


async def _exchange_with(d, monkeypatch, headers, pol):
    from tests.test_broker_keys_upstream import _Rig, _q, _scripted, _Up
    from tests.test_tickets_keys import FRONT_CLAIMS, MODEL, RUN, TOKEN, _claims
    from tests.ledger_support import file_lines
    body = json.dumps({"model": MODEL, "max_tokens": 10, "messages": []}).encode()
    up = _Up()
    st_ = _scripted(monkeypatch, up)
    async with _Rig(d, dispatch_policy=(lambda r, pin: pol)) as rig:
        rig.k.cache[digest_of_token(TOKEN)] = {"session": "s1", "claims": _claims(RUN)}
        rig.front.feed_data(gwwire.encode(b"B", body) + gwwire.encode(b"E"))
        up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        up.feed(b"")
        q = dict(_q("length:%d" % len(body)), route="glm")
        q["headers"] = [["content-type", "application/json"]] + [["anthropic-beta", h] for h in headers]
        await asyncio.wait_for(rig.run(q), 10)
        got = await rig.got()
        path = rig.ledger.path
    return {"frames": got, "handshakes": st_["handshakes"], "lines": file_lines(path)}


def test_a_dispatch_request_with_an_unlisted_beta_is_refused_before_any_hold(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    out = _run(lambda d: _exchange_with(d, monkeypatch, ["claude-code-20250219,mcp-client-2025-11-20"], _beta_pol()))
    assert out["frames"][-1][0] == b"F"
    assert json.loads(out["frames"][-1][1])["refused"] == "beta-unlisted:mcp_client_2025_11_20", out["frames"]
    assert out["handshakes"] == 0 and not [x for x in out["lines"] if x["kind"] == "reserved"]


def test_a_dispatch_request_with_the_captured_betas_is_admitted_and_held(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    out = _run(lambda d: _exchange_with(d, monkeypatch, [_captured()[0]], _beta_pol()))
    assert not (out["frames"][-1][0] == b"F" and "beta-unlisted" in json.loads(out["frames"][-1][1]).get("refused", ""))
    assert [x for x in out["lines"] if x["kind"] == "reserved"]


def test_a_widening_pattern_on_the_keys_path_still_refuses_an_unlisted_beta(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    out = _run(lambda d: _exchange_with(d, monkeypatch, ["advisor-tool-2026-03-01"], _beta_pol(".*")))
    assert json.loads(out["frames"][-1][1])["refused"] == "beta-unlisted:advisor_tool_2026_03_01", out["frames"]


def test_a_repeated_header_on_the_keys_path_is_refused_on_the_second(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    out = _run(lambda d: _exchange_with(d, monkeypatch, ["effort-2025-11-24", "mcp-client-2025-11-20"], _beta_pol()))
    assert json.loads(out["frames"][-1][1])["refused"] == "beta-unlisted:mcp_client_2025_11_20", out["frames"]


def test_the_refusal_is_one_refused_ledger_reason_and_no_hold(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    assert re.fullmatch(r"[a-z0-9-]{1,32}", "beta-unlisted")   # the ledger's reason grammar takes the prefix
    out = _run(lambda d: _exchange_with(d, monkeypatch, ["mcp-client-2025-11-20"], _beta_pol()))
    assert not [x for x in out["lines"] if x["kind"] in ("reserved", "settled")]


def test_keys_calls_the_beta_check_with_dispatch_true_on_a_dispatch_route(monkeypatch):
    seen = []
    real = AM.check_betas

    def spy(headers, *, dispatch):
        seen.append(dispatch)
        return real(headers, dispatch=dispatch)
    monkeypatch.setattr(AM, "check_betas", spy)
    from tests.test_broker_keys_upstream import _run
    _run(lambda d: _exchange_with(d, monkeypatch, ["effort-2025-11-24"], _beta_pol()))
    assert seen == [True]
