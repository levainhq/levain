"""S4MD-2: a flow Needs-you ping's detail view. flow's push body is `repo: what` / an options line / a sign line, each one line
(the row itself is one line), and the detail view used to draw it as one run-on paragraph with a raw address. The headline
behaviours: the body is read back into parts (and the title is plain), the text's list and link draw as blocks, and the options
draw as rows that open THIS cockpit's sign page with the pick appended. The gap-closers (review r1): the sign link is never
taken from the push, a request that is not open draws inert rows, a list's last bullet is split only at a fixed boundary, flow's
structured fields (data v1) are read before the body, and the store carries them. What stays undischarged here is how it
reads in his hands on his iPhone, and the Face ID round trip from a tapped option."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.consent import sign
from levain.webpush import center, sender
from levain.webpush.store import DATA_MAX, DIR_NAME, PushStore, data_text
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_notify_center_page import MODAL, OPEN, OPENED, PILL, Rig, _env, _json, _url  # noqa: F401  (_env: the autouse fixture)

RID = "np-7121be43-4a9b2da2"
SIGN = f"https://missinglayer.example.ts.net/sign/{RID}#sign={RID}&v=7daebba8dcab02c8"
WHAT = ("**S4MD test ping** (desk 18). Tap into this item's *detail view* and judge how the markdown renders: - **bold** and *italic* "
        "- a list (this one) - `inline code` - a link with its address shown: [Levain repo](https://github.com/levainhq/levain) "
        "Then answer (a) if it reads right, or (b) and say what looks wrong.")
OPTS = (("a", "renders right, S4MD discharged"), ("b", "something is off (say what, on which device)"))
BODY = (f"levain: {WHAT}\na) {OPTS[0][1]} · b) {OPTS[1][1]} · Rec a\n"
        f"Sign your answer (Face ID): {SIGN}")
NOOPTS = "levain: Talk about the hosting move when you are back at the desk.\nNo listed options.\nAnswer in flowConnect: /desk + your words (no id needed)"


def _file(q: Path, rid: str, item: str, options=OPTS, rec: str | None = "a", *, asked: str = WHAT, stopped: str = "nothing: S4MD sign-off",
          namespace: str = "flow-needs-phill", ttl: float = 3600) -> str:
    """A sign request file in flow's real shape (its needs_phill_sign.build_message and choice request), read back only through
    levain's own reader. Returns the 16-hex version a link carries."""
    msg = "\n".join(["flow needs-phill answer", f"item: {item}", "row_version: 0123abcd", "repo: levain", f"asked: {asked}", f"stopped: {stopped}"])
    data = {"format": sign.CHOICE_FORMAT, "id": rid, "namespace": namespace, "message": msg, "title": f"NEEDS PHILL {item}",
            "requester": "flow", "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
            "options": [{"key": k, "text": t} for k, t in options], "write_in": True}
    if rec:
        data["recommended"] = rec
    d = sign.sign_dir(q)
    d.mkdir(mode=0o700, exist_ok=True)
    raw = json.dumps(data).encode("utf-8")
    (d / f"{rid}.json").write_bytes(raw)
    os.chmod(d / f"{rid}.json", 0o600)
    return hashlib.sha256(raw).hexdigest()[:16]


def _data(item: str, sign_id: str | None, **over) -> dict:
    d = {"v": 1, "kind": "decision", "id": item, "repo": "tao", "what": "Move the TAO mail to its own domain.\n\n- MX at the new host\n- SPF and DKIM set\n\nThen answer (a) or (b).",
         "options": [["a", "move it now"], ["b", "wait for Jeanne"]], "rec": "a", "rec_reason": "the DMARC date is 11-16",
         "stopped": "the MX switch", "sign_id": sign_id, "expires_at": time.time() + 3600, "dated": "2026-11-16"}
    d.update(over)
    return d


