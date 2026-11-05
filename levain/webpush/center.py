"""The notification center's read model and writes: Levain's pings and the notices posted at the operator, as ONE list.

The push record (``pings``, ``deliveries``, ``opens``) is append-only, so what the operator does to an item (read it, keep
it unread, delete it) is kept apart, in ``center_marks``, and nothing here ever changes the record. An item is either a
ping Levain sent about something that waits on him (``p<ping id>``) or a notice a service posted (``n<notice id>``); a
notice's own delivery ping is the notice, shown once. Held-summary pings link a view and are not items.

Everything here is a read at display time or an operator mark: deleting or clearing an item hides the ping, and it never
touches the Needs-you item the ping was about (the consent queue and the question store are only READ, for
``needs_open``). What existed when the center first came is history and reads as read (the two floors in ``meta``).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.webpush.store import DATA_MAX, NOTICES_DAYS, NOTICES_KEEP, PAIRED_PREFIX, STAMP, PushStore, now_stamp

__all__ = ["PAGE", "PAGE_MAX", "CenterError", "clear", "delete", "get", "list_items", "mark_all_read", "needs_data", "needs_item",
           "needs_wire", "plain_preview", "plain_text", "push_url", "set_read", "unread_count"]

PAGE = 50                  # the default page
PAGE_MAX = 200
PREVIEW_MAX = 160
TITLE_MAX = 80
# a ping of these kinds is not an item: a notice's own delivery ping is the notice, and a summary only links a view
_NOT_ITEMS = ("summary", "notify", "notify_urgent")
_ITEM = re.compile(r"[pn][1-9][0-9]{0,17}")
_SOURCE_OF_KIND = {"consent": "approval", "push": "approval", "reping": "approval", "question": "needs",
                   "attention": "needs", "ruling": "needs", "dated": "dated", "test": "test"}
_LABEL_OF_SOURCE = {"approval": "Approval needed", "needs": "Needs you", "dated": "Due", "test": "Test ping"}


class CenterError(Exception):
    """An item the center does not hold: ``code`` is ``not_found`` or ``gone`` (deleted by the operator), ``status`` the
    HTTP status it maps to."""

    def __init__(self, code: str, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- the preview -----------------------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*(```|~~~).*$", re.M)
_FENCE_LINE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[^`~\n]*(?:\n|$)", re.M)   # a whole fence line (the page renderer's rule); prose that merely starts with ``` stays
# link text and address are bounded runs (and the text takes no `[`): a body of unclosed brackets stays linear, not quadratic
_IMAGE = re.compile(r"!\[([^\]\[\n]{0,500})\]\([^)\n]{0,2000}\)")
_LINK = re.compile(r"\[([^\]\[\n]{0,500})\]\([^)\n]{0,2000}\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
# the same three marks for plain_text, which keeps its line breaks: horizontal space only, so a mark never eats the blank line before it
_HEADING_T = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_QUOTE_T = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
_BULLET_T = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.M)
_EMPH = re.compile(r"(\*\*|\*|`|(?<!\w)_+|_+(?!\w))")


# paired emphasis only, on one line, and only where the marks could be CommonMark delimiters: the opener is followed by a
# non-space and the closer is preceded by one (`2 * 3 * 4` and `Use ** not ** as glob` are text), and an `_` pair never touches a
# word character (a path, a snake_case name). A lone `_` or `*` is text and stays.
_PAIRED = ((re.compile(r"\*\*(?=[^\s*])([^*\n]*?[^\s*])\*\*"), r"\1"),
           (re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![\w*])"), r"\1"),
           (re.compile(r"(?<!\w)__(?=[^\s_])([^_\n]*?[^\s_])__(?!\w)"), r"\1"),
           (re.compile(r"(?<!\w)_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)"), r"\1"),
           (re.compile(r"`([^`\n]+)`"), r"\1"))


def plain_text(markdown: str) -> str:
    """The body as plain text with its line breaks kept: the same Markdown stripped as :func:`plain_preview` (fence lines,
    link syntax to its text, heading, quote and list marks, paired emphasis and backticks; a lone mark stays), each line's own spacing tidied and
    runs of blank lines folded to one. The push layer sends this, because a seat's question text is line-shaped."""
    text = _FENCE_LINE.sub("", markdown or "")   # a fence line goes whole, its newline with it
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING_T.sub("", text)
    text = _QUOTE_T.sub("", text)
    text = _BULLET_T.sub("", text)
    for pat, sub in _PAIRED:
        text = pat.sub(sub, text)
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def plain_preview(markdown: str, limit: int = PREVIEW_MAX) -> str:
    """The body as plain text on one line, Markdown stripped, at most ``limit`` characters (``…`` when cut). A link keeps
    its text only: the preview is never rendered, so it cannot show a link or hide an address."""
    text = _FENCE.sub("", markdown or "")
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPH.sub("", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- a flow Needs-you ping, read into its parts ------------------------------------------------------------------------
# Two readers, one shape. A ping that carries flow's structured fields (``data``, version 1, stored beside the body; see
# :func:`needs_data`) is read from them; any other needs ping's body is read back from flow's flat push text (`repo: what` /
# an options line / a sign line, each field one line), the fallback. Neither reader takes a link from the ping: only a sign
# request id, which must belong to this ping's own item. The page builds the sign link on its own origin, and the option
# rows are live only for a request this queue holds, in flow's namespace, naming this item, open and unexpired
# (``sign_state``). The request file's options and Rec win over the ping's copy, and its asked and stopped lines fill in
# what a flat body cut short or lacks.
# flow's store name, compared with a request file's namespace: levain never signs in it, so it is not a levain SSHSIG
# namespace (levain/contracts/namespaces.py registers only those; the *NAMESPACE* name is what the AST test reads).
FLOW_NEEDS_STORE = "flow-needs-phill"
_WIRE_HEAD = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,79}): (\S.*)")
_WIRE_SIGN = re.compile(r".*\(Face ID\): (\S+)")
# the one sign link flow sends (its needs_phill_sign.deep_link): <origin>/sign/<id>#sign=<id>&v=<16 hex>, a pick adding
# &choice=<key>. Only its id is used: the version a pick link carries is the request file's own, read here.
_WIRE_SIGN_URL = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/sign/([A-Za-z0-9_-]{8,64})"
                            r"#sign=([A-Za-z0-9_-]{8,64})&v=([0-9a-f]{16})(?:&choice=[a-z0-9]{1,8})?")
