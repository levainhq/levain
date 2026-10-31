"""The short pairing code's two surviving pins (W1b slice C): the folding rule the cockpit's short code is typed through
(``levain.cockpit.shortcode``), and the installed phone page's copy that still carries the typed box. The phone port's
own short-code registry, route and ``levain ping pair`` are retired; the cockpit's mint, redeem and lock are pinned in
``tests/test_cockpit_w1b_server.py``."""
from __future__ import annotations

import re
from pathlib import Path

import levain
from levain.cockpit.shortcode import normalize_short

_JS = Path(levain.__file__).parent / "templates" / "web" / "phone_app.js"


def test_normalize_folds_case_hyphens_spaces_and_look_alikes():
    assert normalize_short("abcd-efgh") == "ABCDEFGH"
    assert normalize_short("  AB CD\tEF-GH \n") == "ABCDEFGH"
    assert normalize_short("abcd–efgh") == "ABCDEFGH"            # a keyboard's en dash for the hyphen
    assert normalize_short("OIL0-1234") == "01101234"                  # O reads 0, I and L read 1
    assert normalize_short("oil0-1234") == "01101234"
    for bad in ("", "   ", "-", "ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFGU", "ABCD-EF!H", "ABCD-EFGÉ", "ABCD-ＥFGH",
                "x" * 65, None, 12345678, b"ABCDEFGH"):
        assert normalize_short(bad) is None, bad


def test_the_page_offers_the_typed_code_in_the_installed_app():
    js = _JS.read_text(encoding="utf-8")
    for words in ('This app is not paired yet: type the pairing code from the laptop" + (isIos ? " or from Safari." : ".")',
                  "That code did not match, or it expired (only the newest code works). Get a new one: run levain ping "
                  'pair on the laptop" + (isIos ? ", or in Safari tap Install on this phone." : ".")',
                  "Too many wrong codes. Try again in ", " min, then get a new code.",
                  "Type this code in the box the app shows.", "New code", "Pairing code"):
        assert words in js, words
    for attr in ('id: "pair-code"', 'autocapitalize: "characters"', 'autocorrect: "off"', 'autocomplete: "off"',
                 'spellcheck: "false"', 'inputmode: "text"', "JSON.stringify({ short: text })"):
        assert attr in js, attr
    # the long-code and run-token boot paths are still there, and the redeem of a typed code sends no token header
    assert "JSON.stringify({ code: pair })" in js and "phone_token=" in js
    redeem = js[js.index("function redeemShort"):js.index("function submitPair")]
    assert "X-Levain-Phone-Token" not in redeem and "location.reload()" in js
    assert "innerHTML" not in js


def test_safari_is_named_only_on_ios():
    """The strings that name Safari are built from ``isIos``, which is computed before the first of them is made
    (a ``const`` read earlier is a ReferenceError that blanks the page)."""
    js = _JS.read_text(encoding="utf-8")
    assert js.index("const isIos") < js.index("const NOT_PAIRED"), "isIos must be defined before NOT_PAIRED uses it"
    assert js.count("const isIos") == 1
    for line in js.splitlines():
        if "Safari" in line and "Try Chrome on Android" not in line and not line.lstrip().startswith("//"):
            assert "isIos" in line, line


def test_the_page_copy_never_sends_the_operator_back_to_a_browser_link():
    """String guards only: the behaviour behind each (the revoked box, one mint per tap, the dead run token's switch,
    a 403 that is not the credential one) is proven by driving the page in a browser, not here."""
    js = _JS.read_text(encoding="utf-8")
    for gone in ("Open the link the laptop printed to install.", "open the link the laptop printed instead",
                 "then open the install link from the laptop again", "pair it again from the laptop",
                 'r.body.error === "forbidden"'):
        assert gone not in js, gone
    for kept in ("This phone was unpaired on the laptop: type a new pairing code in the box above to pair it again.",
                 "This browser will not keep the pairing, and the code is spent. Get a new code and try again in a ",
                 "and type it here.", "this page's (token|credential) is needed", "if (installBusy) return;"):
        assert kept in js, kept
    # the cockpit's credential-needed sentence is the one the page keys on
    assert re.match(r"this page's (token|credential) is needed", "this page's credential is needed")


def test_the_box_takes_a_pasted_code_with_invisible_characters_in_front_of_it():
    """The server strips format characters up to its own raw-input cap, so the box must not cut the paste first."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "phone_app.js").read_text()
    box = js[js.index('id: "pair-code"'):][:200]
    assert 'maxlength: "64"' in box and 'maxlength: "12"' not in box


def test_the_page_drops_a_subscription_made_under_another_installs_key():
    # the page's resync compares the browser subscription's key to the laptop's before handing it over; the run that
    # proves it on a real browser is in levain-memory 1019-app (a stale-key subscription reads "off")
    js = _JS.read_text(encoding="utf-8")
    for words in ("function keyMismatch(s)", "s.options.applicationServerKey", 'api("GET", "/webpush/key")',
                  "a.length !== b.length || a.some((v, i) => v !== b[i])",
                  "were made for a different Levain install, so they were switched off",
                  "could not be switched off here: tap Turn off, then turn on", 'pingNote = subState === "off"'):
        assert words in js, words
    assert "keyMismatch(s).then((stale) =>" in js
