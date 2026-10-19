"""R1 write L3 r1 fixes, server side: a downstream verb's declared fields reach the manifest and the
allowlist, and a canonical bug is logged where it is mapped to transient."""

from __future__ import annotations

import logging
from pathlib import Path

from levain.cockpit.effects import DownstreamEffect
from levain.writes import ActionVerb
from tests._inbox_fake import FakeInbox
from tests._chrome_flags import CHROME_SAFE_FLAGS
from tests._page_drive_run import run as run_driver
from tests.test_cockpit_k2a import _get, _install, _post, _serve
from tests.test_cockpit_r1_write import INBOX_PANELS, _inbox_panel


def _server(src, verb, effects=None):
    return _serve(src, extra_verbs={"send_inbox": verb}, extra_panels=lambda: INBOX_PANELS,
                  extra_effects=effects or {}, surface_tokens={"browser": "T"}, install_class="reported")


def test_a_downstream_verbs_declared_fields_are_published_and_enforced(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    verb = ActionVerb(handler=lambda p: {"summary": "sent"}, label="send", fields=("message", "for"))
    with _server(src, verb) as (url, _httpd):
        man = _get(f"{url}/cockpit/manifest.json", token="T")
        assert man["verbs"]["send_inbox"]["fields"] == ["message", "for"]
        pid = _inbox_panel(url)
        status, out = _post(f"{url}/cockpit/verb", {
            "verb": "send_inbox", "panel_id": pid, "params": {"message": "x", "other": 1}, "confirm": True,
            "dry_run": True}, token="T")
        assert (status, out["error"]) == (400, "field_not_allowed"), out


def test_an_undeclared_downstream_verb_publishes_no_fields_and_takes_any(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    verb = ActionVerb(handler=lambda p: {"summary": "sent"}, label="send")
    with _server(src, verb) as (url, _httpd):
        man = _get(f"{url}/cockpit/manifest.json", token="T")
        assert man["verbs"]["send_inbox"]["fields"] == []
        pid = _inbox_panel(url)
        status, out = _post(f"{url}/cockpit/verb", {
            "verb": "send_inbox", "panel_id": pid, "params": {"anything": 1}, "confirm": True,
            "dry_run": True}, token="T")
        assert status == 200, out


def test_a_canonical_bug_is_logged_before_it_is_mapped_to_transient(tmp_path: Path, caplog) -> None:
    _root, src, _ids = _install(tmp_path)

    def broken(params, *, origin_key):
        raise RuntimeError("canonical exploded")

    verb = ActionVerb(handler=lambda p: {}, label="send", fields=("message", "for"))
    effects = {"send_inbox": DownstreamEffect(FakeInbox(), broken)}
    with caplog.at_level(logging.WARNING), _server(src, verb, effects) as (url, _httpd):
        pid = _inbox_panel(url)
        body = {"verb": "send_inbox", "panel_id": pid, "params": {"message": "x"}, "confirm": True}
        _status, pre = _post(f"{url}/cockpit/verb", {**body, "dry_run": True}, token="T")
        _post(f"{url}/cockpit/verb", {**body, "intent": "reported", "idempotency_key": "k1",
                                      "tier_seen": pre.get("tier"), "gesture_seen": pre.get("gesture")}, token="T")
    assert any("canonical exploded" in (r.exc_text or "") or (r.exc_info and "canonical exploded" in str(r.exc_info[1]))
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# --- the page in a real browser -------------------------------------------------------------------

import json
import re
import shutil
from datetime import datetime, timezone

import pytest

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _dump(url: str) -> str:
    return run_driver([CHROME, *CHROME_SAFE_FLAGS, "--dump-dom", "--virtual-time-budget=8000",
                       "--window-size=1440,1000", url], timeout=90).stdout


def _bare_install(tmp_path: Path, texts: list[str]):
    from levain.dashboard import SubstrateSource
    from anneal_memory.spores import SporeStore
    from tests.test_cockpit_k2a import TODAY
    from tests.test_dashboard import _make_levain_install

    root = _make_levain_install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    for t in texts:
        store.add(type="task", text=t, disposition="seed", today=TODAY)
    (root / ".levain" / "context.json").write_text(json.dumps(
        {"state": "fine", "state_set_at": datetime.now(timezone.utc).isoformat(), "state_source": "cli"}))
    return SubstrateSource.local(root)


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_the_default_cockpit_in_a_browser_offers_no_rename_and_no_text_edit_on_a_hidden_character_row(tmp_path: Path) -> None:
    src = _bare_install(tmp_path, ["plain seed", "zero​width seed"])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        dom = _dump(f"{url}/?source=manifest#write_token=T")
    assert "plain seed" in dom and "&lt;U+200B&gt;" in dom, dom[:400]
    assert 'class="name-edit"' not in dom, "rename is not offered by the kernel"
    assert 'placeholder="add a note' in dom, "an empty Keep still has its capture box"
    edits = re.findall(r'<button[^>]*data-verb="spore_update"[^>]*>edit</button>', dom)
    locked = [b for b in edits if "hidden characters" in b]
    assert len(edits) == 2 and len(locked) == 1 and "disabled" in locked[0], edits


def test_action_verb_fields_must_be_none_or_a_tuple_of_unique_non_empty_strings() -> None:
    import pytest

    def h(p):
        return {}

    assert ActionVerb(handler=h).fields is None
    assert ActionVerb(handler=h, fields=()).fields == ()
    assert ActionVerb(handler=h, confirm_required=True, label="x", fields=("message", "for")).fields == ("message", "for")
    for bad in ("message", ["message"], ("a", "a"), ("a", ""), ("a", 1)):
        with pytest.raises(ValueError):
            ActionVerb(handler=h, fields=bad)


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_the_default_page_draws_the_manifest_and_offers_a_downstream_send(tmp_path: Path) -> None:
    """The useManifest flip (HANDOFF OPEN #2): the default page is the manifest source, so an embedder's
    downstream send fires through /cockpit/verb and the broker. Before the flip `/` drew the legacy page,
    whose send posted /action, which K2b refuses for a T2 verb (measured by 1011+37 on a served Bridge).
    `?source=substrate` keeps the legacy page reachable."""
    from tests._inbox_fake import inbox_downstream
    from tests.test_cockpit_r1_write import INBOX_PANELS

    _root, src, _ids = _install(tmp_path)
    verb = ActionVerb(handler=lambda p: {"summary": "sent"}, label="send", fields=("message", "for"))
    with _serve(src, extra_verbs={"send_inbox": verb}, extra_panels=lambda: INBOX_PANELS,
                extra_effects={"send_inbox": inbox_downstream()[1]}, surface_tokens={"browser": "T"},
                install_class="reported") as (url, _httpd):
        default = _dump(f"{url}/#write_token=T")
        legacy = _dump(f"{url}/?source=substrate#write_token=T")
    # the shell draws a card's count as its own chip beside the title (`<h2>Now</h2><span class="ck-count">1`), not inside it
    assert '<h2>Now</h2><span class="ck-count">' in default and 'aria-label="message"' in default, default[:400]
    assert "<h2>Now (" not in legacy and '<h2>Now</h2><span class="ck-count">' not in legacy and 'aria-label="message"' not in legacy
