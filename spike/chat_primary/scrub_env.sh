# source-able: runs "$@" with the same env scrub the spike's cc_drive.py and flowConnect use
exec env $(env | grep -E '^(CLAUDE_CODE_[A-Z_]*|CLAUDECODE|CLAUDE_AGENTS[A-Z_]*|ANTHROPIC_API_KEY)=' | cut -d= -f1 | sed 's/^/-u /') "$@"
