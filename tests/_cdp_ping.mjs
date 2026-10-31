// Asks the Chrome on port argv[2] for Browser.getVersion over its browser websocket; exit 0 and print the product on an answer,
// exit 1 otherwise. The caller bounds the wait (a Chrome that does not answer is the case this exists for).
const port = process.argv[2];
try {
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener("open", r); ws.addEventListener("error", () => j(new Error("refused"))); });
  const answer = await new Promise((r) => {
    ws.addEventListener("message", (m) => r(JSON.parse(m.data)));
    ws.send(JSON.stringify({ id: 1, method: "Browser.getVersion" }));
  });
  ws.close();
  if (!answer.result?.product) process.exit(1);
  process.stdout.write(answer.result.product);
  process.exit(0);
} catch { process.exit(1); }
