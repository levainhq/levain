"""Linux: the slots' own copy of bubblewrap (CONTRACT §1.2 ``levain-slots`` row; §9.2 lists it in the
root view), ``/usr/libexec/levain/bwrap``, root : ``levain-slots`` 0750, copied from the system's.

Where the kernel restricts unprivileged user namespaces through AppArmor
(``kernel.apparmor_restrict_unprivileged_userns`` = 1), the copy cannot map its uid without a profile
granting it ``userns`` (R-L2). Setup DETECTS that by reading the sysctl (:func:`userns_restricted`,
the read injected for tests) and renders and loads the profile only then.

The profile is ``levain-slot-bwrap``: hands' floor already installs a profile named ``levain-bwrap``
(for the system bwrap), and ours must never replace it. Undo unloads and removes only ours.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from levain.firing.hands import Step, _abs

if TYPE_CHECKING:
    from levain.slot.removal import Live

LEVAIN_BWRAP = Path("/usr/libexec/levain/bwrap")
#: The digests setup recorded (CONTRACT §23), beside the retired-ids file (§1.1's Linux dir).
BWRAP_DIGEST = Path("/var/lib/levain/bwrap.sha256")
BWRAP_GROUP = "levain-slots"
BWRAP_MODE = 0o750
APPARMOR_PROFILE_NAME = "levain-slot-bwrap"
APPARMOR_PROFILE = Path("/etc/apparmor.d") / APPARMOR_PROFILE_NAME
USERNS_SYSCTL = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"


def _read(path: str) -> str:
    return Path(path).read_text(encoding="ascii")


def userns_restricted(read: Callable[[str], str] = _read) -> bool:
    """True when the sysctl reads 1. Absent (no AppArmor userns restriction on this kernel) = False."""
    try:
        return read(USERNS_SYSCTL).strip() == "1"
    except FileNotFoundError:
        return False


def system_bwrap() -> str:
    return _abs("bwrap")


def apparmor_profile_text() -> str:
    """Grants the Levain copy of bwrap user namespaces and nothing else (the shape R-L2 ran)."""
    return (
        "# Levain session slots' bubblewrap (CONTRACT §1.2). Written by the install helper; removed by its undo verb.\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "\n"
        f"profile {APPARMOR_PROFILE_NAME} {LEVAIN_BWRAP} flags=(unconfined) {{\n"
        "  userns,\n"
        "\n"
        f"  include if exists <local/{APPARMOR_PROFILE_NAME}>\n"
        "}\n"
    )


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_record_text(source: str, source_digest: str, copy_digest: str) -> str:
    """``sha256sum`` check format: the source binary and the slots' copy, each with its digest, so
    ``sha256sum --check`` passes only while the system bwrap is unchanged AND the copy is that binary."""
    return f"{source_digest}  {source}\n{copy_digest}  {LEVAIN_BWRAP}\n"


def record_digest(source: str, record: Path = BWRAP_DIGEST) -> tuple[bool, str]:
    """After the copy: record the source's and the copy's digests, root's, world-readable."""
    src, cp = file_sha256(source), file_sha256(LEVAIN_BWRAP)
    if src != cp:
        return False, f"{LEVAIN_BWRAP} is not a copy of {source}"
    record.parent.mkdir(parents=True, exist_ok=True)
    tmp = record.with_name(record.name + ".tmp")
    tmp.write_text(digest_record_text(source, src, cp), encoding="ascii")
    os.chmod(tmp, 0o644)
    os.replace(tmp, record)
    return True, ""


