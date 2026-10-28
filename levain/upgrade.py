"""``levain upgrade``: replace the Levain package with the installer that put it there, then ask the incumbent root
helper to repair against the new package. Run as the operator; it adds no root verb, sudoers line or authority, and it
never weakens what ``repair --source`` checks in root: it hands root one path and the helper's own gates decide.

Hidden like ``levain session`` (no ``help=``, so ``levain --help`` does not list it). Not ``levain update``, which
reconciles one install directory's anneal, schema and hooks.

Every effect goes through :class:`Seams`, so each path is reachable without root, an installer or sudo.

Every child it starts (the installer, sudo) and its own re-exec get an environment built here, never ``os.environ``:
:mod:`levain.launch` restores the operator's carried secrets (``GH_TOKEN``, cloud keys) into ``os.environ``, and whatever
a child is given sits in its exec-time block, readable by every process of this user (L3 r1).

Known edges, not handled (L3 r1):
- Two upgrades at once in one environment can interleave their installer steps; nothing here locks that phase (the
  root helper's own lock covers only the repair).
- ``_h11`` reads ``is_link`` twice, so a path flipped between the reads can name the wrong reason in the message; the
  refusal itself stands either way.
- ``_check_writers`` maps gid to name with one name per gid, so on a host with a group alias the pre-check's advice
  may name the alias; root's own check is unaffected.
- An installer that authenticates through a keyring on the session bus does not get ``DBUS_SESSION_BUS_ADDRESS``;
  configure the index through ``PIP_*``/``UV_*``/``PIPX_*`` or a netrc instead.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

SUDO = "/usr/bin/sudo"
EXIT_OK, EXIT_REFUSED, EXIT_HELD, EXIT_INTERNAL = 0, 1, 3, 70
_HELD_ROW = "agent held sessions"
_STDERR_TAIL = 20
_PENDING = "levain install-helper: pending:"
# Children run from here, never the operator's directory: a cwd `pip/` package would shadow `python -m pip`, and uv
# reads `uv.toml`/`pyproject.toml` from the cwd upward (an index the operator did not choose for this upgrade).
_NEUTRAL_CWD = "/"
# What sudo and the installer need from the environment. Everything else is dropped.
_BASE_ENV = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR"})
# The installer also needs: the dynamic loader's paths (an interpreter found only through one would not start), the
# netrc path, and its own config namespaces (XDG_ holds pip's and uv's config dirs on Linux).
_INSTALLER_ENV = _BASE_ENV | {"LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "NETRC"}
_INSTALLER_PREFIXES = ("LC_", "PIP_", "UV_", "PIPX_", "XDG_")


class Refusal(Exception):
    """A refusal by ``upgrade`` itself: ``text`` is printed, ``code`` is the exit code."""

    def __init__(self, text: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(text)
        self.text, self.code = text, code


# --- installer evidence -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    owns: bool                       # the distribution owns the running package's __init__.py
    installer: Optional[str]         # INSTALLER, stripped; None when absent
    direct_url: Optional[dict]       # direct_url.json, parsed; None when absent
    venv_root: Optional[str]         # sys.prefix, only inside a venv
    pipx: bool
    uv_receipt: bool
    under_user_site: bool
    externally_managed: bool


def gather_evidence(pkg: str, dist: Any, *, prefix: str, base_prefix: str, user_site: str, stdlib: str,
                    exists: Callable[[str], bool] = os.path.exists) -> Evidence:
    """Everything is read from the running package outward, so no interpreter is guessed. A read error raises
    ``Refusal`` (ambiguous): an unreadable marker is never read as absent."""
    try:
        owns = (os.path.realpath(str(dist.locate_file("levain/__init__.py")))
                == os.path.realpath(os.path.join(pkg, "__init__.py")))
        raw = dist.read_text("INSTALLER")
        installer = raw.strip() if raw is not None else None
        du = dist.read_text("direct_url.json")
        direct = json.loads(du) if du is not None else None
        if direct is not None and type(direct) is not dict:
            raise ValueError("direct_url.json is not an object")
    except Exception as e:  # noqa: BLE001 - any read error is "ambiguous", never a guess
        raise Refusal(_cannot_tell(f"could not read the install record: {type(e).__name__}")) from None
    venv = prefix if prefix != base_prefix else None
    user = bool(user_site) and (pkg == user_site or pkg.startswith(user_site.rstrip(os.sep) + os.sep))
    return Evidence(owns, installer, direct, venv,
                    bool(venv) and exists(os.path.join(venv, "pipx_metadata.json")),
                    bool(venv) and exists(os.path.join(venv, "uv-receipt.toml")),
                    user, exists(os.path.join(stdlib, "EXTERNALLY-MANAGED")))


def _cannot_tell(evidence: str) -> str:
    return (f"Could not tell how Levain was installed ({evidence}). Upgrade it your usual way, then run "
            "`levain upgrade --no-pip`.")


def _is_source_tree(direct: Optional[dict]) -> bool:
    if not direct:
        return False
    info = direct.get("dir_info")
    return (type(info) is dict and bool(info.get("editable"))) or str(direct.get("url", "")).startswith("file:")


def decide_installer(ev: Evidence, *, python: str, which: Callable[[str], Optional[str]]) -> tuple[str, list[str]]:
    """``(tool, argv)`` for the installer's own upgrade of ``levain[isolation]``, or ``Refusal``. Marker rows (source
    tree, pipx, uv tool) are decided together: a second marker is ambiguous, never a pick. The INSTALLER rows apply
    only when no marker did. Never falls back to pip."""
    if not ev.owns:
        raise Refusal(_cannot_tell("the installed record does not own the running package"))
    markers = [m for m, on in (("source", _is_source_tree(ev.direct_url)), ("pipx", ev.pipx),
                               ("uv-tool", ev.uv_receipt)) if on]
    if len(markers) > 1:
        raise Refusal(_cannot_tell("more than one install marker: " + ", ".join(markers)))
    if markers == ["source"]:
        raise Refusal("Levain is installed from a source tree; update that tree, then run "
                      "`levain upgrade --no-pip`.")
    if markers == ["pipx"]:
        exe = which("pipx")
        if not exe:
            raise Refusal(_cannot_tell("pipx metadata found but pipx is not on PATH"))
        return "pipx", [exe, "upgrade", "levain"]
    if markers == ["uv-tool"]:
        exe = which("uv")
        if not exe:
            raise Refusal(_cannot_tell("a uv tool receipt found but uv is not on PATH"))
        return "uv", [exe, "tool", "upgrade", "levain"]
    spec = "levain[isolation]"
    if ev.installer == "uv" and ev.venv_root:
        exe = which("uv")
        if not exe:
            raise Refusal(_cannot_tell("installed by uv but uv is not on PATH"))
        return "uv", [exe, "pip", "install", "-U", spec, "--python", python]
    if ev.installer == "pip":
        base = [python, "-m", "pip", "install", "-U", spec]
        if ev.venv_root:
            return "pip", base
        if ev.under_user_site:
            return "pip", [*base, "--user"]
        if ev.externally_managed:
            raise Refusal(_cannot_tell("this Python is externally managed"))
        raise Refusal(_cannot_tell("pip outside a virtual environment and outside your user site"))
    raise Refusal(_cannot_tell(f"INSTALLER is {ev.installer!r}" if ev.installer else "no INSTALLER record"))


# --- the seams --------------------------------------------------------------------------------------------------


def installer_env(environ: Optional[Any] = None) -> dict:
    """The installer's environment: the base names, the loader paths, its config namespaces and the network names
    (proxies, CA bundles) from :data:`levain.launch.NETWORK`. ``PIP_*``/``UV_*``/``PIPX_*`` can hold an index URL with
    credentials in it; that is the installer's own config, and it goes to the installer only."""
    from levain.launch import NETWORK

    e = os.environ if environ is None else environ
    names = _INSTALLER_ENV | set(NETWORK)
    return {k: v for k, v in e.items() if k in names or k.startswith(_INSTALLER_PREFIXES)}


