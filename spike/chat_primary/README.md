# Chat-to-primary spike (throwaway, 1006+9)

Not product code. Probes for driving a hosted-harness entity from the cockpit chat.
Findings live flow-side (`projects/levain/reference/chat_primary_spike_1006.md`).

- `cc_drive.py`: Claude Code over `claude -p --input-format stream-json --output-format
  stream-json --permission-prompt-tool stdio --include-hook-events`. Permission prompts arrive
  as `control_request` / `can_use_tool`; the host answers with `control_response`.
- `codex_drive.py`: Codex over `codex app-server` JSON-RPC (stdio), or `--sock PATH` to join a
  shared `codex app-server --listen unix://PATH` through `ws_uds_bridge.py`. Approval requests
  (`item/commandExecution/requestApproval`, ...) are answered with `{"decision": ...}`.
- `ws_uds_bridge.py`: stdio JSONL to WebSocket over a unix socket (what `--listen unix://` speaks).

Run Codex probes under an isolated `CODEX_HOME`: `levain init --adapter codex` writes the
machine-global Codex home.
