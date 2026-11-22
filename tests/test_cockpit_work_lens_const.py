"""The cockpit cut (np-021ab5aa): the header's Work lens is hidden by one const in topology_view.js, and flipping it brings it back.

Node only, no browser: the file is loaded in a bare vm context with a stub storage, a stub width and a stub ``document``, so the
lens list, the stored-choice resolution and the phone-width default run as shipped. The const-true run loads the same source with
the const's text replaced, which is the one-line revert."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "topology_view.js"
OFF = 'const SHOW_WORK_LENS = typeof window !== "undefined" && window.LEVAIN_SHOW_WORK_LENS === true;'
ON = "const SHOW_WORK_LENS = true;"

# argv[1]: the source text; argv[2]: [{store: {key: value} | null, width: number, set: id | null, optin: bool}, ...]
# optin puts window.LEVAIN_SHOW_WORK_LENS = true in the stub, as a page's init script does before the scripts run.
DRIVER = r"""
const vm = require("vm");
const [src, cases] = [process.argv[1], JSON.parse(process.argv[2])];
const out = [];
for (const c of cases) {
  const mem = Object.assign({}, c.store || {});
  const el = () => ({ children: [], dataset: {}, className: "", textContent: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, appendChild(n) { this.children.push(n); return n; }, addEventListener() {} });
  const sandbox = { innerWidth: c.width, document: { createElement: el },
    localStorage: { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); } } };
  if (c.optin) sandbox.window = { LEVAIN_SHOW_WORK_LENS: true };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const TV = sandbox.LevainTopologyView;
  if (c.set) TV.setHeaderLens(c.set);
  const seg = TV.headerToggle(TV.headerLens(), () => {});
  out.push({ lens: TV.headerLens(), buttons: seg.children.map((b) => b.dataset.tv), stored: mem });
}
console.log(JSON.stringify(out));
"""
KEY = "levain_cockpit_header_lens"


def _run(src: str, cases: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed")
    done = subprocess.run([node, "-e", DRIVER, src, json.dumps(cases)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_with_the_const_false_the_lens_list_has_no_work_and_a_stored_work_resolves_to_agents() -> None:
    """Red with the const absent (the source assert), then with 'work' still in the lens list or still returned from headerLens
    for a stored 'work' or for the phone-width default; and with a click that could still store 'work' as the active lens."""
    text = SRC.read_text()
    assert OFF in text, "SHOW_WORK_LENS must be a const, off unless the page sets window.LEVAIN_SHOW_WORK_LENS, at the top of topology_view.js"
    wide, narrow = 1440, 400
    got = _run(text, [
        {"store": None, "width": wide},
        {"store": {KEY: "work"}, "width": wide},                    # a browser that chose Work before the cut
        {"store": {KEY: "events"}, "width": wide},                  # a kept choice stays
        {"store": None, "width": narrow},                           # the phone-width default used to open on Work
        {"store": {KEY: "work"}, "width": narrow},
        {"store": None, "width": wide, "set": "work"},             # nothing writes 'work' as the active lens
    ])
    assert got[0]["buttons"] == ["agents", "events"] and got[0]["lens"] == "agents", got[0]
    assert got[1]["lens"] == "agents" and "work" not in got[1]["buttons"], got[1]
    assert got[2]["lens"] == "events", got[2]
    assert got[3]["lens"] == "agents", got[3]
    assert got[4]["lens"] == "agents", got[4]
    assert got[5]["lens"] == "agents" and "work" not in got[5]["stored"].values(), got[5]


def test_flipping_the_const_true_brings_the_work_lens_back() -> None:
    """Red if the const does not govern the lens: with the const replaced by true, 'work' must be in the list again, a stored
    'work' must be honoured and the phone-width default must open on Work, as before the cut."""
    text = SRC.read_text()
    assert OFF in text
    got = _run(text.replace(OFF, ON), [
        {"store": None, "width": 1440},
        {"store": {KEY: "work"}, "width": 1440},
        {"store": None, "width": 400},
        {"store": None, "width": 1440, "set": "work"},
    ])
    assert got[0]["buttons"] == ["agents", "events", "work"] and got[0]["lens"] == "agents", got[0]
    assert got[1]["lens"] == "work", got[1]
    assert got[2]["lens"] == "work", got[2]
    assert got[3]["lens"] == "work" and got[3]["stored"][KEY] == "work", got[3]


def test_the_page_opt_in_brings_the_work_lens_back_without_editing_the_source() -> None:
    """Red if window.LEVAIN_SHOW_WORK_LENS = true does not govern the lens: with it set in the stub window, 'work' must be in the
    list, a stored 'work' must be honoured and a set 'work' must be stored; with a window that lacks it, 'work' stays out."""
    text = SRC.read_text()
    assert OFF in text
    got = _run(text, [
        {"store": None, "width": 1440, "optin": True},
        {"store": {KEY: "work"}, "width": 1440, "optin": True},
        {"store": None, "width": 1440, "set": "work", "optin": True},
        {"store": {KEY: "work"}, "width": 1440},
    ])
    assert got[0]["buttons"] == ["agents", "events", "work"] and got[0]["lens"] == "agents", got[0]
    assert got[1]["lens"] == "work", got[1]
    assert got[2]["lens"] == "work" and got[2]["stored"][KEY] == "work", got[2]
    assert got[3]["lens"] == "agents" and "work" not in got[3]["buttons"], got[3]