def sudo_env(environ: Optional[Any] = None) -> dict:
    """sudo's environment: the base names and the locale. sudo resets the helper's environment itself; this keeps
    the rest out of sudo's own exec-time block."""
    e = os.environ if environ is None else environ
    return {k: v for k, v in e.items() if k in _BASE_ENV or k.startswith("LC_")}


def reexec_env(environ: Optional[Any] = None) -> dict:
    """The re-exec's environment: :func:`levain.launch.allowed_env`, the list levain's own re-exec uses (it drops
    ``LEVAIN_CARRY_FD``, so the new image does not look for a carry it was not given)."""
    from levain.launch import allowed_env

    return allowed_env(os.environ if environ is None else environ)


def _run(argv: Sequence[str], *, env: dict, cwd: str = _NEUTRAL_CWD, capture_stderr: bool = True,
         capture_stdout: bool = False) -> tuple[int, str, str]:
    import subprocess

    p = subprocess.run(list(argv), shell=False, env=env, cwd=cwd, stdout=subprocess.PIPE if capture_stdout else None,
                       stderr=subprocess.PIPE if capture_stderr else None, text=True)
    return p.returncode, p.stdout or "", p.stderr or ""


def _execve(path: str, argv: list, env: dict) -> None:
    os.execve(path, argv, env)


