// Asks the Chrome on port argv[2] for Browser.getVersion over its browser websocket; exit 0 and print the product on an
// answer to that very request, exit 1 otherwise (an error reply, a close, an unreadable message, no answer in argv[3] ms,
// default 2000, counted from here: node's own start is not part of it). The caller bounds the whole process on top.
const port = process.argv[2];
const budget = Number(process.argv[3]) || 2000;
const bail = setTimeout(() => process.exit(1), budget);
try {
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener("open", r); ws.addEventListener("error", () => j(new Error("refused"))); ws.addEventListener("close", () => j(new Error("closed"))); });
  const answer = await new Promise((r, j) => {
    ws.addEventListener("message", (m) => { let d; try { d = JSON.parse(m.data); } catch { return; } if (d.id === 1) r(d); });
    ws.addEventListener("error", () => j(new Error("failed")));
    ws.addEventListener("close", () => j(new Error("closed")));
    ws.send(JSON.stringify({ id: 1, method: "Browser.getVersion" }));
  });
  clearTimeout(bail);
  if (answer.error || !answer.result?.product) process.exit(1);
  process.stdout.write(answer.result.product, () => process.exit(0));
} catch { process.exit(1); }
