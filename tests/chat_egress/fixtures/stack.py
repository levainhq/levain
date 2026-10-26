"""The egress test rig: PKI, a local DNS responder, a recording origin, a raw byte listener, and the SPIKE glue
(`spike_glue/decider_spike4.py` + `lib_proxy4.mjs`) over a real srt 0.0.81. Everything is a child process or a socket on
127.0.0.1; every piece is started by an explicit call and stopped by `stop()`, and `Rig.leftovers()` is how a teardown
test proves nothing it started is still there.

Port stand-ins (as in the spikes): macOS refuses a non-root bind of :80/:443, so a run uses FREE ports as the stand-ins:
`http` for 80 and `tls` for 443 (members of the allow-sets, together with the real 80 / 443), `off_http` / `off_tls`
genuinely outside the sets. The same membership test, one extra member per class.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import env as envmod
from .procs import Jsonl, Managed, free_tcp_ports, free_udp_port, lsof_listeners, tcp_listening, udp_bound, pid_alive

PY = sys.executable
SETTLE = 0.9          # the spikes' settle: the origin logs a connection at close and holds it 0.4 s


@dataclass
class Ports:
    http: int
    tls: int
    off_http: int
    off_tls: int
    raw: int
    dns: int

    @classmethod
    def allocate(cls) -> "Ports":
        a = free_tcp_ports(5)
        return cls(http=a[0], tls=a[1], off_http=a[2], off_tls=a[3], raw=a[4], dns=free_udp_port())

    def tcp(self) -> list[int]:
        return [self.http, self.tls, self.off_http, self.off_tls, self.raw]


def make_pki(root: Path) -> Path:
    """mkcerts.py: an ORIGIN CA + a BAD CA, the good wildcard leaf (*.rb.test) and the bad leaf (bad.rb.test)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run([PY, str(envmod.TOOLS_DIR / "mkcerts.py"), str(root)], check=True, capture_output=True, timeout=120)
    return root / "pki"


# one entry per name x tag so a test can pick a name nothing else has asked for (the responder counts queries per name)
def default_dns_schedule(tag: str = "t") -> dict:
    sched: dict = {}
    for stem in ("pin", "pin2"):
        sched[f"{stem}-{tag}.rb.test"] = {"A": ["127.0.0.1", "10.0.0.1"]}
    for cls, addr in (("loop", "127.0.0.2"), ("1918", "10.0.0.1"), ("ll", "169.254.169.254"), ("doc", "192.0.2.10")):
        sched[f"rb-{cls}-{tag}.rb.test"] = {"A": ["93.184.216.34", addr]}
    sched[f"rb-nat64-{tag}.rb.test"] = {"A": ["93.184.216.34", ""], "AAAA": ["64:ff9b::7f00:1"]}
    # round 4 P6's error path: a name that does not resolve (the spike used nx-p6.invalid, a lookup that leaves the machine)
    sched["nx-p6.rb.test"] = {"A": [""], "AAAA": [""]}
    return sched