def _has_tty() -> bool:
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        with open("/dev/tty", "rb+", buffering=0):
            pass
    except (OSError, ValueError):     # ValueError: a closed stdin
        return False
    return True


def _held_row() -> Any:
    from levain import doctor

    for row in doctor._check_slot_services():
        if row.name == _HELD_ROW:
            return row
    return None


def _held_lines(host: str, pkg: str) -> Optional[Sequence[str]]:
    from levain import doctor

    # Intent: print doctor's own clean-up commands for a held session. Doctor gains `reap_held_lines` in another
    # change; until it is present this returns None and `_held_stop` sends the operator to `levain doctor` instead.
    fn = getattr(doctor, "reap_held_lines", None)
    return None if fn is None else fn(host, pkg)


def _current_generation(host: str) -> Optional[str]:
    from levain.slot import root_tree
    from levain.slot.layout import IMPORT_TREE

    try:
        return root_tree.read_current(IMPORT_TREE[host])
    except Exception:  # noqa: BLE001
        return None


def _read_directory(host: str) -> Any:
    from levain.slot import install_helper as IH

    d = IH.read_directory(host)
    return IH.read_primary_groups(d) if host == "darwin" else d


def _check_writers(parent: Path, uid: int, d: Any) -> None:
    from levain.slot import install_helper as IH
    from levain.slot import root_tree

    root_tree.check_source_writers(parent, operator_uid=uid, operator_gids=IH.operator_only_gids(d, uid),
                                   group_names={i: g for g, i in sorted(d.groups.items(), reverse=True)})


def _real_dist() -> Any:
    import importlib.metadata as md

    return md.distribution("levain")


def _user_site() -> str:
    import site

    try:
        return site.getusersitepackages()
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class Seams:
    host: str = "darwin" if sys.platform == "darwin" else "linux"
    geteuid: Callable[[], int] = lambda: os.geteuid()
    getuid: Callable[[], int] = lambda: os.getuid()
    has_tty: Callable[[], bool] = _has_tty
    record_path: Optional[Path] = None
    helper_path: Optional[Path] = None
    lstat: Callable[[str], os.stat_result] = os.lstat
    read_record: Callable[[Path], bytes] = lambda p: p.read_bytes()
    pkg: Optional[str] = None
    dist: Callable[[], Any] = _real_dist
    prefix: str = sys.prefix
    base_prefix: str = sys.base_prefix
    executable: str = sys.executable
    exe_exists: Callable[[str], bool] = os.path.exists
    user_site: Callable[[], str] = _user_site
    stdlib: Optional[str] = None
    marker_exists: Callable[[str], bool] = os.path.exists
    which: Callable[[str], Optional[str]] = lambda n: __import__("shutil").which(n)
    run: Callable[..., tuple[int, str, str]] = _run
    execv: Callable[[str, list, dict], Any] = _execve       # os.execve, always with an explicit env
    held_row: Callable[[], Any] = _held_row
    held_lines: Callable[[str, str], Optional[Sequence[str]]] = _held_lines
    read_directory: Callable[[str], Any] = _read_directory
    check_writers: Callable[[Path, int, Any], None] = _check_writers
    realpath: Callable[[str], str] = os.path.realpath
    h11_floor: Optional[Callable[[Path], str]] = None
    is_dir: Callable[[str], bool] = os.path.isdir
    is_link: Callable[[str], bool] = os.path.islink
    gen_id: Optional[Callable[[Path, Path], str]] = None
    current_gen: Callable[[str], Optional[str]] = _current_generation
    version: Optional[str] = None


