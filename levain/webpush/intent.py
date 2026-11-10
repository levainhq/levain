"""levain.webpush.intent — the ONE presence record: where an ordinary ping goes, and what reaches him there.

Phill 11-02: *"1b but I think the device following should be default and happen at 10 mins (but you can switch off and set
a custom time)"*, and two things he knows are important: the cockpit and the terminal staying in sync and making sense with
one another, and the operator feeling in control of how residue and approvals reach him.

``<queue_dir>/webpush/intent.json`` (0600, written only here) holds two layers, each carrying the same three settings:

- ``devices``: ``on`` | ``off`` | ``follow`` (follow: devices on after ``follow_after`` seconds without a typed desk message);
- ``follow_after``: seconds, 60 to 86400 (default 600), used only under ``follow``;
- ``lets_through``: ``everything`` ⊃ ``needs`` (approvals + attention) ⊃ ``approvals`` ⊃ ``nothing``.

``base`` is the operator's standing default (``back`` returns to it; only the operator writes it). ``overlay`` is a preset
(``away``, ``quiet``, ``sleep``, ``loud``) or a setting made "for a while", and ends by its own ``until``: ``{"kind":
"timer","at"}`` or ``{"kind": "input"}``. An overlay's end is READ, never written: the record is never rewritten by sensing
(laptop idle, remote input, a desk message, desk liveness), which feeds only ``follow`` (a desk message, remote input) and ``until: input`` (a desk message); laptop idle is shown and decides nothing. An ended overlay
stays on file, inert, until the next write replaces it.

One lock (the presence lock, ``<queue>/webpush/.presence.lock``), one event stream (every write leaves one row in
``presence.log.jsonl``), one decider (:func:`should_push` in :mod:`levain.webpush.presence` calls :func:`_core` here) and one
sentence (:func:`describe_line`), printed verbatim by every surface. Consent asks the same decider
(:func:`approvals_route`), so a push and a hold diverge in one named case only: the ping about a request already
filed (``filed``, :func:`decide`) pushes whatever the desk or devices setting is doing, where the hold's question never
passes ``filed``. It is pinned in ``tests/test_webpush_push_hold_cases.py``. A prompt left to the agent's own dialog
still sends a NOTICE to the devices whenever ``lets_through`` admits approvals (:func:`approval_notice`): a notice, never
a hold. A write against a record that cannot be read is refused, never replaced.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path

from levain.webpush import desk, presence
from levain.webpush.presence import (ACTIVE_WINDOW_SECS, DESK_WORDS, MAX_SECS, MIN_SECS, SEAT_WORDS,
                                     TIMED, WORDS, Decision, PresenceError, kind_class, parse_duration)
from levain.desk.legacy import ROLE_DESK

__all__ = ["VERSION", "FILE_NAME", "BACKUP_DIR", "DEVICES", "LETS", "LETS_CLASSES", "lets_label", "lets_describe", "lets_options", "FOLLOW_DEFAULT", "FOLLOW_MIN",
           "FOLLOW_MAX", "PRESET_TABLE", "overlay_active", "overlay_ended", "devices_on_now", "decide", "read", "view",
           "PRESETS_FILE", "PRESET_OPTION_KEYS", "set_preset_options", "reset_preset", "describe_line", "approvals_to_devices", "approvals_route", "approval_notice", "apply_preset", "apply_settings", "read_changes", "migrate",
           "unmigrate", "legacy_word", "from_legacy", "read_sample"]

VERSION = 1
FILE_NAME = "intent.json"
BACKUP_DIR = "pre-intent"
_MAX_BYTES = 4096
DEVICES = ("on", "off", "follow")
LETS = ("everything", "needs", "approvals", "nothing")
FOLLOW_DEFAULT = 600
FOLLOW_MIN = 60
FOLLOW_MAX = 86400
# the classes each level lets through (the kind -> class map is presence.KIND_CLASSES, unchanged)
LETS_CLASSES = {"everything": frozenset({"approval", "attention", "other"}),
                "needs": frozenset({"approval", "attention"}),
                "approvals": frozenset({"approval"}),
                "nothing": frozenset()}
# how each class is named when a level's label lists what it passes; a new class with no entry is named by its own key
_CLASS_WORDS = {"approval": ("approvals", "approval prompts"), "attention": ("attention items", "attention items"),
                "other": ("other pings", "other pings")}
_CLASS_ORDER = ("approval", "attention", "other")


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def lets_label(level: str) -> str:
    """The visible name of a lets-through level, generated from its member classes (``LETS_CLASSES``) so it always lists
    what the level passes: ``Approvals and attention items``. An empty level reads ``Nothing``."""
    members = _members(level)
    if not members:
        return "Nothing"
    text = _join([_CLASS_WORDS.get(c, (c, c))[0] for c in members])
    return text[:1].upper() + text[1:]


def lets_describe(level: str) -> str:
    """The one line saying what a level passes, generated from the same members as :func:`lets_label`."""
    members = _members(level)
    if not members:
        return "Passes no ping to your devices."
    return "Passes " + _join([_CLASS_WORDS.get(c, (c, c))[1] for c in members]) + " to your devices."


def _members(level: str) -> list[str]:
    got = LETS_CLASSES[level]
    return [c for c in _CLASS_ORDER if c in got] + sorted(c for c in got if c not in _CLASS_ORDER)


def lets_options() -> list[dict]:
    """What the cockpit paints for the lets-through control: one entry per level, in ``LETS`` order, each
    ``{value, label, describe}``. The page carries no copy of these words."""
    return [{"value": k, "label": lets_label(k), "describe": lets_describe(k)} for k in LETS]


# what each preset writes (devices is always `on`; follow_after is the base's; `back` and `loud off` write nothing)
PRESET_TABLE = {"away": {"devices": "on", "lets_through": "everything", "until": "input"},
                "quiet": {"devices": "on", "lets_through": "needs", "until": "timer"},
                "sleep": {"devices": "on", "lets_through": "needs", "until": "input"},
                "loud": {"devices": "on", "lets_through": "everything", "until": "timer"}}
_TITLES = {"away": "Away", "quiet": "Quiet", "sleep": "Sleep", "loud": "Loud"}
# What a preset may carry once the operator changes it (Phill np-8a2d86ca, 11-04): the three settings plus the ending TYPE
# (``input`` or ``timer``). A time or a duration is never carried: it belongs to one activation. ``<queue>/webpush/presets.json``
# holds only the fields that differ from PRESET_TABLE, per preset; no file is the system defaults.
PRESETS_FILE = "presets.json"
PRESET_OPTION_KEYS = ("devices", "follow_after", "lets_through", "until")
_UNTIL_KINDS = ("input", "timer")
_FAULT_WHY = "the presence record could not be read: pushing"
_FAULT_LINE = ("Pings now: your devices (the presence record could not be read: pushing). Devices: unknown. "
               "Lets through: everything. Until: it is repaired (levain ping status --json says why).")
_UNSET = presence._UNSET


# --------------------------------------------------------------------------------------------------- small helpers
def _num(v: object) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v) and abs(v) < 1e11
    except (OverflowError, ValueError):
        return False


def _follow_ok(v: object) -> bool:
    return _num(v) and FOLLOW_MIN <= v <= FOLLOW_MAX


def _intify(v: float) -> int | float:
    return int(v) if float(v).is_integer() else float(v)


def _clock(ts: float, now: float) -> str:
    """HH:MM local, with a weekday prefix when it is not today."""
    t = time.localtime(ts)
    stamp = time.strftime("%H:%M", t)
    if time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return stamp
    return f"{time.strftime('%a', t)} {stamp}"


def _mins(secs: float) -> str:
    m = secs / 60.0
    return f"{int(m)} min" if float(m).is_integer() else f"{m:.1f} min"


def _default_base(ts: float = 0.0) -> dict:
    return {"devices": "follow", "follow_after": FOLLOW_DEFAULT, "lets_through": "everything", "ts": ts, "via": "default"}


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


# --------------------------------------------------------------------------------------------------- the record on disk
def _valid_until(got: object) -> dict | None:
    if not isinstance(got, dict):
        return None
    if got.get("kind") == "input":
        return {"kind": "input"}
    if got.get("kind") == "timer" and _num(got.get("at")):
        return {"kind": "timer", "at": float(got["at"])}
    return None


def _valid_layer(got: object, *, overlay: bool) -> dict | None:
    if not isinstance(got, dict):
        return None
    if got.get("devices") not in DEVICES or got.get("lets_through") not in LETS:
        return None
    if not _follow_ok(got.get("follow_after")) or not _num(got.get("ts")):
        return None
    out: dict = {"devices": got["devices"], "follow_after": _intify(got["follow_after"]),
                 "lets_through": got["lets_through"], "ts": float(got["ts"])}
    if isinstance(got.get("via"), str):
        out["via"] = presence._clean(got["via"], presence._FIELD_MAX)
    if overlay:
        preset = got.get("preset")
        if preset is not None and (preset not in PRESET_TABLE):
            return None
        until = _valid_until(got.get("until"))
        if until is None:
            return None
        out = {"preset": preset, **out, "until": until, **presence._provenance(got)}
        if isinstance(got.get("via"), str):
            out["via"] = presence._clean(got["via"], presence._FIELD_MAX)
    return out


class _Fault(Exception):
    pass


def _load_file(queue_dir: Path | str) -> dict | None:
    """The record on file, ``None`` for no file. Raises :class:`_Fault` for anything that cannot be read safely."""
    try:
        fd = os.open(_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _Fault(f"unreadable ({type(e).__name__})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Fault("not a regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError as e:
        raise _Fault(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise _Fault("file too large")
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise _Fault("not valid JSON") from e
    if not isinstance(got, dict) or isinstance(got.get("version"), bool) or got.get("version") != VERSION:
        raise _Fault(f"version is not {VERSION}")
    base = _valid_layer(got.get("base"), overlay=False)
    if base is None:
        raise _Fault("the base layer is missing or has a bad field")
    ov = got.get("overlay")
    overlay = None
    if ov is not None:
        overlay = _valid_layer(ov, overlay=True)
        if overlay is None:
            raise _Fault("the overlay has a bad field")
    return {"version": VERSION, "base": base, "overlay": overlay}


def read(queue_dir: Path | str | None, now: float | None = None) -> dict:
    """``{status, reason, base, overlay, projection}``. ``status``: ``file`` (a valid ``intent.json``), ``default`` (no
    file, nothing old to project), ``projection`` (no ``intent.json`` but old files present: the migration's mapping run in
    memory, so behaviour is continuous before ``levain ping migrate``) or ``fault`` (a file that cannot be read safely:
    decisions read as push). ``overlay`` is whatever is ON FILE (it may have ended: :func:`overlay_ended`)."""
    t = time.time() if now is None else now
    if queue_dir is None:
        return {"status": "default", "reason": "", "base": _default_base(), "overlay": None, "projection": None}
    try:
        rec = _load_file(queue_dir)
    except _Fault as e:
        return {"status": "fault", "reason": str(e), "base": _default_base(), "overlay": None, "projection": None}
    if rec is not None:
        return {"status": "file", "reason": "", "base": rec["base"], "overlay": rec["overlay"], "projection": None}
    try:
        proj = _project(queue_dir, t)
    except Exception:  # noqa: BLE001 - an old file nobody can map reads as the default, never an error out of a read
        proj = None
    if proj is not None and proj["found"]:
        return {"status": "projection", "reason": "", "base": proj["record"]["base"], "overlay": proj["record"]["overlay"],
                "projection": proj}
    return {"status": "default", "reason": "", "base": _default_base(), "overlay": None, "projection": None}


# --------------------------------------------------------------------------------------------------- the pure rules
def overlay_ended(rec: dict, now: float, last_input_at: float | None, desk_msg_at: float | None) -> str | None:
    """Why the overlay in ``rec`` (a :func:`read` result) has ended at ``now``, or ``None`` while it stands (or when there is
    no overlay). ``timer``: ``now`` past ``at``. ``desk message``: a typed turn into a session after the overlay's ``ts`` (no
    grace). An until-input overlay (an explicit word, away / sleep or a setting) STANDS until another word of his or that
    typed turn (Phill np-a93deaf7 "2a", 11-02): sensed laptop input, fresh or stale, present or missing, never ends it.
    Remote input never ends it. Pure."""
    ov = rec.get("overlay")
    if ov is None:
        return None
    until = ov["until"]
    if until["kind"] == "timer":
        return "timer" if now >= until["at"] else None
    if desk_msg_at is not None and desk_msg_at > ov["ts"]:
        return "desk message"
    return None


def overlay_active(rec: dict, now: float, last_input_at: float | None, desk_msg_at: float | None) -> bool:
    """Whether ``rec`` has an overlay that still stands at ``now`` (:func:`overlay_ended`)."""
    return rec.get("overlay") is not None and overlay_ended(rec, now, last_input_at, desk_msg_at) is None


def _last_input(now: float, reading: float | None) -> float | None:
    return None if reading is None else now - max(0.0, reading)


def _remote_fresh(remote: dict | None, now: float) -> float | None:
    """Age in seconds of a fresh remote-activity record, else ``None`` (a stale one decides nothing, however it is passed)."""
    if remote is None:
        return None
    age = now - remote["at"]
    return max(0.0, age) if age < ACTIVE_WINDOW_SECS else None


def _ago(secs: float) -> str:
    m = int(max(0.0, secs) // 60)
    return "just now" if m < 1 else f"{m} min ago"


def devices_on_now(eff: dict, remote: dict | None, now: float, *, desk_msg_at: float | None) -> tuple[bool, str]:
    """``(on, why)``: whether pings go to his devices under the effective settings ``eff`` at ``now``. ``follow`` returns to the
    desk ONLY on a typed desk message (Phill 11-03, "1a"): laptop keyboard or mouse input, a screen wake included, never counts.
    ``desk_msg_at`` is the newest desk message's clock time, ``None`` when there is none. Remote input feeds ``follow``
    only and never counts as the desk. ``desk_msg_at`` is keyword-only and has no default: the slot once held idle seconds, so a
    caller written for that fails loudly instead of reading a small number as a 1970 desk message. ``eff`` may carry ``preset`` and ``layer`` (``overlay``): they only name the reason."""
    d = eff["devices"]
    if d == "on":
        if eff.get("preset"):
            return True, eff["preset"]
        return True, "set for a while" if eff.get("layer") == "overlay" else "devices on"
    if d == "off":
        return False, "devices off"
    rage = _remote_fresh(remote, now)
    if rage is not None:
        what = "remote terminal input" if remote["source"] == "remote-terminal" else "remote control turn"
        return True, f"follow: {what} {int(rage // 60)} min ago"
    if desk_msg_at is None:
        return True, "follow: no desk message"
    age = max(0.0, now - desk_msg_at)
    if age >= eff["follow_after"]:
        return True, f"follow: no desk message for {int(age // 60)} min"
    return False, f"follow: desk message {_ago(age)}"


def _eff(rec: dict, now: float, reading: float | None, desk_msg_at: float | None) -> tuple[dict, dict | None, str | None]:
    """``(eff, overlay_standing, ended_reason)``: the settings in force. ``eff`` carries ``layer`` and ``preset``."""
    why = overlay_ended(rec, now, _last_input(now, reading), desk_msg_at)
    ov = rec.get("overlay")
    if ov is not None and why is None:
        return {**ov, "layer": "overlay"}, ov, None
    return {**rec["base"], "layer": "base", "preset": None}, None, (why if ov is not None else None)


def decide(now: float, rec: dict, reading: float | None, *, class_name: str, remote: dict | None = None,
           desk_msg_at: float | None, filed: bool = False) -> Decision:
    """Whether a ping of ``class_name`` is pushed at ``now``. Pure. ``rec`` is a :func:`read` result; ``reading`` the laptop
    idle seconds (shown only: neither ``follow`` nor an ``until: input`` overlay reads it, both read ``desk_msg_at``, Phill 11-03 "1a"). ``filed``: the ping is about a request already FILED and waiting
    for a signed answer (an approval-class ping from a consent hook or gate), which goes whatever the desk or devices
    setting is doing (only a mute holds it). A fault reads as push (a ping he did not need costs one notification, one
    he needed and never got costs the day)."""
    return _decide(now, rec, reading, class_name, remote, desk_msg_at, filed)[0]


def _decide(now: float, rec: dict, reading: float | None, class_name: str, remote: dict | None,
            desk_msg_at: float | None, filed: bool = False) -> tuple[Decision, str]:
    if rec["status"] == "fault":
        return Decision(True, "pushing", _FAULT_WHY), "measured"
    eff, ov, _ended = _eff(rec, now, reading, desk_msg_at)
    on, why = devices_on_now(eff, remote, now, desk_msg_at=desk_msg_at)
    preset = (ov or {}).get("preset") or ""
    source = "word" if ov is not None else "measured"
    if class_name not in LETS_CLASSES[eff["lets_through"]]:
        label = preset or "your setting"
        level = eff["lets_through"]
        return Decision(False, "muted", f"{label}: lets through {lets_label(level)}", preset or "lets"), source
    if on:
        return Decision(True, "pushing", why, preset), source
    if class_name == "attention":
        # Phill 10-27: an attention item pings his phone "even when pings are not otherwise on" (the desk being active, or devices
        # off, holds the other classes, never this one); only a mute (lets_through) holds it, above.
        return Decision(True, "pushing", "an attention item reaches your devices whatever the desk is doing", preset), source
    if filed and class_name == "approval":
        # Phill 11-02 (np-e642162b (a)): "yes we DO want these to be able to find us ASAP". A request already filed and
        # held for a signed answer has no dialog to fall back to, so its ping is the only way it finds him. This is the
        # PING's decision only: the hold's question (approvals_to_devices, approvals_route) never passes ``filed``.
        return Decision(True, "pushing", "a filed request waits on your signed answer: it reaches your devices "
                                         "whatever the desk is doing", preset), source
    return Decision(False, "holding", why), source


# --------------------------------------------------------------------------------------------------- sampling the world
def _raw_idle(idle: Callable[[], float | None] | None) -> float | None:
    try:
        if idle is None:
            from levain.consent.auto_away import input_idle_seconds as idle
        return idle()
    except Exception:  # noqa: BLE001 - no reading: the rule reads him as inactive
        return None


def _sample(queue_dir: Path | str | None, now: float, idle: Callable[[], float | None] | None,
            raw: object = _UNSET, remote: object = _UNSET) -> dict:
    """One read of every input the rule uses: the record, the laptop idle (``raw``; a desk message counted into
    ``reading``), the newest desk message and the fresh remote record. A caller that already sampled hands them in."""
    rec = read(queue_dir, now)
    raw_idle = _raw_idle(idle) if raw is _UNSET else raw
    window = max(rec["base"]["follow_after"], (rec["overlay"] or {}).get("follow_after", 0), ACTIVE_WINDOW_SECS)
    reading = desk.adjust_reading(queue_dir, raw_idle, now, window=window) if queue_dir is not None else raw_idle
    msg = desk.read_desk_message(queue_dir, now) if queue_dir is not None else None
    if remote is presence.REMOTE_FAULT:
        raise PresenceError("the remote-activity read faulted")
    if remote is _UNSET:
        remote = presence.read_remote_activity(queue_dir, now)
    return {"rec": rec, "now": now, "raw": raw_idle, "reading": reading, "msg_at": msg["at"] if msg else None,
            "remote": remote}


def read_sample(queue_dir: Path | str | None, now: float | None = None,
                idle: Callable[[], float | None] | None = None) -> dict:
    """The ONE public reading of the presence rule's inputs, for callers outside levain (flow's phone bridge). One sample:
    ``record`` (a :func:`read` result), ``reading`` (the laptop's input idle seconds, a desk message counted in; ``None``
    when unknown), ``last_input_at`` (that reading as a clock time), ``desk_message_at``, ``remote`` (the fresh
    remote-activity record or ``None``), ``overlay_active`` (whether the record's overlay stands at ``now``) and ``now``.
    Raises what :func:`read` and the sensor raise; callers decide how a fault reads."""
    t = time.time() if now is None else now
    smp = _sample(queue_dir, t, idle)
    last = _last_input(t, smp["reading"])
    return {"record": smp["rec"], "reading": smp["reading"], "last_input_at": last, "desk_message_at": smp["msg_at"],
            "remote": smp["remote"], "overlay_active": overlay_active(smp["rec"], t, last, smp["msg_at"]), "now": t}


def _core(queue_dir: Path | str | None, kind: str, now: float | None, idle: Callable[[], float | None] | None,
          suggest: bool, remote: object = _UNSET, raw: object = _UNSET, filed: bool = False) -> tuple[Decision, str, dict]:
    """The one place the rule is read: ``(decision, source, sample)``. Both :func:`presence.should_push` and the status
    (:func:`view`) call it, so the push and the line cannot diverge. ``filed``: see :func:`decide`. Raises on a fault;
    the callers read that as push."""
    at = time.time() if now is None else now
    s = _sample(queue_dir, at, idle, raw=raw, remote=remote)
    d, source = _decide(at, s["rec"], s["reading"], kind_class(kind), s["remote"], s["msg_at"], filed)
    if suggest:   # a delivery: keep what it saw and said, and the event rows (never a fault of the rule)
        try:
            ov = s["rec"]["overlay"] if source == "word" else None
            desk.record_decision(queue_dir, at, kind, d.push, d.state, d.why, source, s["reading"],
                                 {"word": ov["preset"] or "setting"} if ov else None)
            desk.observe(queue_dir, at, s["reading"], msg_at=s["msg_at"])
        except Exception:  # noqa: BLE001
            pass
    return d, source, s


def approvals_to_devices(queue_dir: Path | str | None, *, now: float | None = None,
                         idle: Callable[[], float | None] | None = None) -> tuple[bool, str]:
    """Whether an approval reaches his devices for a signed answer right now, and why: literally ``should_push(queue_dir,
    "consent")`` with ``suggest=False``, so it is the hold's question; a push differs from it only in the case the module docstring
    names (consent reads this, DESIGN §4)."""
    d = presence.should_push(queue_dir, "consent", now=now, idle=idle, suggest=False)
    return d.push, d.why


def _blind(s: dict, eff: dict) -> bool:
    """Whether a ``follow`` decision in sample ``s`` under settings ``eff`` reads as "devices on" ONLY for want of a measurement.
    ``follow`` reads the typed desk message alone (Phill 11-03, "1a"), and a missing message is the rule's answer, not a blind
    spot: a fresh install sends approvals to the devices and consent holds for a signed answer there. So nothing the derivation
    reads can be unmeasured, and this is false; the laptop's own input reading, which ``follow`` no longer reads, never makes
    it true. Kept as the one place a future unmeasurable input would say so."""
    return False


def approvals_route(queue_dir: Path | str | None, *, now: float | None = None,
                    idle: Callable[[], float | None] | None = None) -> tuple[bool, str, bool]:
    """``(to_devices, why, blind)``: :func:`approvals_to_devices` from the same one sample, plus ``blind``, true when the
    record sends approvals to the devices only for want of a measurement (:func:`_blind`). The push
    keeps that fail-open (a ping he did not need costs one notification); consent's HOLD does not (it would block the
    agent on nothing measured), so consent reads ``blind`` and leaves the agent's own dialog to ask. A fault is
    ``(True, why, False)``, as :func:`presence.should_push` reads it."""
    at = time.time() if now is None else now
    try:
        d, _source, s = _core(queue_dir, "consent", at, idle, False)
    except Exception:  # noqa: BLE001 - the same fault rule as should_push
        return True, presence._FAULT, False
    if not d.push or s["rec"]["status"] == "fault":
        return d.push, d.why, False
    eff = _eff(s["rec"], at, s["reading"], s["msg_at"])[0]
    return True, d.why, _blind(s, eff)


def approval_notice(queue_dir: Path | str | None, *, now: float | None = None,
                    idle: Callable[[], float | None] | None = None) -> tuple[bool, str]:
    """``(send, why)``: whether a NOTICE that an approval prompt waits in the agent's own dialog goes to his devices
    (Phill 11-02, np-e642162b (a): *"yes we DO want these to be able to find us ASAP"*). It goes whenever the record's
    ``lets_through`` admits approvals, whatever the desk or the devices setting is doing; only a mute holds it. It
    starts no hold and is not :func:`approvals_to_devices` (the hold's question, unchanged): the dialog stays where
    the agent asked. Read from the same one sample as the decider (:func:`_core`); a fault reads as send, as
    :func:`presence.should_push` reads it."""
    at = time.time() if now is None else now
    try:
        d, _source, _s = _core(queue_dir, "consent", at, idle, False)
    except Exception:  # noqa: BLE001 - the same fault rule as should_push
        return True, presence._FAULT
    return d.state != "muted", d.why


# --------------------------------------------------------------------------------------------------- the describe line
def _base_text(base: dict) -> str:
    d = base["devices"]
    text = f"follow, after {_mins(base['follow_after'])}" if d == "follow" else f"devices {d}"
    if base["lets_through"] != "everything":
        text += f", lets through {lets_label(base['lets_through'])}"
    return text


def _until_text(rec: dict, ov: dict | None, now: float) -> str:
    if ov is None:
        return "I switch it"
    then = _base_text(rec["base"])
    u = ov["until"]
    if u["kind"] == "timer":
        return f"{_clock(u['at'], now)} (then {then})"
    return f"another word of yours or a message you type into a session (then {then})"


def _who_text(ov: dict | None) -> str:
    if not ov or "by" not in ov:
        return ""
    by = ov["by"]
    who = f"{by['agent']} {by['session']}".strip()
    who = f"the desk {who}" if by["role"] == ROLE_DESK else f"seat {who}"
    flag = " (unverified)" if ov.get("verified") is not True else ""
    return f" Set by {who} from your words: \"{ov['said']}\"{flag}."


def _approvals_note(queue_dir: Path | str | None, to_devices: bool | None = None, *, blind: bool = False) -> str | None:
    """The ``Approvals:`` clause, from consent's side (the signed away mark, a key that can sign, and ``blind``: the record
    sends approvals to the devices only for want of a measurement, where consent's hold does not follow it). Consent
    provides ``levain.consent.auto_away.approvals_note(queue_dir)``; until it does, or on any fault, there is none."""
    try:
        from levain.consent import auto_away

        fn = getattr(auto_away, "approvals_note", None)
        return fn(queue_dir, to_devices=to_devices, blind=blind) if fn is not None else None
    except Exception:  # noqa: BLE001
        return None


def _state(queue_dir: Path | str | None, s: dict) -> dict:
    """Everything the line, the chip and the surfaces say, from one sample."""
    now, rec, reading = s["now"], s["rec"], s["reading"]
    if rec["status"] == "fault":
        return {"fault": True, "rec": rec}
    eff, ov, ended = _eff(rec, now, reading, s["msg_at"])
    on, why = devices_on_now(eff, s["remote"], now, desk_msg_at=s["msg_at"])
    lets = eff["lets_through"]
    goes = on and lets != "nothing"
    if lets == "nothing":
        why = "lets through nothing"
    return {"fault": False, "rec": rec, "eff": eff, "ov": ov, "ended": ended, "on": on, "why": why, "goes": goes,
            "lets": lets}


def _line(queue_dir: Path | str | None, s: dict, st: dict, *, approvals: str | None, held_n: int) -> str:
    if st["fault"]:
        return _FAULT_LINE
    now, rec, eff, ov, lets = s["now"], st["rec"], st["eff"], st["ov"], st["lets"]
    where = "your devices" if st["goes"] else "the desk only"
    if st["goes"] and lets in ("needs", "approvals"):
        where += f", {lets_label(lets)}"
    if not st["goes"]:
        # what reaches the devices whatever the desk is doing: an attention item (Phill 10-27), the notice that an
        # approval prompt waits in the agent's own dialog (Phill 11-02, approval_notice) and the ping about a request
        # filed and held for a signed answer (the same ruling, decide's ``filed``); a mute holds all of them
        through = [w for c, w in (("approval", "approval prompts"), ("attention", "attention items"))
                   if c in LETS_CLASSES[lets]]
        if through:
            where += ", except " + " and ".join(through)
    dev = {"on": "on", "off": "off"}.get(eff["devices"]) or f"follow, after {_mins(eff['follow_after'])}"
    line = (f"Pings now: {where} ({st['why']}). Devices: {dev}. Lets through: {lets_label(lets)}. "
            f"Until: {_until_text(rec, ov, now)}.")
    if approvals:
        line += f" Approvals: {approvals}."
    if held_n:
        line += f" Held for your devices: {held_n}."
    line += _who_text(ov)
    if rec["status"] == "projection":
        line += " Not migrated yet: levain ping migrate."
    return line


def _chip(s: dict, st: dict) -> dict:
    if st["fault"]:
        return {"word": "Check", "tail": "→ devices", "state": "fault"}
    now, eff, ov = s["now"], st["eff"], st["ov"]
    if ov is not None:
        preset = ov["preset"]
        if preset is None:
            word = "For a while"
        elif ov["until"]["kind"] == "timer":
            word = f"{_TITLES[preset]} {max(1, math.ceil((ov['until']['at'] - now) / 60))}m"
        else:
            word = _TITLES[preset]
    else:
        word = {"follow": "Follow", "on": "Devices on", "off": "Desk only"}[eff["devices"]]
    return {"word": word, "tail": "→ devices" if st["goes"] else "→ desk", "state": "devices" if st["goes"] else "desk"}


def view(queue_dir: Path | str | None, *, now: float | None = None, idle: Callable[[], float | None] | None = None,
         remote: object = _UNSET, reading: object = _UNSET) -> dict:
    """What every surface paints, from ONE sample (a status read never writes). Keys ``state`` (what an ordinary ping
    meets: ``pushing|holding|muted``), ``pushing``, ``reason``, ``source`` (``word`` when an overlay decided), ``since``,
    ``word`` (the deciding preset), ``remote``, ``you``, ``desk``; plus ``line`` and ``chip`` (:func:`describe_line`,
    DESIGN §7), ``status`` (``file|default|projection|fault``), ``reason_fault``, ``base``, ``overlay`` (standing, else
    ``None``), ``ended`` (why an overlay on file has ended), ``effective`` (the settings in force), ``on`` (devices on now),
    ``where`` (``devices|desk``), ``lets`` (:func:`lets_options`), ``presets`` (:func:`_presets_view`), ``held`` and ``approvals`` (the consent clause, or ``None``). ``reading`` (the raw laptop
    idle) lets a caller that sampled it once hand it in."""
    at = time.time() if now is None else now
    try:
        s = _sample(queue_dir, at, idle, raw=reading, remote=remote)
        d, source = _decide(at, s["rec"], s["reading"], "other", s["remote"], s["msg_at"])
    except Exception:  # noqa: BLE001 - the same fault rule as should_push
        return {"state": "pushing", "pushing": True, "reason": presence._FAULT, "source": "measured", "since": None,
                "word": None, "remote": None, "you": None, "desk": None, "line": _FAULT_LINE,
                "chip": {"word": "Check", "tail": "→ devices", "state": "fault"}, "status": "fault",
                "reason_fault": "the sample faulted", "base": _default_base(), "overlay": None, "ended": None,
                "effective": None, "on": True, "where": "devices", "held": 0, "approvals": None,
                "lets": lets_options(), "presets": _presets_view(None, _default_base())}
    st = _state(queue_dir, s)
    try:
        from levain.webpush import held

        held_n = held.count(queue_dir)
    except Exception:  # noqa: BLE001
        held_n = 0
    approvals = None
    if not st["fault"]:
        # ONE sample: whether the record itself sends approvals to the devices, so consent's clause names only a difference.
        to_dev = _decide(at, s["rec"], s["reading"], "approval", s["remote"], s["msg_at"])[0].push
        approvals = _approvals_note(queue_dir, to_dev, blind=to_dev and _blind(s, st["eff"]))
    ov = None if st["fault"] else st["ov"]
    window = s["rec"]["base"]["follow_after"] if st["fault"] else st["eff"]["follow_after"]
    out = {"state": d.state, "pushing": d.push, "reason": d.why, "source": source,
           "since": ov["ts"] if ov is not None and source == "word" else None,
           "word": ov["preset"] if ov is not None and source == "word" else None,
           "remote": {"source": s["remote"]["source"], "at": s["remote"]["at"]} if s["remote"] else None,
           "you": desk.you_view(at, s["reading"], window=window), "desk": desk.read_desk(queue_dir, at),
           "line": _line(queue_dir, s, st, approvals=approvals, held_n=held_n), "chip": _chip(s, st),
           "status": s["rec"]["status"], "reason_fault": s["rec"]["reason"], "base": s["rec"]["base"], "overlay": ov,
           "ended": None if st["fault"] else st["ended"],
           "effective": None if st["fault"] else {k: v for k, v in st["eff"].items() if k not in ("by", "said", "verified")},
           "on": True if st["fault"] else st["on"], "where": "devices" if st["fault"] or st["goes"] else "desk",
           "held": held_n, "approvals": approvals, "lets": lets_options(),
           "presets": _presets_view(queue_dir, s["rec"]["base"])}
    return out


def describe_line(queue_dir: Path | str | None, *, now: float | None = None,
                  idle: Callable[[], float | None] | None = None) -> str:
    """THE one sentence (DESIGN §7), printed verbatim by ``levain ping status``, the cockpit and flow's ``phone_bridge.py
    status``. No surface composes its own words."""
    return view(queue_dir, now=now, idle=idle)["line"]


# --------------------------------------------------------------------------------------------------- per-preset options
def _presets_path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / PRESETS_FILE


def _option_ok(key: str, v: object) -> bool:
    if key == "devices":
        return v in DEVICES
    if key == "lets_through":
        return v in LETS
    if key == "follow_after":
        return _follow_ok(v)
    return v in _UNTIL_KINDS


def _load_presets(queue_dir: Path | str | None) -> tuple[dict, str]:
    """``({preset: {option: value}}, fault)``. No file, or ``queue_dir`` of ``None``, is the system defaults. A file that cannot
    be read safely reads as the defaults too (a preset then does what the table says) and names why in ``fault``; a WRITE
    refuses it, so the options on file are never replaced silently."""
    if queue_dir is None:
        return {}, ""
    try:
        fd = os.open(_presets_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        return {}, f"unreadable ({type(e).__name__})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}, "not a regular file"
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError as e:
        return {}, f"unreadable ({type(e).__name__})"
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        return {}, "file too large"
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}, "not valid JSON"
    if not isinstance(got, dict) or isinstance(got.get("version"), bool) or got.get("version") != VERSION \
            or not isinstance(got.get("presets"), dict):
        return {}, f"version is not {VERSION}"
    out: dict = {}
    for name, opts in got["presets"].items():
        if name not in PRESET_TABLE or not isinstance(opts, dict) or set(opts) - set(PRESET_OPTION_KEYS):
            return {}, f"{name!r} is not a preset I carry options for"
        if not all(_option_ok(k, v) for k, v in opts.items()):
            return {}, f"a bad option on {name!r}"
        out[name] = {k: (_intify(v) if k == "follow_after" else v) for k, v in opts.items()}
    return out, ""


def _write_presets(queue_dir: Path | str, presets: dict) -> None:
    """Atomic write (tmp + ``os.replace``, ``O_NOFOLLOW``). The caller holds the presence lock."""
    d = Path(queue_dir) / "webpush"
    data = json.dumps({"version": VERSION, "presets": presets}).encode("utf-8")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        tmp = d / f".{PRESETS_FILE}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view_ = memoryview(data)
                while view_:
                    n = os.write(fd, view_)
                    if n <= 0:
                        raise OSError("short write")
                    view_ = view_[n:]
            finally:
                os.close(fd)
            os.replace(tmp, _presets_path(queue_dir))
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as e:
        raise PresenceError(f"preset options not written: {e}") from e


def _spec(word: str, custom: dict, base: dict) -> dict:
    """What activating ``word`` writes: the table's row, then the operator's changes, with ``follow_after`` the base's unless
    the preset carries its own."""
    out = {**PRESET_TABLE[word], **custom.get(word, {})}
    out.setdefault("follow_after", base["follow_after"])
    return out


def _prune(word: str, opts: dict) -> dict:
    """``opts`` without the fields equal to the system default (a stored option is always a difference)."""
    return {k: v for k, v in opts.items() if not (k != "follow_after" and PRESET_TABLE[word].get(k) == v)}


def _opts_text(o: dict, *, ends: bool) -> str:
    dev = {"on": "on", "off": "off"}.get(o["devices"]) or f"follow, after {_mins(o['follow_after'])}"
    text = f"Devices: {dev}. Lets through: {lets_label(o['lets_through'])}."
    if ends:
        text += " Ends: " + ("after a time you set" if o["until"] == "timer" else "on another word of yours or a message you type")
    return text.strip()


def _presets_view(queue_dir: Path | str | None, base: dict) -> dict:
    """``{preset: {options, defaults, custom, summary}}`` for ``back`` (the standing default, whose options ARE the base) and the
    four presets, so the page keeps no copy of a default or a sentence. ``custom`` is true when the options differ from the
    defaults; ``summary`` is the one line the page shows under a customised preset."""
    custom, _fault = _load_presets(queue_dir)
    dflt = _default_base()
    keys = ("devices", "follow_after", "lets_through")
    bo = {k: base[k] for k in keys}
    do = {k: dflt[k] for k in keys}
    out = {"back": {"options": bo, "defaults": do, "custom": bo != do, "summary": _opts_text(bo, ends=False)}}
    for w in PRESET_TABLE:
        o = _spec(w, custom, base)
        d = _spec(w, {}, base)
        out[w] = {"options": o, "defaults": d, "custom": bool(custom.get(w)), "summary": _opts_text(o, ends=True)}
    return out


# --------------------------------------------------------------------------------------------------- writing the record
def _write_record(queue_dir: Path | str, rec: dict) -> bytes:
    """Atomic write of ``rec`` (tmp + ``os.replace``, ``O_NOFOLLOW``, owner-checked directory). The caller holds the
    presence lock. Returns the bytes written."""
    path = _path(queue_dir)
    d = path.parent
    data = json.dumps({"version": VERSION, "base": rec["base"], "overlay": rec["overlay"]}).encode("utf-8")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        tmp = d / f".{FILE_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view_ = memoryview(data)
                while view_:
                    n = os.write(fd, view_)
                    if n <= 0:
                        raise OSError("short write")
                    view_ = view_[n:]
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as e:
        raise PresenceError(f"presence record not written: {e}") from e
    return data


def _prior_for_log(rec: dict) -> dict:
    ov = rec.get("overlay")
    if ov is None:
        return {}
    prior = {"word": ov["preset"] or "set for a while"}
    if "by" in ov:
        prior["by"] = ov["by"]
    return prior


def _current(queue_dir: Path | str, ts: float) -> dict:
    """The record to plan a write against, under the lock: :func:`read`'s result (a ``projection`` stays one until
    :func:`_materialize`). A ``fault`` (an ``intent.json`` that is corrupt, unreadable or of another version) is REFUSED
    with :class:`PresenceError`, never replaced by the default base: a write would silently destroy what is on file."""
    rec = read(queue_dir, ts)
    if rec["status"] == "fault":
        raise PresenceError(f"intent.json is there but cannot be read ({rec['reason']}): nothing was changed; repair it, "
                            "or move it aside (no intent.json reads as the default, or as your old files)")
    return rec


def _materialize(queue_dir: Path | str, rec: dict, ts: float) -> dict:
    """``rec`` as a file record, just before a write that has already been validated and planned: old files with no
    ``intent.json`` are migrated first (with a backup, so ``levain ping migrate --undo`` restores them), so a write never
    layers a second truth over stale files, and a refused write migrates nothing."""
    if rec["status"] != "projection":
        return rec
    _migrate_locked(queue_dir, ts, via="first write")
    got = read(queue_dir, ts)
    if got["status"] != "file":
        raise PresenceError("the migration left no readable intent.json: nothing was changed")
    return got


def _live(queue_dir: Path | str, ts: float, idle: Callable[[], float | None] | None, rec: dict) -> tuple[float | None, float | None]:
    """``(reading, desk_msg_at)`` for deciding whether the overlay on file still stands."""
    s = _sample(queue_dir, ts, idle)
    return s["reading"], s["msg_at"]


def _until_arg(until: object, ts: float) -> dict | None:
    """``None`` for "I switch it" (written to base), else an overlay ``until``."""
    if until is None or until == "switch":
        return None
    if until == "input":
        return {"kind": "input"}
    if isinstance(until, dict):
        got = _valid_until(until)
        if got is None or (got["kind"] == "timer" and got["at"] <= ts):
            raise PresenceError("until is not a valid end (a timer ends after now)")
        return got
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        secs = float(until)
        if not MIN_SECS <= secs <= MAX_SECS:
            raise PresenceError("a time-limited setting runs from 1 minute to 24 hours")
        return {"kind": "timer", "at": ts + secs}
    if isinstance(until, str):
        return {"kind": "timer", "at": ts + parse_duration(until)}
    raise PresenceError("until is `switch`, `input` or a duration like 30m or 2h")


def apply_preset(queue_dir: Path | str, preset: str, *, duration: str | None = None, by: dict | None = None,
                 said: str | None = None, match: object = None, via: str | None = None, wait: float | None = None,
                 now: float | None = None, idle: Callable[[], float | None] | None = None,
                 precheck: Callable[[], None] | None = None, after: Callable[[], None] | None = None,
                 ending: str | None = None) -> dict:
    """Write a preset (``away``, ``back``, ``quiet [dur]``, ``sleep``, ``loud [dur|off]``) and return :func:`view` after.

    The preset writes its table row as the operator has changed it (:func:`set_preset_options`). ``ending`` (``input`` or
    ``timer``; the operator's own call only) ends THIS activation that way and is remembered as the preset's ending type; a
    ``timer`` ending takes ``duration`` (one hour by default), and the duration is never remembered.

    ``back`` clears the overlay and leaves the base (and a standing ``loud``); ``loud off`` clears a standing loud only. A seat
    may set ``away`` only, the desk ``away``, ``quiet`` and ``sleep`` (:func:`levain.webpush.presence.check_setter`):
    ``by`` and ``said`` are checked before anything is read or written, a setter replaces an overlay the operator set only on a
    verified quote typed after it, and every call (a refusal included) leaves one row in the presence log. The whole call runs
    under the presence lock; ``wait`` bounds the lock wait (:class:`presence.PresenceBusy`); ``precheck`` runs once the lock is
    held and may refuse; ``after`` runs under the same hold. Pings the new record frees are due now: the CALLER starts the flush
    after the write (``held.flush_if_due(queue_dir, throttle=False)``, outside the lock), as ``levain ping`` and flow's
    ``phone_bridge`` do; a library write never spawns a process."""
    strict = by is not None or wait is not None or precheck is not None
    with presence._locked(queue_dir, strict=strict, wait=wait):
        ts = time.time() if now is None else now   # after the lock: a long wait must not date the write in the past
        if precheck is not None:
            try:
                precheck()
            except PresenceError as e:
                row = presence._log_row(ts, preset, duration, by, said, None)
                if via:
                    row["via"] = presence._clean(via, presence._FIELD_MAX)
                presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
                raise
        _apply_preset_locked(queue_dir, preset, duration, ts, by, said, match, via, idle, ending)
        if after is not None:
            after()
    return view(queue_dir, now=ts, idle=idle)


def _apply_preset_locked(queue_dir, word, duration, ts, by, said, match, via, idle, ending=None) -> None:
    try:
        rec = _current(queue_dir, ts)
    except PresenceError as e:
        row = presence._log_row(ts, word, duration, by, said, None)
        if via:
            row["via"] = presence._clean(via, presence._FIELD_MAX)
        presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
        raise
    prior = _prior_for_log(rec)
    row = presence._log_row(ts, word, duration, by, said, prior)
    if via:
        row["via"] = presence._clean(via, presence._FIELD_MAX)
    ev = presence._evidence(match) if by is not None else None
    if ev is not None:
        row["verified"] = ev[0]
        if ev[1] is not None:
            row["said_at"] = ev[1]
    try:
        action, new, why = _plan(queue_dir, rec, word, duration, ts, by, said, ev, idle, ending)
        if action == "write" or (action == "clear" and rec["overlay"] is not None):
            rec = _materialize(queue_dir, rec, ts)   # validated and planned against the projection: only now migrate
    except PresenceError as e:
        presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
        raise
    row["outcome"] = "set" if action != "keep" else "unchanged"
    if new is not None and new["until"]["kind"] == "timer":
        row["until"] = new["until"]["at"]
    if why:
        row["why"] = why
    if by is not None:
        try:
            presence._log(queue_dir, row)
        except PresenceError as e:
            raise PresenceError(f"{e}: a setter's write with no record is not made") from e
    try:
        if action == "write":
            _write_record(queue_dir, {"base": rec["base"] if rec["status"] == "file" else _default_base(ts), "overlay": new})
            if ending is not None:
                _remember(queue_dir, word, {"until": ending})
        elif action == "clear" and rec["overlay"] is not None:
            _write_record(queue_dir, {"base": rec["base"] if rec["status"] == "file" else _default_base(ts), "overlay": None})
    except PresenceError as e:
        presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
        raise
    if by is None:
        presence._log_best_effort(queue_dir, row)


def _plan(queue_dir, rec, word, duration, ts, by, said, ev, idle, ending=None) -> tuple[str, dict | None, str]:
    """What a preset call does, with nothing written: ``(action, overlay, note)``, action ``write``, ``clear`` or ``keep``.
    Raises :class:`PresenceError` for a refusal."""
    if word not in WORDS:
        hint = f"; {presence.DROPPED_WORDS[word]}" if word in presence.DROPPED_WORDS else ""
        raise PresenceError(f"{word!r} is not a presence word (one of: {', '.join(WORDS)}){hint}")
    quote = presence.check_setter(word, by, said) if by is not None else None
    ov = rec["overlay"]
    reading, msg_at = _live(queue_dir, ts, idle, rec)
    standing = ov is not None and overlay_active(rec, ts, _last_input(ts, reading), msg_at)
    if by is not None and standing and ov["preset"] == "loud":
        raise PresenceError("`loud` is the operator's standing word; a seat cannot replace it "
                            "(the operator says `loud off` himself)")
    note = ""
    if by is not None:
        ev = ev or (False, None, "no transcript check ran")
        if standing and "by" not in ov:   # his own standing choice
            name = ov["preset"] or "a setting"
            at = time.strftime("%H:%M", time.localtime(ov["ts"]))
            if not ev[0]:
                raise PresenceError(f"he set `{name}` himself at {at} and your quote is not in your transcript "
                                    f"({ev[2]}); he ends it himself")
            if ev[1] <= ov["ts"]:
                raise PresenceError(f"he set `{name}` himself at {at}, after the sentence you quote; he ends it himself")
        elif not ev[0]:
            note = f"unverified: {ev[2]}"
    if ending is not None:
        if by is not None:
            raise PresenceError("the ending of a preset is the operator's own to choose")
        if ending not in _UNTIL_KINDS or word == "back" or (word == "loud" and (duration or "").strip().lower() == "off"):
            raise PresenceError("a preset ends on `input` or on a `timer` (Back and `loud off` have no ending)")
        _, fault = _load_presets(queue_dir)
        if fault:   # before anything is written: the ending could not be remembered, so the activation is refused too
            raise PresenceError(f"presets.json is there but cannot be read ({fault}): nothing was changed; repair it, "
                                "or move it aside (no file is the system defaults)")
    if word == "loud" and (duration or "").strip().lower() == "off":
        if ov is not None and ov["preset"] == "loud":
            return "clear", None, "loud off"
        return "keep", None, "loud off: no loud was standing"
    if word == "back" and standing and ov["preset"] == "loud":
        return "keep", None, "back leaves a standing loud"
    if word == "back":
        return "clear", None, ""
    base = rec["base"]
    custom, _fault = _load_presets(queue_dir)   # an unreadable options file reads as the system defaults
    spec = _spec(word, custom, base)
    if ending is not None:
        kind = ending
    elif by is not None:
        kind = PRESET_TABLE[word]["until"]    # a seat or the desk never inherits the operator's remembered ending
    elif duration and word in TIMED:
        kind = "timer"                        # a duration the caller gave outranks the remembered type
    else:
        kind = spec["until"]
    if kind != "timer" and duration:
        raise PresenceError(f"`{word}` takes no duration")
    new: dict = {"preset": word, "devices": spec["devices"], "follow_after": spec["follow_after"],
                 "lets_through": spec["lets_through"], "ts": ts,
                 "until": {"kind": "timer", "at": ts + parse_duration(duration)} if kind == "timer" else {"kind": "input"}}
    if quote is not None:
        new["by"] = {"agent": presence._clean(by.get("agent"), presence._FIELD_MAX),
                     "session": presence._clean(by.get("session"), presence._FIELD_MAX), "role": by["role"]}
        new["said"] = quote
        new["verified"] = ev[0]
    return "write", new, note


def _remember(queue_dir: Path | str, word: str, changed: dict) -> None:
    """Merge ``changed`` into the options ``word`` carries and write them (only the differences from the table stay). The caller
    holds the presence lock; a file that cannot be read is refused, never replaced."""
    custom, fault = _load_presets(queue_dir)
    if fault:
        raise PresenceError(f"presets.json is there but cannot be read ({fault}): nothing was changed; repair it, "
                            "or move it aside (no file is the system defaults)")
    opts = _prune(word, {**custom.get(word, {}), **changed})
    if opts:
        custom[word] = opts
    else:
        custom.pop(word, None)
    _write_presets(queue_dir, custom)


def _forget(queue_dir: Path | str, word: str) -> None:
    custom, fault = _load_presets(queue_dir)
    if fault:
        raise PresenceError(f"presets.json is there but cannot be read ({fault}): nothing was changed; repair it, "
                            "or move it aside (no file is the system defaults)")
    if custom.pop(word, None) is not None:
        _write_presets(queue_dir, custom)


def _preset_edit(queue_dir: Path | str, word: str, changed: dict | None, *, reset: bool, via: str | None, wait: float | None,
                 now: float | None, idle: Callable[[], float | None] | None, precheck: Callable[[], None] | None,
                 after: Callable[[], None] | None) -> dict:
    if word not in PRESET_TABLE:
        raise PresenceError(f"{word!r} is not a preset that carries options (one of: {', '.join(PRESET_TABLE)})")
    label = f"reset {word}" if reset else f"set {word} " + " ".join(sorted(changed or {}))
    with presence._locked(queue_dir, strict=wait is not None or precheck is not None, wait=wait):
        ts = time.time() if now is None else now
        row = {**presence._log_row(ts, presence._clean(label, 60), None, None, None, None),
               **({"via": presence._clean(via, presence._FIELD_MAX)} if via else {})}
        try:
            if precheck is not None:
                precheck()
            rec = _current(queue_dir, ts)
            if reset:
                _forget(queue_dir, word)
            else:
                _remember(queue_dir, word, changed or {})
            ov = rec["overlay"]
            reading, msg_at = _live(queue_dir, ts, idle, rec)
            if ov is not None and ov["preset"] == word and overlay_active(rec, ts, _last_input(ts, reading), msg_at):
                # the preset that stands takes its new options now, keeping when it began and how it ends
                custom, _ = _load_presets(queue_dir)
                spec = _spec(word, custom, rec["base"])
                rec = _materialize(queue_dir, rec, ts)
                new = {**rec["overlay"], **{k: spec[k] for k in ("devices", "follow_after", "lets_through")}}
                _write_record(queue_dir, {"base": rec["base"], "overlay": new})
        except PresenceError as e:
            presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
            raise
        presence._log_best_effort(queue_dir, {**row, "outcome": "set"})
        if after is not None:
            after()
    return view(queue_dir, now=ts, idle=idle)


def set_preset_options(queue_dir: Path | str, preset: str, *, devices: str | None = None, follow_after: float | str | None = None,
                       lets_through: str | None = None, until: str | None = None, via: str | None = None,
                       wait: float | None = None, now: float | None = None, idle: Callable[[], float | None] | None = None,
                       precheck: Callable[[], None] | None = None, after: Callable[[], None] | None = None) -> dict:
    """Change what ``preset`` (away, quiet, sleep, loud) does from now on, and return :func:`view` after. The operator's own
    call; the options stick until :func:`reset_preset`. ``until`` is the ending TYPE (``input`` or ``timer``) only. When the
    preset stands, it takes the new settings now and keeps its start and its end."""
    changed: dict = {}
    if devices is not None:
        changed["devices"] = devices
    if lets_through is not None:
        changed["lets_through"] = lets_through
    if follow_after is not None:
        if isinstance(follow_after, str):
            changed["follow_after"] = _intify(parse_duration(follow_after))
        else:
            changed["follow_after"] = follow_after
    if until is not None:
        changed["until"] = until
    if not changed:
        raise PresenceError("set at least one of devices, follow_after, lets_through or until")
    bad = [k for k, v in changed.items() if not _option_ok(k, v)]
    if bad:
        raise PresenceError(f"not a valid {', '.join(bad)} (devices: {', '.join(DEVICES)}; lets_through: {', '.join(LETS)}; "
                            f"follow_after 1 minute to 24 hours; until: input or timer)")
    return _preset_edit(queue_dir, preset, changed, reset=False, via=via, wait=wait, now=now, idle=idle, precheck=precheck,
                        after=after)


def reset_preset(queue_dir: Path | str, preset: str, *, via: str | None = None, wait: float | None = None,
                 now: float | None = None, idle: Callable[[], float | None] | None = None,
                 precheck: Callable[[], None] | None = None, after: Callable[[], None] | None = None) -> dict:
    """Return ``preset`` to the system defaults (away, quiet, sleep, loud: its stored options go; ``back``: the standing default
    returns to follow after 10 minutes, everything), and return :func:`view` after."""
    if preset == "back":
        return apply_settings(queue_dir, devices=_default_base()["devices"], follow_after=FOLLOW_DEFAULT,
                              lets_through=_default_base()["lets_through"], until="switch", via=via, wait=wait, now=now,
                              idle=idle, precheck=precheck, after=after)
    return _preset_edit(queue_dir, preset, None, reset=True, via=via, wait=wait, now=now, idle=idle, precheck=precheck,
                        after=after)


def apply_settings(queue_dir: Path | str, *, devices: str | None = None, follow_after: float | str | None = None,
                   lets_through: str | None = None, until: object = "switch", via: str | None = None,
                   wait: float | None = None, by: dict | None = None, now: float | None = None,
                   idle: Callable[[], float | None] | None = None, precheck: Callable[[], None] | None = None,
                   after: Callable[[], None] | None = None) -> dict:
    """Set ``devices``, ``follow_after`` (seconds, or text like ``15m``) and/or ``lets_through``; return :func:`view` after.
    Operator only: an agent (``by``) is refused, and the refusal is one log row.

    ``until="switch"`` (the default) writes the BASE (the standing default) and clears the overlay: "until I switch it" is
    exactly what writing base means. ``until="input"`` or a duration writes an overlay with no preset, copying the effective
    settings with the changed fields."""
    word = _settings_word(devices, follow_after, lets_through, until)
    if by is not None:
        who = "the desk" if isinstance(by, dict) and by.get("role") == ROLE_DESK else "a seat"
        msg = (f"the presence settings are the operator's own: {who} may not change them "
               "(the operator sets them himself, from a terminal outside any agent session)")
        with presence._locked(queue_dir, strict=False):
            t = time.time() if now is None else now
            presence._log_best_effort(queue_dir, {**presence._log_row(t, word, None, by, None, None), "outcome": "refused", "why": msg})
        raise PresenceError(msg)
    if devices is None and follow_after is None and lets_through is None:
        raise PresenceError("set at least one of devices, follow_after or lets_through")
    if devices is not None and devices not in DEVICES:
        raise PresenceError(f"devices is one of: {', '.join(DEVICES)}")
    if lets_through is not None and lets_through not in LETS:
        raise PresenceError(f"lets_through is one of: {', '.join(LETS)}")
    follow = None
    if follow_after is not None:
        if isinstance(follow_after, str):
            follow = parse_duration(follow_after)
        elif _num(follow_after) and FOLLOW_MIN <= follow_after <= FOLLOW_MAX:
            follow = float(follow_after)
        else:
            raise PresenceError("follow_after runs from 1 minute to 24 hours")
    strict = wait is not None or precheck is not None
    with presence._locked(queue_dir, strict=strict, wait=wait):
        ts = time.time() if now is None else now   # after the lock: a long wait must not date the write in the past
        refused = lambda e: presence._log_best_effort(   # noqa: E731
            queue_dir, {**presence._log_row(ts, word, None, None, None, None), "outcome": "refused",
                        "why": presence._clean(str(e), presence._WHY_MAX),
                        **({"via": presence._clean(via, presence._FIELD_MAX)} if via else {})})
        if precheck is not None:
            try:
                precheck()
            except PresenceError as e:
                refused(e)
                raise
        try:
            end = _until_arg(until, ts)   # validated before anything is read for writing, so a refusal migrates nothing
            rec = _current(queue_dir, ts)
        except PresenceError as e:
            refused(e)
            raise
        row = presence._log_row(ts, word, None, None, None, _prior_for_log(rec))
        if via:
            row["via"] = presence._clean(via, presence._FIELD_MAX)
        try:
            reading, msg_at = _live(queue_dir, ts, idle, rec)
            standing = rec["overlay"] is not None and overlay_active(rec, ts, _last_input(ts, reading), msg_at)
            cur = ({k: v for k, v in rec["overlay"].items() if k in ("devices", "follow_after", "lets_through")}
                   if standing else {k: rec["base"][k] for k in ("devices", "follow_after", "lets_through")})
            changed = {k: v for k, v in (("devices", devices), ("follow_after", None if follow is None else _intify(follow)),
                                         ("lets_through", lets_through)) if v is not None}
            rec = _materialize(queue_dir, rec, ts)   # validated and planned against the projection: only now migrate
            if end is None:
                base = {**rec["base"], **changed, "ts": ts, "via": via or "cli"}
                new = {"base": base, "overlay": None}
            else:
                new = {"base": rec["base"] if rec["status"] == "file" else _default_base(ts),
                       "overlay": {"preset": None, **cur, **changed, "ts": ts, "until": end, "via": via or "cli"}}
                if end["kind"] == "timer":
                    row["until"] = end["at"]
            _write_record(queue_dir, new)
        except PresenceError as e:
            presence._log_best_effort(queue_dir, {**row, "outcome": "refused", "why": presence._clean(str(e), presence._WHY_MAX)})
            raise
        row["outcome"] = "set"
        presence._log_best_effort(queue_dir, row)
        if after is not None:
            after()
    return view(queue_dir, now=ts, idle=idle)


def _settings_word(devices, follow_after, lets_through, until) -> str:
    parts = []
    if devices is not None:
        parts.append(f"devices {devices}" + (f" {follow_after}" if devices == "follow" and follow_after is not None else ""))
    elif follow_after is not None:
        parts.append(f"follow_after {follow_after}")
    if lets_through is not None:
        parts.append(f"lets {lets_through}")
    word = "set " + " ".join(parts) if parts else "set"
    if until == "input":
        word += " until input"
    elif until not in (None, "switch"):
        word += f" for {until}" if isinstance(until, (str, int, float)) and not isinstance(until, bool) else " for a while"
    return presence._clean(word, 60)


# --------------------------------------------------------------------------------------------------- compat and events
def legacy_word(rec: dict, now: float, last_input_at: float | None, desk_msg_at: float | None) -> dict | None:
    """The overlay as the old ``{word, ts, until?, by?, said?, verified?}`` record, while it stands and is a preset; else
    ``None``. For flow's installed half until flow 4 (one release)."""
    ov = rec.get("overlay")
    if ov is None or ov["preset"] is None or not overlay_active(rec, now, last_input_at, desk_msg_at):
        return None
    out: dict = {"word": ov["preset"], "ts": ov["ts"]}
    if ov["until"]["kind"] == "timer":
        out["until"] = ov["until"]["at"]
    for k in ("by", "said", "verified"):
        if k in ov:
            out[k] = ov[k]
    return out


def from_legacy(word: dict | None, route: str = "desk", now: float = 0.0) -> dict:
    """A :func:`read`-shaped record (``file``) for a pre-1b word and route, used by the deprecated ``presence.decide``."""
    ov = _word_overlay(word, FOLLOW_DEFAULT, now)
    if route == "devices":
        ov = ov or {"preset": None, "devices": "on", "follow_after": FOLLOW_DEFAULT, "lets_through": "everything",
                    "ts": 0.0, "until": {"kind": "timer", "at": now + 1e9}}
    return {"status": "file", "reason": "", "base": _default_base(), "overlay": ov, "projection": None}


def read_changes(queue_dir: Path | str | None, since: float) -> list[dict]:
    """The record's event stream since ``since`` (F3: a record change from any surface wakes the desk): record writes and
    ``ended ...`` rows, oldest first, each ``{ts, word, by, via, outcome, line_after}``. ``line_after`` is the describe line as
    it stands NOW (the rows do not keep a line of their own)."""
    if queue_dir is None:
        return []
    keep = []
    for r in presence.read_log(queue_dir):
        word = str(r.get("word", ""))
        if r["ts"] <= since or r.get("outcome") != "set":
            continue
        if word in WORDS or word.startswith("set ") or word.startswith("ended ") or word in ("migrate", "migrate undo"):
            keep.append(r)
    if not keep:
        return []
    line = describe_line(queue_dir)
    return [{"ts": r["ts"], "word": r["word"], "by": r.get("by"), "via": r.get("via"), "outcome": r["outcome"],
             "line_after": line} for r in keep]


# --------------------------------------------------------------------------------------------------- migration
_OLD_FILES = ("presence.json", "route.json", "policy.json")
_SETTINGS = ("claude_code", "codex")


def _file_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 1 << 16)
    except OSError:
        return None
    finally:
        os.close(fd)