# --- the headline three ------------------------------------------------------------------------------------------------
def test_a_needs_ping_is_read_back_into_its_parts_and_its_title_is_plain(tmp_path: Path) -> None:
    """The push's words come back as repo, text, options and Rec; the sign request the link names is read from a REAL file in
    flow's shape (no stand-in reader), and its options, Rec and stop text win; the title is the text's first sentence with no
    Markdown; a body off the shape is left alone."""
    got = center.needs_wire(BODY, None, "np-7121be43")
    assert got and got["repo"] == "levain" and got["what"] == WHAT and got["rec"] == "a" and got["sign_id"] == RID, got
    assert [o["key"] for o in got["options"]] == ["a", "b"] and got["title"] == "S4MD test ping (desk 18).", got
    assert got["sign_state"] == "unknown" and got["sign_v"] is None and got["stopped_known"] is False, got   # nothing read: inert
    v = _file(tmp_path, RID, "np-7121be43", options=(("a", OPTS[0][1] + " IN FULL"), OPTS[1]), rec="b")
    full = center.needs_wire(BODY, tmp_path, "np-7121be43")
    assert full["sign_state"] == "open" and full["sign_v"] == v and full["stopped"] == "nothing: S4MD sign-off", full
    assert full["options"][0]["text"].endswith("IN FULL") and full["rec"] == "b", full                      # the request's words, not the push's
    assert center.needs_wire("claude_code asks: Ship it?", tmp_path) is None and center.needs_wire("just text\nmore", tmp_path) is None
    assert center.needs_wire(NOOPTS)["options"] == [] and center.needs_wire(NOOPTS)["sign_state"] is None
    store = PushStore(tmp_path)
    store.add_ping("attention", "np-7121be43", "levain", BODY)
    item = center.list_items(store)["items"][0]
    assert item["title"] == "S4MD test ping (desk 18)." and "*" not in item["title"] and item["needs"]["sign_state"] == "open", item
    assert "missinglayer" not in json.dumps(item["needs"]), item["needs"]                                   # no address leaves the center


def _probe() -> str:
    return (f"JSON.stringify({{ title: {MODAL}.querySelector('.nc-dtitle').textContent, repo: ({MODAL}.querySelector('.nc-repo') || {{}}).textContent,"
            f" li: [...{MODAL}.querySelectorAll('.nc-nlist li, .nc-nbody li')].map(x => x.textContent), strong: [...{MODAL}.querySelectorAll('.nc-nbody strong')].map(x => x.textContent),"
            f" code: [...{MODAL}.querySelectorAll('.nc-nbody code')].map(x => x.textContent), full: [...{MODAL}.querySelectorAll('.nc-nbody .nc-fullurl')].map(x => x.textContent),"
            f" after: [...{MODAL}.querySelectorAll('.nc-nbody > *')].map(x => x.tagName + ':' + x.textContent),"
            f" picks: [...{MODAL}.querySelectorAll('.nc-pick')].map(a => [a.querySelector('.nc-pickkey').textContent, a.querySelector('.nc-picktext').textContent,"
            f" a.getAttribute('href'), (a.querySelector('.nc-recmark') || {{}}).textContent || null, a.getBoundingClientRect().height, a.tagName, a.getAttribute('rel'),"
            f" getComputedStyle(a).backgroundColor === getComputedStyle(document.documentElement).getPropertyValue('--ok') || a.classList.contains('is-rec')]),"
            f" items: [...{MODAL}.querySelectorAll('.nc-picks > li')].length, note: [...{MODAL}.querySelectorAll('.nc-ask .nc-note')].map(x => x.textContent),"
            f" why: ({MODAL}.querySelector('.nc-recwhy') || {{}}).textContent || null, stop: ({MODAL}.querySelector('.nc-nstop') || {{}}).textContent || null,"
            f" signgo: ({MODAL}.querySelector('.nc-signgo') || {{}}).textContent || null, box: !!{MODAL}.querySelector('.nc-detail .nc-needsbox'),"
            f" open: ({MODAL}.querySelector('.nc-ndetail') || {{}}).open, origin: location.origin,"
            f" raw: {MODAL}.querySelector('.nc-detail').textContent.includes('https://missinglayer'), stars: {MODAL}.querySelector('.nc-detail').textContent.includes('**'),"
            f" order: [...{MODAL}.querySelector('.nc-detail').children].map(c => c.className || c.tagName), sw: {MODAL}.scrollWidth <= {MODAL}.clientWidth + 1 }})")


