"""CH-D2 S5b: the decider's bounded resolver (`levain.chat_egress.resolver`), row R15-12-B15's resolver half:
"a resolver seam that hangs past the timeout -> that request is denied, and a concurrent request to another host
completes; requests past the resolver concurrency cap wait or are denied, never an unbounded thread count"."""

from __future__ import annotations

import threading
import time

import pytest

from levain.chat_egress import resolver as rs


class Seam:
    """A lookup map; a name in `hang` blocks until `release` is set."""

    def __init__(self, answers: dict[str, list[str]], hang: set[str] = frozenset()) -> None:
        self.answers, self.hang, self.release = answers, set(hang), threading.Event()
        self.calls: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        if host in self.hang:
            self.release.wait(30)
        if host not in self.answers:
            raise OSError("NXDOMAIN")
        return self.answers[host]


def _resolver_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "levain-egress-resolve")


def test_a_hung_lookup_denies_that_request_and_another_host_completes() -> None:
    seam = Seam({"good.test": ["93.184.216.34"], "slow.test": ["93.184.216.35"]}, hang={"slow.test"})
    r = rs.Resolver(seam, max_lookups=4, timeout_s=0.5)
    out: dict = {}

    def slow() -> None:
        t0 = time.monotonic()
        try:
            r.resolve("slow.test", 443)
        except rs.ResolveTimeout as exc:
            out["slow"] = (exc.reason, time.monotonic() - t0)

    th = threading.Thread(target=slow)
    th.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    assert r.resolve("good.test", 443) == ["93.184.216.34"]
    assert time.monotonic() - t0 < 0.4
    th.join(5)
    assert out["slow"][0] == "resolve_timeout" and 0.45 <= out["slow"][1] < 1.5
    seam.release.set()


def test_past_the_cap_requests_wait_then_are_denied_and_threads_stay_bounded() -> None:
    seam = Seam({f"h{i}.test": ["93.184.216.34"] for i in range(40)}, hang={f"h{i}.test" for i in range(40)})
    r = rs.Resolver(seam, max_lookups=3, timeout_s=0.4)
    before = _resolver_threads()
    errors: list[str] = []

    def one(i: int) -> None:
        try:
            r.resolve(f"h{i}.test", 443)
        except rs.ResolveError as exc:
            errors.append(exc.reason)

    ths = [threading.Thread(target=one, args=(i,)) for i in range(40)]
    for t in ths:
        t.start()
    peak = 0
    for _ in range(20):
        peak = max(peak, _resolver_threads() - before)
        time.sleep(0.02)
    for t in ths:
        t.join(5)
    assert peak <= 3, peak
    assert len(seam.calls) == 3                        # the 37 others never got a slot: no thread, no lookup
    assert errors == ["resolve_timeout"] * 40
    seam.release.set()
    for _ in range(100):
        if _resolver_threads() == before:
            break
        time.sleep(0.01)
    assert _resolver_threads() == before
    # the hung slots came back when the lookups returned: the pool serves again
    seam.hang.clear()
    assert r.resolve("h1.test", 443) == ["93.184.216.34"]


def test_no_answer_and_an_error_are_resolve_failed() -> None:
    r = rs.Resolver(Seam({"empty.test": []}), timeout_s=1)
    with pytest.raises(rs.ResolveFailed):
        r.resolve("empty.test", 443)
    with pytest.raises(rs.ResolveFailed):
        r.resolve("nx.test", 443)


def test_the_system_lookup_queries_the_absolute_name(monkeypatch) -> None:
    seen: list[str] = []

    def fake(host, port, **kw):
        seen.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(rs.socket, "getaddrinfo", fake)
    assert rs.system_lookup("example.com", 443) == ["93.184.216.34"]
    assert seen == ["example.com."]