def _word_overlay(word: dict | None, follow_after: int | float, now: float) -> dict | None:
    """The overlay a pre-1b word maps to, or ``None`` (no word, ``back``, or a timed word already past)."""
    if word is None or word["word"] == "back":
        return None
    name = word["word"]
    spec = PRESET_TABLE[name]
    if name in TIMED:
        until = {"kind": "timer", "at": word["until"]}
        if word["until"] <= now:
            return None
    else:
        until = {"kind": "input"}
    ov = {"preset": name, "devices": "on", "follow_after": follow_after, "lets_through": spec["lets_through"],
          "ts": word["ts"], "until": until}
    for k in ("by", "said", "verified"):
        if k in word:
            ov[k] = word[k]
    return ov


def _consent_settings(queue_dir: Path | str) -> dict:
    out = {}
    for name in _SETTINGS:
        raw = _file_bytes(Path(queue_dir) / f"{name}.json")
        if raw is None:
            continue
        try:
            got = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if isinstance(got, dict):
            out[name] = got
    return out


def _project(queue_dir: Path | str, now: float) -> dict | None:
    """The mapping of the old files onto a record, run in memory (DESIGN §6): ``{record, lines, found, files, dropped}``.
    Writes nothing."""
    from levain.webpush import route as route_mod

    d = Path(queue_dir) / "webpush"
    lines: list[str] = []
    files = [n for n in _OLD_FILES if os.path.lexists(d / n)]
    settings = _consent_settings(queue_dir)
    follow = FOLLOW_DEFAULT
    custom = False
    for name, got in settings.items():
        idle = got.get("away_idle_seconds")
        if _num(idle) and idle != FOLLOW_DEFAULT and not isinstance(idle, bool):
            if not custom and FOLLOW_MIN <= idle <= FOLLOW_MAX:
                follow, custom = _intify(idle), True
                lines.append(f"{name}.json: away_idle_seconds {_intify(idle)} becomes follow_after "
                             f"({_mins(follow)}, your own number)")
            else:
                lines.append(f"{name}.json: away_idle_seconds {_intify(idle)} is not used (the one threshold is follow_after)")
    for name, got in settings.items():
        word = got.get("away_word")
        if word not in ("on", "off", "auto"):
            word = "on" if got.get("away_mode") is True else "off"
            legacy = "away_word" not in got
        else:
            legacy = False
        if word == "off" and (legacy or "away_word" not in got):
            lines.append(f"consent away was off for {name}: approvals now follow the record (your devices when the record says so)")
        elif word == "on":
            lines.append(f"consent away was on for {name}: approvals now follow the record; a signed away mark (if you recorded "
                         "one) still stands until you leave it with levain consent away off")
        elif word == "auto":
            lines.append(f"consent away was auto for {name}: approvals now follow the record (follow, after {_mins(follow)})")
    # the word
    word = presence.read_legacy_word(queue_dir)
    if word is None and os.path.lexists(d / "presence.json"):
        lines.append("presence.json could not be read as a word: dropped")
    overlay = _word_overlay(word, follow, now)
    if word is not None and word["word"] in TIMED and overlay is None:
        lines.append(f"{word['word']} had already ended: dropped")
    # the route
    if os.path.lexists(d / "route.json"):
        try:
            rrec = route_mod._load(queue_dir)
        except (ValueError, ArithmeticError) as e:
            rrec = None
            lines.append(f"route.json could not be read ({e}): dropped")
        if rrec is not None:
            until = rrec["until"]
            ended = until is not None and now >= until
            if ended:
                lines.append(f"route {rrec['route']} had already ended: dropped")
            elif rrec["source"] == "auto":
                lines.append("route devices was an automatic suggestion (a remote login): dropped, no automatic flips of the record")
            elif rrec["route"] == "desk":
                lines.append("route desk meant the measurement decides: that is follow, the new default")
            elif overlay is None or overlay["preset"] == "away":
                at = until if until is not None else rrec["ts"] + MAX_SECS
                if overlay is not None:
                    lines.append("route devices outlasted away: kept as one overlay until the route ended (devices on, lets through everything)")
                else:
                    lines.append("route devices becomes an overlay: devices on until it ended")
                overlay = {"preset": None, "devices": "on", "follow_after": follow, "lets_through": "everything",
                           "ts": rrec["ts"], "until": {"kind": "timer", "at": float(at)}, "via": "migrate"}
            else:
                lines.append(f"route devices dropped: the {overlay['preset']} overlay wins")
    if word is not None and overlay is not None and overlay["preset"] == word["word"]:
        lines.append(f"{word['word']} becomes the {word['word']} overlay")
    # the policy
    pol = Path(d / "policy.json")
    if os.path.lexists(pol):
        cells, kind, reasons = presence._read_policy(queue_dir)
        if kind == "loaded" and cells != presence.DEFAULT_CELLS:
            for m in presence.MUTE_WORDS:
                for c in presence.CLASSES:
                    if cells[m][c] != presence.DEFAULT_CELLS[m][c]:
                        lines.append(f"your {m} {'held' if cells[m][c] == 'hold' else 'pushed'} {presence._CLASS_WORDS[c]}; "
                                     f"the {m} preset now {'pushes' if presence.DEFAULT_CELLS[m][c] == 'push' else 'holds'} "
                                     f"them by default (kept in the backup)")
        elif kind == "malformed":
            lines.append("policy.json was malformed (the default had been in effect): dropped")
    record = {"version": VERSION, "base": {"devices": "follow", "follow_after": follow, "lets_through": "everything",
                                           "ts": now, "via": "migrate"}, "overlay": overlay}
    found = bool(files) or custom
    return {"record": record, "lines": lines, "found": found, "files": files, "settings": sorted(settings)}