def _drive_details(tmp_path: Path, rows: list[tuple[str, str, str, dict | None]], width: int = 390, setup=None) -> list[dict]:
    """Open each (title, body, ref, data) ping's detail in turn on the real page over a scratch server; each probe's result."""
    r = Rig(tmp_path)
    for title, body, ref, data in rows:
        PushStore(r.q).add_ping("attention", ref, "levain", body, data=data)
    if setup:
        setup(r)
    shots = os.environ.get("S4MD_SHOTS")
    steps = [{"emulate": {"w": width, "h": 844, "dpr": 2, "mobile": width < 700}},
             {"until": f"{MODAL}.querySelectorAll('.nc-row').length >= {6 + len(rows)} && 1", "ms": 30000}]
    for title, _body, ref, _data in rows:
        row = f"[...{MODAL}.querySelectorAll('.nc-row')].find(r => r.querySelector('.nc-title').textContent === {json.dumps(title)})"
        steps += [{"until": f"{row} && 1", "ms": 20000}, {"eval": f"{row}.querySelector('.nc-main').click(); 1"},
                  {"until": f"({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent === {json.dumps(title)} && 1"},
                  {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": _probe()}]
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            steps.append({"shot": str(Path(shots) / f"needs_{ref}_{width}.png"), "full": False})
        steps.append({"eval": f"(() => {{ const b = {MODAL}.querySelector('.nc-back, [data-nc-back]'); if (b && b.offsetParent) b.click(); return 1; }})()"})
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, *steps])
    got = _json(out)
    assert len(got) == len(rows), out["values"]
    return got


def _drive_detail(tmp_path: Path, title: str, body: str, ref: str, width: int = 390, setup=None) -> dict:
    return _drive_details(tmp_path, [(title, body, ref, None)], width, setup)[0]


@needs_browser
def test_the_texts_list_and_link_draw_as_blocks_and_the_title_has_no_raw_markdown(tmp_path: Path) -> None:
    d = _drive_detail(tmp_path, "S4MD test ping (desk 18).", BODY, "np-7121be43")
    assert d["title"] == "S4MD test ping (desk 18)." and d["repo"] == "levain" and d["stars"] is False and d["raw"] is False, d
    assert d["li"] == ["bold and italic", "a list (this one)", "inline code", "a link with its address shown: Levain repo (github.com)"], d     # the one-line list is a list again
    assert d["after"][-1].startswith("P:Then answer (a)"), d                                                 # the closer split off at its fixed boundary
    assert d["strong"] == ["bold"] and d["code"] == ["inline code"] and d["full"] == [" (github.com)"], d
    assert d["open"] is True and d["sw"] is True, d                                                          # a short Detail starts open


@needs_browser
def test_the_options_draw_as_large_rows_that_open_the_sign_page_with_the_pick(tmp_path: Path) -> None:
    """The rows open THIS page's sign page (its own origin, never the push's host) for the request the queue holds open, with
    the request's version and the pick; the Rec row is the filled one and says so in words."""
    v = {}
    d = _drive_detail(tmp_path, "S4MD test ping (desk 18).", BODY, "np-7121be43", setup=lambda r: v.setdefault("v", _file(r.q, RID, "np-7121be43")))
    base = f"{d['origin']}/sign/{RID}#sign={RID}&v={v['v']}"
    assert [(p[0], p[1], p[2], p[3], p[5]) for p in d["picks"]] == [("a", OPTS[0][1], base + "&choice=a", "✓ Rec", "A"),
                                                                     ("b", OPTS[1][1], base + "&choice=b", None, "A")], d
    assert all(p[4] >= 44 and p[6] == "noopener noreferrer" for p in d["picks"]) and d["items"] == 2, d   # the HIG hit height; a list of items
    assert d["signgo"] is None and d["stop"] == "nothing: S4MD sign-off", d                              # the rows ARE the sign route
    assert d["order"][:3] == ["nc-dmeta", "nc-dtitle", "nc-ask"] and d["order"][-1] == "nc-tools nc-foot", d      # the ask first, the housekeeping verbs last


