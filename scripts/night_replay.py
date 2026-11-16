#!/usr/bin/env python3
"""Replay the night desk's synthetic event corpus through its deterministic layers and print the disagreements with
the written oracle (ND-9a, board levain#40; design section 8.1 row ND-9).

Offline by construction: each event is replayed alone against a fresh store in a scratch directory (never the real
levain home), the reader's and drafter's "model" is a fake runner that returns the corpus's scripted output, and the
push is a fake transport that records what it was handed. No network, no real mail, no model.

Layers: ``mail`` (:func:`levain.desk.events.post_mail`), ``sensor`` (:func:`levain.desk.events.post`), ``reader`` and
``drafter`` (:func:`levain.desk.sealed.parse_reader_output`, :func:`levain.desk.sealed.parse_drafter_output`). The
oracle (``tests/drills/night_oracle.json``) was committed before this file; an entry compares only the keys it names.

NOT here (levain#18): the fresh and resident desk runs with a model in the loop. :func:`replay_all` takes any runner
with the :class:`FakeRunner` call shape, so that run swaps the runner and keeps the corpus, the oracle and the table.

Known edge: a reader or drafter output the parser accepts is observed as ``accepted`` only; the fields it parsed to are
not compared.

An oracle can never expect an event that deliberately raises: an observed error (an event that raised, or an alert that
failed) is always a disagreement row, and a raising event gets that one row only.

Exit status: 0 no disagreement, 1 at least one, 2 a file that is not a corpus or an oracle.

Usage: ``python3 scripts/night_replay.py --corpus tests/drills/night_corpus.json --oracle tests/drills/night_oracle.json``
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from levain.desk import events, sealed          # noqa: E402
from levain.desk.store import CoordError, CoordStore   # noqa: E402

NOW = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)
WRONG_CREDENTIAL = "0" * 64
_RAISED = "_raised"                             # private mark set where replay_all catches an exception
Row = tuple[str, str, str, str, str]            # (event id, class, key, expected, observed)


class FakeRunner:
    """The model's stand-in for the reader and the drafter: the corpus's scripted output for an event, as text.
    ``output_text_len`` scripts a well-formed reader answer whose untrusted_text is that many characters."""

    def __call__(self, ev: dict[str, Any]) -> str:
        if "output" in ev:
            return str(ev["output"])
        return json.dumps({"category": "other", "sender_as_authenticated": "consistent",
                           "instructions_present": "no", "untrusted_text": "a" * int(ev["output_text_len"])})


class _Transport:
    """The fake push: the production sender's call shape, recording each send, reaching nothing."""

    def __init__(self, sent: list) -> None:
        self.sent = sent

    def __call__(self, queue_dir: Any, **kw: Any) -> list[tuple[int, int]]:
        self.sent.append(dict(kw))
        return [(1, 201)]


def _text_class(given: str | None, stored: str | None) -> str:
    if given is None:
        return "none" if stored is None else "UNEXPECTED"
    if stored is None:
        return "dropped"
    if sealed._UNSAFE_CHARS.search(stored) is not None:
        return "UNSAFE"
    if stored == given:
        return "plain"
    return "capped" if len(given) > events.UNTRUSTED_TEXT_MAX else "marked"


def _given_text(ev: dict[str, Any]) -> str | None:
    if "untrusted_text_repeat" in ev:
        unit, n = ev["untrusted_text_repeat"]
        return unit * n
    return ev.get("untrusted_text")


def _scratch_store(root: Path, corpus: dict[str, Any]) -> tuple[CoordStore, dict[str, str]]:
    qdir = root / "consent"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir, clock=lambda: NOW)
    store.coord_set_agent("claude", cwd=str(root))
    creds: dict[str, str] = {}
    for sid, trust in corpus["sensors"].items():
        path = root / f"{sid}.cred"
        events.register(store, sid, trust, path)
        creds[sid] = events.read_credential(path)
    ms = corpus["mail_sensor"]
    path = root / f"{ms['sensor_id']}.cred"
    events.register(store, ms["sensor_id"], "authenticated", path, authserv_id=ms["authserv_id"])
    creds[ms["sensor_id"]] = events.read_credential(path)
    return store, creds