def migrate(queue_dir: Path | str, *, dry_run: bool = False, now: float | None = None) -> dict:
    """Fold ``presence.json``, ``route.json`` and ``policy.json`` (and the consent away threshold) into ``intent.json``.
    Idempotent (a valid ``intent.json`` already present is a no-op), reversible (:func:`unmigrate`), and a ``dry_run`` writes
    nothing. Returns ``{status, lines, record, line, backup?}``; ``status`` is ``migrated``, ``dry run``, ``already migrated``
    or ``nothing to migrate``."""
    ts = time.time() if now is None else now
    if dry_run:
        return _migrate_report(queue_dir, ts, write=False)
    with presence._locked(queue_dir, strict=True):
        return _migrate_report(queue_dir, ts, write=True, via="cli")


def _migrate_locked(queue_dir: Path | str, ts: float, *, via: str) -> dict:
    return _migrate_report(queue_dir, ts, write=True, via=via)


def _migrate_report(queue_dir: Path | str, ts: float, *, write: bool, via: str = "cli") -> dict:
    cur = read(queue_dir, ts)
    if cur["status"] == "file":
        return {"status": "already migrated", "lines": [], "record": {"base": cur["base"], "overlay": cur["overlay"]},
                "line": describe_line(queue_dir, now=ts)}
    if cur["status"] == "fault":
        raise PresenceError(f"intent.json is there but cannot be read ({cur['reason']}): repair or remove it first")
    proj = _project(queue_dir, ts)
    if proj is None or not proj["found"]:
        return {"status": "nothing to migrate", "lines": [], "record": None, "line": describe_line(queue_dir, now=ts)}
    record = proj["record"]
    sample = {"rec": {"status": "file", "reason": "", "base": record["base"], "overlay": record["overlay"], "projection": None},
              "now": ts, "raw": None, "reading": None, "msg_at": None, "remote": None}
    try:
        live = _sample(queue_dir, ts, None)
        sample = {**live, "rec": sample["rec"]}
    except Exception:  # noqa: BLE001 - the line is a preview; it never blocks the migration
        pass
    st = _state(queue_dir, sample)
    preview = _line(queue_dir, sample, st, approvals=None, held_n=0)
    out = {"status": "migrated" if write else "dry run", "lines": proj["lines"], "record": record, "line": preview,
           "files": proj["files"]}
    if not write:
        return out
    d = Path(queue_dir) / "webpush"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))
    backup = d / BACKUP_DIR / stamp
    n = 1
    while backup.exists():
        n += 1
        backup = d / BACKUP_DIR / f"{stamp}-{n}"
    try:
        os.makedirs(backup, mode=0o700)
    except OSError as e:
        raise PresenceError(f"the backup directory was not made: {e}") from e
    data = json.dumps({"version": VERSION, "base": record["base"], "overlay": record["overlay"]}).encode("utf-8")
    sums = {}
    for name in proj["files"]:
        raw = _file_bytes(d / name)
        sums[name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    receipt = {"at": ts, "from": {"files": sums, "settings": proj["settings"]}, "to": record,
               "intent_sha256": hashlib.sha256(data).hexdigest(), "lines": proj["lines"], "via": via, "undone": False}
    try:
        (backup / "receipt.json").write_text(json.dumps(receipt))
        _write_record(queue_dir, record)
        for name in proj["files"]:
            os.replace(d / name, backup / name)
    except OSError as e:
        raise PresenceError(f"migration did not complete: {e} (levain ping migrate --undo restores the files)") from e
    presence._log_best_effort(queue_dir, {"ts": ts, "word": "migrate", "prior": {}, "outcome": "set", "via": via,
                                          "why": presence._clean("; ".join(proj["lines"]) or "no old settings to carry", presence._WHY_MAX)})
    out["backup"] = str(backup)
    out["line"] = describe_line(queue_dir, now=ts)
    return out


def unmigrate(queue_dir: Path | str, *, force: bool = False, now: float | None = None) -> dict:
    """Restore the files the newest migration moved, byte-identical, and remove ``intent.json``. Refuses when ``intent.json``
    changed since the receipt (``force`` overrides, only when he says), or when a file it would restore has been written
    again. Returns ``{status, restored, backup}``."""
    ts = time.time() if now is None else now
    d = Path(queue_dir) / "webpush"
    with presence._locked(queue_dir, strict=True):
        root = d / BACKUP_DIR
        try:
            cands = sorted((p for p in root.iterdir() if (p / "receipt.json").is_file()), key=lambda p: p.name)
        except OSError:
            cands = []
        pick = None
        for p in reversed(cands):
            try:
                rc = json.loads((p / "receipt.json").read_text())
            except (OSError, ValueError):
                continue
            if rc.get("undone") is not True:
                pick = (p, rc)
                break
        if pick is None:
            return {"status": "nothing to undo", "restored": [], "backup": None}
        backup, rc = pick
        raw = _file_bytes(_path(queue_dir))
        if raw is not None and hashlib.sha256(raw).hexdigest() != rc.get("intent_sha256") and not force:
            raise PresenceError("intent.json changed since the migration: undoing would drop what you set since "
                                "(levain ping migrate --undo --force only when you mean it)")
        names = [n for n in rc["from"]["files"] if (backup / n).exists()]
        clash = [n for n in names if os.path.lexists(d / n)]
        if clash and not force:
            raise PresenceError(f"{', '.join(clash)} was written again since the migration: refusing to overwrite it")
        for n in names:
            os.replace(backup / n, d / n)
        try:
            os.unlink(_path(queue_dir))
        except FileNotFoundError:
            pass
        shutil.rmtree(backup, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass
        presence._log_best_effort(queue_dir, {"ts": ts, "word": "migrate undo", "prior": {}, "outcome": "set", "via": "cli",
                                              "why": f"restored {', '.join(names) or 'nothing'}"})
        return {"status": "undone", "restored": names, "backup": str(backup)}
