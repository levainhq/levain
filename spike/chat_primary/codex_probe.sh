#!/bin/bash
# codex_probe.sh ENTITY CODEX_HOME SANDBOX LOGFILE TCP_PORT UDS_PATH  (isolated CODEX_HOME required)
ENT=$1; export CODEX_HOME=$2; SB=$3; LOG=$4; PORT=$5; UDS=$6
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$ENT" && codex exec --json --skip-git-repo-check -s "$SB" -c approval_policy=never -c check_for_update_on_startup=false $CODEX_EXTRA \
 "Run exactly this one shell command and print its raw output, nothing else: python3 $HERE/probe_net.py $PORT $UDS . If refused or errored, say so verbatim." < /dev/null > "$LOG" 2>&1
grep -E '"command_execution"|"agent_message"' "$LOG" | python3 -c "
import sys,json
for l in sys.stdin:
    o=json.loads(l).get('item',{})
    print(o.get('type'), json.dumps(o.get('command') or o.get('text'))[:200], '| out=', json.dumps(o.get('aggregated_output'))[:400], '| exit=', o.get('exit_code'))"
