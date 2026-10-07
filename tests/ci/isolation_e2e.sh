#!/usr/bin/env bash
# End-to-end `levain setup-isolation` on a throwaway CI runner (Linux and macOS): set up, check what
# the hands user can and cannot reach, undo, check that everything is gone. Runs as the runner
# account, which has passwordless sudo; never run it on a machine you care about.
#
# The entity lives under /tmp, which every user can traverse, so this checks the user, the sudoers
# rule, the workspace ACL and the uid gate, not how a workspace under a private home is reached.
set -uo pipefail

LEVAIN="$(command -v levain)"
PY="$(command -v python3)"
E="$(mktemp -d /tmp/levain-iso-XXXXXX)/coyote"
FAILS=0
pass() { echo "OK   $*"; }
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }
refused() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$what (was allowed)"; else pass "$what (refused)"; fi; }

echo "== init a scratch OpenHands entity"
"$LEVAIN" init --adapter openhands --answers-template 2>/dev/null \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d.update(OPERATOR_NAME="CI", ENTITY_NAME="coyote"); print(json.dumps(d))' \
  > /tmp/levain-iso-answers.json
"$LEVAIN" init --adapter openhands --path "$E" --answers /tmp/levain-iso-answers.json >/dev/null || { echo "init failed"; exit 1; }
chmod 755 "$(dirname "$E")" "$E"

echo "== dry run needs no root and changes nothing"
check "dry run" "$LEVAIN" setup-isolation --path "$E" --dry-run
refused "non-root setup" "$LEVAIN" setup-isolation --path "$E"

echo "== setup"
sudo -E env "PATH=$PATH" "$LEVAIN" setup-isolation --path "$E" || { echo "setup failed"; exit 1; }
H=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["hands_user"])' "$E/.levain/confinement.json")
echo "hands user: $H"
WS="$E/workspace"
ME="$(id -un)"

check "hands user exists" id "$H"
if id -nG "$H" | tr ' ' '\n' | grep -qxE 'staff|admin|sudo|wheel|users|docker'; then fail "hands user is in a privileged or shared group: $(id -nG "$H")"; else pass "hands user groups: $(id -nG "$H")"; fi
check "operator can run as hands" sudo -n -u "$H" /usr/bin/true
refused "hands can sudo" sudo -n -u "$H" sudo -n /usr/bin/true
check "levain doctor is green on isolation" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'bash runs as $H'"

echo "== the uid gate"
SECRET="$HOME/levain-iso-secret"; echo x > "$SECRET"; chmod 600 "$SECRET"
refused "hands reads the operator's 0600 file" sudo -n -u "$H" /bin/cat "$SECRET"
CANARY="LEVAIN_ISO_CANARY=$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
env "$CANARY" sleep 300 & CPID=$!
sleep 1
if [ -r /proc/self/environ ]; then
  refused "hands reads the operator's /proc/PID/environ" sudo -n -u "$H" /bin/cat "/proc/$CPID/environ"
  if sudo -n -u "$H" /bin/cat "/proc/$CPID/cmdline" >/dev/null 2>&1; then
    echo "INFO hands can read the operator's /proc/PID/cmdline (argv is world-readable on Linux unless /proc is mounted hidepid)"
  fi
else
  RD="$(mktemp /tmp/levain-iso-read-XXXXXX.py)"; chmod 644 "$RD"
  cat > "$RD" <<'PYEOF'
import ctypes, ctypes.util, sys
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
mib = (ctypes.c_int * 3)(1, 49, int(sys.argv[1]))  # CTL_KERN, KERN_PROCARGS2
size = ctypes.c_size_t(1 << 20); buf = ctypes.create_string_buffer(size.value)
sys.exit(0 if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) == 0 else 1)
PYEOF
  check "operator reads its own process's KERN_PROCARGS2 (control)" /usr/bin/python3 "$RD" "$CPID"
  refused "hands reads the operator's KERN_PROCARGS2" sudo -n -u "$H" /usr/bin/python3 "$RD" "$CPID"
fi
kill "$CPID" 2>/dev/null

echo "== workspace and git"
check "hands creates a file" sudo -n -u "$H" bash -c "cd '$WS' && echo h > from-hands"
check "operator appends to it" bash -c "echo o >> '$WS/from-hands'"
check "operator creates a file" bash -c "echo o > '$WS/from-op'"
check "hands appends to it" sudo -n -u "$H" bash -c "cd '$WS' && echo h >> from-op"
check "operator git repo" bash -c "cd '$WS' && git init -q repo && cd repo && git -c user.name=ci -c user.email=ci@invalid commit -q --allow-empty -m init"
check "hands git status in the operator's repo" sudo -n -u "$H" env "HOME=$(eval echo ~"$H")" bash -c "cd '$WS/repo' && git status --short"
check "hands git commit in it" sudo -n -u "$H" env "HOME=$(eval echo ~"$H")" bash -c "cd '$WS/repo' && echo x > f && git add f && git -c user.name=h -c user.email=h@invalid commit -q -m h"
check "operator git log sees it" bash -c "cd '$WS/repo' && test \"\$(git log --oneline | wc -l)\" -eq 2"

echo "== undo"
SUDOERS="/etc/sudoers.d/levain-$H"
sudo -E env "PATH=$PATH" "$LEVAIN" setup-isolation --path "$E" --undo || fail "undo exited nonzero"
refused "hands user still exists" id "$H"
refused "sudoers drop-in still exists" sudo test -e "$SUDOERS"
if grep -q hands_user "$E/.levain/confinement.json" 2>/dev/null; then fail "hands_user still recorded"; else pass "hands_user removed from confinement.json"; fi
check "operator still has sudo" sudo -n /usr/bin/true
check "operator owns its workspace files" test -O "$WS/from-op"
check "operator owns the files the hands created" test -O "$WS/from-hands"
check "operator can still commit in the repo the hands wrote to" bash -c "cd '$WS/repo' && echo y > g && git add g && git -c user.name=ci -c user.email=ci@invalid commit -q -m y"
if git -C "$WS/repo" config --global --get-all safe.directory 2>/dev/null | grep -qF "$WS/*"; then fail "operator safe.directory entry left behind"; else pass "operator safe.directory entry removed"; fi
rm -f "$SECRET"

echo "== $FAILS failure(s)"
exit "$FAILS"