class Rig:
    """One module's worth of fixtures. Build it, `start_*` what a test file needs, `stop()` in teardown."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ports = Ports.allocate()
        self.pki: Path | None = None
        self.managed: list[Managed] = []
        self.dns: Managed | None = None
        self.origin: Managed | None = None
        self.raw: Managed | None = None
        self.glues: list["GlueRun"] = []
        self.origin_log = Jsonl(self.root / "origin.jsonl")
        self.dns_log = Jsonl(self.root / "dns.jsonl")
        self.raw_log = Jsonl(self.root / "rawlisten.jsonl")
        self.repo_root: Path | None = None

    # -- starting pieces ---------------------------------------------------------------------------------------------
    def start_pki(self) -> Path:
        self.pki = make_pki(self.root)
        return self.pki

    def start_dns(self, schedule: dict | None = None) -> None:
        import json
        sched = self.root / "dns_schedule.json"
        sched.write_text(json.dumps(default_dns_schedule() if schedule is None else schedule))
        self.dns = Managed([PY, str(envmod.TOOLS_DIR / "dns_local.py"), str(self.ports.dns), str(sched),
                            str(self.dns_log.path), "86400"], workdir=self.root, name="dns", ready="DNS READY").start()
        self.managed.append(self.dns)

    def start_origin(self, kind: str = "bytes") -> None:
        """kind 'bytes': round 3's origin.py (every byte of every connection, TLS bad.rb.test gets the BAD leaf).
        kind 'git': round 4's git_origin.py (per-request log, smart HTTP through git http-backend, `fakeadv`)."""
        assert self.pki is not None, "start_pki() first"
        p = self.ports
        if kind == "bytes":
            argv = [PY, str(envmod.TOOLS_DIR / "origin.py"), str(self.pki), str(self.origin_log.path), "86400",
                    f"plain:{p.http}", f"plain:{p.off_http}", f"tls:{p.tls}", f"tls:{p.off_tls}"]
            ready = "ORIGIN READY"
        else:
            self.repo_root = self.root / "repos"
            self.repo_root.mkdir(exist_ok=True)
            argv = [PY, str(envmod.TOOLS_DIR / "git_origin.py"), str(self.pki), str(self.repo_root),
                    str(self.origin_log.path), "86400", f"plain:{p.http}", f"plain:{p.off_http}", f"tls:{p.tls}", f"tls:{p.off_tls}"]
            ready = "GIT ORIGIN READY"
        self.origin = Managed(argv, workdir=self.root, name=f"origin-{kind}", ready=ready).start()
        self.managed.append(self.origin)

    def start_raw_listener(self, port: int | None = None) -> int:
        port = port or self.ports.raw
        m = Managed([PY, str(envmod.TOOLS_DIR / "rawlisten.py"), str(port), str(self.raw_log.path), "86400"],
                    workdir=self.root, name=f"rawlisten-{port}", ready="RAWLISTEN READY").start()
        self.raw = m
        self.managed.append(m)
        return port

    def start_glue(self, name: str, **kw) -> "GlueRun":
        g = GlueRun(self, name, **kw).start()
        self.glues.append(g)
        return g

    # -- stopping + the leftovers check --------------------------------------------------------------------------------
    def stop(self) -> None:
        for g in reversed(self.glues):
            g.stop()
        for m in reversed(self.managed):
            m.stop()

    def leftovers(self) -> list[str]:
        """Everything this rig started that is still there: a listening port, a bound UDP port, a live process."""
        out = []
        for p in self.ports.tcp():
            if tcp_listening(p):
                out.append(f"tcp {p} still accepts")
        if udp_bound(self.ports.dns):
            out.append(f"udp {self.ports.dns} still bound")
        ls = lsof_listeners(self.ports.tcp() + [g.port for g in self.glues if g.port], [self.ports.dns])
        for ln in ls or []:
            out.append("lsof: " + ln)
        for g in self.glues:
            if g.port and tcp_listening(g.port):
                out.append(f"proxy {g.port} still accepts")
            for pid in g.all_pids:
                if pid_alive(pid):
                    out.append(f"pid {pid} ({g.name}) alive")
        for m in self.managed:
            if m.pgid and pid_alive(m.pgid):
                out.append(f"pid {m.pgid} ({m.name}) alive")
        return out

    # -- observation ---------------------------------------------------------------------------------------------------
    def mark(self, glue: "GlueRun | None" = None) -> "Mark":
        return Mark(origin=self.origin_log.mark(), dns=self.dns_log.mark(), raw=self.raw_log.mark(),
                    frames=glue.frames.mark() if glue else 0, events=glue.events.mark() if glue else 0,
                    obs=glue.obs.mark() if glue else 0, bodies=glue.bodies.mark() if glue else 0)

    def since(self, m: "Mark", glue: "GlueRun | None" = None, settle: float = SETTLE) -> "Seen":
        time.sleep(settle)
        return Seen(origin=self.origin_log.since(m.origin), dns=self.dns_log.since(m.dns), raw=self.raw_log.since(m.raw),
                    frames=[d["msg"] | {"way": d["way"]} for d in glue.frames.since(m.frames)] if glue else [],
                    events=glue.events.since(m.events) if glue else [], obs=glue.obs.since(m.obs) if glue else [],
                    bodies=glue.bodies.since(m.bodies) if glue else [])