_WIRE_OPT_SPLIT = re.compile(r"\s·\s(?=[a-z0-9]{1,8}\)\s|Rec\s[a-z0-9]{1,8}$)")
_WIRE_OPT = re.compile(r"([a-z0-9]{1,8})\)\s+(\S.*)")
_WIRE_REC = re.compile(r"Rec ([a-z0-9]{1,8})")
_OPT_KEY = re.compile(r"[a-z0-9]{1,8}")
_DATA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_DATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATA_KINDS = ("", "decision", "talk", "fyi")
_SENTENCE_END = re.compile(r"[.!?](?=\s)")
HEADLINE_MAX = 200


def _headline(text: str) -> str:
    """The first sentence that ends 20 or more characters in and inside 200 (the card format's rule), else the whole text when
    it is 200 characters or fewer, else its first 160 characters cut back to a word."""
    flat = " ".join((text or "").split())
    for m in _SENTENCE_END.finditer(flat):
        cut = m.start() + 1
        if cut > HEADLINE_MAX:
            break
        if cut >= 20:
            return flat[:cut]
    return flat if len(flat) <= HEADLINE_MAX else flat[:160].rsplit(" ", 1)[0]


def _plain_line(markdown: str, limit: int) -> str:
    """``markdown`` as one plain line by :func:`plain_text`'s careful rules (paired marks only: a lone ``*`` stays), cut at
    ``limit`` with ``…``."""
    text = " ".join(plain_text(markdown).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _signed_fields(message: str) -> dict[str, str]:
    """``item``, ``asked`` and ``stopped`` from a needs-phill sign request's message (``item: ...`` ``asked: ...`` lines);
    the first of each, only what is there."""
    got: dict[str, str] = {}
    for line in (message or "").split("\n"):
        m = re.fullmatch(r"(item|asked|stopped): (.*)", line.rstrip("\r"))
        if m and m.group(1) not in got:
            got[m.group(1)] = m.group(2)
    return got


def _ties(item: str | None, request_id: str) -> bool:
    """Whether sign request ``request_id`` can be item ``item``'s: flow files an item's request as ``<item id>-<nonce>``
    (flow's needs_phill_sign.py: ``request_id = f"{row['id']}-{nonce}"``). With no item to hold it to (a ping with no ref),
    any id is taken."""
    return item is None or request_id == item or request_id.startswith(item + "-")


def _request(queue_dir: Path | None, request_id: str, item: str | None) -> tuple[str, Any]:
    """``(sign_state, request)`` for sign request ``request_id``: ``open`` (signable now), ``closed`` (signed or denied),
    ``expired``, ``unverified`` (it is not this item's flow request) or ``unknown`` (it could not be read). ``request`` is
    the :class:`levain.consent.sign.SignRequest` when it was read as this item's, else None."""
    if queue_dir is None:
        return "unknown", None
    try:
        from levain.consent import sign

        req = sign.read_request(Path(queue_dir), request_id)
        if req.refused is not None:
            return "unknown", None
        if req.namespace != FLOW_NEEDS_STORE:
            return "unverified", None
        named = _signed_fields(req.message).get("item")
        if item is not None and named != item:
            return "unverified", None
        if sign.state_of(Path(queue_dir), request_id) != "open":
            return "closed", req
        if req.expires_at is not None and req.expires_at <= datetime.now(timezone.utc):
            return "expired", req
        return "open", req
    except Exception:  # noqa: BLE001 - no request file (or no reading it): the rows stay inert, the sign page decides
        return "unknown", None


def _wire_options(line: str, strict: bool) -> tuple[list[dict[str, str]], str | None] | None:
    """An options line (``a) .. · b) .. · Rec a``) as ``(options, rec)``, or None when any part of it is not an option. With
    ``strict`` (nothing better to check it against) the keys must run a, b, c... and Rec name one of them, so an
    ``x) `` inside an option's own text never makes a phantom option."""
    parts = _WIRE_OPT_SPLIT.split(line)
    rec = None
    r = _WIRE_REC.fullmatch(parts[-1]) if parts else None
    if r:
        rec, parts = r.group(1), parts[:-1]
    rows = [_WIRE_OPT.fullmatch(p) for p in parts]
    if not rows or not all(rows):
        return None
    opts = [{"key": m.group(1), "text": m.group(2)} for m in rows if m]
    keys = [o["key"] for o in opts]
    if strict and (keys != [chr(ord("a") + i) for i in range(len(keys))] or (rec is not None and rec not in keys)):
        return None
    return opts, rec


def needs_data(raw: object, item: str | None = None) -> dict[str, Any] | None:
    """flow's structured fields for a needs ping, version 1, or None when ``raw`` (the stored JSON text) is not that: an
    unknown ``v`` or any field off its type, and then the body is read instead (never half of an object). The shape:
    ``{"v": 1, "kind": "decision"|"talk"|"fyi"|"", "id": "<item id>", "repo": str, "what": str (Markdown, real newlines),
    "options": [[key, text], ...], "rec": key|null, "rec_reason": str|null, "stopped": str|null, "sign_id": "<sign
    request id>"|null, "expires_at": epoch seconds|null, "dated": "YYYY-MM-DD"|null}``. A missing field reads as null and
    a key not named here is ignored. ``id`` must be ``item`` (the ping's ref) when the ping has one, and ``sign_id`` that
    item's request."""
    if not isinstance(raw, str) or len(raw.encode("utf-8", "surrogatepass")) > DATA_MAX:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or type(d.get("v")) is not int or d["v"] != 1:
        return None
    from levain.sign_request_id import request_id_ok

    def opt_str(k: str, most: int) -> bool:
        return d.get(k) is None or (isinstance(d[k], str) and len(d[k]) <= most)

    kind, iid, repo, what = d.get("kind"), d.get("id"), d.get("repo"), d.get("what")
    if not (kind is None or kind in _DATA_KINDS) or not (isinstance(iid, str) and _DATA_ID.fullmatch(iid)):
        return None
    if item is not None and iid != item:
        return None
    if not (isinstance(repo, str) and _WIRE_HEAD.fullmatch(repo + ": x")) or not (isinstance(what, str) and what.strip()):
        return None
    if not all(opt_str(k, 4000) for k in ("rec_reason", "stopped")):
        return None
    raw_opts = d.get("options")
    raw_opts = [] if raw_opts is None else raw_opts
    if not isinstance(raw_opts, list) or len(raw_opts) > 8:
        return None
    opts: list[dict[str, str]] = []
    for o in raw_opts:
        if not (isinstance(o, list) and len(o) == 2 and isinstance(o[0], str) and _OPT_KEY.fullmatch(o[0])
                and isinstance(o[1], str) and o[1].strip()):
            return None
        opts.append({"key": o[0], "text": o[1]})
    if len({o["key"] for o in opts}) != len(opts):
        return None
    rec, sid, exp, dated = d.get("rec"), d.get("sign_id"), d.get("expires_at"), d.get("dated")
    if rec is not None and not (isinstance(rec, str) and rec in {o["key"] for o in opts}):
        return None
    if sid is not None and not (request_id_ok(sid) and _ties(iid, sid)):
        return None
    if exp is not None:
        try:
            if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
                return None
        except OverflowError:   # an integer too large for a float
            return None
    if dated is not None and not (isinstance(dated, str) and _DATED.fullmatch(dated)):
        return None
    return {"kind": kind or "", "id": iid, "repo": repo, "what": what, "options": opts, "rec": rec,
            "rec_reason": d.get("rec_reason"), "stopped": d.get("stopped"), "sign_id": sid, "expires_at": exp,
            "dated": dated}


def _finish(out: dict[str, Any], queue_dir: Path | None, item: str | None, expires_at: float | None = None) -> dict[str, Any]:
    """``out`` with its sign state read from the request it names, the request's own words over the push's, its title."""
    req = None
    if out["sign_id"] is not None and out["sign_state"] is None and item is None:
        out["sign_state"], out["sign_id"] = "unverified", None    # a ping with no ref names no item to hold the request to
    if out["sign_id"] is not None and out["sign_state"] is None:
        out["sign_state"], req = _request(queue_dir, out["sign_id"], item)
        if out["sign_state"] == "unknown" and expires_at is not None and expires_at <= time.time():
            out["sign_state"] = "expired"
    if req is not None:
        fields = _signed_fields(req.message)
        if fields.get("stopped") and not out["stopped"]:
            out["stopped"] = fields["stopped"]
        if fields.get("asked") and out["from"] == "body":
            out["what"] = fields["asked"]       # the push may have cut the text; the signed line is whole
        if req.options:                         # the options the sign page signs, and its Rec
            if req.recommended != out["rec"]:
                out["rec_reason"] = None
            out["options"] = [{"key": k, "text": t} for k, t in req.options]
            out["rec"] = req.recommended
        elif out["options"] and out["sign_state"] == "open":
            out["sign_state"], out["sign_id"] = "unverified", None   # the sign page offers no keys: no row may link a pick
        out["stopped_known"] = True
        if out["sign_state"] == "open":
            out["sign_v"] = req.sha256[:16]     # the version the sign page lists now: a pick holds only on it
    if out["sign_state"] != "open":
        out["sign_v"] = None
    first = next((ln for ln in out["what"].split("\n") if ln.strip()), "")
    out["headline"] = _headline(first)
    out["title"] = _plain_line(out["headline"], HEADLINE_MAX)    # the detail's title: plain, whole
    return out


def needs_wire(body: str, queue_dir: Path | None = None, item: str | None = None) -> dict[str, Any] | None:
    """The parts of a flow Needs-you ping's flat push text, or None when the body is not in that shape (the detail view then
    draws it as plain Markdown). ``item`` is the ping's ref (flow's item id), which the sign request must belong to. The
    shape is :func:`needs_item`'s."""
    lines = [ln.strip() for ln in (body or "").split("\n") if ln.strip()]
    head = _WIRE_HEAD.fullmatch(lines[0]) if lines else None
    if head is None:
        return None
    opt_line: str | None = None
    no_opts = False
    signs: list[str] = []
    for ln in lines[1:]:
        m = _WIRE_SIGN.fullmatch(ln)
        if m:
            signs.append(m.group(1))
        elif ln == "No listed options.":
            no_opts = True
        elif opt_line is None and not no_opts and _WIRE_OPT.match(ln):
            opt_line = ln
    if opt_line is None and not no_opts:
        return None
    out: dict[str, Any] = {"from": "body", "repo": head.group(1), "what": head.group(2), "stopped": None,
                           "stopped_known": False, "options": [], "rec": None, "rec_reason": None, "sign_id": None,
                           "sign_v": None, "sign_state": None}
    if signs:
        link = _WIRE_SIGN_URL.fullmatch(signs[0]) if len(signs) == 1 else None
        if link is None or link.group(1) != link.group(2) or not _ties(item, link.group(1)):
            out["sign_state"] = "unverified"       # two sign lines, a link off flow's shape, or another item's request
        else:
            out["sign_id"] = link.group(1)
    _finish(out, queue_dir, item)
    if opt_line is not None and not out["options"]:
        got = _wire_options(opt_line, strict=True)
        if got is None:
            return None
        out["options"], out["rec"] = got
        if out["sign_state"] == "open":      # the readable request offered none: the push's keys are not the sign page's
            out["sign_state"], out["sign_id"], out["sign_v"] = "unverified", None, None
    return out


def needs_item(body: str, data: object = None, queue_dir: Path | None = None, item: str | None = None) -> dict[str, Any] | None:
    """A needs ping's parts, from its structured ``data`` when that is a version 1 object (:func:`needs_data`), else from
    its body (:func:`needs_wire`); None when neither reads. ``{from ("data"|"body"), repo, what, headline, title, stopped,
    stopped_known (the stop text's source was read, so None means there is none), options [{key, text}], rec, rec_reason,
    sign_id, sign_v (the version a pick link carries; None unless open), sign_state}``. ``sign_state`` is None when the ping
    names no sign request, else ``open``, ``closed``, ``expired``, ``unverified`` or ``unknown``; only ``open`` makes the
    option rows live."""
    d = needs_data(data, item)
    if d is None:
        return needs_wire(body, queue_dir, item)
    out: dict[str, Any] = {"from": "data", "repo": d["repo"], "what": d["what"], "stopped": d["stopped"],
                           "stopped_known": True, "options": d["options"], "rec": d["rec"], "rec_reason": d["rec_reason"],
                           "sign_id": d["sign_id"], "sign_v": None, "sign_state": None, "kind": d["kind"], "dated": d["dated"]}
    return _finish(out, queue_dir, item, d["expires_at"])


def _first_line(body: str, source: str) -> str:
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1].rstrip() + "…"
    return _LABEL_OF_SOURCE.get(source, "levain")


