"""levain.consent.sign_setting — whether a caller files its operator's answers as passkey sign requests at all.

Ruled 2026-10-20 (Phill, at the desk, on the signed band, spore-1439): *"if it causes friction I am going to drop that
shit faster than a hot coal with sharp teeth and a biting problem AND make it something that you can opt out of in
levain"*, then *"1d"*: only answers that AUTHORISE something are signed; the setting is off | authority-only | all.

The setting lives in the queue directory a caller files sign requests into, as ``<queue dir>/signed-answers.json``::

    {"signed_answers": "off" | "authority-only" | "all"}

and a caller reads it before filing (:func:`read_setting`, or ``levain consent signed-answers --json``):

  - ``off``: file nothing; the caller answers the way it did before sign requests existed;
  - ``authority-only``: file a sign request only for an answer that authorises an act (a release, a merge, a
    security claim's words, an outward act); every other answer stays a plain tap;
  - ``all``: file every answer that has options.

levain itself files nothing and enforces nothing here: what counts as authorising is the caller's to say
(:func:`should_sign`). The setting is not a security bound: any process of the operator's uid can rewrite it, and the
most it can do is turn signing off (answers fall back to the caller's unsigned path) or on (more passkey prompts).

A file that is missing reads as :data:`DEFAULT`; a file that is there and cannot be read or is malformed reads as
:data:`DEFAULT` too, with a note saying why, never as ``off``.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

__all__ = ["DEFAULT", "SETTING_NAME", "VALUES", "read_setting", "should_sign", "write_setting"]

SETTING_NAME = "signed-answers.json"
VALUES = ("off", "authority-only", "all")
DEFAULT = "authority-only"
_MAX = 4096


def read_setting(queue_dir: Path | str) -> "tuple[str, str, str]":
    """``(value, source, note)``: ``source`` is ``file`` or ``default``; ``note`` says why a file there was not used,
    else ``""``."""
    path = Path(queue_dir) / SETTING_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return DEFAULT, "default", ""
    except OSError as exc:
        return DEFAULT, "default", f"{path} cannot be read ({exc.strerror or exc}); the default stands"
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_MAX + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= _MAX else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        data = None
    value = data.get("signed_answers") if isinstance(data, dict) and set(data) == {"signed_answers"} else None
    if value not in VALUES:
        return DEFAULT, "default", (f'{path} is not {{"signed_answers": one of {", ".join(VALUES)}}}; '
                                    "the default stands")
    return value, "file", ""


def write_setting(queue_dir: Path | str, value: str) -> Path:
    """``value`` into ``<queue dir>/signed-answers.json``, whole (a temporary file, then a rename), mode 0600."""
    if value not in VALUES:
        raise ValueError(f"signed answers is one of {', '.join(VALUES)}, not {str(value)[:40]!r}")
    path = Path(queue_dir) / SETTING_NAME
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"signed_answers": value}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def should_sign(value: str, authorises: bool) -> bool:
    """Whether a caller files an answer as a sign request under setting ``value``: ``authorises`` is the caller's own
    word that the answer authorises an act."""
    return value == "all" or (value == "authority-only" and authorises)


def add_parser(cs_sub: argparse._SubParsersAction) -> None:
    """``levain consent signed-answers`` under the ``consent`` subparsers ``cs_sub``."""
    p = cs_sub.add_parser(
        "signed-answers", help="Show or set whether answers are filed as passkey sign requests.",
        description=f"off: nothing is filed, answers stay plain taps. authority-only: only an answer "
                    "that authorises an act (a release, a merge, a security claim's words, an outward act) is "
                    "filed as a sign request, signed on the phone with the passkey. all: every answer with options "
                    f"is. Default: {DEFAULT}. A caller reads this before filing; levain files nothing itself. Kept "
                    f"in the queue dir's {SETTING_NAME}.")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    p.add_argument("--set", dest="value", choices=VALUES, default=None, help="Set it (else it is shown).")
    p.add_argument("--json", action="store_true", help='Print {"signed_answers", "source", "note"} as JSON.')
    p.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace) -> int:
    import sys

    from levain.consent.queue import default_queue_dir

    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    if args.value is not None:
        try:
            write_setting(qdir, args.value)
        except OSError as exc:
            print(f"levain consent signed-answers: cannot write {qdir / SETTING_NAME}: {exc.strerror or exc}",
                  file=sys.stderr)
            return 1
    value, source, note = read_setting(qdir)
    if args.json:
        print(json.dumps({"signed_answers": value, "source": source, "note": note}))
    else:
        print(f"signed answers: {value} ({'set in ' + str(qdir / SETTING_NAME) if source == 'file' else 'the default'})"
              + (f"; {note}" if note else ""))
    return 0