# --- gap-closers: the sign link (F1), the request's state (F2), the options (F5), the structured fields --------------------
def test_the_sign_link_is_never_taken_from_the_push(tmp_path: Path) -> None:
    """Only a request id leaves the body, and only from flow's one link shape naming this item's request: another host is
    fine (the page never uses it), but a link off the shape, two sign lines, another item's request, or a non-http scheme
    leave nothing to link to."""
    _file(tmp_path, RID, "np-7121be43")
    head = f"levain: {WHAT}\na) yes · b) no · Rec a\n"
    other = "np-99999999-4a9b2da2"
    _file(tmp_path, other, "np-99999999")
    for line, why in [("Sign your answer (Face ID): https://evil.example/phish", "no /sign/ path"),
                      (f"Sign your answer (Face ID): {SIGN}\nSign your answer (Face ID): https://evil.example/sign/{RID}#sign={RID}&v=0000000000000000", "two lines"),
                      (f"Sign your answer (Face ID): https://evil.example/sign/{other}#sign={other}&v=7daebba8dcab02c8", "another item's request"),
                      (f"Sign your answer (Face ID): https://missinglayer.example.ts.net/sign/{RID}", "no fragment"),
                      (f"Sign your answer (Face ID): https://missinglayer.example.ts.net/sign/{RID}#sign=np-00000000-1&v=7daebba8dcab02c8", "ids differ"),
                      ("Sign your answer (Face ID): javascript:alert(1)//sign/np-7121be43-4a9b2da2#sign=np-7121be43-4a9b2da2&v=7daebba8dcab02c8", "scheme"),
                      ("Sign your answer (Face ID): data:text/html,x", "data:")]:
        got = center.needs_wire(head + line, tmp_path, "np-7121be43")
        assert got["sign_state"] == "unverified" and got["sign_id"] is None and got["sign_v"] is None, (why, got)
        assert "evil" not in json.dumps(got), (why, got)
    got = center.needs_wire(head + f"Sign your answer (Face ID): https://evil.example/sign/{RID}#sign={RID}&v=7daebba8dcab02c8&choice=a",
                            tmp_path, "np-7121be43")
    assert got["sign_state"] == "open" and got["sign_id"] == RID and "evil" not in json.dumps(got), got   # the host is dropped, the id kept


def test_a_request_that_is_not_open_is_never_live(tmp_path: Path) -> None:
    """open, closed (signed or denied), expired, unknown (no file, a refused file) and unverified (another namespace, another
    item named inside): only the first leaves a version for the page to link with."""
    q = tmp_path / "q"
    q.mkdir()

    def state(rid: str, item: str = "np-7121be43") -> tuple:
        g = center.needs_wire(BODY.replace(RID, rid), q, item)
        return g["sign_state"], g["sign_v"], g["stopped_known"]

    v = _file(q, RID, "np-7121be43")
    assert state(RID) == ("open", v, True)
    (sign.sign_dir(q) / f"{RID}.denied").write_text("")
    assert state(RID)[:2] == ("closed", None)
    rid2 = "np-7121be43-00000002"
    _file(q, rid2, "np-7121be43", ttl=-60)
    assert state(rid2)[:2] == ("expired", None)
    assert state("np-7121be43-00000003") == ("unknown", None, False)                     # no such file
    (sign.sign_dir(q) / "np-7121be43-00000004.json").write_text("{not json")
    assert state("np-7121be43-00000004") == ("unknown", None, False)
    _file(q, "np-7121be43-00000005", "np-7121be43", namespace="flow-other")
    assert state("np-7121be43-00000005") == ("unverified", None, False)                  # not flow's needs request
    _file(q, "np-7121be43-00000006", "np-55555555")
    assert state("np-7121be43-00000006") == ("unverified", None, False)                  # it names another item inside


def test_an_options_line_that_does_not_read_whole_is_not_drawn(tmp_path: Path) -> None:
    """With no request to check it against, every part must be an option, the keys must run a, b, c... and Rec must name one;
    with the request readable, its options and Rec stand whatever the push's line says."""
    head = "levain: Pick the host for the TAO site now.\n"
    for line in ["a) one · b) two · c)  · Rec a", "a) one · c) fake · b) two", "a) one · b) two · Rec z", "b) one · c) two"]:
        assert center.needs_wire(head + line, tmp_path, "np-1") is None, line
    assert [o["key"] for o in center.needs_wire(head + "a) one · b) two · Rec b", tmp_path, "np-1")["options"]] == ["a", "b"]
    _file(tmp_path, RID, "np-7121be43", options=(("a", "one"), ("b", "two · c) not an option")), rec="a")
    got = center.needs_wire(f"{head}a) one · b) two · c) not an option · Rec a\nSign your answer (Face ID): {SIGN}", tmp_path, "np-7121be43")
    assert [(o["key"], o["text"]) for o in got["options"]] == [("a", "one"), ("b", "two · c) not an option")] and got["rec"] == "a", got


