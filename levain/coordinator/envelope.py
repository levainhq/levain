"""levain.coordinator.envelope — a thread envelope's body, its digest and the operator's signature over it (C2).

An ENVELOPE is the one authority the coordinator design adds (C0 §4; Phill 10-19: *"sign the envelope not the plan"*):
WHERE work may happen (repos and paths), WHICH irreversible effects are granted (lands, pushes, publishes), a BUDGET
and a STOP time. The plan is information and is re-plannable inside it; only a WIDER envelope needs a new signature.

This module is pure: the body's exact shape (:func:`parse_body`), its digest (:func:`envelope_digest`, built the way
a dispatch contract's digest is), the payload a signer signs (:func:`sign_payload`) and the one signature check
(:func:`refusal`, in :func:`levain.dispatch.sign.refusal`'s order). The store side (acceptance in one transaction,
binding to thread revisions, narrowing, liveness) is :mod:`levain.coordinator.envelopes`.

A body is accepted only as its own canonical encoding (``levain.contracts.canon.canonical``), so the bytes a phone
renders, the bytes a CLI signs and the bytes the store re-derives a digest from are one string.

WHY T2 is the signing floor: a Face ID passkey covers T2 and never T3, and an envelope signed with one may land only
where the landing tier is T2 (C0 §5 row C3: the ceiling is the SIGNER's class, recorded at acceptance). A body is not
re-checked against the disk here: repo existence and branch shape are the store's checks at acceptance (they read
git), and everything is re-resolved at use. Stdlib only.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.authority import PASSKEY, AuthoritySnapshot, verify_link_sig
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import class_covers
from levain.contracts.canon import canonical, canonical_bytes, parse
from levain.dispatch.registry import CRITERION_INT_MAX, CRITERION_KEYS

__all__ = [
    "BODY_FORMAT", "BODY_KEYS", "BODY_MAX", "BUDGET_KEYS", "BUDGET_MAX", "EFFECT_LISTS", "EnvelopeError",
    "PAYLOAD_KEYS", "SIGN_FORMAT", "SIGN_NAMESPACE", "SIGN_TIER", "effect_identity", "envelope_digest",
    "parse_body", "refusal", "sign_message", "sign_payload",
]

BODY_FORMAT = "levain-thread-envelope/1"
SIGN_FORMAT = "levain-thread-envelope-sign/1"
SIGN_NAMESPACE = "levain-thread-envelope"
SIGN_TIER = "T2"

BODY_KEYS = frozenset({"format", "store_id", "envelope_id", "origin", "drop_id", "words_sha256", "binds",
                       "supersedes", "where", "lands", "pushes", "publishes", "budget", "stop_at", "issued_at"})
PAYLOAD_KEYS = frozenset({"format", "store_id", "envelope_id", "digest", "signer_fp", "signed_at"})
BUDGET_KEYS = ("tool_calls", "tokens", "dollars_cents", "hours")
EFFECT_LISTS = ("lands", "pushes", "publishes")
ORIGINS = ("structured", "worded")
ONTO_FROM = ("drop", "picker", "inferred")

BODY_MAX = 64 * 1024
FIELD_MAX = 256
SIGNATURE_MAX = 64 * 1024
_LIST_MAX = 64
_PATH_MAX = 4096
# a bound on every budget count, so a typo cannot sign an effectively unbounded gauge [seat]
BUDGET_MAX = {"tool_calls": 100_000, "tokens": 1_000_000_000, "dollars_cents": 1_000_000, "hours": 24}

_ENVELOPE_ID = re.compile(r"ev-[0-9a-f]{32}")
_DROP_ID = re.compile(r"dr-[0-9a-f]{32}")
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_STORE_ID = re.compile(r"[0-9a-f]{32}")
_PACKAGE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]{0,63}")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class EnvelopeError(ValueError):
    """A body or payload that is not an envelope this reader accepts; the message is the line shown to the human."""


def _text(value: Any, name: str, high: int = FIELD_MAX) -> str:
    if not isinstance(value, str) or not value or len(value) > high:
        raise EnvelopeError(f"{name} is text of 1 to {high} characters")
    if unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise EnvelopeError(f"{name} is one line of plain text")
    return value


def _stamp(value: Any, name: str) -> str:
    _text(value, name)
    try:
        parsed = parse_stamp(value)
    except ConsentError:
        raise EnvelopeError(f"{name} is a UTC timestamp") from None
    if utc_stamp(parsed) != value:
        raise EnvelopeError(f"{name} is written as {utc_stamp(parsed)}, the one form a stamp takes")
    return value


def _list(value: Any, name: str, *, empty: bool = True) -> list[Any]:
    if not isinstance(value, list) or len(value) > _LIST_MAX or (not empty and not value):
        raise EnvelopeError(f"{name} is a list of {'0' if empty else '1'} to {_LIST_MAX} entries")
    return value


def _exact(value: Any, keys: frozenset[str] | set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EnvelopeError(f"{name} has exactly the keys {sorted(keys)}")
    return value


def _repo(value: Any, name: str) -> str:
    repo = _text(value, name, _PATH_MAX)
    # an absolute path in its normal form; that it is a repo's real path is the store's check (it reads the disk)
    if not repo.startswith("/") or str(Path(repo)) != repo or "/../" in repo + "/" or "/./" in repo + "/":
        raise EnvelopeError(f"{name} {repo[:120]!r} is an absolute path in its normal form")
    return repo


def _rel_path(value: Any, name: str) -> str:
    p = _text(value, name, _PATH_MAX)
    if p == ".":
        return p
    if unicodedata.normalize("NFC", p) != p:
        raise EnvelopeError(f"{name} {p[:80]!r} is not in Unicode NFC form")
    if p.startswith("/") or "\\" in p:
        raise EnvelopeError(f"{name} {p[:80]!r} is relative to its repo, with forward slashes")
    parts = p[:-1].split("/") if p.endswith("/") else p.split("/")
    if any(s in ("", ".", "..") for s in parts) or parts[0] == ".git":
        raise EnvelopeError(f"{name} {p[:80]!r} has an empty, '.', '..' or .git segment")
    return p


def path_within(path: str, allowed: tuple[str, ...]) -> bool:
    """True when the repo-relative file or directory ``path`` lies inside one of ``allowed`` (``"."`` is the whole
    repo; ``dir/`` a directory; anything else one exact file). Byte for byte, never casefolded (C0 §4.3)."""
    for a in allowed:
        if a == "." or path == a or (a.endswith("/") and path.startswith(a)):
            return True
    return False


def _where(value: Any) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for i, w in enumerate(_list(value, "where", empty=False)):
        w = _exact(w, {"repo", "paths"}, f"where[{i}]")
        repo = _repo(w["repo"], f"where[{i}].repo")
        if repo in seen:
            raise EnvelopeError(f"where names {repo[:120]!r} twice")
        seen.add(repo)
        paths = [_rel_path(p, f"where[{i}].paths") for p in _list(w["paths"], f"where[{i}].paths", empty=False)]
        if len(set(paths)) != len(paths):
            raise EnvelopeError(f"where[{i}].paths names a path twice")
        if "." in paths and paths != ["."]:
            raise EnvelopeError(f"where[{i}].paths is [\".\"] (the whole repo) or a list of paths, never both")
        out.append({"repo": repo, "paths": paths})
    return out


def _criteria(value: Any, name: str, scope: tuple[str, ...]) -> list[dict[str, Any]]:
    # F11 §5's closed vocabulary, minus "onto" (a land names its own branch) and with no required bound: RULED by
    # §11, "B/C agree with your take here" (C0 §4.3). A paths criterion lies inside the land repo's where
    out = []
    seen: set[str] = set()
    for c in _list(value, name):
        if not isinstance(c, dict) or len(c) != 1:
            raise EnvelopeError(f"a criterion in {name} is an object with exactly one key (free text is checked by "
                                "nothing, so it is refused)")
        (key, v), = c.items()
        if key not in CRITERION_KEYS or key == "onto":
            raise EnvelopeError(f"criterion {str(key)[:64]!r} in {name} is not one of "
                                f"{[k for k in CRITERION_KEYS if k != 'onto']}")
        if key in seen:
            raise EnvelopeError(f"criterion {key} is named twice in {name}")
        seen.add(key)
        if key == "paths":
            ps = [_rel_path(p, f"{name} paths") for p in _list(v, f"{name} paths", empty=False)]
            for p in ps:
                if p == "." or not path_within(p, scope):
                    raise EnvelopeError(f"criterion paths in {name} names {p[:80]!r}, outside its repo's where")
        elif key == "no_new_files":
            if v is not True:
                raise EnvelopeError(f"criterion no_new_files in {name} is true")
        else:
            high = CRITERION_INT_MAX[key]
            if type(v) is not int or not 1 <= v <= high:
                raise EnvelopeError(f"criterion {key} in {name} is an integer from 1 to {high}")
        out.append({key: v})
    return out


def parse_body(text: Any) -> dict[str, Any]:
    """The envelope body in ``text`` (its canonical JSON), every field checked for shape; raises
    :class:`EnvelopeError` with the first line that fails. Shape only: the store checks it against itself."""
    if not isinstance(text, str) or not text or len(text.encode("utf-8", "surrogatepass")) > BODY_MAX:
        raise EnvelopeError(f"an envelope body is canonical JSON of at most {BODY_MAX} bytes")
    try:
        body = parse(text.encode("utf-8"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise EnvelopeError(f"an envelope body is canonical JSON: {str(exc)[:160]}") from None
    if not isinstance(body, dict) or set(body) != BODY_KEYS:
        raise EnvelopeError(f"an envelope body has exactly the keys {sorted(BODY_KEYS)}")
    if canonical(body) != text:
        raise EnvelopeError("an envelope body is accepted only as its own canonical encoding")
    if body["format"] != BODY_FORMAT:
        raise EnvelopeError(f"envelope format {str(body['format'])[:64]!r} is not {BODY_FORMAT!r}")
    if not isinstance(body["store_id"], str) or _STORE_ID.fullmatch(body["store_id"]) is None:
        raise EnvelopeError("store_id is the dispatch store's 32-hex id")
    if not isinstance(body["envelope_id"], str) or _ENVELOPE_ID.fullmatch(body["envelope_id"]) is None:
        raise EnvelopeError("envelope_id is ev- and 32 hex")
    if body["origin"] not in ORIGINS:
        raise EnvelopeError(f"origin is one of {list(ORIGINS)}")
    if not isinstance(body["drop_id"], str) or _DROP_ID.fullmatch(body["drop_id"]) is None:
        raise EnvelopeError("drop_id is dr- and 32 hex")
    if not isinstance(body["words_sha256"], str) or _SHA.fullmatch(body["words_sha256"]) is None:
        raise EnvelopeError("words_sha256 is sha256: and 64 hex")
    binds = _list(body["binds"], "binds")
    for i, b in enumerate(binds):
        b = _exact(b, {"thread_id", "rev", "members_sha256"}, f"binds[{i}]")
        if not isinstance(b["thread_id"], str) or _THREAD_ID.fullmatch(b["thread_id"]) is None:
            raise EnvelopeError(f"binds[{i}].thread_id is th- and 32 hex")
        if type(b["rev"]) is not int or not 1 <= b["rev"] <= 1_000_000:
            raise EnvelopeError(f"binds[{i}].rev is a revision number")
        if not isinstance(b["members_sha256"], str) or _SHA.fullmatch(b["members_sha256"]) is None:
            raise EnvelopeError(f"binds[{i}].members_sha256 is sha256: and 64 hex")
    if len({b["thread_id"] for b in binds}) != len(binds):
        raise EnvelopeError("binds names a thread twice")
    # a structured envelope binds mechanically, at acceptance, to its own drop's revision 1 (C0 §4.3); a worded one
    # names the exact revisions its echo covered
    if body["origin"] == "structured" and binds:
        raise EnvelopeError("a structured envelope names no binds (it binds to its own drop's thread)")
    if body["origin"] == "structured" and body["supersedes"] is not None:
        raise EnvelopeError("a structured envelope is a new drop's first envelope; it supersedes nothing")
    if body["origin"] == "worded" and not binds:
        raise EnvelopeError("a worded envelope binds the thread revisions its echo covered")
    sup = body["supersedes"]
    if sup is not None:
        sup = _exact(sup, {"envelope_id", "digest"}, "supersedes")
        if not isinstance(sup["envelope_id"], str) or _ENVELOPE_ID.fullmatch(sup["envelope_id"]) is None \
                or sup["envelope_id"] == body["envelope_id"]:
            raise EnvelopeError("supersedes.envelope_id is another envelope's id")
        if not isinstance(sup["digest"], str) or _SHA.fullmatch(sup["digest"]) is None:
            raise EnvelopeError("supersedes.digest is sha256: and 64 hex")
    where = _where(body["where"])
    scope = {w["repo"]: tuple(w["paths"]) for w in where}
    lands = _list(body["lands"], "lands")
    for i, land in enumerate(lands):
        land = _exact(land, {"repo", "onto", "from", "onto_from", "criteria"}, f"lands[{i}]")
        repo = _repo(land["repo"], f"lands[{i}].repo")
        if repo not in scope:
            raise EnvelopeError(f"lands[{i}].repo is not one of the envelope's where repos")
        onto = _text(land["onto"], f"lands[{i}].onto")
        if onto.startswith(("refs/", "-")) or onto.casefold().startswith("levain/"):
            raise EnvelopeError(f"lands[{i}].onto is a bare branch name outside levain/")
        if land["from"] == "source":
            if land["onto_from"] not in ("drop", "picker"):
                raise EnvelopeError(f"lands[{i}] from source names the branch the drop was made from (drop or "
                                    "picker)")
        elif land["from"] == "named":
            if land["onto_from"] != "inferred":
                raise EnvelopeError(f"lands[{i}] from named is the coordinator's reading, onto_from inferred")
        else:
            raise EnvelopeError(f"lands[{i}].from is source or named")
        _criteria(land["criteria"], f"lands[{i}].criteria", scope[repo])
    if len({(x["repo"], x["onto"]) for x in lands}) != len(lands):
        raise EnvelopeError("lands names one repo and branch twice")
    for i, p in enumerate(_list(body["pushes"], "pushes")):
        p = _exact(p, {"repo", "remote", "url", "branch"}, f"pushes[{i}]")
        if _repo(p["repo"], f"pushes[{i}].repo") not in scope:
            raise EnvelopeError(f"pushes[{i}].repo is not one of the envelope's where repos")
        if not isinstance(p["remote"], str) or _REMOTE.fullmatch(p["remote"]) is None:
            raise EnvelopeError(f"pushes[{i}].remote is a remote's name")
        _text(p["url"], f"pushes[{i}].url", 1024)
        branch = _text(p["branch"], f"pushes[{i}].branch")
        if branch.startswith(("refs/", "-")):
            raise EnvelopeError(f"pushes[{i}].branch is a bare branch name")
    for i, p in enumerate(_list(body["publishes"], "publishes")):
        p = _exact(p, {"package", "index", "version"}, f"publishes[{i}]")
        if not isinstance(p["package"], str) or _PACKAGE.fullmatch(p["package"]) is None:
            raise EnvelopeError(f"publishes[{i}].package is a package name")
        _text(p["index"], f"publishes[{i}].index", 1024)
        if not isinstance(p["version"], str) or _VERSION.fullmatch(p["version"]) is None:
            raise EnvelopeError(f"publishes[{i}].version is one exact version")
    budget = _exact(body["budget"], set(BUDGET_KEYS), "budget")
    for k in BUDGET_KEYS:
        if type(budget[k]) is not int or not 0 <= budget[k] <= BUDGET_MAX[k]:
            raise EnvelopeError(f"budget.{k} is an integer from 0 to {BUDGET_MAX[k]}")
    issued = parse_stamp(_stamp(body["issued_at"], "issued_at"))
    if parse_stamp(_stamp(body["stop_at"], "stop_at")) <= issued:
        raise EnvelopeError("stop_at is after issued_at")
    return body


def envelope_digest(body: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over the body's canonical bytes: the one digest the CLI, the phone page and every check use."""
    return "sha256:" + hashlib.sha256(canonical_bytes(dict(body))).hexdigest()