def _posted(store: CoordStore, posts: list, given: str | None, transport: list) -> dict[str, Any]:
    """The intake outcome (the last post) and the alert outcome, apart: every post that asked for an alert is pushed,
    and an alert that errors is an ``alert_error`` on a posted event, never a refusal."""
    p = posts[-1]
    e = p.event
    asked = [q for q in posts if q.alert]
    out = {"outcome": "posted", "trust": e.trust, "alert": bool(asked), "created": p.created,
           "text": _text_class(given, e.untrusted_text), "pinged": False}
    if "auth" in e.fields:
        out["auth"], out["sender_class"] = e.fields["auth"], e.fields["sender_class"]
    errors: list[str] = []
    before = len(transport)
    for q in asked:
        try:
            events.alert_untrusted(store, q, pinger=_Transport(transport))
        except Exception as exc:                # the alert's own failure, reported as such
            errors.append(f"{type(exc).__name__}: {exc}")
    out["pinged"] = len(transport) > before   # a push the fake transport was actually handed
    if errors:
        out["alert_error"] = "; ".join(errors)
    return out


def _replay_door(ev: dict[str, Any], corpus: dict[str, Any], transport: list) -> dict[str, Any]:
    given = _given_text(ev)
    with tempfile.TemporaryDirectory(prefix="night-replay-") as tmp:
        root = Path(tmp)
        root.chmod(0o700)
        store, creds = _scratch_store(root, corpus)
        posts: list = []
        try:                                    # the intake only: what the door takes or refuses
            if ev["layer"] == "mail":
                ms = corpus["mail_sensor"]
                raw = ("\r\n".join(ev["headers"]) + "\r\n\r\n" + ev["body"] + "\r\n").encode("utf-8")
                posts.append(events.post_mail(store, ms["sensor_id"], 1, raw, credential=creds[ms["sensor_id"]],
                                              subject=ev["subject"], category=ev["category"],
                                              known_from=ms["known_from"], untrusted_text=given))
            else:
                # a sensor nobody registered has no credential of ours: the door's own refusal answers it
                cred = WRONG_CREDENTIAL if ev.get("credential") == "wrong" else creds.get(ev["sensor"], WRONG_CREDENTIAL)
                seq, fields = int(ev.get("seq", 1)), ev["fields"]
                for _ in range(int(ev.get("times", 1))):
                    posts.append(events.post(store, ev["sensor"], seq, ev["kind"], fields, credential=cred,
                                             untrusted_text=given))
                if "then" in ev:
                    posts.append(events.post(store, ev["sensor"], seq, ev["kind"], ev["then"]["fields"],
                                             credential=cred, untrusted_text=given))
        except CoordError as exc:               # EventRefused and EventConflict are CoordErrors
            return {"outcome": "refused", "refusal": type(exc).__name__, "why": str(exc)}
        return _posted(store, posts, given, transport)


def replay_event(ev: dict[str, Any], corpus: dict[str, Any], runner: Callable[[dict[str, Any]], str],
                 transport: list) -> dict[str, Any]:
    layer = ev["layer"]
    if layer in ("mail", "sensor"):
        return _replay_door(ev, corpus, transport)
    parse = {"reader": sealed.parse_reader_output, "drafter": sealed.parse_drafter_output}.get(layer)
    if parse is None:
        raise ValueError(f"{ev['id']}: no such layer {layer!r}")
    return {"outcome": "untrusted" if parse(runner(ev)) is sealed.UNTRUSTED else "accepted"}


def replay_all(corpus: dict[str, Any], *, runner: Callable[[dict[str, Any]], str] | None = None,
               transport: list | None = None) -> dict[str, dict[str, Any]]:
    """Every corpus event's observed result, by id. An event that raises is an ``error`` result for that event (a
    disagreement row against any oracle entry), never the end of the drill."""
    run = runner or FakeRunner()
    sent = transport if transport is not None else []
    seen: dict[str, dict[str, Any]] = {}
    for ev in corpus["events"]:
        try:
            seen[ev["id"]] = replay_event(ev, corpus, run, sent)
        except Exception as exc:
            seen[ev["id"]] = {"outcome": "error", "error": f"{type(exc).__name__}: {exc}", _RAISED: True}
    return seen