# --- the checks -------------------------------------------------------------------------------------------------


def _state(s: Seams) -> tuple[Path, Path, bool, bool]:
    from levain.slot import layout

    rec = s.record_path or layout.operator_record_path(s.host)
    hlp = s.helper_path or layout.install_helper_path(s.host)

    def there(p: Path) -> bool:
        try:
            s.lstat(str(p))
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    return rec, hlp, there(rec), there(hlp)


def _unsafe(s: Seams, path: Path) -> None:
    try:
        st = s.lstat(str(path))
        bad = not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or bool(st.st_mode & 0o022)
    except OSError:
        bad = True
    if bad:
        raise Refusal(f"{path} is not a root-owned file only root can change; not running it. "
                      "Run `levain doctor`.")


def _operator(s: Seams, rec: Path) -> tuple[str, int]:
    from levain.slot import install_helper as IH
    from levain.slot import Refused

    try:
        obj = IH.parse_operator_record(s.read_record(rec))
    except (Refused, OSError):
        raise Refusal(f"{rec} cannot be read as Levain's operator record. Run `levain doctor`.") from None
    return str(obj["name"]), int(obj["uid"])  # type: ignore[call-overload]


def _package_names(pkg: str) -> None:
    from levain.slot import Refused
    from levain.slot.grammar import _root_syntax

    if os.path.basename(pkg) != "levain":
        raise Refusal(f"{pkg} is not a directory named levain; the helper would refuse it.")
    try:
        _root_syntax(pkg)
    except Refused:
        raise Refusal(f"the helper cannot take this path: {pkg!r}") from None


def _h11(s: Seams, pkg: str, *, no_pip: bool) -> Path:
    from levain.slot import Refused
    from levain.slot import root_tree

    h11 = Path(s.realpath(os.path.dirname(pkg))) / "h11"
    again = ("run `levain upgrade` again" if no_pip else "install levain[isolation]")
    if s.is_link(str(h11)) or not s.is_dir(str(h11)):
        raise Refusal(f"{h11} is not a real h11 directory ({'a link' if s.is_link(str(h11)) else 'missing'}); "
                      f"{again}.")
    try:
        (s.h11_floor or root_tree.h11_floor)(h11)
    except Refused as e:
        raise Refusal(f"{h11}: {e.detail}; {again}.") from None
    return h11


def _writers(s: Seams, pkg: str, uid: int) -> Optional[str]:
    """Advisory only: it can refuse earlier, never admit. A failed directory read skips it and root decides."""
    from levain.slot import Refused

    parent = Path(s.realpath(os.path.dirname(pkg)))
    try:
        d = s.read_directory(s.host)
    except (Refused, OSError):
        return "skipped (the directory read failed; root decides)"
    try:
        s.check_writers(parent, uid, d)
    except Refused as e:
        raise Refusal(f"{e.detail}\nFix that, then run `levain upgrade` again.") from None
    return "ok"


def _held_stop(s: Seams, pkg: str) -> Optional[Refusal]:
    row = s.held_row()
    if row is None or row.ok is not False:
        return None
    lines = [f"Held sessions: {row.detail}"]
    cmds = s.held_lines(s.host, pkg)
    if cmds is None:
        lines.append("This build has no printed clean-up commands for a held session. Run `levain doctor` and "
                     f"follow its '{_HELD_ROW}' row. Nothing here ran anything.")
    else:
        lines.append("Run these, in order, as root. Nothing here ran them:")
        lines += [f"  {i}. {c}" for i, c in enumerate(list(cmds)[:5], 1)]
        lines.append("If 3 fails, still run 4 and 5.")
    lines.append("Then run `levain upgrade` again; it runs the repair (step 6) for you.")
    return Refusal("\n".join(lines), EXIT_HELD)


def sudo_argv(s: Seams, pkg: str) -> list[str]:
    """Exactly this, with ``pkg`` as Python sees it: not realpath'd, so root's own link refusal still fires. No
    ``-n``, ``-A``, ``-S`` or ``-E``: it can neither hang on stdin nor reach an askpass."""
    from levain.slot import layout

    return [SUDO, str(s.helper_path or layout.install_helper_path(s.host)), "repair", "--source", pkg]