def _iso(stamp: str | None) -> str | None:
    """The store's whole-second UTC stamp as ISO 8601 with an explicit offset."""
    if not stamp:
        return None
    return stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp


def _queue_dir(store: PushStore) -> Path:
    return store.directory.parent


# --- the candidates --------------------------------------------------------------------------------------------------
def _floor(c: sqlite3.Connection, key: str) -> int:
    row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _cutoff(now: float | None = None) -> str:
    return time.strftime(STAMP, time.gmtime((time.time() if now is None else now) - NOTICES_DAYS * 86400))


_NOT_ITEMS_SQL = ",".join(f"'{k}'" for k in _NOT_ITEMS)


def _candidates(c: sqlite3.Connection, *, limit: int | None = None,
                after: tuple[str, int, str] | None = None) -> list[tuple[str, int, str, bool, str | None]]:
    """The visible items, lightweight and newest first: ``(kind, number, at, read, read_at)`` with kind ``n`` or ``p``.
    Visible: not deleted by the operator; a ping within the retention window, a notice the record still keeps. ``after`` is
    a keyset ``(at, number, kind)``: only the items strictly older than it, in the list's own order. ``limit`` bounds the
    result and is applied in SQL per source, so no item is ever hidden by a cap: with no ``limit`` it is every visible item."""
    fp, fn = _floor(c, "center_floor_ping"), _floor(c, "center_floor_notice")
    rows: list[tuple[str, int, str, bool, str | None]] = []
    for kind, table, floor, extra, args in (
            ("n", "notices", fn, "", ()),
            ("p", "pings", fp, f"AND t.kind NOT IN ({_NOT_ITEMS_SQL}) AND t.at >= ? ", (_cutoff(),))):
        where, params = "", list(args)
        if after is not None:
            at, num, k = after
            # (at, id, kind) < (after): this source's kind is fixed, so the third key is a constant truth
            where = "AND (t.at < ? OR (t.at = ? AND (t.id < ? OR (t.id = ? AND ?)))) "
            params += [at, at, num, num, 1 if kind < k else 0]
        sql = (f"SELECT t.id, t.at, m.is_read, m.read_at FROM {table} t LEFT JOIN center_marks m "
               f"ON m.item = '{kind}' || t.id WHERE m.deleted_at IS NULL {extra}{where}ORDER BY t.at DESC, t.id DESC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        for r in c.execute(sql, params):
            read = bool(r[2]) if r[2] is not None else r[0] <= floor
            rows.append((kind, r[0], r[1], read, r[3]))
    rows.sort(key=lambda t: (t[2], t[1], t[0]), reverse=True)
    return rows if limit is None else rows[:limit]


def _cursor_key(c: sqlite3.Connection, before: str) -> tuple[str, int, str]:
    """``(at, number, kind)`` of the item a ``before`` cursor names, read from the table whether or not the item is still
    visible (deleted by the operator, cleared, aged out): a cursor stays good for the whole paging session. A row the record
    no longer holds at all takes the ``at`` of the nearest newer row of its source (a repeat is possible, a skipped item
    is not); with no newer row either, the id never existed here: ``not_found``."""
    kind, num = before[0], int(before[1:])
    table = "notices" if kind == "n" else "pings"
    row = c.execute(f"SELECT at FROM {table} WHERE id = ?", (num,)).fetchone()
    if row is None:
        row = c.execute(f"SELECT at FROM {table} WHERE id > ? ORDER BY id LIMIT 1", (num,)).fetchone()
    if row is None:
        raise CenterError("not_found", "that item is not in the list", 404)
    return (row[0], num, kind)


def _counts(c: sqlite3.Connection) -> tuple[int, int]:
    """``(unread, total)`` straight from the tables, so a count never stops at a page or at the list's bound."""
    fp, fn = _floor(c, "center_floor_ping"), _floor(c, "center_floor_notice")
    cutoff = _cutoff()
    n_u, n_t = c.execute(
        "SELECT COALESCE(SUM(COALESCE(m.is_read, n.id <= ?) = 0), 0), COUNT(*) FROM notices n "
        "LEFT JOIN center_marks m ON m.item = 'n' || n.id WHERE m.deleted_at IS NULL", (fn,)).fetchone()
    p_u, p_t = c.execute(
        "SELECT COALESCE(SUM(COALESCE(m.is_read, p.id <= ?) = 0), 0), COUNT(*) FROM pings p "
        "LEFT JOIN center_marks m ON m.item = 'p' || p.id "
        f"WHERE p.kind NOT IN ({_NOT_ITEMS_SQL}) AND p.at >= ? AND m.deleted_at IS NULL", (fp, cutoff)).fetchone()
    return int(n_u) + int(p_u), int(n_t) + int(p_t)


# --- one item --------------------------------------------------------------------------------------------------------
class _Reader:
    """The things an item's fields are read from, opened once for a page: the consent queue and the question store (for
    ``needs_open``) and each paired device's label."""

    def __init__(self, c: sqlite3.Connection, queue_dir: Path) -> None:
        self.c = c
        self.queue_dir = queue_dir
        self._queue: Any = None
        self._questions: Any = None
        self._labels: dict[tuple[Any, str], str] = {}

    def needs_open(self, ref: Any) -> bool:
        """Whether the Needs-you item a ping was about still waits on him, read now; an unreadable store is False (the
        Needs-you card is the authority, and a wrong True would point at nothing)."""
        if not isinstance(ref, str) or ":" not in ref:
            return False
        from levain.webpush import record

        kind = ref.partition(":")[0]
        try:
            if kind == "request" and self._queue is None:
                from levain.consent.queue import ConsentQueue, authority_source

                self._queue = ConsentQueue(self.queue_dir, home=Path.home(),
                                           authority=authority_source(self.queue_dir, anchor=None))
            if kind == "question" and self._questions is None:
                from levain.consent.questions import QuestionStore

                self._questions = QuestionStore(self.queue_dir)
        except Exception:  # noqa: BLE001 - outcome_of says "unread" for the row
            pass
        try:
            got = record.outcome_of(self.queue_dir, ref, _queue=self._queue, _questions=self._questions)
            return got.get("state") in ("pending", "open")
        except Exception:  # noqa: BLE001 - one ref that cannot be read is False for that item, never a failed page
            return False

    def consent_sheet(self, ref: Any) -> dict[str, str] | None:
        """For a ping about a filed consent request (``request:<id>``): ``{"id", "state"}``, the request's standing read
        now, so the focus view can link its sign sheet (``/sign/consent/<id>``, drawn on the page's own origin from the id
        alone: no link is taken from the ping). ``state``: ``open`` (pending with its hold running, or undecided: a
        passkey may answer or rule it, ConsentSheet.row's own condition), ``expired`` (its hold ended), ``closed``
        (decided, withdrawn or otherwise final), ``gone`` (no longer in the queue), ``unread`` (the store could not be
        read). None for any other ref: no request, no sheet."""
        if not isinstance(ref, str) or not ref.startswith("request:") or len(ref) == len("request:"):
            return None
        rid = ref[len("request:"):]
        try:
            if self._queue is None:
                from levain.consent.queue import ConsentQueue, authority_source

                self._queue = ConsentQueue(self.queue_dir, home=Path.home(),
                                           authority=authority_source(self.queue_dir, anchor=None))
            got = self._queue.get(rid)
        except Exception:  # noqa: BLE001 - an unreadable store is said as such, never drawn as an open sheet
            return {"id": rid, "state": "unread"}
        if got is None:
            return {"id": rid, "state": "gone"}
        request, state = got
        if state == "pending":
            from levain.cockpit.consent import utc_stamp

            return {"id": rid, "state": "expired" if request.expires_at <= utc_stamp(datetime.now(timezone.utc))
                    else "open"}
        return {"id": rid, "state": "open" if state == "undecided" else "expired" if state == "expired" else "closed"}

    def label(self, journal: Any, device_id: str) -> str:
        key = (journal, device_id)
        if key not in self._labels:
            self._labels[key] = _device_label(journal, device_id)
        return self._labels[key]


def _device_label(journal: Any, device_id: str) -> str:
    """The name the operator gave a paired device (the cockpit pairing journal's ``label``), else its id."""
    fallback = device_id[len(PAIRED_PREFIX):] if device_id.startswith(PAIRED_PREFIX) else device_id
    if not journal or not device_id.startswith(PAIRED_PREFIX):
        return fallback
    try:
        path = (Path(journal) / "cockpit.db").resolve(strict=True)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.25)
        try:
            row = conn.execute("SELECT label FROM paired_devices WHERE id = ?", (fallback,)).fetchone()
        finally:
            conn.close()
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return fallback
    return row[0] if row and isinstance(row[0], str) and row[0] else fallback