def disagreements(corpus: dict[str, Any], oracle: dict[str, Any], observed: dict[str, dict[str, Any]]) -> list[Row]:
    """One row per expected key the observation does not match, per event with no oracle entry, and per oracle entry
    naming no event."""
    rows: list[Row] = []
    expect = oracle["expect"]
    for ev in corpus["events"]:
        eid, cls = ev["id"], ev["class"]
        got = observed.get(eid)
        if got is None:
            rows.append((eid, cls, "observation", "one", "missing"))
            continue
        # an error the harness saw is a disagreement whatever the oracle names: the drill never passes over one
        if "alert_error" in got:
            rows.append((eid, cls, "alert_error", "none", str(got["alert_error"])))
        if got.get(_RAISED):                        # one failure, one row: nothing else about this event can be compared
            rows.append((eid, cls, "error", "none", str(got.get("error"))))
            continue
        want = expect.get(eid)
        if want is None:
            rows.append((eid, cls, "oracle", "an entry", "no oracle entry"))
            continue
        for key, value in want.items():
            if key == "alert_error" and "alert_error" in got:   # already a row above; naming it cannot make it agree
                continue
            if key not in got:
                rows.append((eid, cls, key, str(value), "missing"))
            elif key == "why":                  # the refusal's reason: the oracle names a part of the door's message
                if not isinstance(got[key], str) or str(value) not in got[key]:
                    rows.append((eid, cls, key, str(value), str(got[key])))
            elif got[key] != value:
                rows.append((eid, cls, key, str(value), str(got[key])))
    known = {ev["id"] for ev in corpus["events"]}
    rows.extend((eid, "-", "oracle", "an event", "no corpus event") for eid in expect if eid not in known)
    return rows


def table(rows: list[Row]) -> str:
    head = ("event", "class", "key", "expected", "observed")
    cells = [head, *rows]
    widths = [max(len(r[i]) for r in cells) for i in range(5)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen[k] = v
    return seen


_LAYER_KEYS = {"mail": ("headers", "body", "subject", "category"), "sensor": ("sensor", "kind", "fields"),
               "reader": (), "drafter": ()}


def _corpus_problem(doc: dict[str, Any]) -> str | None:
    """The first thing wrong with the shape of a corpus, or None."""
    if not isinstance(doc.get("sensors"), dict):
        return "has no 'sensors' object"
    ms = doc.get("mail_sensor")
    if not isinstance(ms, dict) or not all(isinstance(ms.get(k), str) for k in ("sensor_id", "authserv_id")) \
            or not isinstance(ms.get("known_from"), list):
        return "has no 'mail_sensor' with sensor_id, authserv_id and known_from"
    if not isinstance(doc.get("events"), list):
        return "has no 'events' list"
    if not doc["events"]:
        return "has an empty 'events' list"
    seen: set[str] = set()
    for i, ev in enumerate(doc["events"]):
        if not isinstance(ev, dict) or not all(isinstance(ev.get(k), str) for k in ("id", "layer", "class")):
            return f"event {i} is not an object with a text id, layer and class"
        if ev["id"] in seen:
            return f"event id {ev['id']!r} appears twice"
        seen.add(ev["id"])
        if ev["layer"] not in _LAYER_KEYS:
            return f"event {ev['id']} has the layer {ev['layer']!r}, not one of {', '.join(_LAYER_KEYS)}"
        missing = [k for k in _LAYER_KEYS[ev["layer"]] if k not in ev]
        if ev["layer"] in ("reader", "drafter") and "output" not in ev and "output_text_len" not in ev:
            missing = ["output"]
        if missing:
            return f"event {ev['id']} lacks {', '.join(missing)}"
    return None


def _oracle_problem(doc: dict[str, Any]) -> str | None:
    if not isinstance(doc.get("expect"), dict):
        return "has no 'expect' object"
    if not doc["expect"]:
        return "has an empty 'expect' object"
    bad = [eid for eid, want in doc["expect"].items() if not isinstance(want, dict)]
    if bad:
        return f"entry {bad[0]!r} is not an object"
    empty = [eid for eid, want in doc["expect"].items() if not want]
    return f"entry {empty[0]!r} is empty" if empty else None


def _load(path: str, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit(f"night_replay: {what} {path} is not readable JSON ({exc})") from None
    problem = "is not a JSON object" if not isinstance(doc, dict) else (
        _corpus_problem(doc) if what == "corpus" else _oracle_problem(doc))
    if problem:
        raise SystemExit(f"night_replay: {what} {path} {problem}")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay the night desk event corpus against its written oracle, offline.")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--oracle", required=True)
    args = ap.parse_args(argv)
    try:
        corpus, oracle = _load(args.corpus, "corpus"), _load(args.oracle, "oracle")
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    observed = replay_all(corpus)
    rows = disagreements(corpus, oracle, observed)
    if rows:
        print(table(rows))
    print(f"{len(corpus['events'])} events, {len(rows)} disagreements")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