def test_flows_structured_fields_are_read_before_the_body(tmp_path: Path) -> None:
    """A v1 data object wins over any body (a garbage one included); an unknown v, or one field off its type, is ignored whole
    and the body is read; a missing field reads as null and an unknown key is ignored."""
    sid = "np-d4d4d4d4-00000002"
    v = _file(tmp_path, sid, "np-d4d4d4d4", options=(("a", "move it now"), ("b", "wait for Jeanne")), stopped="from the file")
    raw = json.dumps(_data("np-d4d4d4d4", sid, extra_key={"ignored": True}))
    got = center.needs_item("garbage", raw, tmp_path, "np-d4d4d4d4")
    assert got["from"] == "data" and got["repo"] == "tao" and got["what"].count("\n") >= 4 and got["title"] == "Move the TAO mail to its own domain.", got
    assert got["rec"] == "a" and got["rec_reason"] == "the DMARC date is 11-16" and got["stopped"] == "the MX switch", got
    assert got["sign_state"] == "open" and got["sign_v"] == v, got
    assert center.needs_item("garbage", json.dumps({"v": 1, "id": "np-d4d4d4d4", "repo": "tao", "what": "Just a note."}), None, "np-d4d4d4d4")["options"] == []
    bad = [dict(_data("np-d4d4d4d4", sid), v=2), dict(_data("np-d4d4d4d4", sid), v=True), dict(_data("np-d4d4d4d4", sid), v="1"),
           _data("np-d4d4d4d4", sid, options=[{"key": "a", "text": "x"}, {"key": "b", "text": "y"}]),       # objects, not pairs
           _data("np-d4d4d4d4", sid, rec="z"), _data("np-d4d4d4d4", sid, id="np-11111111"),                  # another item
           _data("np-d4d4d4d4", "np-11111111-00000002"), _data("np-d4d4d4d4", sid, what=""),
           _data("np-d4d4d4d4", sid, expires_at="soon"), _data("np-d4d4d4d4", sid, repo="two words"), [1, 2]]
    for b in bad:
        assert center.needs_data(json.dumps(b), "np-d4d4d4d4") is None, b
        assert center.needs_item(BODY, json.dumps(b), None, "np-7121be43")["from"] == "body", b
    assert center.needs_item("garbage", "{not json", None, "x") is None and center.needs_item("garbage", "x" * (DATA_MAX + 1), None, "x") is None
    gone = center.needs_item("garbage", json.dumps(_data("np-d4d4d4d4", "np-d4d4d4d4-00000009", expires_at=time.time() - 5)), tmp_path, "np-d4d4d4d4")
    assert gone["sign_state"] == "expired", gone                                          # no file, and the data says it ran out


def test_the_store_keeps_a_pings_data_beside_it_and_an_old_store_gains_the_column(tmp_path: Path) -> None:
    store = PushStore(tmp_path)
    a = store.add_ping("attention", "np-1", "levain", "a", data={"v": 1, "x": "y"})
    b = store.add_ping("attention", "np-2", "levain", "b", data={"big": "x" * DATA_MAX})       # too large: dropped, the ping kept
    c = store.add_ping("attention", "np-3", "levain", "c")
    got = {r["id"]: r["data"] for r in store.record()}
    assert got == {a: '{"v":1,"x":"y"}', b: None, c: None}, got
    assert data_text([1]) is None and data_text({"n": float("nan")}) is None
    db = tmp_path / DIR_NAME / "webpush.db"
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER pings_frozen")
    conn.execute("DROP TRIGGER pings_kept")
    conn.execute("ALTER TABLE pings DROP COLUMN data")
    conn.commit()
    conn.close()
    assert [r["data"] for r in PushStore(tmp_path).record()] == [None, None, None]