def _delivery(rd: _Reader, ping_id: int | None) -> dict[str, Any] | None:
    """``{pushed, failed, held, opened_at}`` from the push record, or None when the ping has no delivery to speak of (a
    ``low`` notice is recorded only; a ping to no phone has no rows)."""
    if ping_id is None:
        return None
    c = rd.c
    pushed: list[str] = []
    failed: list[str] = []
    rows = c.execute(
        "SELECT d.status, s.device_id, s.journal FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id "
        "WHERE d.ping_id = ? ORDER BY d.id", (ping_id,)).fetchall()
    for status, device, journal in rows:
        name = rd.label(journal, device or "")
        bucket = pushed if 200 <= int(status) < 300 else failed
        if name not in bucket:
            bucket.append(name)
    failed = [n for n in failed if n not in pushed]
    held_row = c.execute("SELECT reason FROM held WHERE ping_id = ? AND released_at IS NULL", (ping_id,)).fetchone()
    opened = c.execute("SELECT MIN(at) FROM opens WHERE ping_id = ?", (ping_id,)).fetchone()[0]
    held = held_row[0] if held_row else None
    if not (pushed or failed or held or opened or c.execute(
            "SELECT 1 FROM held WHERE ping_id = ?", (ping_id,)).fetchone()):
        return None
    return {"pushed": pushed, "failed": failed, "held": held, "opened_at": _iso(opened)}