# --- the run ----------------------------------------------------------------------------------------------------


def run_upgrade(*, no_pip: bool = False, dry_run: bool = False, verbose: bool = False,
                seams: Optional[Seams] = None) -> int:
    s = seams or Seams()
    try:
        return _run_upgrade(s, no_pip, dry_run, verbose)
    except Refusal as r:
        print(r.text)
        return r.code
    except Exception as e:  # noqa: BLE001 - one line, the helper's own internal code
        print(f"levain upgrade: internal error: {type(e).__name__}: {e}")
        return EXIT_INTERNAL


def _run_upgrade(s: Seams, no_pip: bool, dry_run: bool, verbose: bool) -> int:
    import levain

    if s.geteuid() == 0:
        raise Refusal("Run `levain upgrade` as yourself, not with sudo. It asks for your password when it "
                      "needs root.")
    rec, hlp, have_rec, have_hlp = _state(s)
    isolated = have_rec or have_hlp
    uid = name = None
    if isolated:
        if have_rec != have_hlp:
            raise Refusal(f"Levain's root install is incomplete ({hlp if have_rec else rec} is missing). "
                          "Run `levain doctor`.")
        _unsafe(s, hlp)
        _unsafe(s, rec)
        name, uid = _operator(s, rec)
        if uid != s.getuid():
            raise Refusal(f"This machine's Levain install belongs to {name}. Run this as {name}.")
        if not dry_run and not s.has_tty():
            raise Refusal("sudo needs your password and there is no terminal here. Open a terminal and run "
                          "`levain upgrade`.")
    pkg = s.pkg if s.pkg is not None else os.path.dirname(os.path.abspath(levain.__file__))
    version = s.version if s.version is not None else getattr(levain, "__version__", "?")

    tool = argv = None
    if not no_pip:
        ev = gather_evidence(pkg, s.dist(), prefix=s.prefix, base_prefix=s.base_prefix, user_site=s.user_site(),
                             stdlib=s.stdlib or __import__("sysconfig").get_path("stdlib"),
                             exists=s.marker_exists)
        tool, argv = decide_installer(ev, python=s.executable, which=s.which)
        if isolated:
            _package_names(pkg)

    if dry_run:
        return _dry_run(s, pkg, tool, argv, isolated, uid, no_pip)

    if argv is not None:
        print(f"Upgrading levain with {tool} (now {version})")
        rc, _, _ = s.run(argv, env=installer_env(), cwd=_NEUTRAL_CWD, capture_stderr=False)
        if rc != 0:
            print(f"The {tool} upgrade failed (exit {rc}). Nothing on the root side changed. Fix that, then run "
                  "`levain upgrade` again.")
            return EXIT_REFUSED
        if not s.exe_exists(s.executable):
            print(f"Python at {s.executable} is gone after the upgrade; run `levain upgrade --no-pip`.")
            return EXIT_REFUSED
        # -P: `python -m` would otherwise put the cwd first on sys.path, and a `levain/` there would run instead of
        # the package just installed. Not -I, which also drops the user site and so a `pip --user` install.
        s.execv(s.executable, [s.executable, "-P", "-m", "levain", "upgrade", "--no-pip",
                               *(["--verbose"] if verbose else [])], reexec_env())
        return EXIT_OK      # only reached where exec is a seam

    if not isolated:
        print("No Levain isolation install on this machine; nothing else to upgrade.")
        return EXIT_OK
    assert uid is not None
    _package_names(pkg)
    h11 = _h11(s, pkg, no_pip=True)
    _writers(s, pkg, uid)
    stop = _held_stop(s, pkg)
    if stop is not None:
        raise stop
    gen = _generation(s, pkg, h11)
    # No "already current" skip (L3 r1): matching generation and shape say nothing about the other root rows (units,
    # sudoers, membership) that repair re-applies, and three unlocked reads race a repair. Repair is idempotent and
    # decides under its own lock. The cost: every run asks for the password and restarts Levain's services, even
    # when the installer found nothing newer. `--dry-run` shows both generations without sudo.
    print("Updating the root side (stops and restarts Levain's services). sudo will ask for your password.")
    return _sudo(s, pkg, gen, verbose)


