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

echo "== workspace: the entity's; the operator reads it (ruling A)"
check "the workspace belongs to the hands user" test "$(stat -c %u "$WS" 2>/dev/null || stat -f %u "$WS")" = "$HID"
if [ -n "$(sudo find "$WS" ! -user "$HID" -print -quit)" ]; then fail "setup left something in the workspace that is not the hands user's"; else pass "setup left nothing in the workspace that is not the hands user's"; fi
check "hands creates a file" as_hands bash -c "cd '$WS' && echo h > from-hands"
check "operator reads it" /bin/cat "$WS/from-hands"
check "operator lists the workspace" /bin/ls "$WS"
refused "operator appends to it" bash -c "echo o >> '$WS/from-hands'"
refused "operator creates a file" bash -c "echo o > '$WS/from-op'"
refused "operator creates a folder" mkdir "$WS/op-folder"
refused "operator renames the entity's file" mv "$WS/from-hands" "$WS/renamed"
refused "operator deletes the entity's file" rm -f "$WS/from-hands"
check "hands creates a folder" as_hands mkdir "$WS/hands-folder"
refused "operator creates a file in the entity's folder" bash -c "echo o > '$WS/hands-folder/x'"
as_hands /usr/bin/python3 -c "import os,tempfile; fd,p=tempfile.mkstemp(dir='$WS'); os.write(fd,b'x'); os.close(fd); os.replace(p,'$WS/atomic-0600')" 2>/dev/null
if /bin/cat "$WS/atomic-0600" >/dev/null 2>&1; then pass "operator reads a 0600 file the hands wrote atomically"; else
  info "operator cannot read a 0600 file the hands wrote atomically (ACL mask follows the file mode)"
  "$PY" -c 'import json,subprocess,sys; from levain.firing import ws_git; h=ws_git.load_hands(sys.argv[1]); sys.exit(subprocess.run(ws_git.mask_repair_argv(h)).returncode)' "$E" >/dev/null 2>&1
  check "after the mask repair the operator reads it" /bin/cat "$WS/atomic-0600"
fi

echo "== ws-put: a file of the operator's, written by the entity's user as data"
printf '#!/bin/sh\necho hi\n' > /tmp/levain-iso-put.sh; chmod 755 /tmp/levain-iso-put.sh
check "ws-put into a new folder" "$LEVAIN" ws-put --path "$E" /tmp/levain-iso-put.sh notes/run.sh
check "it belongs to the hands user" test "$(stat -c %u "$WS/notes/run.sh" 2>/dev/null || stat -f %u "$WS/notes/run.sh")" = "$HID"
check "it is not executable" test "$(stat -c %a "$WS/notes/run.sh" 2>/dev/null || stat -f %Lp "$WS/notes/run.sh")" = 644
OUT=/tmp/levain-iso-out; rm -rf "$OUT"; mkdir -m 777 "$OUT"
as_hands ln -s "$OUT" "$WS/lnk"
refused "ws-put through a symlinked folder" "$LEVAIN" ws-put --path "$E" /tmp/levain-iso-put.sh lnk/x
check "nothing was written through it" test -z "$(ls -A "$OUT")"
refused "ws-put out of the workspace" "$LEVAIN" ws-put --path "$E" /tmp/levain-iso-put.sh ../escaped
refused "ws-put of a file inside the workspace (a link the entity left)" "$LEVAIN" ws-put --path "$E" "$WS/lnk" copied
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

