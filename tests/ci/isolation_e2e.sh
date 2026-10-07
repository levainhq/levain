#!/usr/bin/env bash
# End-to-end `levain setup-isolation` on a throwaway CI runner (Linux and macOS): set up, check what
# the hands user can and cannot reach, undo (with a hands process still running), check that
# everything is gone, then set up again to check the id is not reused. Runs as the runner account,
# which has passwordless sudo; never run it on a machine you care about.
set -uo pipefail

LEVAIN="$(command -v levain)"
PY="$(command -v python3)"
E="$HOME/levain-iso/coyote"   # under the operator's home: the case the recorded workspace exists for
FAILS=0
pass() { echo "OK   $*"; }
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
info() { echo "INFO $*"; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }
refused() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$what (was allowed)"; else pass "$what (refused)"; fi; }
cfgval() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$E/.levain/confinement.json" "$1"; }
as_hands() { sudo -n -u "$H" env -i "HOME=$HHOME" PATH=/usr/bin:/bin "$@"; }
setup() { sudo -E env "PATH=$PATH" "$LEVAIN" setup-isolation --path "$E" "$@"; }

echo "== init a scratch OpenHands entity under \$HOME"
mkdir -p "$(dirname "$E")"
"$LEVAIN" init --adapter openhands --answers-template 2>/dev/null \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d.update(OPERATOR_NAME="CI", ENTITY_NAME="coyote"); print(json.dumps(d))' \
  > /tmp/levain-iso-answers.json
"$LEVAIN" init --adapter openhands --path "$E" --answers /tmp/levain-iso-answers.json >/dev/null || { echo "init failed"; exit 1; }
git config --global user.name "CI Operator"; git config --global user.email ci-operator@invalid
SSHD_DROPINS=0; grep -qiE '^\s*include\s+/etc/ssh/sshd_config.d' /etc/ssh/sshd_config 2>/dev/null && SSHD_DROPINS=1
if [ "$(uname)" = Darwin ]; then CRON_DENY=/usr/lib/cron/cron.deny; else CRON_DENY=/etc/cron.deny; fi

echo "== dry run needs no root and changes nothing"
check "dry run" "$LEVAIN" setup-isolation --path "$E" --dry-run
refused "non-root setup" "$LEVAIN" setup-isolation --path "$E"
check "doctor warns before setup" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'NOT SET UP'"

