"""CH-D2 S9 session 2b: the R15-10-A14 umbrella checker. R15-10-A14 makes every red test complement listed a build
condition, quoted item by item. `ITEMS` maps each quoted item to the test ids that hold it; the checks:

- every id names a test that exists (a Python test function by AST, a node test by its title's opening words);
- every item has at least one id; an item whose map is partial also sits in `GAPS`, saying what is not held;
- the quotes are the row's, verbatim and all of them, read from the conditions file in the levain project-memory
  checkout (`CHD2_DOCS_DIR`, as `test_doc_checks.py`). That one check is LOCAL-ONLY by design: project memory is a
  separate repo, so in CI it skips, and a conditions row that adds, drops or renames an item with `ITEMS` left stale
  is caught on a machine that has the file, not in CI. What CI does hold: an `ITEMS` entry naming a test that does not
  exist fails (`test_every_item_maps_to_tests_that_exist`), and so does an item with no id.

Which test holds which item is a reading, recorded per item [judged by lane-s9s2b, 10-25, against each test's body].
"""

from __future__ import annotations

import ast
import os
import pwd
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]          # tests/chat_egress
DOCS = Path(os.environ.get("CHD2_DOCS_DIR", Path(pwd.getpwuid(os.getuid()).pw_dir) /
                           "Briefcase/levain/project_memory/1024-6/chd1"))

ITEMS: dict[str, list[str]] = {
    "Re-attach never on a cleartext or non-TLS upstream (finding 1).": [
        "test_keys.py::test_r15_10_a6_r15_12_b12_no_key_off_https_443",
        "probes/test_chd2_r3.py::test_b12_no_key_and_no_client_credential_on_plain_http",
        "probes/test_chd2_r3.py::test_b12_negative_control_a_decider_that_keys_plain_http_is_caught",
        "probes/test_chd2_r3.py::test_b1_1_connect_to_an_off_tls_port_keyed_host"],
    "Plain-HTTP keep-alive multi-authority (finding 2).": [
        "node/glue_srt.test.mjs::R15-10-A9 + R15-30-4: one keep-alive plain connection"],
    "Flapping DNS (finding 3).": [
        "probes/test_chd2_r3.py::test_b4_rebinding_is_refused_at_dial",
        "probes/test_chd2_r3.py::test_b4_pin_one_lookup_and_that_address_dialed",
        "test_guard_srt.py::test_r15_12_b4_each_rebinding_class_refused_at_dial",
        "test_decider.py::test_a_post_to_a_name_answering_loopback_walls_and_files_no_card"],
    "Upstream cert verify fails closed, plus a closed Node env (finding 3).": [
        "test_launch_tls.py::test_the_explicit_roots_alone_refuse_a_bad_cert_under_an_inherited_hostile_ca",
        "test_launch_tls.py::test_the_closed_env_alone_refuses_a_bad_cert_with_the_roots_unset",
        "test_launch.py::test_the_spawned_glue_env_is_closed_whatever_the_parent_holds",
        "probes/test_chd2_r3.py::test_b6_system_roots_refuse_the_bad_cert_with_a_hostile_parent_variable",
        "probes/test_chd2_r3.py::test_b6_negative_control_a_glue_that_trusts_the_bad_ca_reaches_the_origin"],
    "Strip differential suite (finding 6).": [
        "test_strip.py::test_r15_12_b12_the_strip_is_the_same_on_plain_http",
        "composed/test_strip_multivalue.py::test_sixteen_credential_values_reach_the_origin_as_none",
        "composed/test_tls_keyed.py::test_a_multi_valued_credential_set_from_the_client_leaves_exactly_the_held_one",
        "composed/test_k1_corpus.py::test_a_real_client_carrying_credentials_reaches_the_origin_with_none"],
    "Open network + keyed write (finding 4).": [
        "test_classify.py::test_verdict_table",
        "composed/test_a14_gaps.py::test_open_network_a_keyed_write_is_a_card_and_an_anonymous_one_is_silent"],
    "Concurrent silent reads during a held card, and a 120 s hold (finding 7).": [
        "test_decider.py::test_two_held_cards_do_not_stall_a_get",
        "test_decider_glue.py::test_two_cards_held_20_s_while_a_get_completes",
        "test_lifecycle.py::test_r15_10_a3_hold_is_100s_under_claude_codes_120s_bash_default"],
    "`Expect: 100-continue` and pipelining (finding 7).": [
        "composed/test_a13_continue_pipelining.py::test_expect_100_continue_sends_no_byte_upstream_before_his_allow",
        "composed/test_a13_continue_pipelining.py::test_expect_100_continue_denied_sends_no_byte_upstream",
        "composed/test_a13_continue_pipelining.py::test_a_get_pipelined_behind_a_held_post"],
    "R-1's smuggling suite, run through the actual srt parser.": [
        "composed/test_smuggling.py::test_plain_path_smuggling_form_reaches_the_origin_as_nothing",
        "composed/test_smuggling.py::test_tls_smuggling_form_reaches_the_origin_as_nothing",
        "composed/test_a14_gaps.py::test_te_chunked_identity_on_the_plain_path_reaches_the_origin_as_nothing",
        "composed/test_a14_gaps.py::test_te_chunked_identity_inside_tls_reaches_the_origin_as_nothing"],
    "Absolute-form target in a tunnel must equal Host, and `X-Original-URL`, `X-Rewrite-URL` and `X-Forwarded-Host` "
    "handling.": [
        "composed/test_tls_probes.py::test_an_absolute_form_target_in_tls_naming_another_authority_gets_421",
        "probes/test_chd2_r3.py::test_b9_host_other_than_the_absolute_form_target_on_plain_http",
        "test_parse.py::test_r15_12_b14_routing_headers_are_named_for_strip",
        "composed/test_a14_gaps.py::test_routing_headers_are_stripped_from_a_forwarded_read"],
    "A decider crash fails closed with a visible state.": [
        "composed/test_a14_gaps.py::test_a_decider_crash_fails_closed_with_a_visible_state",
        "node/glue_unit.test.mjs::machine: pending + inner throws -> closed"],
    "A package-ecosystem corpus (npm, pip, cargo, go, gem, git LFS, `gh`) for the \"1 anonymous-write card per 20 "
    "tasks\" threshold. LFS and GraphQL reads are POSTs and will card.": [
        "test_decider_glue.py::test_real_clones_n_runs_zero_cards"],
    "Card text sanitisation (finding 11).": [
        "test_cards.py::test_a_bidi_override_and_a_control_in_every_agent_field_show_as_markers_never_raw_or_dropped",
        "test_cards.py::test_markup_in_a_field_is_carried_as_text_for_the_page_to_draw_as_text"],
}

