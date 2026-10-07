"""Print the tool_use / tool_result / result rows of a claude stream-json log (tolerates non-JSON lines)."""
import json, sys
for l in open(sys.argv[1]):
    try: o = json.loads(l)
    except Exception: print("RAW", l.strip()[:200]); continue
    m = o.get("message")
    if isinstance(m, dict) and isinstance(m.get("content"), list):
        for c in m["content"]:
            if isinstance(c, dict) and c.get("type") in ("tool_use", "tool_result"):
                print(c["type"].upper(), json.dumps(c.get("input") or c.get("content"))[:600], "is_error=%s" % c.get("is_error"))
    if o.get("type") == "result": print("RESULT", str(o.get("result"))[:500])
