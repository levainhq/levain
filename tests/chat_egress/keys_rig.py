"""The S6 rig: a reported cockpit whose anchored authority log enrols one laptop-presence key (a scratch ssh key in
tmp, never the operator's), a FAKE credential store (never the real keychain: a real item is S10's, a person's), and
frame builders. The key VALUE lives only in the fake store's memory; `artifact_hits` greps every file a test wrote."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.consent.queue import authority_source

AUDIT = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"
# a value no code in the repo spells, so a grep hit is this test's leak and nothing else
KEY = "lvn-S6-HELD-KEY-7f3c9a2e5b1d4068"
SOURCE = "levain-chat-test-item"
ACCOUNT = "levain-chat-test-account"


class FakeStore:
    """The credential store a test hands the decider: the value by source name, and every read counted (`reads` the
    sources, `accounts` the accounts each read named)."""

    def __init__(self, values: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.values = dict(values if values is not None else {SOURCE: KEY})
        self.fail = fail
        self.reads: list[str] = []
        self.accounts: list[str | None] = []

    def read(self, source: str, account: str | None = None) -> str:
        self.reads.append(source)
        self.accounts.append(account)
        if self.fail or source not in self.values:
            raise LookupError("no such item")
        return self.values[source]


class Cockpit:
    """A scratch reported cockpit at tmp/cockpit with one enrolled laptop-presence key."""

    def __init__(self, tmp_path: Path) -> None:
        keys = tmp_path / "keys"
        keys.mkdir()
        self.key = keys / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(self.key)],
                       check=True, capture_output=True, timeout=30)
        pub = " ".join((keys / "laptop.pub").read_text().split()[:2])
        self.fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.key)], check=True,
                                 capture_output=True, text=True, timeout=30).stdout.split()[1]
        self.root = tmp_path / "cockpit"
        self.root.mkdir()
        CockpitDB(self.root).set_install_class("reported")
        self.link({"op": "enrol", "fingerprint": self.fp, "public_key": pub, "class": "laptop-presence",
                   "custody": "passphrase"})

    def sign(self, m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.key), "-n", auth.NAMESPACE],
                              input=m, capture_output=True, check=True, timeout=30).stdout.decode()

    def link(self, op, *, at: datetime | None = None):
        conn = sqlite3.connect(self.root / "cockpit.db", isolation_level=None)
        try:
            return auth.sign_and_append(conn, op, signer_fp=self.fp, sign=self.sign,
                                        clock=lambda: at or datetime.now(timezone.utc), cache=ReplayCache(),
                                        anchor=anchor_path(self.root))
        finally:
            conn.close()

    def state(self) -> auth.AuthoritySnapshot:
        return self.source()()

    def source(self):
        return authority_source(self.root / "cockpit.db", anchor=anchor_path(self.root))

    def kw(self) -> dict:
        """The signing keywords the setup operations take."""
        return {"signer_fp": self.fp, "sign": self.sign, "clock": lambda: datetime.now(timezone.utc)}


def record(rid: str, host: str, *, entity: str = "ent", source: str = SOURCE, repos=("/o/r.git",),
           days: int = 7, placement=None, account: str | None = ACCOUNT) -> dict:
    """A setup record as the log stores it (host already canonical); `account=None` leaves the account out."""
    extra = {} if account is None else {"account": account}
    return {"id": rid, "kind": auth.EGRESS_KEY_KIND, "entity": entity, "host": host, "source": source, **extra,
            "placement": placement or {"scheme": "bearer"}, "repos": sorted(repos),
            "expires_at": auth._stamp(datetime.now(timezone.utc) + timedelta(days=days))}


def frame(method="GET", host="example.com", port=443, path="/", query=None, headers=None, sni="default", rid=1,
          audit=True):
    """A `req` frame as srt sends it (tests/chat_egress/test_parse.py's builder)."""
    hdrs = {k: list(v) for k, v in (headers or {}).items()}
    if audit:
        hdrs.setdefault("x-levain-audit", [AUDIT])
    f = {"t": "req", "id": rid, "method": method, "host": host, "port": port, "path": path}
    if sni == "default":
        sni = host if port == 443 else None
    if sni:
        f["sni"] = sni
    if query is not None:
        f["query"] = query
    if hdrs:
        f["headers"] = hdrs
    return f


def artifact_hits(*roots: Path, value: str = KEY) -> list[str]:
    """Every file under `roots` holding `value` (as bytes, or base64'd as a basic credential would be)."""
    import base64

    needles = [value.encode(), base64.b64encode(value.encode()).rstrip(b"=")]
    hits = []
    for root in roots:
        for p in Path(root).rglob("*"):
            if p.is_file() and not p.is_symlink():
                data = p.read_bytes()
                if any(n in data for n in needles):
                    hits.append(str(p))
    return hits