def _notice_item(rd: _Reader, num: int, read: bool, read_at: str | None) -> dict[str, Any] | None:
    r = rd.c.execute("SELECT * FROM notices WHERE id = ?", (num,)).fetchone()
    if r is None:
        return None
    if r["origin"] != "token":   # a deleted origin on disk is refused (the item is left out), never read as a token's
        return None
    return {"id": f"n{num}", "origin": "token", "source": r["source"], "title": r["title"], "body": r["body"],
            "preview": plain_preview(r["body"]), "at": _iso(r["at"]), "priority": r["priority"], "read": read,
            "read_at": _iso(read_at) if read else None, "needs_ref": None, "needs_open": False,
            "token_name": r["token"], "image_url": None, "sheet": None,
            "delivery": _delivery(rd, r["ping_id"])}


def _ping_item(rd: _Reader, num: int, read: bool, read_at: str | None) -> dict[str, Any] | None:
    r = rd.c.execute("SELECT * FROM pings WHERE id = ?", (num,)).fetchone()
    if r is None:
        return None
    source = _SOURCE_OF_KIND.get(r["kind"], r["kind"])
    ref = r["ref"] if isinstance(r["ref"], str) else None
    # the Needs-you card's id: a consent request is ``consent:<id>`` on the page, a question ``question:<n>`` in both
    needs_ref = ("consent:" + ref[len("request:"):]) if ref and ref.startswith("request:") else (
        ref if ref and ref.startswith("question:") else None)
    body = r["body"] or ""
    data = r["data"] if "data" in r.keys() else None
    needs = None
    if source == "needs":
        try:
            needs = needs_item(body, data, rd.queue_dir, ref)
        except Exception:  # noqa: BLE001 - one stored row that cannot be read draws from its body, never takes the list down
            try:
                needs = needs_wire(body, rd.queue_dir, ref)
            except Exception:  # noqa: BLE001
                needs = None
    title = _plain_line(needs["headline"], TITLE_MAX) if needs else _first_line(body, source)
    return {"id": f"p{num}", "origin": "levain", "source": source, "title": title, "body": body, "needs": needs,
            "preview": plain_preview(body), "at": _iso(r["at"]), "priority": "normal", "read": read,
            "read_at": _iso(read_at) if read else None, "needs_ref": needs_ref,
            "needs_open": bool(needs_ref) and rd.needs_open(ref), "token_name": None, "image_url": None,
            "sheet": rd.consent_sheet(ref) if source == "approval" else None,
            "delivery": _delivery(rd, num)}


