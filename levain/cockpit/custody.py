"""levain.cockpit.custody: how a cockpit signing key is held (trust-tiering DESIGN §3.2, rev 6 presence floor).

The verifier cannot tell a touch key from a no-touch one: the Secure Enclave provider macOS ships
(``/usr/lib/ssh-keychain.dylib``) echoes the presence flag it is handed, so a key made with no touch
requirement verifies exactly like one made with it. The touch rests wholly on how the key was MADE, which is
why creation is the load-bearing piece here, and why every enrol record declares its custody.

- :func:`create_touch_id` (macOS) makes a Secure Enclave identity with ``sc_auth create-ctk-identity -k
  p-256-ne -t bio`` (biometric protection is a constant: no flag, environment or hook offers anything else),
  confirms ``sc_auth list-ctk-identities`` shows it with protection ``bio`` before anything else happens,
  fetches its reference file with ``ssh-keygen -K -w /usr/lib/ssh-keychain.dylib`` and keeps the one whose
  public key is the identity's. The identity's hash column is the SHA-1 of the public key's uncompressed
  point (0x04 || X || Y), measured on macOS 26.6.2 against a reference file ``ssh-keygen -K`` wrote.
- :func:`create_passphrase` makes an ed25519 key with ``ssh-keygen``, which asks the passphrase on the
  terminal itself, and refuses the key unless the file is encrypted.
- :func:`passphrase_problem` is that check: the file is an OpenSSH private key of this public key, its
  cipher is not ``none``, and ``ssh-keygen -y -P ""`` cannot open it.
- A key the helper makes gets a SIDECAR (``<key>.levain-key.json``) naming its custody and, for a Secure
  Enclave key, its provider and identity hash. ``levain.cockpit.keys.ssh_signer`` reads it to set
  ``SSH_SK_PROVIDER`` itself; the CLI reads it for the default ``--custody``. The provider must be one this
  module knows: a sidecar never names a library for ssh-keygen to load.

Every tool this module starts is given an explicit environment: ``child_env()`` (the allowlist levain.launch
documents, nothing carried) for ``sc_auth`` and the interactive ``ssh-keygen``, and the fixed ``_VERIFY_ENV``
alone for the ``ssh-keygen`` checks. ``-K`` names its provider on argv, so no call needs ``SSH_SK_PROVIDER``.

Every custody is a DECLARATION the enrolling key signs, never a proof; these checks stop an accidental weak
enrolment on the machine that makes or holds the key, nothing more.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.authority import CUSTODIES, LAPTOP, UNSTATED, custody_problem
from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN
from levain.cockpit.keys import KeyError_, normalise_public_key
from levain.launch import child_env

__all__ = ["APPLE_PROVIDER", "SC_AUTH", "SIDECAR_SUFFIX", "CreatedKey", "Identity", "Sidecar", "create_passphrase",
           "create_touch_id", "ctk_status", "custody_note", "declared_custody", "default_key_dir", "is_encrypted_file",
           "list_identities", "passphrase_problem", "point_sha1", "read_sidecar", "sidecar_path"]

APPLE_PROVIDER = "/usr/lib/ssh-keychain.dylib"
SC_AUTH = Path("/usr/sbin/sc_auth")
SIDECAR_SUFFIX = ".levain-key.json"
SIDECAR_FORMAT = "levain-key/1"
TOUCH_ID, PASSPHRASE = "touch-id", "passphrase"
# the only library a sidecar may name for ssh-keygen to load
_PROVIDERS = frozenset({APPLE_PROVIDER})
_LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SHA1_HEX = re.compile(r"[0-9A-F]{40}")
_FILE_MAX = 64 * 1024
_ARMOR_BEGIN, _ARMOR_END = "-----BEGIN OPENSSH PRIVATE KEY-----", "-----END OPENSSH PRIVATE KEY-----"
_MAGIC = b"openssh-key-v1\x00"
_MAX_FETCH = 32                     # CTK identities the fetch walks before it refuses
_TOOL_TIMEOUT = 60.0
_TYPING_TIMEOUT = 600.0             # ssh-keygen waiting on a person typing a passphrase


@dataclass(frozen=True)
class Sidecar:
    custody: str
    provider: str | None = None
    ctk_hash: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class Identity:
    """One row of ``sc_auth list-ctk-identities``."""
    hash: str          # SHA-1 of the public key's uncompressed point, upper-case hex
    prot: str          # "bio" | "none" | ...
    label: str


@dataclass(frozen=True)
class CreatedKey:
    path: Path               # the key file to pass as --signing-key
    public_path: Path
    public_key: str          # its normal form
    custody: str
    label: str
    ctk_hash: str | None = None


# ---------------------------------------------------------------------------------------------------
# the sidecar


def _private_path(key_path: Path) -> Path:
    key_path = Path(key_path).expanduser()
    return key_path.with_name(key_path.name[:-4]) if key_path.name.endswith(".pub") else key_path


def sidecar_path(key_path: Path) -> Path:
    """``<private key path>.levain-key.json``, for the key file or its ``.pub``."""
    private = _private_path(key_path)
    return private.with_name(private.name + SIDECAR_SUFFIX)


def read_sidecar(key_path: Path) -> Sidecar | None:
    """The sidecar beside ``key_path`` (the key or its ``.pub``), None when there is none; a sidecar that
    is there but malformed, or names a provider this module does not know, refuses."""
    path = sidecar_path(key_path)
    try:
        with path.open("rb") as fh:
            raw = fh.read(_FILE_MAX + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KeyError_(f"cannot read the key's sidecar {path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= _FILE_MAX else None
    except (UnicodeDecodeError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("format") != SIDECAR_FORMAT:
        raise KeyError_(f"{path} is not a {SIDECAR_FORMAT} sidecar")
    custody, provider, ctk_hash, label = (data.get("custody"), data.get("provider"), data.get("ctk_hash"),
                                          data.get("label"))
    if custody not in (TOUCH_ID, PASSPHRASE):
        raise KeyError_(f"{path} names custody {str(custody)[:40]!r}; a sidecar names touch-id or passphrase")
    if provider is not None and provider not in _PROVIDERS:
        raise KeyError_(f"{path} names provider {str(provider)[:80]!r}, which levain does not load "
                        f"(only {', '.join(sorted(_PROVIDERS))})")
    if custody == TOUCH_ID and (provider is None or not isinstance(ctk_hash, str)
                                or not _SHA1_HEX.fullmatch(ctk_hash)):
        raise KeyError_(f"{path}: a touch-id sidecar names its provider and its CTK identity hash")
    if custody == PASSPHRASE and (provider is not None or ctk_hash is not None):
        raise KeyError_(f"{path}: a passphrase sidecar names no provider or identity")
    if label is not None and not isinstance(label, str):
        raise KeyError_(f"{path}: the label is not text")
    return Sidecar(custody, provider, ctk_hash, label)


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_sidecar(key_path: Path, sidecar: Sidecar, made: list[Path]) -> None:
    body: dict[str, Any] = {"format": SIDECAR_FORMAT, "custody": sidecar.custody}
    for name in ("provider", "ctk_hash", "label"):
        if getattr(sidecar, name) is not None:
            body[name] = getattr(sidecar, name)
    _write_new(sidecar_path(key_path), (json.dumps(body, sort_keys=True, indent=1) + "\n").encode(), 0o600)
    made.append(sidecar_path(key_path))


# ---------------------------------------------------------------------------------------------------
# the encrypted-file check (passphrase custody)


def _strings(blob: bytes, count: int) -> tuple[list[bytes], bytes]:
    out = []
    for _ in range(count):
        (n,) = struct.unpack(">I", blob[:4])
        if len(blob) < 4 + n:
            raise ValueError("truncated")
        out.append(blob[4:4 + n])
        blob = blob[4 + n:]
    return out, blob


def _private_header(private: Path) -> tuple[str, bytes] | None:
    """``(cipher name, public key blob)`` of an OpenSSH-format private key file, None when the file is
    not one (or cannot be read)."""
    try:
        with private.open("rb") as fh:
            raw = fh.read(_FILE_MAX + 1)
        text = raw.decode("ascii") if len(raw) <= _FILE_MAX else ""
    except (OSError, UnicodeDecodeError):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0] != _ARMOR_BEGIN or lines[-1] != _ARMOR_END:
        return None
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
        if not blob.startswith(_MAGIC):
            return None
        (cipher, _kdf, _kdfopts), rest = _strings(blob[len(_MAGIC):], 3)
        (nkeys,) = struct.unpack(">I", rest[:4])
        if nkeys != 1:
            return None
        (public,), _ = _strings(rest[4:], 1)
        return cipher.decode("ascii"), public
    except (binascii.Error, struct.error, ValueError, UnicodeDecodeError):
        return None


def is_encrypted_file(key_path: Path) -> bool:
    """Whether ``key_path`` (or the private file beside its ``.pub``) is an OpenSSH private key whose
    cipher is not ``none``."""
    header = _private_header(_private_path(key_path))
    return header is not None and header[0] != "none"


def passphrase_problem(key_path: Path, public_key: str, *,
                       ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> str | None:
    """Why the key file of ``public_key`` at ``key_path`` (the key or its ``.pub``) is not a passphrase
    key, None when it is: the private file must be there (passphrase custody is checked on the file), be
    an OpenSSH private key of exactly this public key, carry a cipher, and refuse ``ssh-keygen -y -P ""``."""
    private = _private_path(key_path)
    if not private.is_file():
        return (f"no private key file at {private}: passphrase custody is checked on the key file, so enrol it "
                "where the file is")
    header = _private_header(private)
    if header is None:
        return f"{private} is not an OpenSSH-format private key"
    cipher, public_blob = header
    if base64.b64encode(public_blob).decode("ascii") != public_key.split()[1]:
        return f"{private} is not the private half of this key"
    if cipher == "none":
        return f"{private} is not encrypted: a passphrase key needs a passphrase (an empty one is refused)"
    try:
        cp = subprocess.run([str(ssh_keygen), "-y", "-P", "", "-f", str(private)], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=_TOOL_TIMEOUT, check=False, start_new_session=True,
                            env=dict(_VERIFY_ENV))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ssh-keygen could not check {private} ({type(exc).__name__})"
    if cp.returncode == 0:
        return f"{private} opens with an empty passphrase"
    return None


# ---------------------------------------------------------------------------------------------------
# CTK identities (touch-id custody, macOS)


def point_sha1(public_key: str) -> str | None:
    """The SHA-1 (upper-case hex) of an ECDSA or sk-ECDSA key's uncompressed point: the hash
    ``sc_auth list-ctk-identities`` prints for its identity. None for any other key."""
    words = public_key.split()
    if len(words) < 2 or words[0] not in ("sk-ecdsa-sha2-nistp256@openssh.com", "ecdsa-sha2-nistp256"):
        return None
    try:
        (_type, _curve, point), _ = _strings(base64.b64decode(words[1], validate=True), 3)
    except (binascii.Error, struct.error, ValueError):
        return None
    return hashlib.sha1(point).hexdigest().upper()


def list_identities(*, sc_auth: Path | str = SC_AUTH) -> list[Identity]:
    """Every CTK identity ``sc_auth list-ctk-identities`` prints. Its output is columns aligned under a
    header line; a row is read at the header's column offsets."""
    try:
        cp = subprocess.run([str(sc_auth), "list-ctk-identities"], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=_TOOL_TIMEOUT, check=False, start_new_session=True, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeyError_(f"sc_auth could not list CTK identities ({type(exc).__name__})") from None
    text = cp.stdout.decode("utf-8", "replace")
    lines = text.splitlines()
    if cp.returncode != 0 or not lines:
        raise KeyError_(f"sc_auth list-ctk-identities failed (exit {cp.returncode}): "
                        f"{cp.stderr.decode('utf-8', 'replace').strip()[:200]}")
    header = lines[0]
    cols = [header.find(name) for name in ("Public Key Hash", "Prot", "Label", "Common Name")]
    if min(cols) < 0 or cols != sorted(cols):
        raise KeyError_(f"sc_auth list-ctk-identities printed a header levain does not read: {header[:120]!r}")
    h, p, lab, cn = cols
    out = []
    for line in lines[1:]:
        if not line.strip():
            continue
        digest, prot, label = line[h:p].strip(), line[p:lab].strip(), line[lab:cn].strip()
        if not _SHA1_HEX.fullmatch(digest):
            raise KeyError_(f"sc_auth list-ctk-identities printed a row levain does not read: {line[:120]!r}")
        out.append(Identity(digest, prot, label))
    return out


def _delete_identity(digest: str, sc_auth: Path | str) -> str | None:
    """Delete one CTK identity (no touch: measured); the reason it could not be, else None."""
    try:
        cp = subprocess.run([str(sc_auth), "delete-ctk-identity", "-h", digest], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=_TOOL_TIMEOUT, check=False, start_new_session=True,
                            env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"sc_auth could not delete CTK identity {digest} ({type(exc).__name__})"
    if cp.returncode != 0:
        return f"sc_auth could not delete CTK identity {digest} (exit {cp.returncode})"
    return None


def ctk_status(public_key: str, *, sc_auth: Path | str = SC_AUTH,
               identities: list[Identity] | None = None) -> tuple[bool | None, str]:
    """A touch-id key's CTK identity on THIS Mac: ``(True, ...)`` present with protection ``bio``,
    ``(False, ...)`` present with any other protection, ``(None, ...)`` not on this Mac (or not a Mac),
    where only the recorded custody can be shown."""
    if sys.platform != "darwin":
        return None, "not a Mac: custody as recorded"
    digest = point_sha1(public_key)
    if digest is None:
        return None, "not a Secure Enclave key shape: custody as recorded"
    if identities is None:
        identities = list_identities(sc_auth=sc_auth)
    found = [i for i in identities if i.hash == digest]
    if not found:
        return None, "no CTK identity for it on this Mac: custody as recorded"
    if all(i.prot == "bio" for i in found):
        return True, "CTK identity on this Mac, protection bio"
    return False, f"its CTK identity on this Mac has protection {', '.join(sorted({i.prot for i in found}))}, not bio"


def custody_note(rows: Any, *, sc_auth: Path | str = SC_AUTH, platform: str | None = None) -> dict[str, str]:
    """For each ``touch-id`` row of a key listing, the re-check of its CTK identity on this Mac (DESIGN
    §3.2 rev 6 item 4): ``{fingerprint: note}``. Elsewhere (not a Mac, no sc_auth) nothing: the listing
    shows the recorded custody only. sc_auth failing is a note, never a failed listing."""
    touch = [r for r in rows if r.custody == TOUCH_ID and r.public_key]
    if not touch or (platform or sys.platform) != "darwin" or not Path(sc_auth).exists():
        return {}
    try:
        identities = list_identities(sc_auth=sc_auth)
    except KeyError_ as exc:
        return {r.fingerprint: f"CTK identity not re-checked ({exc})" for r in touch}
    out = {}
    for r in touch:
        ok, why = ctk_status(r.public_key, identities=identities)
        out[r.fingerprint] = ("WARNING: " if ok is False else "") + why
    return out


# ---------------------------------------------------------------------------------------------------
# the custody a CLI act declares


def declared_custody(key_file: Path, given: str | None, key_class: str, public_key: str, *,
                     ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, sc_auth: Path | str = SC_AUTH,
                     platform: str | None = None) -> str:
    """The custody to declare for the key at ``key_file`` (its private file or its ``.pub``), checked
    before anything is signed: ``given`` (``--custody``), else its sidecar's, else ``unstated``, which
    a laptop-presence key may not be. A ``--custody`` that disagrees with the sidecar refuses. The custody
    table (``levain.authority.custody_problem``) binds; ``passphrase`` gets the encrypted-file check; a
    ``touch-id`` key whose CTK identity is on this Mac must still be ``bio``, and one whose sidecar was
    written here must still have its identity."""
    sidecar = read_sidecar(key_file)
    if given is not None and given not in CUSTODIES:
        raise KeyError_(f"unknown custody {given!r}; one of {', '.join(CUSTODIES)}")
    if given is not None and sidecar is not None and sidecar.custody != given:
        raise KeyError_(f"--custody {given} disagrees with the key's sidecar ({sidecar_path(key_file)} says "
                        f"{sidecar.custody})")
    custody = given or (sidecar.custody if sidecar is not None else None)
    if custody is None:
        if key_class == LAPTOP:
            raise KeyError_(f"a {LAPTOP} key states its custody: pass --custody (touch-id, passphrase, tpm, fido, "
                            "windows-hello), or make the key with `levain cockpit key create`")
        custody = UNSTATED
    normal = normalise_public_key(public_key)
    problem = custody_problem(key_class, custody, normal.split()[0])
    if problem is not None:
        raise KeyError_(problem)
    if custody == PASSPHRASE:
        problem = passphrase_problem(key_file, normal, ssh_keygen=ssh_keygen)
        if problem is not None:
            raise KeyError_(problem)
    if custody == TOUCH_ID and (platform or sys.platform) == "darwin" and Path(sc_auth).exists():
        ok, why = ctk_status(normal, sc_auth=sc_auth)
        if ok is False or (ok is None and sidecar is not None):
            raise KeyError_(f"touch-id custody refused: {why}")
    return custody


# ---------------------------------------------------------------------------------------------------
# `levain cockpit key create`


def default_key_dir() -> Path:
    """``<levain home>/keys``: ``$LEVAIN_HOME/keys`` if set, else ``~/.levain/keys``."""
    home = os.environ.get("LEVAIN_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".levain") / "keys"


def _key_dir(key_dir: Path | None) -> Path:
    d = (Path(key_dir).expanduser() if key_dir is not None else default_key_dir()).absolute()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(d)
    except OSError as exc:
        raise KeyError_(f"cannot make the key directory {d}: {exc.strerror or exc}") from None
    if (st.st_mode & 0o170000) != 0o040000:
        raise KeyError_(f"{d} is not a directory (a link is refused)")
    if st.st_uid != os.getuid():
        raise KeyError_(f"{d} is not owned by this user")
    if st.st_mode & 0o077:
        raise KeyError_(f"{d} is open to other users (mode {st.st_mode & 0o777:o}); chmod 700 it")
    return d


def _check_label(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise KeyError_(f"label {label[:80]!r}: letters, digits, '.', '_' and '-' only, at most 64")
    return label


def _destinations(d: Path, label: str) -> tuple[Path, Path]:
    key, pub = d / label, d / f"{label}.pub"
    for p in (key, pub, sidecar_path(key)):
        if os.path.lexists(p):
            raise KeyError_(f"{p} already exists; choose another --label")
    return key, pub


def _install(src: Path, key: Path, pub: Path, normal: str, label: str, made: list[Path]) -> None:
    """The key and its .pub into place; each path this run creates is added to ``made`` (and only those
    are removed if the run fails)."""
    os.chmod(src, 0o600)
    os.link(src, key)                          # never replaces a file that appeared meanwhile
    made.append(key)
    _write_new(pub, f"{normal} {label}\n".encode("ascii"), 0o644)
    made.append(pub)


def create_touch_id(key_dir: Path | None = None, label: str | None = None, *,
                    sc_auth: Path | str = SC_AUTH, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                    provider: str = APPLE_PROVIDER, platform: str | None = None,
                    stamp: Callable[[], str] | None = None) -> CreatedKey:
    """A Secure Enclave key whose every signature needs Touch ID, in ``key_dir`` (0700, default
    :func:`default_key_dir`). Any failure or interrupt once the create has started deletes the identity this
    run made (attributed by the before/after listing, deleted by hash), and only that one."""
    if (platform or sys.platform) != "darwin":
        raise KeyError_("touch-id custody is macOS only (the Secure Enclave via Touch ID); on this machine "
                        "use --custody passphrase")
    if provider not in _PROVIDERS or not Path(provider).exists():
        raise KeyError_(f"this Mac has no {provider} (macOS 26 ships it); use --custody passphrase")
    if not Path(sc_auth).exists():
        raise KeyError_(f"this Mac has no {sc_auth}; use --custody passphrase")
    label = _check_label(label or f"levain-{(stamp or _stamp)()}")
    d = _key_dir(key_dir)
    key, pub = _destinations(d, label)
    before = list_identities(sc_auth=sc_auth)
    if any(i.label == label for i in before):
        raise KeyError_(f"a CTK identity labelled {label!r} already exists; choose another --label")
    known = {i.hash for i in before}
    # The identity is attributed, never found by label: it is the one digest in after-minus-before that
    # carries the label. Another run with the same label can land between the snapshot and the create; two
    # candidates is a refusal that deletes neither. Cleanup deletes the attributed digest only, by hash.
    ours: Identity | None = None
    unattributed = False         # the create may have made one we have not attributed yet
    tmp: Path | None = None
    made: list[Path] = []
    kept = False

    def cleanup() -> str:
        nonlocal ours, unattributed
        if ours is None and unattributed:
            unattributed = False
            try:
                fresh = [i for i in list_identities(sc_auth=sc_auth) if i.hash not in known and i.label == label]
            except KeyError_ as exc:
                return f"; could not look for the new identity to delete it ({exc})"
            if len(fresh) > 1:
                return (f"; {len(fresh)} new identities labelled {label!r} ({', '.join(i.hash for i in fresh)}): "
                        "cannot tell which is ours, so deleted none (delete yours with "
                        "`sc_auth delete-ctk-identity -h <hash>`)")
            ours = fresh[0] if fresh else None
        if ours is None:
            return ""
        doomed, ours = ours, None
        why = _delete_identity(doomed.hash, sc_auth)
        if why is not None:
            return f"; {why} (delete it with `sc_auth delete-ctk-identity -h {doomed.hash}`)"
        return f"; the identity was deleted ({doomed.hash})"

    try:
        unattributed = True
        try:
            cp = subprocess.run([str(sc_auth), "create-ctk-identity", "-l", label, "-k", "p-256-ne", "-t", "bio"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=_TOOL_TIMEOUT, check=False,
                                start_new_session=True, env=child_env())
        except OSError as exc:
            unattributed = False                       # it never started
            raise KeyError_(f"sc_auth could not create the identity ({type(exc).__name__})") from None
        except subprocess.TimeoutExpired:
            raise KeyError_("sc_auth could not create the identity (TimeoutExpired)") from None
        fresh = [i for i in list_identities(sc_auth=sc_auth) if i.hash not in known and i.label == label]
        if len(fresh) > 1:
            unattributed = False
            raise KeyError_(f"{len(fresh)} new identities labelled {label!r} ({', '.join(i.hash for i in fresh)}): "
                            "another run used the label at the same time; cannot tell which is ours, so deleted "
                            "none (delete yours with `sc_auth delete-ctk-identity -h <hash>`)")
        unattributed = False
        ours = fresh[0] if fresh else None
        if cp.returncode != 0 or ours is None:
            raise KeyError_(f"sc_auth create-ctk-identity did not leave one new identity labelled {label!r} (exit "
                            f"{cp.returncode}, {len(fresh)} found): "
                            f"{cp.stderr.decode('utf-8', 'replace').strip()[:200]}")
        ident = ours
        if ident.prot != "bio":
            raise KeyError_(f"the identity {ident.hash} was created with protection {ident.prot!r}, not bio; "
                            "nothing is enrolled")
        tmp = Path(tempfile.mkdtemp(prefix=".create-", dir=d))
        refs = _fetch_references(tmp, ssh_keygen=ssh_keygen, provider=provider)
        matches = [(path, normal) for path, normal in refs if point_sha1(normal) == ident.hash]
        if len(matches) != 1:
            raise KeyError_(f"ssh-keygen -K fetched {len(refs)} reference key(s), {len(matches)} of them the new "
                            f"identity {ident.hash}; exactly one must be")
        src, normal = matches[0]
        _install(src, key, pub, normal, label, made)
        _write_sidecar(key, Sidecar(TOUCH_ID, provider, ident.hash, label), made)
        kept = True
    except KeyError_ as exc:
        raise KeyError_(f"{exc}" + cleanup()) from None
    except Exception as exc:  # noqa: BLE001 - any failure after the create started deletes ours
        raise KeyError_(f"key create failed ({type(exc).__name__}: {exc})" + cleanup()) from None
    except BaseException:
        cleanup()                          # Ctrl-C: still never leave our identity behind
        raise
    finally:
        if not kept:
            for path in made:
                path.unlink(missing_ok=True)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return CreatedKey(key, pub, normal, TOUCH_ID, label, ident.hash)


def _fetch_references(tmp: Path, *, ssh_keygen: Path | str, provider: str) -> list[tuple[Path, str]]:
    """Every reference key ``ssh-keygen -K -w <provider>`` downloads, as ``(file, normal public key)``.

    ``-K`` names each file ``id_ecdsa_sk_rk`` and asks ``Overwrite (y/n)?`` (on stdin) before the second;
    an answer it cannot read ends the run. Its first stdin line is read as the authenticator PIN (stdin is
    not a terminal: the session is new), which the Secure Enclave provider ignores. So the run in
    directory k answers k overwrites and stops, leaving the (k+1)-th key; the walk ends when a run exits 0
    (it reached the last key). Measured on macOS 26.6.2 with two identities; no touch is asked."""
    out: list[tuple[Path, str]] = []
    for k in range(_MAX_FETCH):
        sub = tmp / f"k{k}"
        sub.mkdir(mode=0o700)
        try:
            cp = subprocess.run([str(ssh_keygen), "-K", "-w", provider, "-N", ""], input=("\n" + "y\n" * k).encode(),
                                cwd=sub, capture_output=True, timeout=_TOOL_TIMEOUT, check=False,
                                start_new_session=True, env=dict(_VERIFY_ENV))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyError_(f"ssh-keygen -K could not run ({type(exc).__name__})") from None
        pubs = sorted(sub.glob("*.pub"))
        said = (cp.stdout + cp.stderr).decode("utf-8", "replace")
        if len(pubs) != 1:
            if k and not pubs:
                break
            raise KeyError_(f"ssh-keygen -K left {len(pubs)} reference keys (exit {cp.returncode}): {said.strip()[:200]}")
        try:
            normal = normalise_public_key(pubs[0].read_text("ascii"))
        except (OSError, UnicodeDecodeError) as exc:
            raise KeyError_(f"cannot read the fetched public key ({type(exc).__name__})") from None
        out.append((pubs[0].with_name(pubs[0].name[:-4]), normal))
        if cp.returncode == 0:
            return out
        if "already exists" not in said:
            raise KeyError_(f"ssh-keygen -K failed (exit {cp.returncode}): {said.strip()[:200]}")
    else:
        raise KeyError_(f"more than {_MAX_FETCH} CTK identities on this Mac; ssh-keygen -K cannot be walked")
    return out


def create_passphrase(key_dir: Path | None = None, label: str | None = None, *,
                      ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, interactive: bool | None = None,
                      stamp: Callable[[], str] | None = None) -> CreatedKey:
    """An ed25519 key encrypted with a passphrase ssh-keygen asks on the terminal (never on argv, never
    seen by levain), in ``key_dir``. An empty passphrase leaves the file unencrypted, which refuses and
    deletes it."""
    at_terminal = sys.stdin is not None and sys.stdin.isatty() if interactive is None else interactive
    if not at_terminal:
        raise KeyError_("passphrase custody asks the passphrase on the terminal: run this at one")
    label = _check_label(label or f"levain-{(stamp or _stamp)()}")
    d = _key_dir(key_dir)
    key, pub = _destinations(d, label)
    tmp = Path(tempfile.mkdtemp(prefix=".create-", dir=d))
    made: list[Path] = []
    kept = False
    try:
        src = tmp / label
        try:
            # ssh-keygen asks the passphrase (twice) on the terminal itself: it never reaches levain or argv
            cp = subprocess.run([str(ssh_keygen), "-t", "ed25519", "-C", label, "-f", str(src)],
                                timeout=_TYPING_TIMEOUT, check=False, env=child_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyError_(f"ssh-keygen could not make the key ({type(exc).__name__})") from None
        if cp.returncode != 0 or not src.is_file():
            raise KeyError_(f"ssh-keygen did not make the key (exit {cp.returncode})")
        try:
            normal = normalise_public_key((tmp / f"{label}.pub").read_text("ascii"))
        except (OSError, UnicodeDecodeError) as exc:
            raise KeyError_(f"cannot read the new public key ({type(exc).__name__})") from None
        problem = passphrase_problem(src, normal, ssh_keygen=ssh_keygen)
        if problem is not None:
            raise KeyError_(f"{problem}; the key was not kept")
        try:
            _install(src, key, pub, normal, label, made)
            _write_sidecar(key, Sidecar(PASSPHRASE, label=label), made)
        except OSError as exc:
            raise KeyError_(f"cannot put the key in place: {exc.strerror or exc}") from None
        kept = True
    finally:
        if not kept:
            for path in made:
                path.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return CreatedKey(key, pub, normal, PASSPHRASE, label)


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")