def copy_steps(source: str | None = None) -> list[Step]:
    """Copy the system bwrap to the slots' path with its owner, group and mode in one ``install``
    (no window where the copy is another group's or wider) and record both digests. Skipped while the
    recorded digests still check (CONTRACT §23): setup, replenish and ``doctor --repair`` run this
    list, and it re-copies exactly when the system bwrap changed or the copy is not it."""
    src = source or system_bwrap()
    unchanged = (_abs("sha256sum"), "--check", "--status", "--strict", str(BWRAP_DIGEST))
    return [
        Step(f"create {LEVAIN_BWRAP.parent}", (_abs("mkdir"), "-p", "-m", "755", str(LEVAIN_BWRAP.parent))),
        Step(f"copy {src} to {LEVAIN_BWRAP}, root:{BWRAP_GROUP} {BWRAP_MODE:04o} (the slots' bwrap)",
             (_abs("install"), "-o", "root", "-g", BWRAP_GROUP, "-m", f"{BWRAP_MODE:04o}", src, str(LEVAIN_BWRAP)),
             skip_if=unchanged),
        Step(f"record the digests of {src} and its copy in {BWRAP_DIGEST}",
             call=lambda: record_digest(src), skip_if=unchanged),
    ]


def bwrap_steps(*, apparmor_userns_restricted: bool, source: str | None = None) -> list[Step]:
    """:func:`copy_steps`, then, when the host restricts user namespaces (the caller passes what
    :func:`userns_restricted` read), the profile, validated with ``apparmor_parser -Q -K`` (parse only:
    no kernel load, no cache write) before it is installed, then loaded."""
    steps = copy_steps(source)
    if apparmor_userns_restricted:
        steps += [
            Step(f"write the AppArmor profile {APPARMOR_PROFILE_NAME} (user namespaces for the slots' bwrap)",
                 write=(APPARMOR_PROFILE, apparmor_profile_text(), 0o644),
                 validate=(_abs("apparmor_parser"), "-Q", "-K")),
            Step(f"load {APPARMOR_PROFILE_NAME}", (_abs("apparmor_parser"), "-r", str(APPARMOR_PROFILE))),
        ]
    return steps


__all__ = ["APPARMOR_PROFILE", "APPARMOR_PROFILE_NAME", "BWRAP_DIGEST", "BWRAP_GROUP", "BWRAP_MODE", "LEVAIN_BWRAP", "USERNS_SYSCTL",
           "apparmor_profile_text", "bwrap_steps", "copy_steps", "digest_record_text",
           "file_sha256", "record_digest", "system_bwrap", "userns_restricted"]


# --- row 33 R1b: removal by the RECORDED identity (§1.1 table rows 9-10) -------------------------------------------

#: The kernel's list of loaded profiles (``<name> (<mode>)`` per line); absent = AppArmor is not enabled, so nothing is
#: loaded. Written to ``.remove`` (the kernel interface ``apparmor_parser -R`` uses) only when a profile is loaded
#: with no profile file left to name it.
APPARMOR_LOADED = Path("/sys/kernel/security/apparmor/profiles")
APPARMOR_REMOVE = Path("/sys/kernel/security/apparmor/.remove")
_PROFILE_HEADER = b"# Levain session slots' bubblewrap"


def _profile_loaded(name: str, live: "Live") -> bool:
    from levain.slot.removal import ReadFailed

    try:
        text = live.at(APPARMOR_LOADED).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError as e:
        raise ReadFailed(f"cannot read {APPARMOR_LOADED}: {e.strerror or e}") from None
    return any(ln.split(" (", 1)[0] == name for ln in text.splitlines())


def apparmor_state(name: str, live: "Live") -> str:
    """OURS: ``/etc/apparmor.d/<name>`` is a regular root file whose first line is Levain's header, or the file is
    gone and the profile is still loaded (the bound reserves ``levain-slot-*``). ABSENT: no file and not loaded."""
    from levain.slot.removal import ABSENT, OURS, root_file

    state = root_file(live, Path("/etc/apparmor.d") / name, check=lambda b: b.startswith(_PROFILE_HEADER))
    if state != ABSENT:
        return state
    return OURS if _profile_loaded(name, live) else ABSENT