def _build(rd: _Reader, cands: list[tuple[str, int, str, bool, str | None]]) -> list[dict[str, Any]]:
    out = []
    for kind, num, _at, read, read_at in cands:
        item = (_notice_item if kind == "n" else _ping_item)(rd, num, read, read_at)
        if item is not None:
            out.append(item)
    return out


# --- reads -----------------------------------------------------------------------------------------------------------
def list_items(store: PushStore, *, limit: int = PAGE, before: str | None = None) -> dict[str, Any]:
    """``{items (newest first), unread, total, next_before}``. ``before`` is an item id: the page holds the items older
    than it, and ``next_before`` is the id to pass for the next page (None on the last). One read transaction, so the
    counts and the rows agree."""
    n = max(1, min(int(limit), PAGE_MAX))
    if before is not None and not (isinstance(before, str) and _ITEM.fullmatch(before)):
        raise CenterError("bad_request", "before is an item id", 400)
    with store.read() as c:
        after = _cursor_key(c, before) if before is not None else None
        cands = _candidates(c, limit=n + 1, after=after)
        page = cands[:n]
        more = len(cands) > n
        unread, total = _counts(c)
        items = _build(_Reader(c, _queue_dir(store)), page)
    return {"items": items, "unread": unread, "total": total,
            "next_before": (f"{page[-1][0]}{page[-1][1]}" if more and page else None)}


