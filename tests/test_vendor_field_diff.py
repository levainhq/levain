"""Row 150: `scripts/vendor_field_diff.py`, offline, on saved (trimmed) schema snapshots.

The snapshots in `tests/fixtures/vendor_schemas/` hold each vendor's real request-field names as published on
2026-11-09 and nothing else. The network fetch is the script's own path and is never run here (`_fetch` is stubbed to
fail).
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from levain.broker import codec

HERE = os.path.dirname(__file__)
SNAP = os.path.join(HERE, "fixtures", "vendor_schemas")
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "vendor_field_diff.py")

pytest.importorskip("yaml")


def _load():
    spec = importlib.util.spec_from_file_location("vendor_field_diff", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vfd = _load()


def _raw(name):
    with open(os.path.join(SNAP, name), "rb") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(url):
        raise AssertionError("the network was read: " + url)
    monkeypatch.setattr(vfd, "_fetch", boom)


@pytest.mark.parametrize("source", sorted(vfd.SOURCES))
def test_every_vendor_field_in_the_snapshot_is_triaged(source):
    assert vfd.untriaged(source, _raw(vfd.SOURCES[source][0])) == []


def test_openai_fields_are_read_through_allof_and_ref():
    got = vfd.vendor_fields("openai-chat", _raw("openai.yaml"))
    assert {"messages", "model", "web_search_options", "max_completion_tokens"} <= got
    assert len(got) > 30     # both halves of the allOf: the $ref'd base and the request's own properties
    assert {"input", "previous_response_id"} <= vfd.vendor_fields("openai-responses", _raw("openai.yaml"))


def test_the_anthropic_fields_are_the_typeddict_names_plus_stream():
    got = vfd.vendor_fields("anthropic-messages", _raw("anthropic_message_create_params.py"))
    assert {"max_tokens", "messages", "model", "context_management", "compaction", "stream"} <= got


def test_a_field_added_to_the_snapshot_is_printed():
    import yaml
    doc = yaml.safe_load(_raw("openai.yaml"))
    doc["components"]["schemas"]["CreateResponse"]["allOf"][1]["properties"]["zz_new"] = {"type": "string"}
    assert vfd.untriaged("openai-responses", yaml.safe_dump(doc).encode()) == ["zz_new"]

    orr = json.loads(_raw("openrouter.json"))
    orr["components"]["schemas"]["MessagesRequest"]["properties"]["zz_new"] = {"type": "string"}
    assert vfd.untriaged("openrouter-messages", json.dumps(orr).encode()) == ["zz_new"]

    py = _raw("anthropic_message_create_params.py") + b"\n\nclass Extra(MessageCreateParamsBase):\n    zz: str\n"
    assert vfd.untriaged("anthropic-messages", py) == []            # a field of another class is not the request's
    py2 = _raw("anthropic_message_create_params.py").replace(b"    model: Optional[str]\n",
                                                             b"    model: Optional[str]\n    zz_new: str\n", 1)
    assert vfd.untriaged("anthropic-messages", py2) == ["zz_new"]


def test_a_field_already_listed_prints_nothing_and_a_removed_one_is_not_reported():
    orr = json.loads(_raw("openrouter.json"))
    props = orr["components"]["schemas"]["ChatRequest"]["properties"]
    props["logit_bias"] = {"type": "object"}                         # on the list: nothing
    del props["temperature"]                                         # vendor removed it: the list keeps it, no report
    assert vfd.untriaged("openrouter-chat", json.dumps(orr).encode()) == []
    assert "temperature" in codec.CODECS["openai-chat"].dispatch_fields


def test_an_unjudged_field_is_not_reported_again_and_is_not_admitted():
    c = codec.CODECS["openai-responses"]
    assert "background" in c.unjudged_fields and "background" not in c.dispatch_fields
    orr = json.loads(_raw("openrouter.json"))
    orr["components"]["schemas"]["ResponsesRequest"]["properties"]["background"] = {"type": "boolean"}
    assert vfd.untriaged("openrouter-responses", json.dumps(orr).encode()) == []


def test_the_three_lists_are_disjoint_per_codec():
    for c in codec.CODECS.values():
        assert not set(c.unjudged_fields) & set(c.dispatch_fields), c.name
        assert not set(c.unjudged_fields) & set(c.unbounded_fields), c.name


def test_main_over_a_snapshot_directory_exits_clean_and_prints_each_source(capsys):
    assert vfd.main(["--snapshots", SNAP]) == 0
    out = capsys.readouterr().out
    assert out.count("nothing untriaged") == len(vfd.SOURCES)


def test_main_exits_one_and_names_an_untriaged_field(tmp_path, capsys):
    for name in ("openai.yaml", "anthropic_message_create_params.py"):
        (tmp_path / name).write_bytes(_raw(name))
    orr = json.loads(_raw("openrouter.json"))
    orr["components"]["schemas"]["ChatRequest"]["properties"]["zz_new"] = {"type": "string"}
    (tmp_path / "openrouter.json").write_text(json.dumps(orr))
    assert vfd.main(["--snapshots", str(tmp_path)]) == 1
    assert "openrouter-chat (vs openai-chat): zz_new" in capsys.readouterr().out


def test_properties_and_allof_beside_a_ref_are_walked_too():
    # OpenAPI 3.1 lets a `$ref` carry sibling keywords; a walk that returned at the `$ref` lost them
    doc = {"components": {"schemas": {
        "Base": {"properties": {"a": {}}},
        "Other": {"properties": {"c": {}}},
        "Req": {"$ref": "#/components/schemas/Base", "properties": {"b": {}},
                "allOf": [{"$ref": "#/components/schemas/Other"}, {"properties": {"d": {}}}]},
    }}}
    assert vfd.schema_fields(doc, "Req") == {"a", "b", "c", "d"}


# --- r1 F4: a field moved to an inherited TypedDict ----------------------------------------------------------------

def test_typeddict_fields_union_every_local_base():
    src = (
        "from typing_extensions import TypedDict, Required\n"
        "class Root(TypedDict, total=False):\n    a: int\n"
        "class Mixin(TypedDict, total=False):\n    m: int\n"
        "class Mid(Root, total=False):\n    b: Required[str]\n"
        "class Req(Mid, Mixin):\n    c: int\n    def f(self): pass\n"
    )
    assert vfd.typeddict_fields(src, "Req") == {"a", "b", "c", "m"}
    assert vfd.typeddict_fields(src, "Mid") == {"a", "b"}


def test_a_field_moved_into_a_base_class_is_still_reported(tmp_path, capsys):
    src = _raw("anthropic_message_create_params.py").decode() + (
        "\nclass ZzBase(TypedDict, total=False):\n    zz_moved: int\n"
        "\nclass MessageCreateParamsBase(ZzBase):\n    model: str\n")
    (tmp_path / "anthropic_message_create_params.py").write_text(src)
    for name in ("openai.yaml", "openrouter.json"):
        (tmp_path / name).write_bytes(_raw(name))
    assert vfd.main(["--snapshots", str(tmp_path)]) == 1
    assert "anthropic-messages (vs anthropic-messages): zz_moved" in capsys.readouterr().out


# --- r1 F5: a check that could not run exits 2, never 1 ------------------------------------------------------------

def _snapdir(tmp_path, **replace):
    for name in ("openai.yaml", "openrouter.json", "anthropic_message_create_params.py"):
        data = replace.get(name.replace(".", "_"), _raw(name))
        if data is not None:
            (tmp_path / name).write_bytes(data)
    return str(tmp_path)


def _one_line(capsys):
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1, err
    return err


def test_a_fetch_failure_exits_two_with_one_line(monkeypatch, capsys):
    def down(url):
        raise OSError("connection refused")
    monkeypatch.setattr(vfd, "_fetch", down)
    assert vfd.main([]) == 2
    assert "could not run" in _one_line(capsys)


def test_a_missing_snapshot_exits_two_with_one_line(tmp_path, capsys):
    assert vfd.main(["--snapshots", _snapdir(tmp_path, openrouter_json=None)]) == 2
    assert "openrouter.json" in _one_line(capsys)


def test_an_unresolved_ref_exits_two_with_one_line(tmp_path, capsys):
    orr = json.loads(_raw("openrouter.json"))
    orr["components"]["schemas"]["ChatRequest"] = {"$ref": "#/components/schemas/ZzGone"}
    assert vfd.main(["--snapshots", _snapdir(tmp_path, openrouter_json=json.dumps(orr).encode())]) == 2
    assert "ZzGone" in _one_line(capsys)


def test_a_missing_request_schema_or_class_exits_two(tmp_path, capsys):
    orr = json.loads(_raw("openrouter.json"))
    del orr["components"]["schemas"]["MessagesRequest"]
    assert vfd.main(["--snapshots", _snapdir(tmp_path, openrouter_json=json.dumps(orr).encode())]) == 2
    assert "MessagesRequest" in _one_line(capsys)
    src = _raw("anthropic_message_create_params.py").replace(b"class MessageCreateParamsBase", b"class Renamed")
    assert vfd.main(["--snapshots", _snapdir(tmp_path, anthropic_message_create_params_py=src)]) == 2
    assert "MessageCreateParamsBase" in _one_line(capsys)


def test_an_unparseable_snapshot_exits_two(tmp_path, capsys):
    assert vfd.main(["--snapshots", _snapdir(tmp_path, openrouter_json=b"{not json")]) == 2
    _one_line(capsys)
