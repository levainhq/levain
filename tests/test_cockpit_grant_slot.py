"""M17 S-b, the desktop half: ``grantSlot()`` beside ``proposalSlot()`` in the Decisions history.

Each standing grant is a card with what it answered (refused calls first, and a grant with a refused call first) and
a ``revoke`` button that copies the exact terminal line (the page signs nothing). Driven in headless Chrome against a
served cockpit whose consent hook names a real cockpit with real grants (signed by a real laptop key)."""
from __future__ import annotations

import shlex

from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_grant_phone import HOSTILE, world  # noqa: F401  (world is a fixture)
from tests.test_phone_grants_tab import _answered, _grant

_CARDS = "[...document.querySelectorAll('#levain-modal [data-consent-grant]')]"


@needs_browser
def test_the_decisions_history_lists_each_grant_refused_first_with_a_copy_the_line_revoke(world, tmp_path, monkeypatch):
    """Reproduced red before the build: no `[data-consent-grant]` card was ever drawn (the wait timed out)."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, "cg-0000000000aa", "Write")
    _grant(world, "cg-0000000000bb", "Edit")
    _grant(world, HOSTILE, "MultiEdit")
    assert _answered(world, "Write", "a.txt", "accepted") == "cg-0000000000aa"
    assert _answered(world, "Edit", "b.txt", "accepted") == "cg-0000000000bb"
    assert _answered(world, "Edit", "c.txt", "refused") == "cg-0000000000bb"
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 3"},
            {"eval": f"{_CARDS}.map(c => c.dataset.consentGrant)"},
            {"eval": f"[...{_CARDS}[0].querySelectorAll('.grant-call')].map(c => c.textContent)"},
            {"eval": f"{_CARDS}.map(c => c.querySelector('.consent-code').textContent)"},
            {"eval": f"{_CARDS}.map(c => c.querySelector('.consent-copy').textContent)"},
            {"eval": "[...document.querySelectorAll('#levain-modal .ext-grouphead')].map(h => h.textContent)"},
            {"eval": f"{_CARDS}[0].classList.contains('grant-demote')"},
        ])
    order, calls, codes, buttons, heads, demote = out["values"][-6:]
    # the grant that answered a call which was then refused leads, and its refused call leads its card
    assert order[0] == "cg-0000000000bb" and set(order) == {"cg-0000000000aa", "cg-0000000000bb", HOSTILE}, order
    assert calls[0].startswith("refused") and "Edit" in calls[0] and demote is True, calls
    # each card's button copies the exact revoke line: the id is one shell word after `--`, the root the hook's
    for gid, code in zip(order, codes):
        words = shlex.split(code)
        assert words[:4] == ["levain", "consent", "grant", "revoke"] and words[-2:] == ["--", gid], code
        assert words[words.index("--data-root") + 1] == str(world.root), code
    assert buttons == ["revoke"] * 3, buttons
    assert "standing grants" in heads, heads
    assert out["posts"] == []          # the page sends nothing: it never signs


@needs_browser
def test_a_page_without_its_token_draws_no_grant(world, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, "cg-0000000000aa", "Write")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": "!!document.querySelector('#levain-modal')"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelectorAll('#levain-modal [data-consent-grant]').length), 1500))"},
        ])
    assert out["values"][-1] == 0


_TYPE = ("(v => { const i = document.querySelector('#levain-modal .xfilter-input'); i.value = v;"
         " i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })")


@needs_browser
def test_a_failed_grants_read_is_not_cached_as_a_good_one(world, tmp_path, monkeypatch):
    """M17 L3 r1 M-2. The first grants.json read fails (the fetch throws, so the queued read answers ``{error}``); the
    next repaint of the slot (a filter repaint, inside the 30 s cache) must read again and draw the grant. Reproduced
    red on 7ae3def0: the ``{error}`` object was cached as a read, so no card came back until the modal was reopened."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, "cg-0000000000aa", "Write")
    assert _answered(world, "Write", "a.txt", "accepted") == "cg-0000000000aa"   # a decided row: the modal draws its filter
    _root, src, _ids = _install(tmp_path)
    stub = ("(() => { const real = window.fetch.bind(window); let n = 0; window.fetch = (u, o) => {"
            " if (String(u).includes('/grants.json') && n++ < 1) throw new TypeError('the first grants read fails');"
            " return real(u, o); }; return 1; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": stub}, {"eval": OPEN_DECISIONS},
            {"until": "!!document.querySelector('#levain-modal .xfilter-input')"},
            {"eval": f"new Promise(r => setTimeout(() => r({_CARDS}.length), 1500))"},
            {"eval": f"{_TYPE}('zzz-no-row')"}, {"eval": f"{_TYPE}('')"},
            {"eval": f"new Promise(r => setTimeout(() => r({_CARDS}.length), 2000))"},
        ])
    first, after = out["values"][-4], out["values"][-1]
    assert first == 0, out["values"]            # the failed read drew nothing
    assert after == 1, out["values"]            # and was not kept: the repaint read again


@needs_browser
def test_a_refusal_older_than_the_newest_calls_still_leads_the_slot(world, tmp_path, monkeypatch):
    """M17 L3 r1 M-5 (ruling M17-1), desktop. Reproduced red on 7ae3def0: the grant whose one refusal is older than
    its 5 newest calls was neither first nor accented, and its head said no refusal."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, "cg-0000000000aa", "Write")
    _grant(world, "cg-0000000000bb", "Edit")
    assert _answered(world, "Write", "a.txt", "accepted") == "cg-0000000000aa"
    assert _answered(world, "Edit", "r.txt", "refused") == "cg-0000000000bb"
    for i in range(5):
        assert _answered(world, "Edit", f"b{i}.txt", "accepted") == "cg-0000000000bb"
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 2"},
            {"eval": f"{_CARDS}.map(c => c.dataset.consentGrant)"},
            {"eval": f"{_CARDS}[0].classList.contains('grant-demote')"},
            {"eval": f"{_CARDS}[0].querySelector('.consent-left').textContent"},
        ])
    order, demote, left = out["values"][-3:]
    assert order == ["cg-0000000000bb", "cg-0000000000aa"] and demote is True, out["values"]
    assert "answered 6×" in left and "1 refused" in left, left