def effect_identity(kind: str, effect: Mapping[str, Any]) -> str:
    """An effect's exact identity inside an envelope (C0 §4.4): its list and the digest of its canonical object, so a
    narrowing names exactly one effect the signed body holds."""
    if kind not in EFFECT_LISTS:
        raise EnvelopeError(f"an effect is one of {list(EFFECT_LISTS)}")
    return f"{kind}:sha256:" + hashlib.sha256(canonical_bytes(dict(effect))).hexdigest()


def sign_payload(body: Mapping[str, Any], *, signer_fp: str, signed_at: datetime) -> dict[str, str]:
    """The payload a signer signs for ``body``: its bytes are :func:`sign_message`'s, in :data:`SIGN_NAMESPACE`."""
    return {"format": SIGN_FORMAT, "store_id": body["store_id"], "envelope_id": body["envelope_id"],
            "digest": envelope_digest(body), "signer_fp": signer_fp, "signed_at": utc_stamp(signed_at)}


def sign_message(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes signed in :data:`SIGN_NAMESPACE`: the payload's canonical JSON."""
    return canonical_bytes(dict(payload))


def refusal(payload: Any, signature: Any, authority: AuthoritySnapshot, body: Mapping[str, Any], *,
            ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not the operator's signature over ``body`` under ``authority``; ``None`` when it
    verifies. Times are the store's check (it holds the clock). Raises :class:`levain.authority.VerifierUnavailable`
    when the verifier cannot run: a fault on this machine is never a verdict on the signature."""
    if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
        return "the signature payload is not an envelope signature record"
    if not all(isinstance(payload[k], str) for k in PAYLOAD_KEYS):
        return "every field of an envelope signature record is text"
    if any(len(payload[k]) > FIELD_MAX for k in PAYLOAD_KEYS):
        return f"a field of the envelope signature record is over {FIELD_MAX} characters"
    if payload["format"] != SIGN_FORMAT:
        return f"signature format {payload['format'][:64]!r} is not {SIGN_FORMAT!r}"
    if payload["store_id"] != body["store_id"]:
        return "the signature is for another dispatch store"
    if payload["envelope_id"] != body["envelope_id"]:
        return "the signature is for another envelope"
    if payload["digest"] != envelope_digest(body):
        return "the signature is over another envelope digest"
    try:
        parse_stamp(payload["signed_at"])
    except ConsentError:
        return "the signature's signed_at is not a timestamp"
    if not isinstance(authority, AuthoritySnapshot):
        return "no authority snapshot to verify the signer against"
    key = authority.key(payload["signer_fp"])
    if key is None:
        return "the signer is not an enrolled key"
    if not key.active:
        return "the signer's key is revoked"
    if not class_covers(key.key_class, SIGN_TIER):
        return f"key class {key.key_class} does not cover {SIGN_TIER}"
    if not isinstance(signature, str) or not signature:
        return "the envelope signature is empty"
    if len(signature) > SIGNATURE_MAX:
        return f"the envelope signature is over {SIGNATURE_MAX} characters"
    if key.custody == PASSKEY:
        # one rule for a passkey on every path: the consent path's WebAuthn checks (user verification, origin)
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, key.public_key)
        if problem is not None:
            return problem
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    ok = verify_link_sig(sign_message(payload), signature, public_key=key.public_key, fingerprint=key.fingerprint,
                         namespace=SIGN_NAMESPACE, **kwargs)
    return None if ok else "the envelope signature does not verify"
