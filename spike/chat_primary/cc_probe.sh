#!/bin/bash
# cc_probe.sh ENTITY_DIR SETTINGS_JSON_FILE SOURCES LOGFILE TCP_PORT UDS_PATH
# One headless CC turn that is told to run probe_net.py through its Bash tool. Raw stream kept in LOGFILE.
ENT=$1; SET=$2; SRC=$3; LOG=$4; PORT=$5; UDS=$6
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$ENT" && command claude -p --model haiku --output-format stream-json --verbose \
  --setting-sources "$SRC" ${SET:+--settings "$SET"} --permission-mode ${CC_PMODE:-bypassPermissions} \
  --max-budget-usd 0.50 \
  "Run exactly this one shell command with your Bash tool and print its raw output, nothing else:

python3 $HERE/probe_net.py $PORT $UDS

If the tool call is refused or errors, say so verbatim." > "$LOG" 2>&1
python3 "$HERE/summarize_stream.py" "$LOG"
