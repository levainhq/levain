"""Round 4: a raw TCP listener on 127.0.0.1:<port> that logs EVERY byte each connection sends (P5: does a TLS
ClientHello reach a plain-HTTP port?). It never answers. usage: rawlisten.py <port> <log> <seconds>"""
import asyncio
import json
import sys
import time

PORT, LOG, SECS = int(sys.argv[1]), open(sys.argv[2], "a"), float(sys.argv[3])


async def h(r, w):
    t0, buf = time.time(), b""
    try:
        while True:
            c = await asyncio.wait_for(r.read(65536), timeout=20)
            if not c:
                break
            buf += c
    except (asyncio.TimeoutError, ConnectionResetError):
        pass
    LOG.write(json.dumps({"port": PORT, "t_open": round(t0, 3), "bytes": len(buf), "tls_handshake": buf[:1] == b"\x16",
                          "head": repr(buf[:120])}) + "\n")
    LOG.flush()
    w.close()


async def main():
    await asyncio.start_server(h, "127.0.0.1", PORT)
    print(f"RAWLISTEN READY {PORT}", flush=True)
    await asyncio.sleep(SECS)


asyncio.run(main())