@dataclass
class Mark:
    origin: int = 0
    dns: int = 0
    raw: int = 0
    frames: int = 0
    events: int = 0
    obs: int = 0
    bodies: int = 0


@dataclass
class Seen:
    origin: list = field(default_factory=list)
    dns: list = field(default_factory=list)
    raw: list = field(default_factory=list)
    frames: list = field(default_factory=list)
    events: list = field(default_factory=list)
    obs: list = field(default_factory=list)
    bodies: list = field(default_factory=list)

    # --- the shapes the verdicts are stated in --------------------------------------------------------------------
    def origin_bytes(self) -> int:
        """Bytes the origin received (round 3's origin.py logs per connection: `bytes`; round 4's per request)."""
        return sum(d.get("bytes", d.get("head_bytes", 0) + d.get("body_wire_bytes", 0)) for d in self.origin if "conn_end" not in d)

    def origin_requests(self) -> list[dict]:
        return [d for d in self.origin if d.get("method") or d.get("bytes", 0) > 0]

    def req_frames(self) -> list[dict]:
        return [f for f in self.frames if f.get("t") == "req"]

    def verdicts(self) -> list[dict]:
        return [f for f in self.frames if f.get("t") == "verdict"]

    def event_types(self, t: str) -> list[dict]:
        return [e for e in self.events if e.get("t") == t]

    def dns_names(self) -> list[str]:
        return [d["name"] for d in self.dns]


class GlueRun:
    """decider_spike4.py + lib_proxy4.mjs over srt 0.0.81: one 'run' in the spikes' sense (D/E/F/G/H, B2, C1-C3)."""

    def __init__(self, rig: Rig, name: str, *, cap: int = 2097152, log: str = "safe", scheme_check: str = "on",
                 upstream_ca: str = "origin", env_parent: bool = False, hostile_extra_ca: bool = False,
                 extra_http_ports: tuple[int, ...] = (), seconds: int = 3600) -> None:
        self.rig, self.name, self.cap, self.log_mode, self.scheme_check = rig, name, cap, log, scheme_check
        self.upstream_ca, self.env_parent, self.hostile_extra_ca = upstream_ca, env_parent, hostile_extra_ca
        self.extra_http_ports, self.seconds = extra_http_ports, seconds
        self.dir = rig.root / f"run-{name}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames = Jsonl(self.dir / "frames.jsonl")
        self.events = Jsonl(self.dir / "events.jsonl")
        self.obs = Jsonl(self.dir / "obs.jsonl")
        self.bodies = Jsonl(self.dir / "bodies.jsonl")
        self.proc: Managed | None = None
        self.port: int = 0
        self.node_pid: int = 0
        self.all_pids: list[int] = []

    @property
    def ca(self) -> Path:
        return self.dir / "ca.pem"

    @property
    def stderr_first(self) -> str:
        p = self.dir / "proxy.stderr"
        return p.read_text().splitlines()[0] if p.exists() and p.read_text() else ""

    def start(self) -> "GlueRun":
        p, pki = self.rig.ports, self.rig.pki
        assert pki is not None
        pkg = envmod.srt_package_dir()
        assert pkg is not None
        up = {"origin": ["--upstream-ca", str(pki / "origin-ca.pem")],
              "system-explicit": ["--upstream-ca", "system-explicit"],
              "system-plus": ["--upstream-ca", f"system-plus:{pki / 'origin-ca.pem'}"],
              "none": []}[self.upstream_ca]
        http_ports = ",".join(str(x) for x in (80, p.http, *self.extra_http_ports))
        carve = ",".join(f"127.0.0.1:{x}" for x in (p.http, p.tls, *self.extra_http_ports))
        glue = ["--tls-ports", f"443,{p.tls}", "--http-ports", http_ports, "--dns-port", str(p.dns), "--carve", carve,
                *up, "--events", str(self.events.path), "--log", self.log_mode, "--scheme-check", self.scheme_check,
                "--srt-dist", str(pkg / "dist/sandbox")]
        env = dict(os.environ)
        env["SPIKE_NODE"] = envmod.node_bin() or "node"
        env.pop("SPIKE_ENV", None)
        env.pop("NODE_EXTRA_CA_CERTS", None)
        if self.env_parent:
            env["SPIKE_ENV"] = "parent"
        if self.hostile_extra_ca:
            env["NODE_EXTRA_CA_CERTS"] = str(pki / "bad-ca.pem")
        argv = [PY, str(envmod.GLUE_DIR / "decider_spike4.py"), str(self.dir), str(self.seconds), str(self.cap), "--", *glue]
        self.proc = Managed(argv, workdir=self.dir, name="decider", ready="READY ", env=env).start()
        parts = self.proc.ready_line.split()
        self.port, self.node_pid = int(parts[1]), int(parts[3])
        self.all_pids = self.proc.group_pids()
        # srt serves only after its decider hello; wait for the glue's own "serving" line
        deadline = time.time() + 20
        while time.time() < deadline:
            if "serving" in (self.dir / "proxy.stderr").read_text(errors="replace"):
                break
            time.sleep(0.05)
        else:
            tail = (self.dir / "proxy.stderr").read_text(errors="replace")[-600:]
            self.stop()
            raise RuntimeError(f"glue {self.name} never served: {tail}")
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.all_pids = sorted(set(self.all_pids) | set(self.proc.group_pids()))
            self.proc.stop()
            self.proc = None