echo "== setup"
OP_SAFE_BEFORE="$(git config --global --get-all safe.directory 2>/dev/null | sort)"
info "safe.directory entries the operator already has: $(git config --show-origin --get-all safe.directory 2>/dev/null | tr '\n' ' ')"
setup || { echo "setup failed"; exit 1; }
if [ "$(git config --global --get-all safe.directory 2>/dev/null | sort)" = "$OP_SAFE_BEFORE" ]; then pass "setup added no safe.directory entry for the operator"; else fail "setup changed the operator's safe.directory"; fi
H="$(cfgval hands_user)"; HID="$(cfgval hands_uid)"; WS="$(cfgval hands_workspace)"; HHOME="$(eval echo ~"$H")"
ME="$(id -un)"
echo "hands user: $H (id $HID), workspace: $WS"
case "$WS" in "$HOME"/*) fail "workspace is under the operator's home";; *) pass "workspace is outside the operator's home";; esac
check "hands user has the recorded id" test "$(id -u "$H")" = "$HID"
if id -nG "$H" | tr ' ' '\n' | grep -qxE 'staff|admin|sudo|wheel|users|docker'; then fail "hands user is in a privileged or shared group: $(id -nG "$H")"; else pass "hands user groups: $(id -nG "$H")"; fi
check "operator can run as hands" sudo -n -u "$H" /usr/bin/true
refused "hands can sudo" sudo -n -u "$H" sudo -n /usr/bin/true
check "doctor: set up, and says bash does not use it yet" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'is set up'"
if [ "$SSHD_DROPINS" = 1 ]; then check "sshd drop-in denies the hands user" sudo grep -qx "DenyUsers $H" "/etc/ssh/sshd_config.d/levain-$H.conf"; else info "sshd here does not read sshd_config.d"; fi
if [ -e "$CRON_DENY" ]; then check "hands user is in $CRON_DENY" grep -qx "$H" "$CRON_DENY"; else info "no $CRON_DENY on this system"; fi

echo "== the entity's own ssh key"
check "hands key exists and is the hands user's" sudo -n -u "$H" test -O "$HHOME/.ssh/id_ed25519"
refused "operator reads the hands key without sudo" /bin/cat "$HHOME/.ssh/id_ed25519"
check "hands can sign with its key" as_hands bash -c "cd /tmp && echo p > /tmp/levain-iso-payload-\$\$ && ssh-keygen -Y sign -f '$HHOME/.ssh/id_ed25519' -n levain /tmp/levain-iso-payload-\$\$"

echo "== the uid gate"
SECRET="$HOME/levain-iso-secret"; echo x > "$SECRET"; chmod 600 "$SECRET"
refused "hands reads the operator's 0600 file" sudo -n -u "$H" /bin/cat "$SECRET"
refused "hands lists the operator's home" sudo -n -u "$H" /bin/ls "$HOME"
refused "hands reads the entity's own store under the operator's home" sudo -n -u "$H" /bin/ls "$E/.levain"
CANARY="LEVAIN_ISO_CANARY=$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
env "$CANARY" sleep 300 & CPID=$!
sleep 1
if [ -r /proc/self/environ ]; then
  refused "hands reads the operator's /proc/PID/environ" sudo -n -u "$H" /bin/cat "/proc/$CPID/environ"
  if sudo -n -u "$H" /bin/cat "/proc/$CPID/cmdline" >/dev/null 2>&1; then
    info "hands can read the operator's /proc/PID/cmdline (world-readable on Linux unless /proc is mounted hidepid)"
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
check "hands creates a file" as_hands bash -c "cd '$WS' && echo h > from-hands"
check "operator appends to it" bash -c "echo o >> '$WS/from-hands'"
check "operator creates a file" bash -c "echo o > '$WS/from-op'"
check "hands appends to it" as_hands bash -c "cd '$WS' && echo h >> from-op"
as_hands /usr/bin/python3 -c "import os,tempfile; fd,p=tempfile.mkstemp(dir='$WS'); os.write(fd,b'x'); os.close(fd); os.replace(p,'$WS/atomic-0600')" 2>/dev/null
if bash -c "echo o >> '$WS/atomic-0600'" 2>/dev/null; then info "operator can append to a 0600 file the hands wrote atomically"; else
  info "operator cannot append to a 0600 file the hands wrote atomically (ACL mask follows the file mode)"
  "$PY" -c 'import json,subprocess,sys; from levain.firing import ws_git; h=ws_git.load_hands(sys.argv[1]); sys.exit(subprocess.run(ws_git.mask_repair_argv(h)).returncode)' "$E" >/dev/null 2>&1
  check "after the mask repair the operator can append to it" bash -c "echo o >> '$WS/atomic-0600'"
fi
echo "== git: every repository is the hands user's (H)"
CANARY_LOG=/tmp/levain-iso-canary; : > "$CANARY_LOG"; chmod 666 "$CANARY_LOG"
PLANT="/usr/bin/id -un >> $CANARY_LOG"
printf '[user]\n\tname = CI Operator\n\temail = ci-operator@invalid\n' > /tmp/levain-iso-clean-gitconfig
opgit() { env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/tmp/levain-iso-clean-gitconfig git "$@"; }
export -f opgit
check "hands creates and commits a repository" as_hands bash -c "cd '$WS' && git init -q repo-h && cd repo-h && echo a > a && git add a && git commit -q -m one"
check "hands commits again, branches and runs gc" as_hands bash -c "cd '$WS/repo-h' && echo b > b && git add b && git commit -q -m two && git checkout -q -b side && git gc -q"
check "the entity's commit carries the operator's git identity" as_hands bash -c "cd '$WS/repo-h' && test \"\$(git log -1 --format=%ae)\" = ci-operator@invalid"
as_hands bash -c "cd '$WS/repo-h' && for h in pre-commit post-checkout post-commit; do printf '#!/bin/sh\n$PLANT\n' > .git/hooks/\$h; chmod +x .git/hooks/\$h; done"
refused "operator git reads a repository the hands user owns" opgit -C "$WS/repo-h" status --short
refused "operator git log in it" opgit -C "$WS/repo-h" log -1
check "ws-git status works on the hands repository" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-h" status --short
check "ws-git log works" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-h" log -1 --oneline
check "ws-git checkout (would fire post-checkout) works" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-h" checkout -q -
as_hands bash -c "cd '$WS' && git init -q repo-p && cd repo-p && git config core.fsmonitor '$PLANT' && git config alias.st '!$PLANT'"
refused "ws-git on a repository whose config names a program" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-p" status
as_hands bash -c "cd '$WS' && git init -q repo-i && cd repo-i && printf '[core]\n\tfsmonitor = $PLANT\n' > /tmp/levain-iso-evil.cfg && git config include.path /tmp/levain-iso-evil.cfg"
refused "ws-git on a repository that includes another config" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-i" status
opgit -C "$WS/repo-h" status >/dev/null 2>&1; opgit -C "$WS/repo-p" status >/dev/null 2>&1
if grep -qx "$ME" "$CANARY_LOG"; then fail "entity-planted code ran as the operator"; else pass "no entity-planted code ran as the operator"; fi
if grep -qx "$H" "$CANARY_LOG"; then info "planted code ran as the hands user under ws-git"; else pass "no planted code ran at all under ws-git"; fi
check "doctor warns that the runner's git trusts every repository" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'EVERY repository'"

echo "== an operator-created repository: doctor fails, ws-adopt hands it over"
check "operator creates a repository in the workspace" bash -c "cd '$WS' && opgit init -q repo-op && cd repo-op && echo o > o && opgit add o && opgit commit -q -m op"
refused "doctor passes with it there" "$LEVAIN" doctor --path "$E"
check "doctor names it" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'do not belong to the entity'"
check "ws-adopt hands it over" "$LEVAIN" ws-adopt --path "$E" "$WS/repo-op"
check "it now belongs to the hands user" test "$(stat -c %u "$WS/repo-op/.git" 2>/dev/null || stat -f %u "$WS/repo-op/.git")" = "$HID"
refused "operator git reads it after adoption" opgit -C "$WS/repo-op" status --short
check "the entity commits in it" as_hands bash -c "cd '$WS/repo-op' && echo e > e && git add e && git commit -q -m entity"
check "its history came across" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-op" log --oneline -2
if "$LEVAIN" doctor --path "$E" | grep -q 'do not belong to the entity'; then fail "doctor still reports an operator repository"; else pass "doctor no longer reports one"; fi
rm -rf "$WS"/repo-op.operator-*

echo "== undo, with a hands process still running"
sudo -n -u "$H" /bin/sleep 600 >/dev/null 2>&1 &
sleep 1
SUDOERS="/etc/sudoers.d/levain-$H"
setup --undo || fail "undo exited nonzero"
refused "hands user still exists" id "$H"
refused "a hands process survived" pgrep -U "$HID"
refused "sudoers drop-in still exists" sudo test -e "$SUDOERS"
refused "sshd drop-in still exists" sudo test -e "/etc/ssh/sshd_config.d/levain-$H.conf"
if [ -e "$CRON_DENY" ]; then refused "hands user still in $CRON_DENY" grep -qx "$H" "$CRON_DENY"; fi
if [ -n "$(cfgval hands_user)$(cfgval hands_uid)$(cfgval hands_workspace)" ]; then fail "hands keys still recorded"; else pass "hands keys removed from confinement.json"; fi
check "the non-empty workspace is kept" test -d "$WS"
check "the files the hands created are now root's, not the operator's" test "$(stat -c %u "$WS/from-hands" 2>/dev/null || stat -f %u "$WS/from-hands")" = 0
check "and the operator can read them" /bin/cat "$WS/from-hands"
if [ -n "$(sudo find "$(dirname "$WS")" -uid "$HID" -print -quit 2>/dev/null)" ]; then fail "files still owned by the dead uid"; else pass "no file left owned by the dead uid"; fi
check "operator creates a file in the workspace after undo" bash -c "echo z > '$WS/after-undo'"
check "the entity's repository went to root, not to the operator" test "$(stat -c %u "$WS/repo-h/.git" 2>/dev/null || stat -f %u "$WS/repo-h/.git")" = 0
refused "operator git reads the entity's repository after undo" opgit -C "$WS/repo-h" status --short
check "operator can clone it to keep the work" bash -c "opgit -c safe.directory='$WS/repo-h' -c core.hooksPath=/dev/null clone -q --no-local '$WS/repo-h' /tmp/levain-iso-kept && test -e /tmp/levain-iso-kept/a"
if grep -qx "$ME" "$CANARY_LOG"; then fail "entity-planted code ran as the operator after undo"; else pass "no entity-planted code ran as the operator after undo"; fi
if [ "$(uname)" = Darwin ]; then
  if ls -leR "$(dirname "$WS")" 2>/dev/null | grep -qE '^ [0-9]+: '; then fail "ACL entries left in the workspace"; else pass "no ACL entries left in the workspace"; fi
else
  if getfacl -Rp "$(dirname "$WS")" 2>/dev/null | grep -qE '^(default:)?user:[^:]+:'; then fail "ACL entries left in the workspace"; else pass "no ACL entries left in the workspace"; fi
fi

echo "== a second setup gets a new id (the first one is retired)"
sudo rm -rf "$(dirname "$WS")"
setup >/dev/null || fail "second setup failed"
HID2="$(cfgval hands_uid)"
if [ -n "$HID2" ] && [ "$HID2" != "$HID" ]; then pass "new id $HID2 (was $HID)"; else fail "id reused or missing: '$HID2'"; fi
setup --undo >/dev/null || fail "second undo failed"
check "operator still has sudo" sudo -n /usr/bin/true
rm -f "$SECRET"

echo "== $FAILS failure(s)"
exit "$FAILS"