def test_spawn_carries_the_data_to_the_detached_child_and_never_into_the_push(tmp_path: Path, monkeypatch) -> None:
    """The real detached child writes the row with its data; the push payload is built from title and body only."""
    qdir = tmp_path / "q"
    qdir.mkdir()
    data = _data("np-d4d4d4d4", None)
    assert sender.spawn(qdir, kind="attention", ref="np-d4d4d4d4", title="t", body="tao: Move the mail.\nNo listed options.", push=False, data=data)
    deadline = time.monotonic() + 20
    rows: list = []
    while time.monotonic() < deadline and not rows:
        time.sleep(0.2)
        rows = PushStore(qdir).record() if (qdir / DIR_NAME / "webpush.db").exists() else []
    assert rows and json.loads(rows[0]["data"]) == data, rows
    seen = []
    monkeypatch.setattr(sender, "build_payload", lambda *a, **k: seen.append((a, k)) or b"{}")
    store = PushStore(qdir)
    sender.deliver(qdir, store, rows[0]["id"], kind="attention", ref="np-d4d4d4d4", title="t", body="b", url="/")
    assert all("DMARC" not in repr(s) for s in seen), seen


@needs_browser
def test_inert_rows_a_whole_last_bullet_the_structured_fields_and_the_needs_card_route(tmp_path: Path) -> None:
    """One page, four pings: a closed request's rows are inert with the reason; a last bullet with prose that only looks like
    a closer stays whole; a v1 ping draws its real-newline Markdown, its Rec reason and live rows; a question ping keeps the
    generic way to its Needs-you card."""
    closed = "np-c1c1c1c1-00000001"
    sid = "np-d4d4d4d4-00000002"
    neg = ("Pick a host for the TAO site soon. What we have: - Rocket.net managed hosting - a cloud VM, e.g. Google Cloud or Linode "
           "- the agency box (the owner) Brian is cc d on the thread OK, pick a")
    shut = SIGN.replace(RID, closed)
    rows = [("Closed request row here.", f"levain: Closed request row here. Then answer.\na) yes · b) no · Rec a\nSign your answer (Face ID): {shut}", "np-c1c1c1c1", None),
            ("Pick a host for the TAO site soon.", f"levain: {neg}\na) Rocket.net · b) a cloud VM · Rec a", "np-e5e5e5e5", None),
            ("Move the TAO mail to its own domain.", "garbage, not the wire shape", "np-d4d4d4d4", _data("np-d4d4d4d4", sid))]
    v = {}

    def setup(r):
        _file(r.q, closed, "np-c1c1c1c1", asked="Closed request row here. Then answer.")
        (sign.sign_dir(r.q) / f"{closed}.denied").write_text("")
        v["v"] = _file(r.q, sid, "np-d4d4d4d4", options=(("a", "move it now"), ("b", "wait for Jeanne")))
    # the Rig files one open question (n 1): a needs ping about it, in the wire shape, still offers the way to its card
    rows.append(("Ship the fix now please.", "levain: Ship the fix now please.\na) yes · b) no · Rec a", "question:1", None))
    c, n, s, q = _drive_details(tmp_path, rows, setup=setup)
    assert [(p[2], p[5]) for p in c["picks"]] == [(None, "DIV"), (None, "DIV")] and c["note"] == ["This sign request is closed: it was answered or withdrawn."], c
    assert n["li"][-1] == "the agency box (the owner) Brian is cc d on the thread OK, pick a" and n["li"][1] == "a cloud VM, e.g. Google Cloud or Linode", n
    assert n["after"][-1].startswith("UL:"), n                                                               # nothing split off the last bullet
    assert n["note"] == ["No sign page for this item: answer at the desk."] and n["stop"] is None and [p[5] for p in n["picks"]] == ["DIV", "DIV"], n
    base = f"{s['origin']}/sign/{sid}#sign={sid}&v={v['v']}"
    assert s["li"] == ["MX at the new host", "SPF and DKIM set"] and s["why"] == "the DMARC date is 11-16" and s["stop"] == "the MX switch", s
    assert [p[2] for p in s["picks"]] == [base + "&choice=a", base + "&choice=b"] and s["picks"][0][3] == "✓ Rec", s
    assert q["box"] is True and len(q["picks"]) == 2 and q["note"] == ["No sign page for this item: answer at the desk."], q