# --- the CH-D2 glue (R15-14-1's port target) --------------------------------------------------------------------------
REBIND = {"loop": "127.0.0.2", "1918": "10.0.0.1", "ll": "169.254.169.254", "doc": "192.0.2.10",
          "nat64": "64:ff9b::7f00:1"}
HELD_KEY = "HELD-KEY-CHD2"


class ProdGlueRun:
    """The production spawn (`launch.start_egress`: `levain/chat_egress/node/glue.mjs`, the in-process decider, Levain's
    SOCKS refuser, the closed env) over the same rig as `GlueRun` (`origin.py` on the four port stand-ins, its PKI).

    What differs from the spike run, each a seam the production launch already names (`launch.GLUE_SEAMS`):
    srt's lookups go through `--resolve-map` (every name a probe uses; the rebinding names `rb-<cls>.rb.test` answer
    their class, `REBIND`), and the glue logs each one to its stderr (`lookups()`); the decider's own lookup is the
    in-process `Lookup()` (a public answer for every name). One keyed route, `api.rb.test`, holds `HELD_KEY`. The real
    `keys.decide_key` attaches only on port 443, so the rig's 443 stand-in is presented to it as 443; `key_on_plain` set
    True also presents the plain stand-in as (https, 443): the TEST-ONLY leaky variant B12's negative control uses.
    There are no decider frames: what the decider decided is `attempts()` (the dispatch store's rows)."""

    def __init__(self, rig: Rig, name: str, *, upstream_ca: str = "origin", hostile_extra_ca: bool = False,
                 extra_http_ports: tuple[int, ...] = (), answer: str | None = None) -> None:
        self.rig, self.name = rig, name
        self.upstream_ca, self.hostile_extra_ca, self.extra_http_ports = upstream_ca, hostile_extra_ca, extra_http_ports
        self.dir = rig.root / f"prod-{name}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events = Jsonl(self.dir / "events.jsonl")
        self.frames = self.obs = self.bodies = Jsonl(self.dir / "no-such-log.jsonl")     # Rig.mark() reads all four
        self.port = 0
        self.all_pids: list[int] = []
        self.e = None
        self.key_on_plain = False
        self.answer = answer
        self.stopped = None

    @property
    def ca(self) -> Path:
        return self.dir / "ca.pem"

    def names(self) -> dict[str, list[str]]:
        out = {n: ["127.0.0.1"] for n in ("api.rb.test", "s.rb.test", "a.rb.test", "b.rb.test", "b7.rb.test",
                                          "bad.rb.test", "pin-t.rb.test", "pin2-t.rb.test")}
        out.update({f"rb-{cls}.rb.test": [addr] for cls, addr in REBIND.items()})
        return out

    def start(self) -> "ProdGlueRun":
        import dataclasses

        import pytest

        from levain.chat_egress import classify, keys
        from levain.chat_egress import decider as dm
        from levain.chat_egress import launch as lm
        from levain.coordinator.envelopes import EnvelopeStore

        from ..decider_rig import Desk, Live, Lookup
        from ..test_launch import real_launch
        sys.path.insert(0, str(envmod.GLUE_DIR))
        import decider_spike4 as spike  # noqa: E402  (its make_ca: an RSA CA srt's node-forge accepts)

        p, pki = self.rig.ports, self.rig.pki
        assert pki is not None
        self.mp = pytest.MonkeyPatch()
        try:
            real_decide = keys.decide_key

            def decide(route, parsed, **kw):
                if parsed.port == p.tls:
                    parsed = dataclasses.replace(parsed, port=443)
                elif self.key_on_plain and parsed.port == p.http:
                    parsed = dataclasses.replace(parsed, port=443, scheme="https")
                return real_decide(route, parsed, **kw)
            self.mp.setattr(keys, "decide_key", decide)
            self.mp.setenv(lm.TEST_SEAMS_ENV, "1")
            if self.hostile_extra_ca:                    # the PARENT's env; the glue is spawned with env={} regardless
                self.mp.setenv("NODE_EXTRA_CA_CERTS", str(pki / "bad-ca.pem"))
            up = {"origin": ("--upstream-ca", str(pki / "origin-ca.pem")),
                  "bad": ("--upstream-ca", str(pki / "bad-ca.pem")), "system": ()}[self.upstream_ca]
            http_ports = ",".join(str(x) for x in (80, p.http, *self.extra_http_ports))
            carve = ",".join(f"127.0.0.1:{x}" for x in (p.http, p.tls, *self.extra_http_ports))
            rmap = ";".join(f"{n}={','.join(a)}" for n, a in self.names().items())
            route = keys.RouteSnapshot(record_id="rec-chd2", canonical_host="api.rb.test", credential_source="svc",
                                       declared_repos=frozenset(), attach=True, credential_account="acct",
                                       incarnation="1:chd2")

            class Store:
                reads = 0

                def read(self, source, account):
                    Store.reads += 1
                    return HELD_KEY
            self.store = Store()
            self.desk = Desk()
            estore = EnvelopeStore(self.dir / "dispatch")
            key_pem, cert_pem = spike.make_ca()
            self.ca.write_bytes(cert_pem)
            self.e = lm.start_egress(
                store=estore, launch_id=real_launch(estore), node=envmod.node_bin(), srt_prefix=envmod.srt_prefix(),
                events=self.events.path, ca_pem=key_pem + cert_pem, routes=keys.RouteSet.of([route]), liveness=Live(),
                credentials=keys.LaunchCredentials(self.store), cards=self.desk, mode=dm.Fixed(classify.GATED),
                limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0),
                seams=lm.TestSeams(glue_args=("--tls-ports", f"443,{p.tls}", "--http-ports", http_ports, "--carve", carve,
                                              "--resolve-map", rmap, *up), lookup=Lookup()))
        except BaseException:
            self.mp.undo()
            raise
        self.port = self.e.http_port
        self.all_pids = [self.e.proc.pid]
        return self

    # --- what the probes read ----------------------------------------------------------------------------------------
    def lookups(self) -> list[str]:
        """srt's lookups, in order, from the glue's own test-seam trace (one stderr line per lookup)."""
        text = self.e.stderr.read_text(errors="replace") if self.e else ""
        return [ln.rsplit(" ", 1)[1] for ln in text.splitlines() if ln.startswith("glue: test-seam lookup ")]

    def attempts(self) -> list:
        """Every request the decider was asked about: its `egress_attempt` rows."""
        return [r for r in self.e.records.rows(self.e.launch_id) if r.kind == "egress_attempt"]

    def walls(self) -> list[dict]:
        return [e for e in self.events.lines() if e.get("t") == "wall"]

    def stop(self):
        from levain.chat_egress import launch as lm
        try:
            if self.e is not None and self.stopped is None:
                self.stopped = lm.stop_egress(self.e)
        finally:
            self.mp.undo()
        return self.stopped