def unread_count(store: PushStore) -> int:
    with store.read() as c:
        return _counts(c)[0]


def get(store: PushStore, item: str) -> dict[str, Any]:
    """One item, wherever it sits in the list. ``gone``: the operator deleted it (a deep link to it says so);
    ``not_found``: there never was one, or the record no longer keeps it."""
    if not isinstance(item, str) or not _ITEM.fullmatch(item):
        raise CenterError("bad_request", "an item id is p or n and a number", 400)
    with store.read() as c:
        built = _one(c, item, _queue_dir(store))
        if built is None:
            deleted = c.execute("SELECT 1 FROM center_marks WHERE item = ? AND deleted_at IS NOT NULL", (item,)).fetchone()
            if deleted:
                raise CenterError("gone", "That notification was deleted.")
            raise CenterError("not_found", "no such notification")
        return {"item": built, "unread": _counts(c)[0]}


def _one(c: sqlite3.Connection, item: str, queue_dir: Path) -> dict[str, Any] | None:
    mark = c.execute("SELECT is_read, read_at, deleted_at FROM center_marks WHERE item = ?", (item,)).fetchone()
    if mark is not None and mark[2] is not None:
        return None
    num = int(item[1:])
    if item[0] == "n":
        if c.execute("SELECT 1 FROM notices WHERE id = ?", (num,)).fetchone() is None:
            return None
        read = bool(mark[0]) if mark is not None else num <= _floor(c, "center_floor_notice")
    else:
        if c.execute(f"SELECT 1 FROM pings WHERE id = ? AND kind NOT IN ({_NOT_ITEMS_SQL}) AND at >= ?",
                     (num, _cutoff())).fetchone() is None:
            return None
        read = bool(mark[0]) if mark is not None else num <= _floor(c, "center_floor_ping")
    rd = _Reader(c, queue_dir)
    return (_notice_item if item[0] == "n" else _ping_item)(rd, num, read, mark[1] if mark is not None else None)


# --- writes ----------------------------------------------------------------------------------------------------------
def _prune_marks(c: sqlite3.Connection) -> None:
    """Drop the marks of items the record no longer keeps, so the marks table is bounded by the record."""
    c.execute("DELETE FROM center_marks WHERE item LIKE 'n%' AND CAST(SUBSTR(item, 2) AS INTEGER) "
              "NOT IN (SELECT id FROM notices)")
    c.execute("DELETE FROM center_marks WHERE item LIKE 'p%' AND CAST(SUBSTR(item, 2) AS INTEGER) "
              "NOT IN (SELECT id FROM pings WHERE at >= ?)", (_cutoff(),))


