#!/usr/bin/env python3
"""SPIKE (throwaway): stdio JSONL <-> WebSocket-over-unix-socket, for joining a shared
`codex app-server --listen unix://PATH` (which speaks WebSocket on the socket, one JSON-RPC
message per text frame). Stdlib only; client frames are masked as RFC 6455 requires.

    ws_uds_bridge.py SOCKET_PATH
"""
import base64
import os
import socket
import struct
import sys
import threading


def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise EOFError
        b += c
    return b


def send_text(s, data: bytes, lock):
    hdr = bytearray([0x81])
    n = len(data)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    with lock:
        s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def main():
    s = socket.socket(socket.AF_UNIX)
    s.connect(sys.argv[1])
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1)
    if b" 101 " not in head.split(b"\r\n")[0]:
        sys.exit("handshake failed: " + head.decode(errors="ignore"))
    lock = threading.Lock()

    def pump_in():
        for line in sys.stdin.buffer:
            line = line.strip()
            if line:
                send_text(s, line, lock)
        s.shutdown(socket.SHUT_WR)

    threading.Thread(target=pump_in, daemon=True).start()
    buf = b""
    try:
        while True:
            b0, b1 = recv_exact(s, 2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", recv_exact(s, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", recv_exact(s, 8))[0]
            payload = recv_exact(s, n)
            if op == 0x9:  # ping -> pong
                with lock:
                    s.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload)
                continue
            if op == 0x8:
                break
            buf += payload
            if b0 & 0x80:
                sys.stdout.buffer.write(buf + b"\n")
                sys.stdout.flush()
                buf = b""
    except EOFError:
        pass


if __name__ == "__main__":
    main()