echo "== the bare-repository attack: no folder of the operator's to plant one in"
as_hands bash -c "cd '$WS' && git init -q --bare plant && git --git-dir=plant config core.fsmonitor '$PLANT'"
as_hands bash -c "mkdir -p '$WS/proj' && cd '$WS/proj' && git init -q --bare .git && git --git-dir=.git config core.fsmonitor '$PLANT' && git --git-dir=.git config core.bare false"
refused "operator git inside a planted bare repository" bash -c "cd '$WS/plant' && opgit status"
refused "operator git in a folder whose .git is a planted repository (the rename variant)" opgit -C "$WS/proj" status
git init -q --bare /tmp/levain-iso-opbare
check "control: operator git uses a bare repository of its own, found by discovery" bash -c "cd /tmp/levain-iso-opbare && opgit rev-parse --git-dir"
refused "with safe.bareRepository=explicit it does not" bash -c "cd /tmp/levain-iso-opbare && opgit -c safe.bareRepository=explicit rev-parse --git-dir"
rm -rf /tmp/levain-iso-opbare
if [ -n "$(sudo find "$WS" ! -user "$HID" -print -quit)" ]; then fail "something in the workspace is not the hands user's"; else pass "still nothing in the workspace that is not the hands user's"; fi
if grep -qx "$ME" "$CANARY_LOG"; then fail "entity-planted code ran as the operator"; else pass "no entity-planted code ran as the operator"; fi
if grep -qx "$H" "$CANARY_LOG"; then info "planted code ran as the hands user under ws-git"; else pass "no planted code ran at all under ws-git"; fi
check "doctor warns that the runner's git trusts every repository" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'EVERY repository'"
check "doctor warns while safe.bareRepository is not explicit" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'safe.bareRepository is not'"
printf '[safe]\n\tbareRepository = explicit\n' > /tmp/levain-iso-explicit-gitconfig
if GIT_CONFIG_GLOBAL=/tmp/levain-iso-explicit-gitconfig "$LEVAIN" doctor --path "$E" | grep -q 'safe.bareRepository is not'; then fail "doctor warns although it is explicit"; else pass "doctor does not warn once it is explicit"; fi

echo "== a folder force-created as the operator: doctor fails"
sudo mkdir "$WS/forced" && sudo chown "$(id -u)" "$WS/forced"
refused "doctor passes with it there" "$LEVAIN" doctor --path "$E"
check "doctor names it" bash -c "\"$LEVAIN\" doctor --path \"$E\" | grep -q 'do not belong to the entity'"
sudo rmdir "$WS/forced"
if "$LEVAIN" doctor --path "$E" | grep -q 'do not belong to the entity'; then fail "doctor still reports it"; else pass "doctor no longer reports it"; fi

echo "== ws-git waits while the entity's user runs anything"
sudo -n -u "$H" /bin/sleep 120 >/dev/null 2>&1 &
sleep 1
refused "ws-git while a hands process runs" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-h" status --short
check "and it says why" bash -c "\"$LEVAIN\" ws-git --path \"$E\" -C \"$WS/repo-h\" status | grep -q 'processes running'"
check "ws-adopt refuses too" bash -c "\"$LEVAIN\" ws-adopt --path \"$E\" \"$HOME\" --as never | grep -q 'processes running'"
sudo -n -u "$H" /usr/bin/pkill -U "$HID" sleep; sleep 1
check "ws-git once it stopped" "$LEVAIN" ws-git --path "$E" -C "$WS/repo-h" status --short

echo "== ws-adopt imports a repository of the operator's, from where it is"
MINE="$HOME/levain-iso-mine"; rm -rf "$MINE"
git init -q "$MINE" && git -C "$MINE" checkout -q -b main && echo m > "$MINE/m" && git -C "$MINE" add m && git -C "$MINE" commit -q -m one
git -C "$MINE" checkout -q -b side && echo s > "$MINE/s" && git -C "$MINE" add s && git -C "$MINE" commit -q -m two
git -C "$MINE" checkout -q -b feature/x && git -C "$MINE" commit -q --allow-empty -m three && git -C "$MINE" checkout -q main && git -C "$MINE" tag v1
BEFORE="$(git -C "$MINE" for-each-ref)"
check "ws-adopt imports it" "$LEVAIN" ws-adopt --path "$E" "$MINE"
check "the import belongs to the hands user" test "$(stat -c %u "$WS/levain-iso-mine/.git" 2>/dev/null || stat -f %u "$WS/levain-iso-mine/.git")" = "$HID"
check "every branch and tag came across" bash -c "test \"\$(git -C '$MINE' for-each-ref --format='%(refname) %(objectname)' refs/heads refs/tags)\" = \"\$('$LEVAIN' ws-git --path '$E' -C '$WS/levain-iso-mine' for-each-ref --format='%(refname) %(objectname)' refs/heads refs/tags)\""
check "the original is where it was, unchanged" test "$(git -C "$MINE" for-each-ref)" = "$BEFORE"
refused "operator git reads the import" opgit -C "$WS/levain-iso-mine" status --short
check "the entity commits in it" as_hands bash -c "cd '$WS/levain-iso-mine' && echo e > e && git add e && git commit -q -m entity"
refused "ws-adopt of a repository inside the workspace" "$LEVAIN" ws-adopt --path "$E" "$WS/repo-h" --as again
rm -rf "$MINE"

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
check "operator still reads the workspace after undo" /bin/ls "$WS"
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
