"""3b-8a guest side (runs IN the clone VM as root): the small effects behind the driver's seams.
  slotstate <n>          state word of slots/e<n> (strict parse; a malformed record exits 4)
  slotof <session>       the slot tag (e<n>) that is LEASED to <session> (strict parse), or 'none'; malformed -> exit 4
  revoke <session>       the REVOKE CONTROL verb through the repo's own ControlClient (cleanup.control_revoke's body)
  resolve <name>         uid of <name> or 'none'
  fhclean -              unlink every drv<slot>x<nonce>.* rendezvous entry in the exchange dir; prints how many survived
  put <name>             create (O_EXCL, no symlink) <exchange dir>/<name>, mode 644; <name> is a bare file name
  get <name>             print <exchange dir>/<name>; exit 3 when absent, 5 when it is not a plain regular file
  cancel <tag>           SIGTERM the guest dispatcher of <tag> (pid from <tag>.pid, checked against ps)
usage: guest_ops.py <verb> <arg>

The exchange dir is owned by the unprivileged dispatch user and this script runs as root, so every access is
relative to a directory fd opened O_NOFOLLOW and every file is opened O_NOFOLLOW + fstat'd regular: a planted
symlink or fifo is refused, never followed."""
import glob
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys

if __package__:
    from scripts.vm_harness.host_profile import profile_for_platform
else:   # run as a script in the clone, next to host_profile.py (python -I leaves the script's own dir off sys.path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from host_profile import profile_for_platform

PROFILE = profile_for_platform(sys.platform)
for root in ["/tmp/lvclean", "/tmp/lv"]:
    if os.path.isdir(os.path.join(root, "levain")):
        sys.path.insert(0, root)
        break
SLOTS = f"{PROFILE.store}/slots"
FH = PROFILE.exchange_dir
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_GET = 1 << 20
RENDEZVOUS_RE = re.compile(r"\.?drv[0-9]+x[0-9a-f]+\.")


def _bare(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        sys.exit(2)
    return name


def _dirfd() -> int:
    return os.open(FH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _read_regular(dfd: int, name: str) -> bytes | None:
    """The file's bytes, None when absent; raises OSError (ELOOP etc.) for a symlink and ValueError for a non-regular file."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        return os.read(fd, MAX_GET)
    finally:
        os.close(fd)


def _slot_states() -> list[tuple[str, object]]:
    """Every slot's strictly parsed state, keyed by the directory it sits in; the directory name must be the
    record's own slot tag. Raises on any malformed record (fail closed)."""
    from levain.slot.records import parse_slot_state
    out = []
    for p in sorted(glob.glob(f"{SLOTS}/*/state")):
        d = p.split("/")[-2]
        st = parse_slot_state(open(p, "rb").read())
        if st.slot.tag != d:
            raise ValueError(f"{p} names slot {st.slot.tag}")
        out.append((d, st))
    return out


def main(argv: list[str]) -> int:
    verb, arg = argv[1], argv[2]
    if verb == "slotstate":
        want = f"e{int(arg)}"
        try:
            states = _slot_states()
        except Exception as e:   # noqa: BLE001 - a record that cannot be trusted is not a state
            print(f"malformed: {type(e).__name__}: {e}", file=sys.stderr)
            return 4
        got = [st for d, st in states if d == want]
        if len(got) != 1:
            print(f"no state for {want}", file=sys.stderr)
            return 4
        print(got[0].state)
    elif verb == "slotof":
        try:
            states = _slot_states()
        except Exception as e:   # noqa: BLE001
            print(f"malformed: {type(e).__name__}: {e}", file=sys.stderr)
            return 4
        hits = [d for d, st in states if st.state == "leased" and st.session == arg]
        print(hits[0] if len(hits) == 1 else "none")
    elif verb == "revoke":
        from levain.slot.control_client import ControlClient
        with ControlClient(PROFILE.ops_control_sock) as c:
            print(json.dumps(c.call("REVOKE", session=arg), sort_keys=True))
    elif verb == "resolve":
        try:
            print(pwd.getpwnam(arg).pw_uid)
        except KeyError:
            print("none")
    elif verb == "fhclean":
        dfd = _dirfd()
        try:
            left = 0
            for n in os.listdir(dfd):
                if not RENDEZVOUS_RE.match(n):    # brief.txt, the repo checkout and the knobs are not ours to touch
                    continue
                try:
                    os.unlink(n, dir_fd=dfd)    # unlink never follows a symlink; it removes the link itself
                except OSError:
                    left += 1
            print(left)
        finally:
            os.close(dfd)
    elif verb == "put":
        name = _bare(arg)
        dfd = _dirfd()
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
            os.fchmod(fd, 0o644)
            os.close(fd)
        except FileExistsError:
            print(f"{name} already exists", file=sys.stderr)
            return 6
        finally:
            os.close(dfd)
    elif verb == "get":
        name = _bare(arg)
        dfd = _dirfd()
        try:
            data = _read_regular(dfd, name)
        except (OSError, ValueError) as e:
            print(f"refused {name}: {e}", file=sys.stderr)
            return 5
        finally:
            os.close(dfd)
        if data is None:
            return 3
        sys.stdout.write(data.decode("utf-8", "replace"))
    elif verb == "cancel":
        tag = _bare(arg)
        dfd = _dirfd()
        try:
            raw = _read_regular(dfd, f"{tag}.pid")
        except (OSError, ValueError):
            return 5
        finally:
            os.close(dfd)
        if raw is None or not raw.strip().isdigit():
            return 3
        pid = int(raw.strip())
        ps = subprocess.run([PROFILE.ps_bin, "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if "guest_dispatch.py" not in ps or tag not in ps.split():
            print(f"pid {pid} is not the dispatcher of {tag}", file=sys.stderr)
            return 5
        os.kill(pid, signal.SIGTERM)
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