def remove_apparmor_profile_steps(name: str, *, live: "Live") -> list[Step]:
    """``apparmor/1`` (R8): unload the recorded profile ONLY while it is LOADED (whether ``apparmor_parser -R`` of an
    unloaded profile exits 0 is unmeasured, so nothing depends on it), then ``rm -f`` its file. Never the bwrap copy
    or its digest (``bwrap-copy/1``'s), never hands' ``levain-bwrap``."""
    from levain.slot.removal import at, chain, point, remove_step

    path = Path("/etc/apparmor.d") / name
    record = ("apparmor/1", name)
    state = lambda: apparmor_state(name, live)   # noqa: E731
    parser, rm = _abs("apparmor_parser"), _abs("rm")

    def unload() -> tuple[bool, str]:
        if not _profile_loaded(name, live):
            return True, ""
        if live.at(path).exists():
            return live.act((parser, "-R", at(live, path)))
        try:
            live.at(APPARMOR_REMOVE).write_text(name, encoding="ascii")
        except OSError as e:
            return False, f"could not unload {name} through {APPARMOR_REMOVE}: {e.strerror or e}"
        return True, ""

    return [
        remove_step(f"unload the AppArmor profile {name}", record, live, state,
                    chain(unload, point(live, "apparmor/1:after-unload")), tools=(parser,)),
        remove_step(f"remove {path}", record, live, state, lambda: live.act((rm, "-f", at(live, path))), tools=(rm,)),
    ]


#: One line of :func:`digest_record_text` (``sha256sum`` check format: a digest, two spaces, an absolute path).
_DIGEST_LINE = re.compile(rb"([0-9a-f]{64})  (/[^\n]*)")


def _digest_lines(data: bytes) -> dict | None:
    """A digest record's ``path -> sha256``; None when the bytes are not one :func:`digest_record_text` wrote."""
    lines = data.split(b"\n")
    if lines[-1] != b"" or len(lines) < 2:
        return None
    out = {}
    for ln in lines[:-1]:
        m = _DIGEST_LINE.fullmatch(ln)
        if m is None:
            return None
        out[m.group(2).decode("utf-8", "replace")] = m.group(1).decode()
    return out


def bwrap_copy_state(copy: str, digest: str, live: "Live") -> str:
    """OURS: the copy or its digest record is present, each a regular root file Levain wrote: the digest record parses
    as :func:`digest_record_text`'s, and the copy's sha256 is the one that record names for it. ABSENT: neither.
    NOT_OURS when either is any other file (the bound admits every name under ``/var/lib/levain``, the retired-ids
    file's included), so nothing is removed. Known edge: a copy whose digest record was never written (setup killed
    between the ``install`` and the record) reads NOT_OURS and is left, logged; the next setup or repair re-records."""
    from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, root_file

    named: dict = {}

    def record_ok(b: bytes) -> bool:
        lines = _digest_lines(b)
        if lines is not None:
            named.update(lines)
        return lines is not None

    d = root_file(live, Path(digest), check=record_ok)
    c = root_file(live, Path(copy))
    if c == OURS:
        try:
            have = file_sha256(live.at(Path(copy)))
        except OSError as e:
            raise ReadFailed(f"cannot read {copy}: {e.strerror or e}") from None
        if d != OURS or named.get(copy) != have:
            c = NOT_OURS
    states = {c, d}
    if NOT_OURS in states:
        return NOT_OURS
    return OURS if OURS in states else ABSENT


def remove_bwrap_copy_steps(copy: str, digest: str, *, live: "Live") -> list[Step]:
    """``bwrap-copy/1`` (R8): ``rm -f`` the recorded copy, then its digest record (U2: the identity names both)."""
    from levain.slot.removal import at, chain, point, remove_step

    record = ("bwrap-copy/1", (copy, digest))
    state = lambda: bwrap_copy_state(copy, digest, live)   # noqa: E731
    rm = _abs("rm")
    return [
        remove_step(f"remove {copy}", record, live, state,
                    chain(lambda: live.act((rm, "-f", at(live, Path(copy)))), point(live, "bwrap-copy/1:after-copy-rm")),
                    tools=(rm,)),
        remove_step(f"remove {digest}", record, live, state, lambda: live.act((rm, "-f", at(live, Path(digest)))),
                    tools=(rm,)),
    ]