def _visible(c: sqlite3.Connection, item: str) -> bool:
    mark = c.execute("SELECT deleted_at FROM center_marks WHERE item = ?", (item,)).fetchone()
    if mark is not None and mark[0] is not None:
        return False
    num = int(item[1:])
    if item[0] == "n":
        return c.execute("SELECT 1 FROM notices WHERE id = ?", (num,)).fetchone() is not None
    return c.execute(f"SELECT 1 FROM pings WHERE id = ? AND kind NOT IN ({_NOT_ITEMS_SQL}) AND at >= ?",
                     (num, _cutoff())).fetchone() is not None


def _check(item: object) -> str:
    if not isinstance(item, str) or not _ITEM.fullmatch(item):
        raise CenterError("bad_request", "an item id is p or n and a number", 400)
    return item


def set_read(store: PushStore, item: str, read: bool) -> int:
    """Mark one item read or unread (the toggle, and mark-read on open). Returns the unread count after. Raises
    :class:`CenterError` for an item that is gone or never was; nothing is written then."""
    _check(item)
    if not isinstance(read, bool):
        raise CenterError("bad_request", "read is true or false", 400)
    with store.write() as c:
        if not _visible(c, item):
            raise CenterError("not_found", "no such notification")
        stamp = now_stamp()
        c.execute("INSERT INTO center_marks (item, is_read, read_at) VALUES (?,?,?) ON CONFLICT(item) DO UPDATE SET "
                  "is_read = excluded.is_read, read_at = excluded.read_at",
                  (item, 1 if read else 0, stamp if read else None))
        _prune_marks(c)
        return _counts(c)[0]


def _visible_sql(c: sqlite3.Connection) -> list[tuple[str, str, tuple[Any, ...], Any]]:
    """The two sources as ``(kind, FROM/WHERE for the visible rows, params, floor)``: the SAME predicate :func:`_candidates`
    and :func:`_counts` use, so a bulk write reaches exactly the rows the list and the badge count."""
    fp, fn = _floor(c, "center_floor_ping"), _floor(c, "center_floor_notice")
    return [("n", "FROM notices t LEFT JOIN center_marks m ON m.item = 'n' || t.id WHERE m.deleted_at IS NULL", (), fn),
            ("p", "FROM pings t LEFT JOIN center_marks m ON m.item = 'p' || t.id WHERE m.deleted_at IS NULL "
                  f"AND t.kind NOT IN ({_NOT_ITEMS_SQL}) AND t.at >= ?", (_cutoff(),), fp)]


def mark_all_read(store: PushStore) -> int:
    """Mark every item in the list read. Returns the unread count after (0, unless an item arrived since). Set-based over
    every visible item, never over a page or a cap."""
    with store.write() as c:
        stamp = now_stamp()
        for kind, frm, params, floor in _visible_sql(c):
            c.execute(f"INSERT INTO center_marks (item, is_read, read_at) SELECT '{kind}' || t.id, 1, ? {frm} "
                      "AND COALESCE(m.is_read, t.id <= ?) = 0 "
                      "ON CONFLICT(item) DO UPDATE SET is_read = 1, read_at = excluded.read_at",
                      (stamp, *params, floor))
        _prune_marks(c)
        return _counts(c)[0]


def delete(store: PushStore, item: str) -> int:
    """Remove one item from the list. The ping stays in the record, and the Needs-you item it was about is untouched.
    Returns the unread count after."""
    _check(item)
    with store.write() as c:
        if not _visible(c, item):
            raise CenterError("not_found", "no such notification")
        c.execute("INSERT INTO center_marks (item, is_read, read_at, deleted_at) VALUES (?,1,?,?) ON CONFLICT(item) DO "
                  "UPDATE SET deleted_at = excluded.deleted_at", (item, now_stamp(), now_stamp()))
        _prune_marks(c)
        return _counts(c)[0]


def clear(store: PushStore) -> int:
    """Remove every item now in the list; Needs-you items are untouched. Returns the unread count after."""
    with store.write() as c:
        stamp = now_stamp()
        for kind, frm, params, _floor_ in _visible_sql(c):
            c.execute(f"INSERT INTO center_marks (item, is_read, read_at, deleted_at) SELECT '{kind}' || t.id, 1, ?, ? "
                      f"{frm} ON CONFLICT(item) DO UPDATE SET deleted_at = excluded.deleted_at",
                      (stamp, stamp, *params))
        _prune_marks(c)
        return _counts(c)[0]


# --- the push --------------------------------------------------------------------------------------------------------
def push_url(store: PushStore, ping_id: int, kind: str, url: str) -> str:
    """Where a push opens: the center on the ping's own item (``/?view=notifications&n=<id>``). A held-summary ping is no
    item and keeps the link it was given. Any trouble reading the record keeps the given link: a push is never lost to it."""
    if kind == "summary":
        return url
    try:
        with store.read() as c:
            row = c.execute("SELECT id FROM notices WHERE ping_id = ?", (int(ping_id),)).fetchone()
    except Exception:  # noqa: BLE001 - the push still goes, to the link it already had
        return url
    if row is None and kind in _NOT_ITEMS:
        return url      # a notify ping whose notice the record has pruned is no item: a tap on p<id> would be a 404
    return f"/?view=notifications&n={'n' + str(row[0]) if row else 'p' + str(int(ping_id))}"
