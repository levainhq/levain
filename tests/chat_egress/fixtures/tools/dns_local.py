"""Round 3 local DNS responder on 127.0.0.1:<port> (UDP), for names under rb.test only. Time-varying per name:
the schedule file maps a name to {"A": [answer for query 1, query 2, ...], "AAAA": [...]}; the last entry repeats;
an entry is an address, a list of addresses, or "" (no data). A name with no schedule answers A 127.0.0.1.
Every query is logged to <log> as JSONL (name, type, nth query of that name+type, answer).

usage: dns_local.py <port> <schedule.json> <log> <seconds>"""
import json
import socket
import struct
import sys
import time
import ipaddress

PORT, SCHED, LOG, SECS = int(sys.argv[1]), json.load(open(sys.argv[2])), open(sys.argv[3], "a"), float(sys.argv[4])
COUNT: dict = {}


def qname(data, at):
    labels = []
    while data[at]:
        n = data[at]
        labels.append(data[at + 1:at + 1 + n].decode())
        at += 1 + n
    return ".".join(labels).lower(), at + 1


def answer(name, qt):
    sched = SCHED.get(name)
    if sched is None:
        return ["127.0.0.1"] if qt == "A" else []
    seq = sched.get(qt, [""])
    n = COUNT.get((name, qt), 0)
    COUNT[(name, qt)] = n + 1
    a = seq[min(n, len(seq) - 1)]
    return [] if a == "" else (a if isinstance(a, list) else [a])


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", PORT))
    s.settimeout(0.5)
    print(f"DNS READY {PORT}", flush=True)
    end = time.time() + SECS
    while time.time() < end:
        try:
            data, peer = s.recvfrom(2048)
        except socket.timeout:
            continue
        tid, _flags, qd = struct.unpack(">HHH", data[:6])
        name, at = qname(data, 12)
        qtype, _qc = struct.unpack(">HH", data[at:at + 4])
        qt = {1: "A", 28: "AAAA"}.get(qtype, str(qtype))
        question = data[12:at + 4]
        if not name.endswith("rb.test"):
            s.sendto(struct.pack(">HHHHHH", tid, 0x8183, 1, 0, 0, 0) + question, peer)   # NXDOMAIN
            continue
        addrs = answer(name, qt) if qt in ("A", "AAAA") else []
        rrs, n_ans = b"", 0
        for a in addrs:
            ip = ipaddress.ip_address(a)
            if (ip.version == 4) != (qt == "A"):
                continue
            rd = ip.packed
            rrs += struct.pack(">HHHIH", 0xC00C, 1 if qt == "A" else 28, 1, 0, len(rd)) + rd
            n_ans += 1
        s.sendto(struct.pack(">HHHHHH", tid, 0x8180, 1, n_ans, 0, 0) + question + rrs, peer)
        LOG.write(json.dumps({"at": round(time.time(), 3), "name": name, "type": qt,
                              "nth": COUNT.get((name, qt), 0), "answer": addrs, "from": peer[1]}) + "\n")
        LOG.flush()


main()