def _generation(s: Seams, pkg: str, h11: Path) -> Optional[str]:
    from levain.slot import generation as G

    try:
        return (s.gen_id or G.generation_id)(Path(pkg), h11)
    except Exception:  # noqa: BLE001 - root computes it again and is the gate
        return None


def _sudo(s: Seams, pkg: str, gen: Optional[str], verbose: bool) -> int:
    rc, out, err = s.run(sudo_argv(s, pkg), env=sudo_env(), cwd=_NEUTRAL_CWD, capture_stderr=not verbose,
                         capture_stdout=True)
    lines = out.strip().splitlines()
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    errs = err.splitlines()
    if type(reply) is not dict or "ok" not in reply:
        # No result line does not mean nothing ran: sudo refusing the password and a helper killed after it stopped
        # services both leave stdout empty. Say only what is known.
        what = ("printed no result line" if not lines else "printed a last line that is not a result")
        print(f"The outcome is unknown: sudo exited {rc} and the helper {what}. If sudo refused the password, "
              "nothing ran; if the helper was stopped partway, the root side may be mid-repair, with Levain's "
              "services stopped.")
        for ln in errs[-_STDERR_TAIL:]:
            print(ln)
        print("Repair is resumable: run `levain upgrade --no-pip` again, then `levain doctor`.")
        return EXIT_REFUSED
    if reply["ok"] is True:
        import levain

        print(f"Done: the root side runs {s.version or getattr(levain, '__version__', '?')}"
              f"{f' ({gen})' if gen else ''}.")
        for ln in errs:
            if ln.startswith(_PENDING):
                print(ln)
        print("Each Levain install: run `levain update` in it to refresh its hooks.")
        return EXIT_OK
    print(f"refused {reply.get('refused', '?')}")
    for ln in errs[-_STDERR_TAIL:]:
        print(ln)
    print("Fix that, then run `levain upgrade` again.")
    return EXIT_REFUSED


def _dry_run(s: Seams, pkg: str, tool: Optional[str], argv: Optional[list], isolated: bool,
             uid: Optional[int], no_pip: bool) -> int:
    if argv is not None:
        print(f"installer: {tool}: {argv}")
    else:
        print("installer: not run (--no-pip)")
    print(f"package: {pkg}")
    if not isolated:
        print("No Levain isolation install on this machine; nothing else to upgrade.")
        print("dry run: nothing was changed")
        return EXIT_OK
    assert uid is not None
    _package_names(pkg)
    h11 = _h11(s, pkg, no_pip=no_pip)
    print(f"h11: {h11}: ok")
    print(f"source writers: {_writers(s, pkg, uid)}")
    row = s.held_row()
    print(f"{_HELD_ROW}: {'none that stop the run' if row is None or row.ok is not False else row.detail}")
    stop = _held_stop(s, pkg)
    gen = _generation(s, pkg, h11)
    print(f"package generation now: {gen}; root's current: {s.current_gen(s.host)} "
          "(after the pip step these can change)")
    print(f"sudo: {sudo_argv(s, pkg)}")
    if stop is not None:
        raise stop
    print("dry run: nothing was changed")
    return EXIT_OK


# --- the command ------------------------------------------------------------------------------------------------


def register(subparsers: Any) -> None:
    """Hidden, as ``levain session`` is: no ``help=``, so the parent's listing and usage line omit it, while
    ``levain upgrade --help`` still works. Make it public when ``session`` is."""
    p = subparsers.add_parser(
        "upgrade",
        description=("Upgrade the Levain package with the installer that put it there, then repair the root side "
                     "(one sudo password). This is not `levain update`, which refreshes one install directory's "
                     "hooks, schema and anneal."))
    p.add_argument("--no-pip", action="store_true", help="Skip the installer step (you already ran it).")
    p.add_argument("--dry-run", action="store_true", help="Print what would run; change nothing.")
    p.add_argument("--verbose", action="store_true", help="Stream the helper's stderr.")
    p.set_defaults(func=lambda a: run_upgrade(no_pip=a.no_pip, dry_run=a.dry_run, verbose=a.verbose))
