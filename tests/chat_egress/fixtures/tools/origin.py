"""Round 3 local ORIGIN: the upstream the proxy dials, on 127.0.0.1 only. Every accepted connection is logged to
<log> (JSONL) at close with every byte it received (repr, capped 4000) and the count: "upstream bytes observed".

usage: origin.py <pki-dir> <log> <seconds> plain:80 plain:8080 tls:443 tls:8443 ...
TLS listeners serve the ORIGIN-CA leaf (*.rb.test), except SNI bad.rb.test, which gets the BAD-CA leaf (B6).
A request is answered once its head (and a Content-Length / chunked body, if declared) is read; the connection then
stays open 0.4 s to catch trailing (smuggled) bytes, and is closed."""
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

PKI = Path(sys.argv[1])
LOG = Path(sys.argv[2]).open("a")


def ctx(stem):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.load_cert_chain(PKI / f"{stem}-leaf.pem", PKI / f"{stem}-leaf.key")
    return c


GOOD, BAD = ctx("good"), ctx("bad")


def pick(sock, name, _c):
    sock.context = BAD if name == "bad.rb.test" else GOOD


GOOD.sni_callback = pick


def body_done(buf: bytes) -> bool:
    head, sep, rest = buf.partition(b"\r\n\r\n")
    if not sep:
        return False
    low = head.lower()
    for line in low.split(b"\r\n"):
        if line.startswith(b"content-length:"):
            try:
                return len(rest) >= int(line.split(b":", 1)[1].strip())
            except ValueError:
                return True
    if b"transfer-encoding:" in low:
        return b"0\r\n\r\n" in rest
    return True


def handler(port, tls):
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        t0, buf, answered = time.time(), b"", False
        sslobj = w.get_extra_info("ssl_object")
        sni = sslobj.server_hostname if sslobj else None
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(r.read(65536), timeout=0.4 if answered else 30)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
                if not answered and body_done(buf):
                    msg = f"origin-ok port={port} tls={tls}\n".encode()
                    w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: " +
                            str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg)
                    await w.drain()
                    answered = True
        except Exception as e:  # noqa: BLE001 - a spike log, never a crash
            buf += f"<<error {type(e).__name__}: {e}>>".encode()
        finally:
            LOG.write(json.dumps({"port": port, "tls": tls, "sni": sni, "t_open": round(t0, 3),
                                  "t_close": round(time.time(), 3), "bytes": len(buf), "answered": answered,
                                  "raw": repr(buf[:4000])}) + "\n")
            LOG.flush()
            w.close()
    return h


async def main():
    servers = []
    for spec in sys.argv[4:]:
        kind, port = spec.split(":")
        port = int(port)
        servers.append(await asyncio.start_server(handler(port, kind == "tls"), "127.0.0.1", port,
                                                  ssl=GOOD if kind == "tls" else None))
    print("ORIGIN READY " + " ".join(sys.argv[4:]), flush=True)
    await asyncio.sleep(float(sys.argv[3]))


asyncio.run(main())
