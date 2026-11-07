"""3b-8a guest side (runs IN the clone VM as root): the small effects behind the driver's seams.
  slotstate <n>          state word of slots/e<n>
  slotof <session>       the slot tag (e<n>) whose state file names <session>, or 'none'
  revoke <session>       the REVOKE CONTROL verb through the repo's own ControlClient (cleanup.control_revoke's body)
  resolve <name>         uid of <name> or 'none'
  put <name>             create /private/tmp/fh/<name> (mode 644; <name> is a bare file name)
  get <name>             print /private/tmp/fh/<name>, or exit 3 when absent
usage: guest_ops.py <verb> <arg>"""
import glob
import json
import os
import pwd
import sys

for root in ["/tmp/lvclean", "/tmp/lv"]:
    if os.path.isdir(os.path.join(root, "levain")):
        sys.path.insert(0, root)
        break
SLOTS = "/private/var/levain/slots"
FH = "/private/tmp/fh"


def _bare(name: str) -> str:
    if not name or "/" in name or name.startswith("."):
        sys.exit(2)
    return os.path.join(FH, name)


def main(argv: list[str]) -> int:
    verb, arg = argv[1], argv[2]
    if verb == "slotstate":
        print(json.load(open(f"{SLOTS}/e{int(arg)}/state"))["state"])
    elif verb == "slotof":
        found = "none"
        for p in sorted(glob.glob(f"{SLOTS}/*/state")):
            try:
                if arg in open(p).read():
                    found = p.split("/")[-2]
                    break
            except OSError:
                continue
        print(found)
    elif verb == "revoke":
        from levain.slot.control_client import ControlClient
        with ControlClient("/var/run/levain/control/control.sock") as c:
            print(json.dumps(c.call("REVOKE", session=arg), sort_keys=True))
    elif verb == "resolve":
        try:
            print(pwd.getpwnam(arg).pw_uid)
        except KeyError:
            print("none")
    elif verb == "put":
        p = _bare(arg)
        open(p, "w").close()
        os.chmod(p, 0o644)
    elif verb == "get":
        p = _bare(arg)
        if not os.path.exists(p):
            return 3
        sys.stdout.write(open(p).read())
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