#: items whose map is partial: what no test here holds
GAPS: dict[str, str] = {
    "A package-ecosystem corpus (npm, pip, cargo, go, gem, git LFS, `gh`) for the \"1 anonymous-write card per 20 "
    "tasks\" threshold. LFS and GraphQL reads are POSTs and will card.":
        "only git clones are measured for cards (zero); the card RATE over npm, pip, cargo, go, gem, git LFS and gh "
        "task flows is not run (K1's corpus runs pip, npm and uv for credential stripping, not for cards)",
}


def _py_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")}


def missing(ids: list[str]) -> list[str]:
    """The ids that name no existing test."""
    out = []
    for tid in ids:
        rel, _, name = tid.partition("::")
        path = TESTS / rel
        if not path.is_file():
            out.append(tid)
        elif rel.endswith(".mjs"):
            if not re.search(r"^test\(['\"]" + re.escape(name), path.read_text(), re.M):
                out.append(tid)
        elif name not in _py_tests(path):
            out.append(tid)
    return out


def test_the_checker_can_fail() -> None:
    """The control: a misspelt function, a missing file and a node title that is not there are each reported."""
    good = "composed/test_a14_gaps.py::test_a_decider_crash_fails_closed_with_a_visible_state"
    bad = [good + "_x", "composed/no_such_file.py::test_x", "node/glue_srt.test.mjs::no such title"]
    assert missing([good] + bad) == bad


@pytest.mark.parametrize("item", list(ITEMS), ids=[f"item{i}" for i in range(len(ITEMS))])
def test_every_item_maps_to_tests_that_exist(item) -> None:
    _check_item(item, ITEMS)


def _check_item(item: str, items: dict[str, list[str]]) -> None:
    ids = items[item]
    assert ids, f"no test for {item!r}"
    assert missing(ids) == [], missing(ids)


def test_an_item_with_no_id_or_a_stale_id_fails_with_no_project_memory_present() -> None:
    """The gate CI holds without the conditions file: the per-item check is a pure function of `ITEMS` and the test tree."""
    some = next(iter(ITEMS))
    _check_item(some, ITEMS)
    for broken in ({some: []}, {some: ["composed/test_a14_gaps.py::test_renamed_away"]}):
        with pytest.raises(AssertionError):
            _check_item(some, broken)


def test_the_gaps_are_named_items() -> None:
    assert set(GAPS) <= set(ITEMS), set(GAPS) - set(ITEMS)


def _row_quotes(text: str) -> list[str]:
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("- **R15-10-A14**"))
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("- **")), len(lines))
    return [" ".join(q.split()) for q in re.findall(r'\*"(.*?)"\*', "\n".join(lines[i:j]), re.S)]


def test_the_items_are_the_rows_quotes_verbatim_and_all_of_them() -> None:
    path = DOCS / "CHD2_BUILD_CONDITIONS_REV15.md"
    if not path.is_file():
        pytest.skip(f"{path} is not on this machine (CHD2_DOCS_DIR): the quote comparison is local-only by design; "
                    "the id-exists checks above still run here")
    assert _row_quotes(path.read_text()) == list(ITEMS)
