"""gw-keys (S1 session 3b): the credential side of the gateway, run as `_levain_keys`. This tip holds WHO MAY CONNECT
(design 1.5, session 3b-3a): keys' `R/keys/front.sock` server, the registration table, the two admission pools, the
`F` path, the NOFILE floor, the process entry and its one shutdown trigger; and WHO MAY ASK (session 3b-3b): the admin
server on `R/keys/admin.sock`; and the `Q` handler: its frame, token states and request path (the route against the
token's claims, keys' own policy load, the re-checks, the dispatch seam at its refusing default, the credential, and
the request through `Upstream.write`); and the UPSTREAM half (session 3b-4): the address check, TLS through
`upstream.py`, the h11 request and response, the relay `R` `b`* `e` through the redaction scanner, and keys' log
`gw.jsonl` (one record per exchange, plus the `levain.broker` notes); and the reservation ledger's wiring (S1 3c-1,
`ledger.py`): the request check's last step admits a hold against the route's per-session cap, the head goes only
behind its durable `reserved` line and the ledger's send gate, and every exit resolves the hold; and the metered
request path (S1 3c-2a, R4 §1.7, `codec.py`): the body buffered under a keys-wide budget, parsed, checked against the
route's price table, its output ceiling written in, held at its estimate, and forwarded as keys' own serialization;
and the attachment (S1 3c-3, CONTRACT §4.5): keys' one long-lived `KEYS-ATTACH` connection to CONTROL, on which it
sends `FRONT-LOST` (a front gone without keys' `K`, or a registration unclaimed V23 after FRONT-ARM), `SPEND-OVERRUN`
(the overrun stop, R4 §1.4) and the `SESSIONS` reconcile; with no attachment keys refuses every token, and the
attachment's EOF is the admin EOF's fail-closed clear and closes the admin connection (each EOF closes the other);
and the lineage outbox (S1 3c-6c; SCHEMAS §2.1 `levain-dispatch-lineage/3`, R4 §3, head rulings H6-3 .. H6-28): a
ticketed exchange's `reserved` line carries its `lineage_base`, and every resolution keys makes of it carries keys' whole
`/3` row (`lineage_row`, from the codec's ONE response-metadata reader); a ticketed `Q` refused before any hold is
counted in its (session, reason) window, whose one `refused` line is written at the window's close; on each attachment
the sender delivers every durable payload line not durably acked, in ref order, one paced envelope at a time
(`LINEAGE-APPEND`), and applies CONTROL's acks and quarantines to the ledger; a full outbox refuses ticketed `Q`s.

Known edges of the ledger wiring (3c-1 B1 L2, 10-23; not built, never a loss):
- before Python 3.12 a cancel landing in the loop turn the durable wait completes is lost (bpo-42130; reproduced on
  /usr/bin/python3 3.9.6); money-safe because `_detach` marks the exchange detached before it cancels, so
  `Upstream.write` raises StaleExchange and `mark_sent` is never reached.

The admin server (design 1.5 "The admin connection", "Fencing", "Order inside one connection"):
- a connection is accepted only from the `_levain_control` uid, read from the kernel; any other peer is closed unread.
- framing and answer shape are `levain-control/1`'s: a 4-byte big-endian length and one canonical JSON object,
  `{"v":1,"op","id",...}` in, `{"id","ok":true,...}` or `{"id","ok":false,"refused",...}` out. A frame that is not
  one closes the connection, which is its EOF.
- each request runs as its own task carrying the admin epoch of its connection, so answers go out in any order; two
  requests naming one session run in frame order, the order assigned in the read loop with no `await` between the
  read and the append to that session's chain.
- FRONT-REGISTER / FRONT-ARM / DROP-SESSION are the fenced table calls; RELOAD {generation, item_sha256, keep} loads
  a dispatch route map under keys' one RELOAD lock (RELOAD names no session, so the per-session order cannot order it):
  every check re-run by keys itself (`levain.slot.routemaps`), keys' own head written durably before it serves or acks;
  its file work is one transaction at a time (`KeysRoutes.txn`) and is waited out even when a preemption cancels it.
- STATUS (3c-9 session D) takes no argument and changes nothing: the reservation ledger's size and the brokered
  (upstream host, credential name) pairs keys serves, and (D1) each served OpenRouter key's limit as keys' own
  refresher last read it, with its age, which CONTROL relays to the operator (`KEYS-STATUS`); names and numbers only,
  never a credential's bytes, and no upstream connection: STATUS answers from memory, the refresher reads.
- a newer connection PREEMPTS in five steps: bump the epoch, fence every live exchange (marked detached, so its next
  upstream write is refused) and stop reading the old one; cancel its request tasks and
  await them, bounded; the fail-closed clear in two phases (`reg_high` kept); close the old connection; adopt.
- the adopted connection's EOF fails closed: the epoch is bumped (its in-flight tasks are stale from that instant) and
  every registration is detached, `K` sent on each STATUS connection, and the attachment closed.

Who may connect, decided at accept, after `peer.peer_of` and before the first frame:
1. the peer's uid must be a `_levain_gw<n>` uid and its `(uid, pid, start)` must name a registration, else the
   connection is closed with no frame (an unregistered peer learns nothing).
2. the STATUS pool: a registration that is ARMED and holds no STATUS candidate makes the connection its candidate; its
   first frame must be `H` naming the registration's session and incarnation, and keys answers `A` (CLAIMED). Never
   counted against anything else, so a registered front's STATUS connection is never refused for room.
3. the exchange pool: a CLAIMED registration makes the connection an exchange candidate; its first frame must be `Q`.
   At most PENDING_PER_PEER candidates per peer that have sent nothing yet and MAX_EXCHANGE_CONNS exchange
   connections in all (both closed at accept), and EXCHANGES_PER_SESSION live exchanges per session (`F`).
4. FIRST_FRAME_S from accept for the first frame, both pools.
Any other first frame, or any frame that does not decode exactly, closes the connection with no frame.

Registrations are FENCED (design 1.5 "Fencing"): one per `front_uid`, replaced only by a greater `reg_token`; an
equal token with every field equal is an idempotent retry; anything else is `registration-stale` carrying `reg_high`.
Every mutation of the table runs under `state_lock` with no `await` inside, and an admin request's checks the admin
epoch first (the one exception: a closing connection leaving its sets, idempotent discards with no `await`). Every
transition is two-phase: phase 1, under the lock, detaches the victims from every table; phase 2, a synchronous loop
right after it, sends `K` on a claimed STATUS connection and closes each victim (aborted ABORT_AFTER_S later).

Runs on the root interpreter (3.9): every lock and event is made inside the running loop.
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import functools
import json
import logging
import os
import pwd
import re
import resource
import signal
import socket
import stat
import sys
import time
from collections import OrderedDict
from typing import Awaitable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar, Union

import h11

from levain.broker import codec, gwwire, ledger, norm, upstream
from levain.broker.creds import (COMMIT_FDS, REFRESH_CONNS, CredLifecycle, CredStore, Credential, CredWriter,
                                 FakeCredLifecycle, check_host, fresh, on_upstream_401, unexpired)
from levain.broker.gwwire import FrameReader, KeysSide, WireError
from levain.broker.peer import PeerUnknown, peer_of, peer_uid
from levain.broker.policy import Price, RoutePolicy, parse_base_map
from levain.broker.redact import Scanner
from levain.broker.upstream import (GRACE, StaleExchange, Upstream, _note, h11_bytes, open_exchange,
                                    pick_address)
from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.slot import Refused as RouteRefused
from levain.slot import routemaps
from levain.slot.grammar import replace_intent_problem

T = TypeVar("T")

# --- the admission values (DECISION, judged against the 8-slot default; one constant each) ------------------------
FIRST_FRAME_S = 5.0
PENDING_PER_PEER = 8
EXCHANGES_PER_SESSION = 32
MAX_EXCHANGE_CONNS = 512
#: the `_levain_gw<n>` index space: at most one STATUS connection per registered front.
STATUS_CONNS = 64
RESOLVE_CONNS = 32
# REFRESH_CONNS (imported): the refresh semaphore's size, creds.CredWriter's
DNS_CONCURRENCY = 32
#: fds one in-flight `getaddrinfo` of an uncached name holds [measured 2026-10-22, macOS 26.6, 32 concurrent lookups
#: of distinct uncached names: 36 extra fds under 3.9.6, 35 under 3.12.13, so 2 per lookup rounded up]. Linux is
#: unmeasured: its VM run prints its own.
DNS_FDS_PER_LOOKUP = 2
#: the second admin connection during a preemption, the refresh's response, the credential read, the reader's
#: credential directory when `cred/` did not exist at start (Keys.open_cred_store), and the writer thread's commit: its
#: lock, its temp (or listing, or compare read) and, in the same case, its directory (creds.COMMIT_FDS).
TRANSIENT_NAMED = 4 + COMMIT_FDS
#: passed to listen() and to every start_unix_server, never asyncio's default of 100: asyncio accepts up to this many
#: connections per wakeup before admission closes any of them.
ACCEPT_BACKLOG = 8
ADMIN_CONNS = 1
NOFILE_TARGET = 4096

#: The fixed terms of the NOFILE floor: each is the value its own cap or semaphore is built from.
NOFILE_TERMS = {
    "exchange": MAX_EXCHANGE_CONNS * 2,   # front side + upstream
    "status": STATUS_CONNS,
    "resolve": RESOLVE_CONNS,
    "refresh": REFRESH_CONNS,
    "dns": DNS_CONCURRENCY * DNS_FDS_PER_LOOKUP,
    "transient": TRANSIENT_NAMED + 2 * ACCEPT_BACKLOG,
    "admin": ADMIN_CONNS,
    "attach": 1,   # the one KEYS-ATTACH connection to CONTROL
    "limit": 1,    # the limit refresher's one upstream connection (its reads are one at a time)
    "reload": 16,  # one RELOAD at a time (its lock): a file read, then ssh-keygen's two input pipes, its three
                   # stdio pipes and the exec error pipe, then the head's temp and directory [judged by 3c4, 10-23,
                   # against subprocess.run(capture_output=True, pass_fds=two pipes): 2*2 + 3*2 + 2 + 2 = 14]
}

#: the `F` path's drain: every `F` is followed by SHUT_WR, then the front's frames are read and discarded until `E`,
#: `X`, EOF, DRAIN_S, or this many body bytes read AFTER the `F` (counted from zero there).
DRAIN_S = 5.0
#: the drain cap of a refusal made before the route is trusted (no route policy, hence no `body_cap`, is known yet).
#: Policy bounds no `body_cap` today; DRAIN_S bounds the drain whatever this is.
UNTRUSTED_DRAIN_CAP = 64 * 1024 * 1024
#: phase 2: a victim closed now is aborted this long after, whether or not the front read its `K`.
ABORT_AFTER_S = 2.0
#: shutdown's awaits (the listeners' closure and the detached exchange tasks) share ONE deadline.
SHUTDOWN_WAIT_S = 3.0

#: resolves one registration may have in flight inside the global RESOLVE_CONNS (DECISION, judged: a session's
#: legitimate first burst is a handful of parallel requests, and the per-digest single-flight folds repeats).
RESOLVE_PER_SESSION = 4
#: ONE deadline per TOKEN-RESOLVE, from the `Q` that wanted it: both permits, the connect and the answer.
RESOLVE_S = 5.0
#: digests keys itself saw dropped, answered `token-revoked` without asking (oldest evicted first).
DROPPED_MAX = 64 * 1024
#: a session token as the front sends it: 43 base64url characters (the `Bearer` value).
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
#: CONTROL's TOKEN-RESOLVE refusals keys maps, and the name each maps to.
#: CONTROL's `run-unknown` (a ticketed session with no bound ticket) and keys' own (a run kind resolved with no `run`)
#: answer `route-not-allowed` (403): such a session may call no route, and no retry changes that (3c-5 fix 1).
RESOLVE_REFUSALS = {"token-unknown": "token-invalid", "token-revoked": "token-revoked",
                    "run-unknown": "route-not-allowed"}
#: the session kinds a run ticket renders as (`levain.slot.tickets.RENDERS_AS`); such a session always carries a run.
RUN_KINDS = frozenset({"dispatch", "planner", "verifier"})

#: preemption's step (2): the old connection's request tasks are awaited this long after their cancel (DECISION,
#: judged: a task's work is memory operations plus closing sockets); one still running is left to its epoch check.
PREEMPT_WAIT_S = 2.0
#: `levain-control/1`'s frame cap, which the admin wire shares.
ADMIN_FRAME_MAX = 64 * 1024
#: the most bytes STATUS's answer (its canonical JSON, `id` and `ok` included) may take: half the frame cap, so CONTROL
#: re-wraps the same fields for the operator inside its own frame of the same cap. A list that does not fit is cut
#: from its end and the cut counted, because a frame past the cap makes CONTROL's reader close the admin connection,
#: and that EOF revokes every session registered on it (L3 r1 K1).
STATUS_ANSWER_MAX = ADMIN_FRAME_MAX // 2
#: keys' own read of each served OpenRouter key's limit (D1, 1025+40 rulings 3, DECISIONS): one read's whole deadline
#: (resolve, connect, TLS, request, response), and the wait between passes; the first pass runs at start. Attributes
#: on Keys (`key_limit_s`, `key_limit_every_s`) so a test scales them. No CONTROL answer waits on a read.
KEY_LIMIT_S = 5.0
KEY_LIMIT_EVERY_S = 300.0
#: why the latest limit read failed: a fixed token each (doctor words every one), never text from the response
LIMIT_FAILURES = ("no-credential", "host-mismatch", "expired", "bad-credential", "address-refused", "unreachable",
                  "timeout", "http-2xx", "http-3xx", "http-4xx", "http-5xx", "http-other", "bad-body", "bad-value",
                  "internal")
#: the largest microdollar amount a limit read carries (10**15 microdollars = one billion dollars), either sign for
#: `limit_remaining` (an overdrawn key's is negative), never negative for `limit`; a number past it or not finite is
#: `bad-value`, never a number
LIMIT_MICRODOLLARS_MAX = 10 ** 15
#: OpenRouter's `limit_reset` as keys carries it: a short period word
LIMIT_RESET_RE = re.compile(r"[a-z_]{1,32}\Z")
#: once an admin frame's first byte is in, the rest of its length and its body must arrive within this, and an
#: answer's write must drain within it (the connection idles legitimately only BETWEEN frames).
ADMIN_FRAME_S = 10.0
#: request tasks one admin connection may hold at once; one more closes the connection, which is its EOF (DECISION,
#: judged: well above a legitimate CONTROL's concurrency, its retransmits included).
ADMIN_INFLIGHT_MAX = 1024
#: the admin verbs and their argument keys, exactly; every argument but `session` is an unsigned integer.
ADMIN_VERBS = {
    "FRONT-REGISTER": frozenset({"session", "front_uid", "pid", "start", "incarnation", "reg_token"}),
    "FRONT-ARM": frozenset({"session", "reg_token"}),
    "DROP-SESSION": frozenset({"session"}),
    "RELOAD": frozenset({"generation", "item_sha256", "keep"}),
    "STATUS": frozenset(),
}
#: RELOAD's `item_sha256`: the hex SHA-256 of the route-map item (3c design r3 §2.3, R4 §2)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

#: CONTRACT §11 V23, the value `levain.slot.control_client.V23_SECONDS` holds (keys imports no `levain.slot` module, so
#: it is restated here; a test pins the two equal): the unclaimed registration's clock from `armed_at` (§4.5), and the
#: wait for an answer to each request keys sends on its attachment.
V23_S = 10.0
#: 3c design r3 §2.1 (e): keys sends SESSIONS on every attachment's open and this often on the open attachment.
RECONCILE_S = 60.0
#: R4 §0 (r3 §2.1 (e) amended): this many CONSECUTIVE SESSIONS sends with no answer is the attachment's EOF.
RECONCILE_MISSES = 3
#: DECISION: the attach client's reconnect backoff, doubling from the first value to the cap.
ATTACH_RETRY_FIRST_S = 0.25
ATTACH_RETRY_MAX_S = 5.0
#: DECISION: one SPEND-OVERRUN attempt's answer deadline (CONTROL's `KEYS_ANSWER_S` for its own admin verbs); a missed
#: one is sent again, same payload, until V23 from the first send.
ATTACH_ATTEMPT_S = 2.0
#: DECISION: the digests one SESSIONS carries (CONTROL's `SESSIONS_DIGESTS_MAX`); more are sent as several.
SESSIONS_CHUNK = 512

GW_ACCOUNT = "_levain_gw{}"
KEYS_ACCOUNT = "_levain_keys"
CONTROL_ACCOUNT = "_levain_control"
FRONTS_GROUP = "levain-gwfronts"
#: admin.sock's group: a shared group whose only members are keys and CONTROL, so no other account can connect (and
#: the peer uid check admits CONTROL alone). keys binds the socket as itself, so it must be a member for the chown to
#: succeed; a primary group stays its own account's only, hence a shared group and not CONTROL's.
ADMIN_GROUP = "levain-keysadmin"

_K = gwwire.encode(b"K")
_A = gwwire.encode(b"A")

#: a brokered route's upstream is `https` (policy refuses any other scheme) and names no port
UPSTREAM_PORT = 443
#: an upstream redirect is never followed: `upstream-redirect` (design 1.6); other 3xx (304) pass
REDIRECTS = frozenset({301, 302, 303, 307, 308})
#: the largest `b` frame keys writes (the wire's cap for `b`)
BODY_FRAME = gwwire.CAPS[b"b"]
#: a header value the wire carries: visible ASCII and tab
_HVALUE = re.compile(r"[\t\x20-\x7e]*\Z")
#: keys' log, under the lib root (design 1.9): one JSON line per exchange and per diagnostic note
GW_LOG = ("keys", "log", "gw.jsonl")
#: the reservation ledger, under the lib root (design 3c-1 §2.5): opened by `main()` before anything is bound
LEDGER = ("keys", "ledger.jsonl")

#: the keys-wide budget for buffered request bodies (R4 §1.7 step 2, r3 §2.2 c step 2), taken by actual bytes as they
#: arrive. [judged by 1023+8 head, 10-23: one max-size body per session] r3 gives no value; the session share is a
#: quarter of it and must be at least `policy.BODY_CAP_MAX`, or a max-size body could never be admitted.
BODY_BUFFER_BUDGET = 128 * 1024 * 1024
#: KNOWN EDGE [measured by 3c2a-b2-fix1, 10-23: 26.32x, empty objects, 3.12; file b2f1_mem.py]: parsing a body of empty
#: objects peaks at about 26x its length (842 MiB on one 32 MiB body), and the buffer does not charge it. It stays
#: one such transient because parse, check, estimate and serialize run in one synchronous call (`_check_request`); a
#: move off the loop must keep one parse at a time. What an exchange keeps after that is the serialization alone, and
#: the buffer holds `max(raw, serialized)` until the exchange ends.
#: one session's share of BODY_BUFFER_BUDGET: a take past it ends that session's exchange, never another session's
BODY_BUFFER_SESSION_MAX = BODY_BUFFER_BUDGET // 4
#: the whole request body must arrive within this many seconds of `A`, else `body-timeout` (408)
BODY_READ_S = 60.0
#: the largest `h11.Data` chunk of the request body keys writes upstream before it drains: an upstream that answers
#: early (a 401, a 413) and stops reading has at most this much past the transport's high-water mark queued, and the
#: head's arrival stops the next chunk. [judged by 3c2a-b2-fix1, 10-23, against asyncio's default 64 KiB write
#: high-water mark: a chunk of that size reaches the drain that waits]
UPLOAD_CHUNK = 64 * 1024

# --- lineage (S1 3c-6c; SCHEMAS §2.1 `levain-dispatch-lineage/3`; R4 §3; head rulings H6-3 .. H6-28) -----------------
LINEAGE_FORMAT = "levain-dispatch-lineage/3"
#: H6-11: the outbox's bound, in records (a DECISION; a K1-style measurement can raise it). At it a TICKETED `Q` is
#: refused `keys-unavailable` until acks drain; an interactive session writes no lineage and is never refused for it.
LINEAGE_OUTBOX_MAX = 4096
#: H6-11 / H6-19: one `LINEAGE-APPEND` envelope carries at most this many records AND at most this many bytes of its
#: canonical encoding (`levain-control/1`'s 64 KiB frame cap less headroom); keys packs greedily and starts a new
#: envelope at either bound, so CONTROL never sees an envelope over the frame cap (whose close would end the
#: attachment). `levain.slot.lineage`'s ENVELOPE_MAX, and the byte bound its 6b fix round adds (keys imports no sink
#: module; a test pins them equal).
LINEAGE_ENVELOPE_RECORDS = 64
LINEAGE_ENVELOPE_BYTES = 48 * 1024
#: H6-10: one refusal window per (session, reason) of a TICKETED session: its count kept in memory, ONE `refused` line
#: written at its close (kill -9 loses at most one window's counts per (session, reason): a known edge, no money)
REFUSED_ROW_S = 60.0
#: DECISION: the wait before an envelope CONTROL refused, or did not answer, is sent again (the attach backoff's cap)
LINEAGE_RETRY_S = 5.0


def _stamp(t: float) -> str:
    """A lineage timestamp: UTC `YYYY-MM-DDTHH:MM:SSZ`, the shape CONTROL's sink checks (`records.check_timestamp`)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t)))


def lineage_base(run: dict, at: str, hold: int, unproven: bool) -> dict:
    """A ticketed exchange's `lineage_base` (its `reserved` line's `meta`): a WHOLE `/3` record but `source` (the
    triple, added at send) and the outcome. Every field is present (H6-15); the outcome fields hold what a row the
    LEDGER builds from it (owner-ended, closed, the durable wait's timeout, replay's restart: H6-28) can honestly
    say: `settled` (the ledger's), the call's facts unknown (`"unknown"`, nulls), `status` 0. The run's facts are
    TOKEN-RESOLVE's `run` (CONTRACT §3.3), renamed per H6-3: `hold_microdollars` this exchange's hold,
    `run_cap_microdollars` the run's cap."""
    return {"format": LINEAGE_FORMAT, "at": at, "ended_at": at,
            "run_id": run["run_id"], "job_id": run["job_id"], "run_index": run["run_index"],
            "contract_digest": run["contract_digest"], "route": run["route"],
            "routemap_generation": run["routemap_generation"], "route_policy_digest": run["route_policy_digest"],
            "model_requested": run["model_requested"], "model_answered": "unknown", "provider_answered": "unknown",
            "tokens_in": None, "tokens_out": None, "cost_microdollars": None, "cost_reported": None,
            "hold_microdollars": hold, "run_cap_microdollars": run["reserved_microdollars"],
            "settled": "uncertain", "overrun": None, "unproven": bool(unproven), "status": 0, "refused": "",
            "count": None}


def lineage_row(base: dict, *, settled: str, ended_at: str, md: "Optional[codec.Metadata]",
                usage: "Optional[codec.Usage]" = None, cost: Optional[int] = None, overrun: int = 0,
                status: int = 0, refused: str = "") -> dict:
    """keys' row for one resolution of a ticketed exchange (H6-28: keys computes it, the ledger carries it): `base`
    plus the outcome. `md` is the response metadata reader's answer (`codec.UsageReader.metadata`, H6-6) when the call
    happened, None when no upstream call was made (H6-21: `model_answered` / `provider_answered` / `cost_reported`
    are then null; with a call, an unsaid string is `"unknown"` and an unsaid cost null). `usage` and `cost` are the
    settle's: `tokens_in` counts every input class (plain, cache write, cache read), `cost_microdollars` the whole
    priced cost, and `overrun` (H6-7: a `"yes"` row) the part past the hold, null when none. A settle with a cost and
    no usage (another model's reported cost past the hold, verify r2 R2) has the cost and null token counts."""
    row = dict(base)
    call = md is not None
    row.update(ended_at=ended_at, settled=settled, status=status, refused=refused,
               model_answered=((md.model or "unknown") if md is not None else None),
               provider_answered=((md.provider or "unknown") if md is not None else None),
               cost_reported=(md.cost_reported if md is not None else None),
               tokens_in=None, tokens_out=None, cost_microdollars=None, overrun=None, count=None)
    if call and cost is not None:
        row.update(cost_microdollars=cost, overrun=overrun if overrun > 0 else None)
        if usage is not None:
            row.update(tokens_in=usage.in_ + usage.cache_write + usage.cache_read, tokens_out=usage.out)
    return row


def refused_row(run: dict, *, reason: str, count: int, at: str, ended_at: str) -> dict:
    """The row of one refusal window of a ticketed session (H6-16): no call (H6-21: the call's facts null), no money
    (`hold_microdollars` 0), `settled` "no", `refused` the reason and `count` its count, `at` the window's first refusal
    and `ended_at` its close; `status` 0, a local refusal (SCHEMAS §2.1)."""
    row = lineage_base(run, at, 0, False)
    row.update(ended_at=ended_at, settled="no", model_answered=None, provider_answered=None, refused=reason,
               count=count)
    return row


def wire_record(triple: Tuple[str, str, int], payload: object) -> dict:
    """One envelope element (H6-14): the payload the outbox holds, as it is on disk, with its `source` triple (every
    stamp is already in CONTROL's second form: keys' `_stamp`, and the ledger's own rows, `ledger._second`, H6-31).
    A payload that is not an object goes as its triple alone, which CONTROL quarantines (the outbox still drains)."""
    rec = dict(payload) if isinstance(payload, dict) else {}
    rec["source"] = {"ledger": triple[0], "epoch": triple[1], "ref": triple[2]}
    return rec


def lineage_envelope(items: Sequence[Tuple[Tuple[str, str, int], object]],
                     max_records: int = LINEAGE_ENVELOPE_RECORDS,
                     max_bytes: int = LINEAGE_ENVELOPE_BYTES) -> Optional[Tuple[List[Tuple[str, str, int]], dict]]:
    """The next envelope from the outbox (`ledger.unacked()`, ref order), or None when it is empty: the records of
    the FIRST item's (ledger, epoch) in ref order (H6-14: one ledger and epoch per envelope; an old epoch's restart
    rows go in their own), packed greedily up to `max_records` AND `max_bytes` of the envelope's canonical encoding
    (H6-19). Returns the triples it carries, in order, and the envelope."""
    if not items:
        return None
    lid, ep = items[0][0][0], items[0][0][1]
    size = len(canon.canonical_bytes({"ledger_id": lid, "epoch": ep, "records": []}))
    records: List[dict] = []
    sent: List[Tuple[str, str, int]] = []
    for t, payload in items:
        if t[0] != lid or t[1] != ep:
            continue
        rec = wire_record(t, payload)
        try:
            n = len(canon.canonical_bytes(rec))
        except (ValueError, TypeError, RecursionError):
            rec = wire_record(t, None)
            n = len(canon.canonical_bytes(rec))
        add = n + (1 if records else 0)   # compact JSON: elements joined by ","
        if records and (len(records) >= max_records or size + add > max_bytes):
            break
        records.append(rec)
        sent.append(t)
        size += add
    return sent, {"ledger_id": lid, "epoch": ep, "records": records}


_LEDGER_FIELD = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")


def read_lineage_ack(ans: dict, sent: Sequence[Tuple[str, str, int]]
                     ) -> Optional[Tuple[List[Tuple[str, str, int]], Dict[Tuple[str, str, int], str]]]:
    """CONTROL's `LINEAGE-APPEND` answer `{acked: [triple..], quarantined: [{source, field}..]}` as (the acked triples,
    {triple: field}), or None when it is not one: not ok, a triple keys did not send, a quarantine of a triple not
    acked, or nothing acked at all (an envelope that drains nothing is never sent again at once)."""
    if ans.get("ok") is not True:
        return None
    acked, quar = ans.get("acked"), ans.get("quarantined")
    if type(acked) is not list or type(quar) is not list:
        return None
    mine = set(sent)

    def triple_of(v: object) -> Optional[Tuple[str, str, int]]:
        # r1 F3: the on-disk triple shape only (32-hex ledger and epoch, an int ref >= 1), checked before the
        # membership test, so a malformed answer is None and never a raise (an unhashable value once was)
        if type(v) is not dict or set(v) != {"ledger", "epoch", "ref"}:
            return None
        lid, ep, ref = v["ledger"], v["epoch"], v["ref"]
        if type(lid) is not str or _HEX32.fullmatch(lid) is None or type(ep) is not str \
                or _HEX32.fullmatch(ep) is None or type(ref) is not int or ref < 1:
            return None
        t = (lid, ep, ref)
        return t if t in mine else None

    out: List[Tuple[str, str, int]] = []
    for v in acked:
        t = triple_of(v)
        if t is None:
            return None
        if t not in out:
            out.append(t)
    q: Dict[Tuple[str, str, int], str] = {}
    for e in quar:
        t = triple_of(e.get("source")) if type(e) is dict else None
        field = e.get("field") if type(e) is dict else None
        if t is None or t not in out or type(field) is not str or _LEDGER_FIELD.match(field) is None:
            return None
        q[t] = field
    return (out, q) if out else None


def _at(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _header(headers: Sequence[Tuple[bytes, bytes]], name: bytes) -> bytes:
    """The first value of header `name` (lower-case) in an h11 head, b"" when absent."""
    for raw_name, raw_value in headers:
        if bytes(raw_name).lower() == name:
            return bytes(raw_value)
    return b""


#: the largest response body `upstream_error` reads, and its field shapes
UPSTREAM_ERROR_BODY_MAX = 8192
UPSTREAM_ERROR_MESSAGE_MAX = 200
_ERR_TYPE = re.compile(r"[a-z_]{1,64}")
_ERR_REQUEST_ID = re.compile(r"req_[A-Za-z0-9]{1,64}")
_ERR_TOKENISH = re.compile(r"[A-Za-z0-9_-]{20,}")


def _json_media(content_type: bytes) -> bool:
    """True for a content-type whose media type is application/json (parameters ignored)."""
    return content_type.split(b";", 1)[0].strip().lower() == b"application/json"


def upstream_error_of(body: bytes, scanner: Scanner) -> Optional[dict]:
    """The exchange record's `upstream_error` for an error response's whole `body`: `{"type", "message",
    "request_id"}` read from the vendor's JSON error, or None when the body is not that. Total: any failure reading
    the body is None, because a record must never break the relay. `message` is scanned whole by the exchange's own
    `scanner` (a held credential anywhere in it makes the message the marker), then cut to UPSTREAM_ERROR_MESSAGE_MAX
    characters, then has every run of 20 or more `[A-Za-z0-9_-]` characters replaced by `<redacted>`; the scanner's
    count is left as the relay made it. `type` is kept only when it is `[a-z_]{1,64}` and `request_id` only when it is
    `req_[A-Za-z0-9]{1,64}`, and either one the scanner would change is None."""
    before = scanner.count
    try:
        return _upstream_error(body, scanner)
    except Exception as e:
        logging.getLogger("levain.broker").warning("upstream_error unreadable (%s)", type(e).__name__)
        return None
    finally:
        scanner.count = before   # the same bytes were already counted when the relay scanned the body


def _upstream_error(body: bytes, scanner: Scanner) -> Optional[dict]:
    doc = json.loads(body.decode("utf-8"))
    err = doc.get("error") if type(doc) is dict else None
    if type(err) is not dict:
        return None

    def clean(v: object, shape: "re.Pattern[str]") -> Optional[str]:
        if type(v) is not str or not shape.fullmatch(v):
            return None
        raw = v.encode("utf-8", "replace")
        return v if scanner.value(raw)[0] == raw else None

    typ = clean(err.get("type"), _ERR_TYPE)
    msg = err.get("message")
    if type(msg) is str:
        scanned, _ = scanner.value(msg.encode("utf-8", "replace"))
        msg = _ERR_TOKENISH.sub("<redacted>", scanned.decode("utf-8", "replace")[:UPSTREAM_ERROR_MESSAGE_MAX])
    else:
        msg = None
    rid = clean(doc.get("request_id"), _ERR_REQUEST_ID)
    if typ is None and msg is None:
        return None
    return {"type": typ, "message": msg, "request_id": rid}


def new_record(session: str, q: dict) -> dict:
    """An exchange's gw.jsonl record as admission makes it: names and counts only, never a value of the request."""
    return {"session": session, "slot": None, "route": q["route"], "method": q["method"], "template": None,
            "status": None, "refused": None, "bytes_up": 0, "bytes_down": 0, "upstream_address": None,
            "redactions": 0, "upstream_error": None, "t0": time.monotonic()}


class GwLog:
    """`L/keys/log/gw.jsonl`: created 0600 in a 0700 directory, opened once (append, no symlink followed) before the
    start log counts keys' fds. A write that fails is reported once on stderr and never raises."""

    def __init__(self, lib_root: str) -> None:
        d = os.path.join(lib_root, *GW_LOG[:-1])
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.path = os.path.join(d, GW_LOG[-1])
        self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self._failed = False

    def write(self, obj: dict) -> None:
        try:
            os.write(self.fd, (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        except Exception as e:
            if not self._failed:
                self._failed = True
                try:
                    print(f"levain gw-keys: gw.jsonl write failed ({type(e).__name__})", file=sys.stderr, flush=True)
                except Exception:
                    pass


class GwLogHandler(logging.Handler):
    """The `levain.broker` logger's handler: each diagnostic note (every one is written by `upstream._note`, from
    names and counts only) becomes a `note` record in gw.jsonl."""

    def __init__(self, log: GwLog) -> None:
        super().__init__(logging.INFO)
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log.write({"at": _at(record.created), "kind": "note", "level": record.levelname,
                            "msg": record.getMessage()})
        except Exception:
            self.handleError(record)


def install_log(lib_root: str) -> GwLog:
    """keys' logging, once, in the process entry: the gw.jsonl handler on `levain.broker`, and stderr kept for
    warnings (the unit's own log), which is where the notes went before a handler existed."""
    log = GwLog(lib_root)
    lg = logging.getLogger("levain.broker")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(GwLogHandler(log))
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    lg.addHandler(err)
    return log


# --- the body buffer ------------------------------------------------------------------------------------------------

class BodyBuffer:
    """The keys-wide budget for buffered request bodies (R4 §1.7 step 2): `used` bytes in all and per session. Every
    take is a non-blocking try: one that cannot be granted ends THAT exchange `keys-unavailable` (503) and keeps
    nothing, so no exchange waits holding bytes (no hold-and-wait, so no deadlock)."""

    def __init__(self, budget: int = BODY_BUFFER_BUDGET, session_max: int = BODY_BUFFER_SESSION_MAX) -> None:
        self.budget, self.session_max = budget, session_max
        self.used = 0
        self.by_session: Dict[str, int] = {}

    def taker(self, session: str) -> "BodyTake":
        return BodyTake(self, session)


class _OneShot:
    """The collected request body, handed over once: `take` returns the bytes and leaves the holder empty, so the
    exchange's frame never keeps the raw body after `_check_request` has parsed it (only the serialization lives)."""

    __slots__ = ("b",)

    def __init__(self, b: bytes) -> None:
        self.b = b

    def take(self) -> bytes:
        b, self.b = self.b, b""
        return b


class BodyTake:
    """One exchange's bytes in its BodyBuffer. `give_back` returns them all and is idempotent (not `release`: T4 reads
    that name as a ledger call); `_serve_q` calls it when the EXCHANGE ends, never at the upload's end."""

    __slots__ = ("buf", "session", "n")

    def __init__(self, buf: BodyBuffer, session: str) -> None:
        self.buf, self.session, self.n = buf, session, 0

    def take(self, n: int) -> None:
        if not n:
            return   # a `length:0` body: no entry, so `by_session` never keeps a zero for a session
        b = self.buf
        mine = b.by_session.get(self.session, 0)
        if b.used + n > b.budget or mine + n > b.session_max:
            raise norm.Refused("keys-unavailable", "body buffer")
        b.used += n
        b.by_session[self.session] = mine + n
        self.n += n

    def cover(self, n: int) -> None:
        """Take what makes this exchange's bytes at least `n` (keys' serialization, which writing the ceiling in can
        grow past the raw body); a take that cannot be granted is `keys-unavailable`, as any take."""
        if n > self.n:
            self.take(n - self.n)

    def give_back(self) -> None:
        n, self.n = self.n, 0
        if not n:
            return
        b = self.buf
        b.used -= n
        left = b.by_session.get(self.session, 0) - n
        if left > 0:
            b.by_session[self.session] = left
        else:
            b.by_session.pop(self.session, None)


#: what an upstream connection's failure raises, each answered `upstream-error` (`upstream-timeout` for a deadline):
#: a refused or reset connect, a TLS failure (`ssl.SSLError` is an OSError), a write or drain on a lost connection,
#: and a seam that is not built yet. `StaleExchange` is none of them: a detached exchange still ends with no answer.
UPSTREAM_FAILURES = (OSError, EOFError, asyncio.TimeoutError, NotImplementedError)


class UpstreamFailed(Exception):
    """An upstream failure translated to the refusal the front is answered: `name` is `upstream-error` or
    `upstream-timeout`."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class FrontAborted(Exception):
    """The front's abort (`X`, EOF, a frame the wire refuses) was read before the request head was written."""


class FrontFrames:
    """The front's frames for one exchange, read by ONE task that outlives whoever waits on it: a waiter that stops
    waiting (a lost race, a cancel) leaves that read in place for the next one, so no frame is lost or split by a
    cancel mid-read, and keys holds no more than the one frame it returns. `read` has `FrameReader.read`'s shape."""

    __slots__ = ("frames", "_t", "responded")

    def __init__(self, frames: FrameReader) -> None:
        self.frames = frames
        self._t: Optional[asyncio.Future] = None
        #: `R` or `U` has been written to the front: from then no `F` may follow (the wire's order)
        self.responded = False

    def pending(self, allowed: frozenset) -> asyncio.Future:
        """The next frame's read, started now if none is in flight (its allowed set is the one given here)."""
        if self._t is None:
            self._t = asyncio.ensure_future(self.frames.read(allowed))
        return self._t

    async def read(self, allowed: frozenset) -> Optional[Tuple[bytes, object]]:
        t = self.pending(allowed)
        got = await asyncio.shield(t)
        self._t = None
        return got

    def aborted(self) -> bool:
        """The next frame, already read, is the front's abort: `X`, EOF, or a read that failed."""
        t = self._t
        if t is None or not t.done():
            return False
        if t.cancelled() or t.exception() is not None:
            return True
        got = t.result()
        return got is None or got[0] == b"X"

    def close(self) -> None:
        t, self._t = self._t, None
        if t is None:
            return
        if not t.done():
            t.cancel()
        elif not t.cancelled():
            t.exception()   # retrieved: the exchange is over, a failed read is its end, not an error to report


async def upstream_step(aw: Awaitable[T]) -> T:
    """Await one upstream step; an UPSTREAM_FAILURES raise becomes UpstreamFailed (noted), anything else, a
    StaleExchange or a cancel included, propagates unchanged."""
    try:
        return await aw
    except UPSTREAM_FAILURES as e:
        # on 3.11+ asyncio's TimeoutError IS the builtin one, an OSError: the deadline is told apart first
        timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
        _note(logging.WARNING, "levain gw-keys: upstream failed (%s%s)", type(e).__name__,
              f": {e.detail}" if isinstance(e, upstream.BadResponse) else "")
        raise UpstreamFailed("upstream-timeout" if timeout else "upstream-error") from None


async def upstream_send(up: Upstream, data: bytes) -> None:
    """`up.write(data)` (the gate) and its drain, as one upstream step."""
    async def send() -> None:
        up.write(data)
        await up.drain()
    await upstream_step(send())


def bounded() -> int:
    return sum(NOFILE_TERMS.values())


# --- the table ------------------------------------------------------------------------------------------------------

class Registration:
    """One front: FRONT-REGISTER's fields, and its admission state REGISTERED -> ARMED -> CLAIMED (-> LOST when its
    STATUS connection ends without keys' `K`)."""

    __slots__ = ("session", "front_uid", "pid", "start", "incarnation", "reg_token", "state", "armed_at",
                 "status", "pending", "exchanges", "resolve_sem", "admin_epoch")

    def __init__(self, session: str, front_uid: int, pid: int, start: int, incarnation: int, reg_token: int) -> None:
        self.session, self.front_uid, self.pid, self.start = session, front_uid, pid, start
        self.incarnation, self.reg_token = incarnation, reg_token
        self.state = "registered"
        self.armed_at: Optional[float] = None
        self.status: Optional[Conn] = None          # the STATUS candidate, then the STATUS connection
        self.pending: Set[Conn] = set()             # exchange candidates that have sent no frame yet
        self.exchanges: Set[Conn] = set()           # connections past `Q`
        #: this registration's share of the resolve slots, made at its first resolve (inside the loop)
        self.resolve_sem: Optional[asyncio.Semaphore] = None
        #: the admin epoch FRONT-REGISTER made it under: an exchange is admitted only while that epoch is current
        self.admin_epoch: Optional[int] = None

    def fields(self) -> tuple:
        return (self.session, self.front_uid, self.pid, self.start, self.incarnation)


class Conn:
    """One accepted connection attributed to a registration. A connection past `Q` IS the exchange (design 1.2's
    `exchange`, 1.5's member of a registration's exchange set): `reg` is the sketch's `exchange.registration`, and
    `upstream` holds the exchange's `Upstream` from `open_exchange`'s store until the release drops it, so a phase 1
    always finds what it must abort."""

    __slots__ = ("writer", "reg", "kind", "task", "detached", "upstream", "admin_epoch", "rec", "usage")

    def __init__(self, writer: asyncio.StreamWriter, reg: Registration, kind: str) -> None:
        self.writer, self.reg, self.kind = writer, reg, kind   # kind: candidate | status | pending | exchange
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.detached = False
        self.upstream: Optional[Upstream] = None
        #: the admin epoch stamped at `Q`, under state_lock: the exchange's mutations and its resolve key compare it
        self.admin_epoch: Optional[int] = None
        #: the exchange's gw.jsonl record, made at admission and written at its end (None before `Q` is admitted)
        self.rec: Optional[dict] = None
        #: the response's side reader once its head arrived: its metadata is the lineage row's (H6-6), a cut one's too
        self.usage: Optional[codec.UsageReader] = None


#: one entry of a phase 1's detached-objects list: a connection to close, or a detached exchange's `Upstream` to abort
Victim = Union[Conn, Upstream]


class AdminConn:
    """One accepted admin connection: the epoch it was adopted on, its read loop's task, its request tasks, and per
    session the last request task read for that session (the tail of that session's chain)."""

    __slots__ = ("reader", "writer", "epoch", "task", "tasks", "last", "preempted", "wlock")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, epoch: int) -> None:
        self.reader, self.writer, self.epoch = reader, writer, epoch
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.tasks: Set[asyncio.Task] = set()
        self.last: Dict[str, asyncio.Task] = {}
        #: set by the preemption that replaces this connection, before it cancels the read loop: the EOF path then
        #: does nothing, and the preemption closes the connection itself, after its clear.
        self.preempted = False
        #: one answer is written and drained at a time (a concurrent drain is not allowed on 3.9's StreamWriter).
        self.wlock = asyncio.Lock()


class Attachment:
    """keys' one `KEYS-ATTACH` connection to CONTROL's socket (§4.5), multiplexed by `id`: the attachment's number (from
    1), the answer futures per request id, one write at a time, the count of consecutive unanswered SESSIONS, and
    whether a SESSIONS was answered on it (a healthy attachment: the reconnect backoff resets after it)."""

    __slots__ = ("reader", "writer", "no", "pending", "next_id", "wlock", "misses", "ended", "healthy")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, no: int) -> None:
        self.reader, self.writer, self.no = reader, writer, no
        self.pending: Dict[int, asyncio.Future] = {}
        self.next_id = 1   # id 1 was the KEYS-ATTACH itself
        self.wlock = asyncio.Lock()
        self.misses = 0
        self.healthy = False
        #: set once its EOF has been handled (`Keys._attach_lost`): nothing is sent on it after that
        self.ended = False


class AdminMalformed(Exception):
    """An admin frame that is not a `levain-control/1` request: the connection is closed, which is its EOF."""


async def read_admin_frame(reader: asyncio.StreamReader) -> Optional[Tuple[int, str, dict]]:
    """The next request as `(id, op, args)`, or None at a clean EOF between frames; anything else that is not a
    canonical `{"v":1,"op":<string>,"id":<uint>,...}` object within ADMIN_FRAME_MAX raises AdminMalformed."""
    try:
        first = await reader.readexactly(1)
    except asyncio.IncompleteReadError:
        return None
    # from the frame's first byte, ONE deadline covers the rest of its length and its body
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMIN_FRAME_S
    try:
        head = first + await asyncio.wait_for(reader.readexactly(3), ADMIN_FRAME_S)
        n = int.from_bytes(head, "big")
        if n == 0 or n > ADMIN_FRAME_MAX:
            raise AdminMalformed(f"frame length {n}")
        data = await asyncio.wait_for(reader.readexactly(n), max(0.0, deadline - loop.time()))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        raise AdminMalformed("a frame cut short or stalled") from None
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise AdminMalformed(str(e)) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise AdminMalformed("not a canonical JSON object")
    v, op, rid = obj.get("v"), obj.get("op"), obj.get("id")
    if type(v) is not int or v != 1 or type(op) is not str or not op:
        raise AdminMalformed("v is 1 and op is a string")
    if type(rid) is not int or not 0 <= rid <= JSON_INT_MAX:
        raise AdminMalformed("id is an unsigned integer")
    return rid, op, {k: x for k, x in obj.items() if k not in ("v", "op", "id")}


async def read_control_answer(reader: asyncio.StreamReader) -> dict:
    """One `levain-control/1` answer from CONTROL: a 4-byte length within ADMIN_FRAME_MAX and one canonical JSON
    object. Anything else raises (the resolve's outcome is then `failed`)."""
    n = int.from_bytes(await reader.readexactly(4), "big")
    if n == 0 or n > ADMIN_FRAME_MAX:
        raise AdminMalformed(f"answer length {n}")
    data = await reader.readexactly(n)
    obj = canon.parse(data)
    if type(obj) is not dict:
        raise AdminMalformed("not a JSON object")
    return obj


async def read_attach_answer(reader: asyncio.StreamReader) -> Optional[dict]:
    """One answer on the attachment, or None at a clean EOF between frames: a 4-byte length within ADMIN_FRAME_MAX,
    then one canonical JSON object `{"id":<int>,"ok":<bool>,...}` (`refused` a string when not ok), the rest of the
    frame within ADMIN_FRAME_S of its first byte. Anything else raises AdminMalformed."""
    try:
        first = await reader.readexactly(1)
    except asyncio.IncompleteReadError:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMIN_FRAME_S
    try:
        head = first + await asyncio.wait_for(reader.readexactly(3), ADMIN_FRAME_S)
        n = int.from_bytes(head, "big")
        if n == 0 or n > ADMIN_FRAME_MAX:
            raise AdminMalformed(f"answer length {n}")
        data = await asyncio.wait_for(reader.readexactly(n), max(0.0, deadline - loop.time()))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        raise AdminMalformed("an answer cut short or stalled") from None
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise AdminMalformed(str(e)) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise AdminMalformed("not a canonical JSON object")
    if type(obj.get("id")) is not int or type(obj.get("ok")) is not bool:
        raise AdminMalformed("an answer carries an integer id and a boolean ok")
    if obj["ok"] is False and type(obj.get("refused")) is not str:
        raise AdminMalformed("a refusal names itself")
    return obj


def _resolve_done(t: asyncio.Task) -> None:
    """Reads the resolve task's outcome so none is left unretrieved; `cancelled()` first, since a preemption's
    cancel makes a cancelled resolve common and `exception()` on one raises."""
    if not t.cancelled():
        t.exception()


def _same_uint(v: object, want: int) -> bool:
    return type(v) is int and v == want


def front_binding(answer: dict, reg: "Registration") -> bool:
    """A TOKEN-RESOLVE answer binds to `reg` only when its session is `reg`'s and its claims name `reg`'s front
    exactly: CONTROL's persisted claims carry `front: {uid, pid, start}` and `incarnation`, and each must be present,
    an integer, and equal to the registration's. A missing or mistyped field fails the binding, never defaults to the
    registration's own value."""
    claims = answer.get("claims")
    front = claims.get("front") if type(claims) is dict else None
    return (answer.get("session") == reg.session and type(front) is dict
            and _same_uint(front.get("uid"), reg.front_uid) and _same_uint(front.get("pid"), reg.pid)
            and _same_uint(front.get("start"), reg.start)
            and _same_uint(claims.get("incarnation"), reg.incarnation))


#: `TOKEN-RESOLVE`'s `run` (CONTRACT §3.3), exactly: name -> its check.
_RUN_ID = re.compile(r"r-[0-9a-f]{16}\Z")
_RUN_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUN_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z")


def _run_uint(v: object, low: int = 0) -> bool:
    return type(v) is int and low <= v <= JSON_INT_MAX


RUN_FIELDS = {
    "run_id": lambda v: type(v) is str and _RUN_ID.match(v) is not None,
    "job_id": lambda v: type(v) is str and _RUN_TEXT.match(v) is not None,
    "run_index": _run_uint,
    "contract_digest": lambda v: type(v) is str and _RUN_DIGEST.match(v) is not None,
    "route": lambda v: type(v) is str and _RUN_TEXT.match(v) is not None,
    "routemap_generation": lambda v: _run_uint(v, 1),
    "route_policy_digest": lambda v: type(v) is str and _RUN_DIGEST.match(v) is not None,
    "model_requested": lambda v: type(v) is str and _RUN_TEXT.match(v) is not None,
    "reserved_microdollars": lambda v: _run_uint(v, 1),
}


def run_ok(run: object) -> bool:
    """A `run` object keys can act on: exactly its fields, each of its type (a malformed one fails the resolve)."""
    return type(run) is dict and set(run) == set(RUN_FIELDS) and all(RUN_FIELDS[k](v) for k, v in run.items())


def resolve_outcome(ans: dict) -> tuple:
    """CONTROL's `TOKEN-RESOLVE` answer as the resolve task's typed outcome: `("answer", {"session", "claims"})`, with
    a ticketed session's `run` (§3.3, CONTROL's from the ticket record) carried in the claims as `run`;
    `("refused", name)` for a refusal keys knows; else `("failed",)`, a malformed `run` or one already in the claims
    included. A session whose claims' `kind` is one a run ticket renders as (:data:`RUN_KINDS`) but whose answer
    carries no `run` is `("refused", "run-unknown")` (3c-5 fix 1, L1 note 4): keys never treats a dispatch session as
    a plain one, whatever CONTROL answered."""
    if ans.get("ok") is True:
        session, claims = ans.get("session"), ans.get("claims")
        if type(session) is str and session and type(claims) is dict and "run" not in claims:
            if "run" in ans:
                if not run_ok(ans["run"]):
                    return ("failed",)
                claims = {**claims, "run": dict(ans["run"])}
            elif ("kind" in claims and type(claims["kind"]) is not str) or claims.get("kind") in RUN_KINDS:
                # 3c-5 fix 2 (complement 5): a `kind` that is not a string (an unhashable list would raise in the
                # set test) is no plain session either
                return ("refused", "run-unknown")
            return ("answer", {"session": session, "claims": claims})
        return ("failed",)
    if ans.get("ok") is False and ans.get("refused") in RESOLVE_REFUSALS:
        return ("refused", ans["refused"])
    return ("failed",)


def encode_admin(answer: dict) -> bytes:
    data = canon.canonical_bytes(answer)
    return len(data).to_bytes(4, "big") + data


def check_admin_args(op: str, args: dict) -> Optional[str]:
    """The refusal name for `op`'s arguments, or None when they are exactly its keys with the right types."""
    want = ADMIN_VERBS.get(op)
    if want is None:
        return "verb-not-allowed-for-peer"
    if any(k not in want for k in args):
        return "unknown-key"
    if any(k not in args for k in want):
        return "missing-key"
    for k, x in args.items():
        if k == "session":
            if type(x) is not str or not x:
                return "wrong-type"
        elif k == "item_sha256":
            if type(x) is not str or not _SHA256_HEX.match(x):
                return "wrong-type"
        elif k == "keep":   # RELOAD's generations still referenced (head ruling H5): distinct positive integers
            if type(x) is not list or any(type(g) is not int or not 1 <= g <= JSON_INT_MAX for g in x) \
                    or len(set(x)) != len(x):
                return "wrong-type"
        elif type(x) is not int or not 0 <= x <= JSON_INT_MAX:
            return "wrong-type"
    return None


# --- the listeners --------------------------------------------------------------------------------------------------

def bind_listeners(specs: Sequence[Tuple[str, Optional[int]]]) -> List[socket.socket]:
    """Create, bind and listen keys' listening sockets, one per `(path, gid)`, WITHOUT registering them with the loop
    (the NOFILE count comes between this and the first accept). Each is created 0660, then given `gid` when it is
    not None. A path that exists is replaced only when it is a socket that REFUSES a connect (stale: nobody listens);
    any other file, or a socket that may be live, raises FileExistsError. On any failure every socket made here is
    closed and every path bound here is unlinked, so a failed start leaves no listener path behind."""
    out: List[socket.socket] = []
    bound: List[str] = []
    try:
        for path, gid in specs:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                st = None
            if st is not None:
                if not stat.S_ISSOCK(st.st_mode):
                    raise FileExistsError(f"{path}: not a socket")
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.setblocking(False)   # a live listener's full backlog must not block start
                try:
                    err = probe.connect_ex(path)
                finally:
                    probe.close()
                if err != errno.ECONNREFUSED:
                    why = errno.errorcode.get(err, err) if err else "listening"
                    raise FileExistsError(f"{path}: in use or unprobeable ({why})")
                os.unlink(path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            out.append(s)
            old = os.umask(0o117)   # created 0660: no window at a wider mode
            try:
                s.bind(path)
            finally:
                os.umask(old)
            bound.append(path)
            if gid is not None:
                os.chown(path, -1, gid)
            s.listen(ACCEPT_BACKLOG)
    except BaseException:
        for s in out:
            s.close()
        for path in bound:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return out


def count_open_fds() -> int:
    """The fds this process holds now, less the one the listing itself opens."""
    d = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
    return len(os.listdir(d)) - 1


def raise_nofile() -> Tuple[int, int]:
    """Raise the soft RLIMIT_NOFILE to min(hard, NOFILE_TARGET) (never lower it); return (soft, hard) after."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = NOFILE_TARGET if hard == resource.RLIM_INFINITY else min(hard, NOFILE_TARGET)
    if soft != resource.RLIM_INFINITY and soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError) as e:
            _note(logging.WARNING, "levain gw-keys: RLIMIT_NOFILE not raised to %s (%s)", want, e)
    return resource.getrlimit(resource.RLIMIT_NOFILE)


# --- the server -----------------------------------------------------------------------------------------------------

class Keys:
    """keys' connection half. The in-process API (`front_register`, `front_arm`, `drop_session`) is what the admin
    server's request tasks call, each passing the admin epoch its connection was adopted on; a call from a stale
    epoch, or one made after the shutdown latch (`closing`) is set, returns None and changes nothing (no answer is
    sent).

    `closing` is the ONE shutdown state: set synchronously by the trigger, never cleared. From that instant admission
    refuses at accept, at the STATUS claim (`H`) and at `Q`, and every in-process API call refuses."""

    def __init__(self, *, gw_uids: frozenset = frozenset(), single_uid: Optional[int] = None,
                 control_path: Optional[str] = None, lib_root: Optional[str] = None,
                 cred_lifecycle: Optional[CredLifecycle] = None, control_uid: Optional[int] = None,
                 ledger: Optional[ledger.Ledger] = None, dispatch_policy: object = None,
                 creds: Optional[CredStore] = None, attach: bool = False,
                 routes: Optional["routemaps.KeysRoutes"] = None) -> None:
        #: keys holds a KEYS-ATTACH connection to CONTROL (`main()` always passes True). While attached is None keys
        #: refuses every token: FRONT-REGISTER is refused `keys-unavailable` and no `Q` is admitted, so every
        #: registration lives inside one attachment, whose EOF clears it (§4.5). False: an in-process harness with no
        #: CONTROL, which never attaches and gates nothing.
        self.attach = attach
        self.attached: Optional[Attachment] = None
        self.attach_no = 0
        #: the clocks of the attachment, attributes so a test can scale them down
        self.attach_answer_s = V23_S
        self.reconcile_s = RECONCILE_S
        self.unclaimed_s = V23_S
        self.attach_attempt_s = ATTACH_ATTEMPT_S
        self._attach_task: Optional[asyncio.Task] = None
        #: the OpenRouter limit refresher (D1): its task (`_serve` starts it, never an in-process Keys), its clocks, and
        #: credential name -> `KeyLimit`, the last read of each served OpenRouter key
        self._limit_task: Optional[asyncio.Task] = None
        self.key_limit_s = KEY_LIMIT_S
        self.key_limit_every_s = KEY_LIMIT_EVERY_S
        self.key_limits: Dict[str, KeyLimit] = {}
        #: the attach sends in flight (FRONT-LOST, SPEND-OVERRUN), kept so none is collected mid-send
        self._attach_sends: Set[asyncio.Task] = set()
        #: the reservation ledger (`main()` opens it; `_serve` attaches and closes it). None refuses every admit
        #: `budget-exhausted` "no ledger" (design 3c-1 §2.5, Q6)
        self.ledger = ledger
        #: the lineage outbox (S1 3c-6c): its bound and its pacing, attributes so a test can scale them; the refusal
        #: windows `(session, reason) -> [first refusal (wall clock), count, run, timer]`; the sender's wake (made in
        #: the loop), set by the ledger's listener as each payload line becomes durable; H6-8's counter of envelopes
        #: CONTROL refused and the last FAILURE note (`lineage_status`)
        self.lineage_outbox_max = LINEAGE_OUTBOX_MAX
        self.refused_row_s = REFUSED_ROW_S
        self.lineage_retry_s = LINEAGE_RETRY_S
        self._refusals: Dict[Tuple[str, str], list] = {}
        self._lineage_wake: Optional[asyncio.Event] = None
        self.lineage_envelopes_refused = 0
        #: r1 F1: refusal-window records not written because the outbox was at its bound
        self.lineage_dropped = 0
        self.lineage_failure: Optional[str] = None
        if ledger is not None:
            ledger.on_outbox = self._lineage_due
        #: the dispatch route maps (S1 3c-4): keys' own head and every generation it verified; `main()` loads them
        #: before anything is served. None (no lib root): no dispatch map, every dispatch route refused.
        self.routes = routes if routes is not None else (
            routemaps.KeysRoutes(lib_root) if lib_root is not None else None)
        self.reload_lock: Optional[asyncio.Lock] = None   # one RELOAD at a time (R4 §2), made in the loop
        #: `dispatch_policy(route, pin)`: a dispatch-map route's policy for the session's pin, or a refusal
        self.dispatch_policy = dispatch_policy if dispatch_policy is not None else self._dispatch_policy
        #: the buffered request bodies' budget (R4 §1.7 step 2) and the body's read deadline from `A`; attributes so a
        #: test can scale them down
        self.body_buffer = BodyBuffer()
        self.body_read_s = BODY_READ_S
        #: the credentials under `L/keys/cred`, opened at the first exchange that names one
        self.creds = creds
        #: gw.jsonl (`main()` opens it); None writes no exchange record
        self.gw_log: Optional[GwLog] = None
        #: the upstream deadlines (design 1.6), attributes so a test can scale them down
        self.connect_timeout_s = upstream.CONNECT_TIMEOUT_S
        self.head_timeout_s = upstream.HEAD_TIMEOUT_S
        self.idle_s = upstream.IDLE_S
        #: the one uid admitted on the admin socket (the `_levain_control` account); None admits nobody.
        self.control_uid = control_uid
        #: the adopted admin connection, None between its EOF and the next adoption.
        self.admin: Optional[AdminConn] = None
        self.admin_lock: Optional[asyncio.Lock] = None    # one adoption (and so one preemption) at a time
        #: the credential layer's lifecycle seam, which shutdown calls; the stand-in until the real writer is passed.
        self.cred_lifecycle: CredLifecycle = cred_lifecycle if cred_lifecycle is not None else FakeCredLifecycle()
        #: uids admitted at accept: the `_levain_gw<n>` accounts.
        self.gw_uids = frozenset(gw_uids)
        #: TEST ONLY (the one-uid fixture harness): peers of this uid are matched to a registration by (pid, start)
        #: whatever its `front_uid`. None on a real install; main() refuses it as root or as `_levain_keys`.
        self.single_uid = single_uid
        self.control_path = control_path   # CONTROL's socket: the real TOKEN-RESOLVE client's
        self.lib_root = lib_root
        #: the last base map that parsed whole: its file's (st_dev, st_ino, st_mtime_ns, st_size), and its routes
        self._base: Optional[Tuple[Tuple[int, int, int, int], Dict[str, RoutePolicy]]] = None
        self.first_frame_s = FIRST_FRAME_S
        self.pending_per_peer = PENDING_PER_PEER
        self.exchanges_per_session = EXCHANGES_PER_SESSION
        self.max_exchange_conns = MAX_EXCHANGE_CONNS
        self.admin_epoch = 0
        self.closing = False
        self.regs: Dict[int, Registration] = {}          # front_uid -> registration
        self.by_session: Dict[str, Registration] = {}
        self.reg_high: Dict[int, int] = {}               # front_uid -> highest reg_token accepted (process life)
        self.pool: Set[Conn] = set()                     # every exchange-pool connection still open, detached or not
        #: token_digest -> CONTROL's answer `{"session", "claims"}`; cleared by DROP-SESSION (that session's) and by
        #: every fail-closed clear
        self.cache: Dict[str, dict] = {}
        #: the sessions an overrun stopped (R4 §1.4): `_settle` adds one whose settle cost more than its hold, and
        #: `_check_q` and the pre-admit statement refuse its every later `Q` `budget-overrun`. Keyed by SESSION, never
        #: by a cache entry, so a re-registration or reattach that drops and re-resolves the session's claims keeps
        #: the stop. Only DROP-SESSION (`drop_session`, registered or not, or the reconcile's drop of the session's
        #: digest, which is one) takes a session out; the fail-closed clear keeps it, so an attachment's EOF and a
        #: re-attach never lose it; a restart loses it with the rest of memory.
        self.budget_stopped: Set[str] = set()
        #: digests keys itself saw dropped (bounded, oldest first): `token-revoked` without asking
        self.dropped: "OrderedDict[str, None]" = OrderedDict()
        #: the per-digest single-flight: (admin_epoch, registration, digest) -> the table-owned resolve task. Keyed by
        #: the asking registration too, so no registration joins a resolve another one started (one started before a
        #: drop can never answer the registration that replaced it), and a detach cancels its own registration's.
        self.resolving: Dict[Tuple[int, "Registration", str], asyncio.Task] = {}
        self.resolve_sem: Optional[asyncio.Semaphore] = None   # the global RESOLVE_CONNS
        self.servers: list = []
        self.state_lock: Optional[asyncio.Lock] = None
        self._done: Optional[asyncio.Event] = None
        self._closer: Optional[asyncio.Future] = None     # the one run of _close, made by the first trigger
        self._starting: Optional[asyncio.Event] = None    # made by start(), set when start() has returned

    # -- start / stop --

    def _in_loop(self) -> None:
        """The locks and the latch's event, made once, inside the running loop (3.9 binds them to the loop)."""
        if self.state_lock is None:
            self.state_lock = asyncio.Lock()
            self.admin_lock = asyncio.Lock()
            self.reload_lock = asyncio.Lock()
            self._done = asyncio.Event()
            self.resolve_sem = asyncio.Semaphore(RESOLVE_CONNS)
            self._lineage_wake = asyncio.Event()

    async def start(self, socks: Sequence[socket.socket], admin: Optional[socket.socket] = None) -> None:
        """Serve `socks` as front listeners and `admin` as the admin listener (bind_listeners' output). start() alone
        creates servers, and the shutdown's close waits for start() to return before it closes `servers`; so a trigger
        before or during start never leaves a listener. Once `closing` is set no further server is created and the
        rest of the sockets are closed unserved; a server whose creation was in flight at the trigger is in `servers`
        by the time the close runs, and is closed there (until then admission refuses every connection it accepts). A
        server creation that FAILS closes the servers already made (never their raw sockets, which the loop watches)
        and the sockets not yet served, then raises."""
        self._in_loop()
        self._starting = asyncio.Event()
        plan = [(s, self._on_conn) for s in socks] + ([(admin, self._on_admin)] if admin is not None else [])
        made = 0
        try:
            for i, (s, handler) in enumerate(plan):
                if self.closing:
                    for rest, _h in plan[i:]:
                        rest.close()
                    return
                self.servers.append(await asyncio.start_unix_server(handler, sock=s, backlog=ACCEPT_BACKLOG))
                made += 1
        except BaseException:
            for srv in self.servers:
                srv.close()
            for rest, _h in plan[made:]:
                rest.close()
            raise
        finally:
            self._starting.set()

    def arm_triggers(self) -> None:
        """The ONE shutdown trigger: SIGTERM or SIGINT, and EOF on stdin when stdin is a pipe (the launcher's), each
        setting the one latch. A stdin that is not a pipe (launchd's, `</dev/null`) is never read: EOF there means
        nothing. SIGINT is a loop handler, never a KeyboardInterrupt, so a Ctrl-C cannot land between `up.write(head)`
        and `mark_sent` and release a hold whose head may be on the wire."""
        self._in_loop()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.trigger)
        loop.add_signal_handler(signal.SIGINT, self.trigger)
        try:
            fifo = stat.S_ISFIFO(os.fstat(0).st_mode)
        except OSError:
            fifo = False
        if not fifo:
            print("levain gw-keys: stdin is not a pipe; the stdin-EOF shutdown trigger is not armed (SIGTERM and "
                  "SIGINT only)",
                  file=sys.stderr, flush=True)
            return
        os.set_blocking(0, False)
        loop.add_reader(0, self._on_stdin)

    def _on_stdin(self) -> None:
        try:
            data = os.read(0, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(0)
            self.trigger()

    def trigger(self) -> None:
        """The latch: `closing` is set here, synchronously, so admission and the admin calls refuse from this instant
        (no table mutation awaits inside its lock, so none can be half-done when it is set). The close runs once, as
        a task; a later trigger is a no-op. The latch's first transition also tells the credential layer
        (`begin_closing`), whose failure is noted and never stops the shutdown."""
        self._in_loop()
        self.closing = True
        if self._closer is None:
            self._closer = asyncio.ensure_future(self._close())
            try:
                self.cred_lifecycle.begin_closing()
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: cred_lifecycle.begin_closing failed (%s)", type(e).__name__)

    async def shutdown(self) -> None:
        """Trigger, then await the one close's completion (a later call awaits the same completion and returns)."""
        self.trigger()
        assert self._done is not None
        await self._done.wait()

    async def _close(self) -> None:
        """Shutdown is a transition (design 1.5 "Shutdown's order"), run once behind the latch: wait for a start() in
        progress to return (its only awaits are its server creations); phase 1 over every registration; (1) the
        listeners' synchronous close; (2) phase 2; (2b) the adopted admin connection's transport aborted and its
        read loop and request tasks cancelled (the EOF path the read loop runs finds the table empty), and the
        attachment's and the limit refresher's tasks cancelled; (3) the credential layer's detached tasks cancelled;
        (4) ONE bounded wait over the listeners' closure, the detached exchange tasks and the admin tasks, nothing
        cancelled by the wait itself. Steps (1) to (3) have no await between them, and a failure of one step never
        skips the rest."""
        assert self._done is not None and self.state_lock is not None
        try:
            if self._starting is not None:
                await self._starting.wait()
            async with self.state_lock:     # phase 1: keys' own act, so no epoch check
                victims = self._detach_all()
            tasks = {v.task for v in victims if isinstance(v, Conn) and v.kind == "exchange" and v.task is not None
                     and v.task is not asyncio.current_task() and not v.task.done()}
            for s in self.servers:          # (1) the listeners' synchronous close: no accept after this
                s.close()
            self._effects(victims)          # (2) phase 2
            admin_tasks: Set[asyncio.Task] = set()
            adm = self.admin
            if adm is not None:             # (2b) aborted, not closed: a CONTROL that stopped reading holds a close
                try:
                    adm.writer.transport.abort()
                except Exception as e:
                    _note(logging.WARNING, "levain gw-keys: aborting the admin connection failed (%s)", type(e).__name__)
                for t in {adm.task} | set(adm.tasks):   # its read loop (whose EOF path finds the table empty) and requests
                    if t is not None and t is not asyncio.current_task() and not t.done():
                        t.cancel()
                        admin_tasks.add(t)
            self._attach_close(self.attached)   # (2b) the attachment too, its client and its sends cancelled
            for t in {self._attach_task, self._limit_task} | set(self._attach_sends):   # (2b) and the limit refresher
                if t is not None and t is not asyncio.current_task() and not t.done():
                    t.cancel()
                    admin_tasks.add(t)
            try:                            # (3)
                self.cred_lifecycle.cancel_detached()
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: cred_lifecycle.cancel_detached failed (%s)", type(e).__name__)
            closures = {asyncio.ensure_future(s.wait_closed()) for s in self.servers}
            waitset = closures | tasks | admin_tasks
            if waitset:                     # an empty set is a ValueError; nothing pending past the deadline is cancelled
                await asyncio.wait(waitset, timeout=SHUTDOWN_WAIT_S)
        finally:
            self._done.set()

    async def wait_done(self) -> None:
        self._in_loop()
        assert self._done is not None
        await self._done.wait()

    # -- the in-process API (the admin tasks') --

    async def front_register(self, epoch: int, session: str, front_uid: int, pid: int, start: int, incarnation: int,
                             reg_token: int) -> Optional[dict]:
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            if self.attach and self.attached is None:
                # no attachment, no registration (§4.5): one made now could outlive no EOF that clears it
                return {"refused": "keys-unavailable"}
            high = self.reg_high.get(front_uid, 0)
            cur = self.regs.get(front_uid)
            if reg_token > high:
                if cur is not None:
                    victims = self._detach(cur)
                reg = Registration(session, front_uid, pid, start, incarnation, reg_token)
                reg.admin_epoch = epoch
                self.regs[front_uid] = reg
                self.by_session[session] = reg
                self.reg_high[front_uid] = reg_token
                answer: dict = {}
            elif (reg_token == high and cur is not None and cur.reg_token == reg_token
                  and cur.fields() == (session, front_uid, pid, start, incarnation)):
                answer = {}
            else:
                answer = {"refused": "registration-stale", "reg_high": high}
        self._effects(victims)
        return answer

    async def front_arm(self, epoch: int, session: str, reg_token: int) -> Optional[dict]:
        assert self.state_lock is not None
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            reg = self.by_session.get(session)
            if reg is None or reg.reg_token != reg_token or self.regs.get(reg.front_uid) is not reg:
                return {"refused": "registration-stale"}
            if reg.armed_at is None:   # the FIRST accepted FRONT-ARM stamps; a duplicate keeps the stamp
                loop = asyncio.get_running_loop()
                reg.armed_at = loop.time()
                reg.state = "armed"
                if self.attach:   # §4.5: V23 from `armed_at` with no `H` is a lost front
                    loop.call_later(self.unclaimed_s, self._unclaimed_due, reg, reg.armed_at)
            return {}

    def _unclaimed_due(self, reg: Registration, armed_at: float) -> None:
        self._attach_send_task(self._unclaimed_expire(reg, armed_at))

    async def _unclaimed_expire(self, reg: Registration, armed_at: float) -> None:
        """The unclaimed registration clock ran out (§4.5; s3b: "3c measures V23 from it"): a
        registration still the session's, still ARMED (no `H` claimed it) with the same stamp, is lost: detached here,
        then `FRONT-LOST` sent so CONTROL revokes the session."""
        assert self.state_lock is not None
        async with self.state_lock:
            if (self.closing or reg.state != "armed" or reg.armed_at != armed_at
                    or self.regs.get(reg.front_uid) is not reg or self.by_session.get(reg.session) is not reg):
                return
            victims = self._detach(reg)
        self._effects(victims)
        _note(logging.WARNING, "levain gw-keys: front-unclaimed session=%s", reg.session)
        await self._front_lost(reg.session)

    async def drop_session(self, epoch: int, session: str) -> Optional[dict]:
        assert self.state_lock is not None
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            held, victims = self._drop_locked(session)
        self._effects(victims)
        return {"dropped": held}   # false = nothing held: still success

    def _drop_locked(self, session: str) -> Tuple[bool, List[Victim]]:
        """DROP-SESSION's phase 1 (caller holds state_lock; no await), which the reconcile's drop is too: the session's
        registration detached with its digests marked dropped, and the session out of `budget_stopped` whether or not
        a registration is held (the fail-closed clear keeps the set and removes every registration): the one place a
        session leaves it. Returns whether a registration was held, and the victims for phase 2."""
        reg = self.by_session.get(session)
        victims = self._detach(reg, revoke=True) if reg is not None else []
        self.budget_stopped.discard(session)
        return reg is not None, victims

    # -- the attachment (§4.5; 3c design r3 §2.1 (e); R4 §1.4) --

    def start_attach(self) -> None:
        """Start the attach client once (`_serve` calls it after the listeners are served): connect to CONTROL's socket,
        `KEYS-ATTACH`, serve the attachment until its EOF, fail closed, and connect again with backoff, until the
        shutdown latch."""
        self._in_loop()
        if self.attach and self._attach_task is None and not self.closing:
            self._attach_task = asyncio.ensure_future(self._attach_loop())

    async def _attach_loop(self) -> None:
        """Attach, serve, fail closed, again. The backoff grows after a failed open AND after an attachment that ended
        before it was healthy (no SESSIONS answered on it, and shorter than `reconcile_s`): a CONTROL that answers
        `ok` and closes at once sees growing gaps, not a connect storm. Only a healthy attachment resets it. Any
        exception out of the serve is that attachment's EOF (logged); the loop itself ends only at the latch."""
        loop = asyncio.get_running_loop()
        backoff = ATTACH_RETRY_FIRST_S
        noted = False
        while not self.closing:
            try:
                att = await self._attach_open()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not noted:   # once per run of failures: CONTROL down is a state, not a stream of events
                    _note(logging.WARNING, "levain gw-keys: KEYS-ATTACH failed (%s); retrying", type(e).__name__)
                    noted = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ATTACH_RETRY_MAX_S)
                continue
            noted = False
            opened = loop.time()
            try:
                await self._attach_serve(att)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: the attachment's read loop failed (%s); its EOF",
                      type(e).__name__)
            finally:
                await asyncio.shield(self._attach_lost(att))
            if att.healthy or loop.time() - opened >= self.reconcile_s:
                backoff = ATTACH_RETRY_FIRST_S
            elif not self.closing:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ATTACH_RETRY_MAX_S)

    async def _attach_open(self) -> Attachment:
        """One connect and `KEYS-ATTACH`, stating the route-map generation keys serves (`KeysRoutes.stated`): its head,
        or none (`null`, `null`) when it serves no map or refuses every dispatch route (R4 §2). The answer must come within `attach_answer_s`; only an `ok` answer makes the
        connection the attachment, and only while the latch is not set."""
        if self.control_path is None:
            raise OSError("no CONTROL socket")
        assert self.state_lock is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.attach_answer_s
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.control_path), self.attach_answer_s)
        try:
            gen, sha = self.routes.stated() if self.routes is not None else (None, None)
            writer.write(encode_admin({"v": 1, "op": "KEYS-ATTACH", "id": 1, "generation": gen, "item_sha256": sha}))
            await asyncio.wait_for(writer.drain(), max(0.0, deadline - loop.time()))
            ans = await asyncio.wait_for(read_attach_answer(reader), max(0.0, deadline - loop.time()))
            if ans is None or ans["id"] != 1 or ans["ok"] is not True:
                raise AdminMalformed("KEYS-ATTACH was not answered ok")
            async with self.state_lock:
                if self.closing:
                    raise AdminMalformed("closing")
                self.attach_no += 1
                att = Attachment(reader, writer, self.attach_no)
                self.attached = att
        except BaseException:
            writer.transport.abort()
            raise
        print(f"levain gw-keys: attached n={att.no}", file=sys.stderr, flush=True)
        return att

    async def _attach_serve(self, att: Attachment) -> None:
        """The attachment's read loop (each answer to its request's future) beside its reconcile ticker and its lineage
        sender (S1 3c-6c); returns at the connection's EOF or a frame the wire does not allow."""
        ticker = asyncio.ensure_future(self._reconcile_loop(att))
        sender = asyncio.ensure_future(self._lineage_loop(att))
        try:
            while True:
                ans = await read_attach_answer(att.reader)
                if ans is None:
                    return
                fut = att.pending.get(ans["id"])
                if fut is not None and not fut.done():
                    fut.set_result(ans)
        except (OSError, EOFError, AdminMalformed, asyncio.IncompleteReadError):
            return
        finally:
            ticker.cancel()
            sender.cancel()
            await asyncio.gather(ticker, sender, return_exceptions=True)

    async def _attach_lost(self, att: Attachment) -> None:
        """The attachment ended (§4.5): keys refuses every token, sends `K` to every front AND closes the other of the
        two connections. In ONE mutation section: the attachment cleared, the admin epoch bumped (the admin connection's
        tasks in flight change nothing after it) and the admin connection let go, and every registration detached (the
        fail-closed clear: `reg_high` and `budget_stopped` kept). Then every waiter on the attachment is answered None,
        both connections aborted, and phase 2 run. Once per attachment."""
        assert self.state_lock is not None
        victims: List[Victim] = []
        adm: Optional[AdminConn] = None
        async with self.state_lock:
            if att.ended:
                return
            att.ended = True
            if self.attached is att:
                self.attached = None
                self.admin_epoch += 1
                adm, self.admin = self.admin, None
                victims = self._detach_all()
        for fut in att.pending.values():
            if not fut.done():
                fut.set_result(None)
        try:
            att.writer.transport.abort()
        except Exception:
            pass
        self._effects(victims)
        if adm is not None:
            for t in list(adm.tasks):          # as `_admin_eof` does: that EOF path returns early for it now
                t.cancel()
            try:
                adm.writer.transport.abort()   # its handler's EOF path finds it no longer adopted: nothing more
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: closing the admin connection failed (%s)", type(e).__name__)
        if not self.closing:
            print(f"levain gw-keys: detached n={att.no}", file=sys.stderr, flush=True)

    def _attach_close(self, att: Optional[Attachment]) -> None:
        """End `att` from keys' side (a FRONT-LOST CONTROL did not ack, the reconcile's third miss, the admin EOF): its
        read loop reads the abort, and `_attach_lost` runs the EOF rule."""
        if att is not None and not att.ended:
            try:
                att.writer.transport.abort()
            except Exception:
                pass

    async def _attach_call(self, att: Attachment, op: str, args: dict, wait: Optional[float] = None) -> Optional[dict]:
        """`op` on `att`; its answer, or None when the attachment is not (or no longer) the current one or no answer
        came within `wait` (default `attach_answer_s`) of the send."""
        if att.ended or self.attached is not att:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.attach_answer_s if wait is None else wait)
        att.next_id += 1
        rid = att.next_id
        fut: asyncio.Future = loop.create_future()
        att.pending[rid] = fut
        try:
            data = encode_admin({"v": 1, "op": op, "id": rid, **args})
            async with att.wlock:
                if att.ended:
                    return None
                att.writer.write(data)
                await asyncio.wait_for(att.writer.drain(), max(0.0, deadline - loop.time()))
            return await asyncio.wait_for(asyncio.shield(fut), max(0.0, deadline - loop.time()))
        except (asyncio.TimeoutError, OSError, EOFError):
            return None
        finally:
            att.pending.pop(rid, None)

    async def _attach_notify(self, op: str, args: dict) -> bool:
        """`op` on the current attachment; True when CONTROL acked it."""
        att = self.attached
        if att is None:
            return False
        ans = await self._attach_call(att, op, args)
        if ans is None or ans["ok"] is not True:
            _note(logging.WARNING, "levain gw-keys: %s not acked (%s)", op,
                  "no answer" if ans is None else ans.get("refused"))
            return False
        return True

    async def _front_lost(self, session: str) -> None:
        """`FRONT-LOST {session}` (§4.5): CONTROL revokes the session. With no ack, keys ends the attachment, whose EOF
        rule makes CONTROL revoke every egress-class session: the one sure way left to reach the same end."""
        att = self.attached
        if att is None:
            return   # unattached: keys holds no registration, and the attachment's EOF already revoked them all
        if not await self._attach_notify("FRONT-LOST", {"session": session}):
            self._attach_close(att)

    async def _spend_overrun(self, args: dict) -> None:
        """`SPEND-OVERRUN` (R4 §1.4 step 2) on the current attachment, kept until acked: an attempt not answered within
        `attach_attempt_s` is sent again with the same payload (CONTROL records it once per (session, ref)), until V23
        (`attach_answer_s`) from the first send. Exhausted, refused, or not encodable, the attachment ends (fail
        closed: CONTROL's EOF rule revokes every egress session): an overrun is never dropped while the attachment it
        was owed on stays up. Unattached, or once that attachment has ended, there is nothing to send it on: the EOF
        already revoked the session. KNOWN EDGE: a cost above the wire's JSON_INT_MAX cannot be encoded, so it ends
        the attachment too."""
        att = self.attached
        if att is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.attach_answer_s
        why = "no answer within V23"
        try:
            while not att.ended and self.attached is att:
                left = deadline - loop.time()
                if left <= 0:
                    break
                sent = loop.time()
                ans = await self._attach_call(att, "SPEND-OVERRUN", args, wait=min(self.attach_attempt_s, left))
                if ans is None:   # attempts stay `attach_attempt_s` apart, a failed write's quick None included
                    await asyncio.sleep(max(0.0, min(sent + self.attach_attempt_s, deadline) - loop.time()))
                    continue
                if ans["ok"] is True:
                    return
                why = "refused %s" % ans.get("refused")
                break
        except Exception as e:   # the payload's encoding (a cost past JSON_INT_MAX), or the send itself
            why = type(e).__name__
        if att.ended or self.attached is not att:
            return
        _note(logging.WARNING, "levain gw-keys: SPEND-OVERRUN not acked (%s); the attachment ends", why)
        self._attach_close(att)

    def _attach_send_task(self, coro: Awaitable[None]) -> None:
        """Run an attach send on its own task (a call site that cannot await it: a `finally`, the settle, a timer)."""
        if not self.attach or self.closing:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return
        t = asyncio.ensure_future(coro)
        self._attach_sends.add(t)
        t.add_done_callback(self._attach_send_done)

    def _attach_send_done(self, t: asyncio.Task) -> None:
        self._attach_sends.discard(t)
        if not t.cancelled() and t.exception() is not None:
            _note(logging.WARNING, "levain gw-keys: an attach send failed (%s)", type(t.exception()).__name__)

    async def _reconcile_loop(self, att: Attachment) -> None:
        """SESSIONS on the attachment's open, then every `reconcile_s` while it stays the attachment. A tick that raises
        ends the attachment (fail closed): an attachment never stays up with its reconcile dead."""
        try:
            while self.attached is att and not att.ended:
                await self._reconcile_tick(att)
                await asyncio.sleep(self.reconcile_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: the reconcile failed (%s); the attachment ends", type(e).__name__)
            self._attach_close(att)

    async def _reconcile_tick(self, att: Attachment) -> None:
        """One reconcile (3c design r3 §2.1 (e), R4 §0): the token digests keys holds a cached resolve for go in
        `SESSIONS`, and every digest CONTROL answers in `drop` is treated exactly as a DROP-SESSION of its session (and
        marked dropped). A send with no answer (or not an `ok` one) is a miss; RECONCILE_MISSES consecutive misses end
        the attachment; an answered tick resets the count."""
        digests = sorted(self.cache)
        chunks = [digests[i:i + SESSIONS_CHUNK] for i in range(0, len(digests), SESSIONS_CHUNK)] or [[]]
        drop: Set[str] = set()
        for chunk in chunks:
            ans = await self._attach_call(att, "SESSIONS", {"digests": chunk})
            got = ans.get("drop") if ans is not None and ans["ok"] is True else None
            if type(got) is not list or any(type(d) is not str for d in got):
                att.misses += 1
                if att.misses >= RECONCILE_MISSES:
                    _note(logging.WARNING, "levain gw-keys: %d SESSIONS unanswered; the attachment ends", att.misses)
                    self._attach_close(att)
                return
            drop.update(d for d in got if d in chunk)
        att.misses = 0
        att.healthy = True
        if not drop:
            return
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:
            if self.attached is not att or self.closing:
                return
            for d in sorted(drop):
                entry = self.cache.get(d)
                if entry is not None:
                    victims += self._drop_locked(entry["session"])[1]
                self._mark_dropped(d)
        self._effects(victims)

    # -- the lineage outbox (S1 3c-6c; R4 §3 "Delivery"; H6-8, H6-11, H6-19) --

    def _lineage_due(self, triple: Tuple[str, str, int], payload: dict) -> None:
        """The ledger's `on_outbox` listener (on the loop): a payload line became durable, so the sender has work."""
        if self._lineage_wake is not None:
            self._lineage_wake.set()

    async def _lineage_loop(self, att: Attachment) -> None:
        """The attachment's lineage sender (R4 §3 "Delivery", H6-11): from its open, every durable payload line with no
        durable ack, in ref order, then each new one as its line becomes durable. ONE envelope in flight
        (`lineage_envelope`: one ledger and epoch, at most LINEAGE_ENVELOPE_RECORDS records and LINEAGE_ENVELOPE_BYTES
        bytes); the next is sent only after CONTROL's answer AND keys' own `lineage-acked` line is durable (R4 §3: an
        ack counts only after its fsync; the ledger moves the refs out of the outbox in that line's callback). Its
        sends share the attachment with the reconcile ticker, so a `SESSIONS` due meanwhile goes between two
        envelopes, never behind a whole resend (CONTROL serves the connection in order and appends an envelope with
        one fsync). An envelope CONTROL refuses (`lineage-record:envelope`: a keys bug; `lineage-unavailable`: its sink
        is poisoned until it restarts) or an answer that is not an ack is counted (`lineage_envelopes_refused`) and
        noted as a FAILURE (H6-8); no answer within V23 is noted only. Either way the envelope is sent again after
        `lineage_retry_s`: the outbox keeps it, so nothing is lost and the attachment stays up. An ack the ledger does not
        take or make durable is noted and its envelope sent again the same way (r1 F3); a cycle that ends with a clean
        ack clears the FAILURE note."""
        led = self.ledger
        wake = self._lineage_wake
        if led is None or wake is None:
            return
        led.on_outbox = self._lineage_due
        try:
            while self.attached is att and not att.ended:
                wake.clear()      # before the read: a line made durable after it sets the wake again
                got = lineage_envelope(led.unacked())
                if got is None:
                    await wake.wait()
                    continue
                sent, env = got
                ans = await self._attach_call(att, "LINEAGE-APPEND", {"envelope": env})
                if self.attached is not att or att.ended:
                    return
                read = read_lineage_ack(ans, sent) if ans is not None else None
                if read is None:
                    if ans is not None and ans.get("ok") is False:
                        self.lineage_envelopes_refused += 1
                        why = "CONTROL refused an envelope of %d record(s) (%s)" % (len(sent), ans.get("refused"))
                    elif ans is None:
                        why = "an envelope of %d record(s) was not answered" % len(sent)
                    else:
                        self.lineage_envelopes_refused += 1
                        why = "CONTROL's answer to an envelope of %d record(s) is not an ack" % len(sent)
                    self._lineage_trouble(why)
                    await asyncio.sleep(self.lineage_retry_s)
                    continue
                triples, quarantined = read
                try:
                    await led.ack(triples, quarantined)
                except Exception as e:
                    # r1 F3: the ack line was not taken or not made durable: noted, and the envelope sent again
                    # after `lineage_retry_s` (the outbox still holds its triples), never the sender's exit
                    self._lineage_trouble("the ack of %d record(s) was not counted (%s)" % (len(triples),
                                                                                           type(e).__name__))
                    await asyncio.sleep(self.lineage_retry_s)
                    continue
                if quarantined:
                    self._lineage_trouble("CONTROL quarantined %d record(s) (%s)" % (
                        len(quarantined), ",".join(sorted(set(quarantined.values())))))
                else:
                    self.lineage_failure = None   # r1 F3: a whole clean cycle clears the last FAILURE note
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._lineage_trouble("the sender stopped (%s); the outbox waits" % type(e).__name__)

    def _lineage_trouble(self, why: str) -> None:
        """H6-8: a FAILURE note (gw.jsonl and stderr), kept as `lineage_failure` for keys' status (3c-9's doctor row)."""
        self.lineage_failure = why
        _note(logging.ERROR, "levain gw-keys: FAILURE lineage: %s", why)

    def lineage_status(self) -> dict:
        """keys' lineage status (H6-8; 3c-9 renders the doctor row from it): the outbox's size and bound, the records
        CONTROL quarantined (durable: the ledger's `lineage-acked` lines, so a restart keeps the count), the envelopes
        CONTROL refused since start, the refusal-window records dropped at a full outbox since start (r1 F1), and the
        last FAILURE note (None: none)."""
        led = self.ledger
        return {"outbox": led.outbox_size if led is not None else 0, "outbox_max": self.lineage_outbox_max,
                "quarantined": len(led.quarantined) if led is not None else 0,
                "envelopes_refused": self.lineage_envelopes_refused, "dropped": self.lineage_dropped,
                "failure": self.lineage_failure}

    def _note_refusal(self, session: str, run: Optional[dict], name: str) -> None:
        """One refusal of a TICKETED `Q` made before any hold (so no hold row records it): counted in its (session,
        reason) window (H6-10), whose ONE `refused` line is written at its close, `refused_row_s` after its first
        refusal. An interactive session's refusal writes none (Q9)."""
        if run is None or self.ledger is None or self.closing:
            return
        key = (session, name)
        w = self._refusals.get(key)
        if w is None:
            w = self._refusals[key] = [time.time(), 0, run, None]
            w[3] = asyncio.get_running_loop().call_later(self.refused_row_s, self._refused_close, key)
        w[1] += 1

    def _refused_close(self, key: Tuple[str, str]) -> None:
        """A refusal window's close: its `refused` line (H6-10: the current epoch, its own seq the triple's ref) carrying
        its row (H6-16), queued for the writer; the payload enters the outbox once the line is durable. A ledger that
        cannot take it loses that window's counts (noted): refusals carry no money."""
        w = self._refusals.pop(key, None)
        if w is None:
            return
        first, count, run, timer = w
        if timer is not None:
            timer.cancel()
        session, name = key
        row = refused_row(run, reason=name, count=count, at=_stamp(first), ended_at=_stamp(time.time()))
        led = self.ledger
        if led is None:
            return
        if led.outbox_committed >= self.lineage_outbox_max:
            # r1 F1 (bound): the outbox never passes `lineage_outbox_max`; a window's record that does not fit is
            # counted (`lineage_status()["dropped"]`), never written. Refusals carry no money.
            self.lineage_dropped += 1
            self._lineage_trouble("the outbox is full: a refusal window's record was dropped (%d %s)" % (count, name))
            return
        try:
            # the ledger's reason grammar is the refusal's name without its `:<what>` (`body-unbounded:document`);
            # the row keeps the whole name
            led.refused(session, run["route"], name.partition(":")[0], count, {"lineage": row}, run_id=run["run_id"])
        except Exception as e:
            # any failure, not only the ledger's own two (r1 complement LOW 5): this runs from a timer callback and
            # from `flush_refusals`' loop over every window, which must reach the rest
            _note(logging.WARNING, "levain gw-keys: a refusal window's line was not written (%s); %d %s lost",
                  type(e).__name__, count, name)

    def flush_refusals(self) -> None:
        """Every open refusal window closed now (keys' shutdown, before the ledger's close)."""
        for key in list(self._refusals):
            self._refused_close(key)

    # -- transitions --

    def _detach(self, reg: Registration, revoke: bool = False) -> List[Victim]:
        """Phase 1 (caller holds state_lock; no await): take `reg` out of every table and return every connection
        attributed to it, each marked detached, preceded by the `Upstream` OBJECT of each detached exchange that holds
        one (phase 2 aborts it: an abort drops the transport's buffer, where a close would flush credential-bearing
        bytes); an exchange's task is cancelled (delivery only, never the guarantee). The session's cached claims
        leave the token cache; with `revoke` (DROP-SESSION) their digests enter the dropped set (`drop_session` takes
        the session out of `budget_stopped`, registered or not)."""
        if self.regs.get(reg.front_uid) is reg:
            del self.regs[reg.front_uid]
        if self.by_session.get(reg.session) is reg:
            del self.by_session[reg.session]
        for digest in [d for d, a in self.cache.items() if a["session"] == reg.session]:
            del self.cache[digest]
            if revoke:
                self._mark_dropped(digest)
        for key, t in list(self.resolving.items()):
            if key[1] is reg:
                t.cancel()   # only this registration's exchanges await it: its permits are freed now, not at RESOLVE_S
        reg.state = "dropped"
        victims = ([reg.status] if reg.status is not None else []) + list(reg.pending) + list(reg.exchanges)
        reg.status = None
        reg.pending.clear()
        reg.exchanges.clear()
        ups: List[Victim] = []
        for c in victims:
            c.detached = True
            if c.upstream is not None:
                ups.append(c.upstream)
            if c.kind == "exchange" and c.task is not None:
                c.task.cancel()
        return ups + victims

    def _detach_all(self) -> List[Victim]:
        """Phase 1 of the fail-closed clear (caller holds state_lock; no await): every registration detached, every
        `Upstream` ahead of every connection, the token cache and the dropped set cleared, and every resolve task
        cancelled (the table is empty after it, so no resolve has a registration left to answer, whatever its epoch).
        `reg_high` is kept: a clear never lowers a fence. `budget_stopped` is kept: a clear never restarts a session's
        spend."""
        regs = {id(r): r for r in list(self.regs.values()) + list(self.by_session.values())}
        got: List[Victim] = []
        for reg in regs.values():
            got += self._detach(reg)
        self.cache.clear()
        self.dropped.clear()
        for t in list(self.resolving.values()):
            t.cancel()   # its permits are freed now, not at its deadline; its `finally` pops the entry
        return [v for v in got if isinstance(v, Upstream)] + [v for v in got if not isinstance(v, Upstream)]

    def _fence_exchanges(self) -> None:
        """Preemption's step (1) (caller holds state_lock; no await): every live exchange is marked detached, so
        `Upstream.write` refuses its next byte from the bump on, through step (2)'s bounded await. The tables are
        left to step (3)'s phase 1, which records each exchange's `Upstream` for phase 2's abort; a new exchange is
        refused at admission meanwhile, its registration having been made under an older epoch."""
        for reg in list(self.regs.values()) + list(self.by_session.values()):
            for c in reg.exchanges:
                c.detached = True

    def _mark_dropped(self, digest: str) -> None:
        self.dropped[digest] = None
        self.dropped.move_to_end(digest)
        while len(self.dropped) > DROPPED_MAX:
            self.dropped.popitem(last=False)

    def _effects(self, victims: List[Victim]) -> None:
        """Phase 2: run right after phase 1 released the lock, synchronously. An `Upstream` is aborted (never closed;
        its abort never raises). Each connection in its own try: `K` if it is a claimed STATUS connection, then close,
        then abort ABORT_AFTER_S later; any failure aborts that one and goes on."""
        if not victims:
            return
        loop = asyncio.get_running_loop()
        for c in victims:
            if isinstance(c, Upstream):
                c.abort()   # never raises an Exception, and notes its own failures
                continue
            t = c.writer.transport
            try:
                if c.kind == "status":
                    t.write(_K)
                t.close()
                loop.call_later(ABORT_AFTER_S, t.abort)
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: closing a detached connection failed (%s)", type(e).__name__)
                try:
                    t.abort()
                except Exception:
                    pass

    # -- the admin server --

    async def _on_admin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One admin connection: the uid check, the adoption (a preemption when one is adopted already), the read
        loop, and, unless a preemption replaced it, its EOF's fail-closed clear."""
        conn: Optional[AdminConn] = None
        try:
            try:
                uid = peer_uid(writer.get_extra_info("socket"))
            except PeerUnknown:
                return
            if self.control_uid is None or uid != self.control_uid or self.closing:
                return
            conn = await self._adopt(reader, writer)
            if conn is None:
                return
            await self._admin_read(conn)
        except (OSError, EOFError, AdminMalformed):
            pass   # CONTROL went away or sent what the wire does not allow: either way, this connection's EOF
        except asyncio.CancelledError:
            if conn is None or not conn.preempted:
                raise
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: admin connection handler failed (%s)", type(e).__name__)
        finally:
            if conn is None:
                writer.close()
            elif not conn.preempted:
                try:
                    await self._admin_eof(conn)
                finally:
                    if not conn.preempted:
                        writer.transport.abort()   # the clear has run: no answer still buffered stands

    async def _adopt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Optional[AdminConn]:
        """Adopt a new admin connection; with one adopted already, preempt it first, in the design's five steps."""
        assert self.admin_lock is not None and self.state_lock is not None
        async with self.admin_lock:
            if self.closing:
                return None
            old = self.admin
            async with self.state_lock:
                self.admin_epoch += 1                # (1) every task of the old connection is stale from here,
                epoch = self.admin_epoch
                self._fence_exchanges()              # (1) and every exchange it admitted sends nothing more
            if old is not None:
                old.preempted = True
                wait: Set[asyncio.Task] = set(old.tasks)
                if old.task is not None:
                    old.task.cancel()                # (1) stop reading it
                    wait.add(old.task)
                try:
                    for t in old.tasks:              # (2) cancel its request tasks, then await them, bounded
                        t.cancel()
                    if wait:
                        await asyncio.wait(wait, timeout=PREEMPT_WAIT_S)
                finally:
                    # (3)-(4) run however this handler leaves step (2), a cancel of it included: the old connection
                    # no longer runs its own EOF clear, so nothing else would
                    await asyncio.shield(self._preempt_clear(old))
            async with self.state_lock:
                # (5) adopt, only while the epoch captured at (1) is still current: an attachment EOF (or another
                # clear) that bumped it during step (2) makes a connection stamped `epoch` stale before its first
                # request, every answer on it withheld. The caller closes the connection on None.
                if self.closing or epoch != self.admin_epoch:
                    return None
                conn = AdminConn(reader, writer, epoch)  # it starts against an empty table
                self.admin = conn
            return conn

    async def _preempt_clear(self, old: AdminConn) -> None:
        """Preemption's steps (3) and (4): the fail-closed clear in its two phases, then the old connection closed.
        Until step (5) adopts the new connection, none is adopted. A clear that runs only after a NEWER connection
        was adopted (its preempting handler was cancelled and the shielded clear ran late) clears nothing: the table
        is the newer connection's, and that adoption's own preemption already cleared what `old` held."""
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:                  # (3) phase 1
            if self.admin is old or self.admin is None:
                self.admin = None
                victims = self._detach_all()
        self._effects(victims)                       # (3) phase 2
        try:
            old.writer.close()                       # (4)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: closing a preempted admin connection failed (%s)",
                  type(e).__name__)

    async def _admin_eof(self, conn: AdminConn) -> None:
        """The adopted connection ended: fail closed. The epoch is bumped in the same mutation section as the clear,
        so a request task of this connection still in flight changes nothing after it."""
        assert self.state_lock is not None
        async with self.state_lock:
            if self.admin is not conn or conn.epoch != self.admin_epoch:
                return
            self.admin_epoch += 1
            self.admin = None
            victims = self._detach_all()
        for t in list(conn.tasks):
            t.cancel()
        self._effects(victims)
        self._attach_close(self.attached)   # §4.5: and the other of the two connections

    async def _admin_read(self, conn: AdminConn) -> None:
        """The read loop. Each request's place in its session's chain is taken HERE, between the read and the spawn,
        with no `await` in between, so two requests naming one session run in frame order."""
        while True:
            got = await read_admin_frame(conn.reader)
            if got is None:
                return
            rid, op, args = got
            if len(conn.tasks) >= ADMIN_INFLIGHT_MAX:
                raise AdminMalformed("too many requests in flight")
            session = args.get("session") if type(args.get("session")) is str else None
            prev = conn.last.get(session) if session is not None else None
            t = asyncio.ensure_future(self._admin_request(conn, rid, op, args, prev))
            conn.tasks.add(t)
            if session is not None:
                conn.last[session] = t
            t.add_done_callback(functools.partial(self._admin_task_done, conn, session))

    def _admin_task_done(self, conn: AdminConn, session: Optional[str], t: asyncio.Task) -> None:
        conn.tasks.discard(t)
        if session is not None and conn.last.get(session) is t:
            del conn.last[session]
        if t.cancelled():
            return
        e = t.exception()
        if e is not None:
            _note(logging.WARNING, "levain gw-keys: admin request failed (%s)", type(e).__name__)

    async def _admin_request(self, conn: AdminConn, rid: int, op: str, args: dict,
                             prev: Optional[asyncio.Task]) -> None:
        """One request: wait for the previous request of its session (its outcome is not this one's), answer it,
        and write the answer only while its connection's epoch is current. A failure inside keys' own call, in the
        answer's encoding or in its write (an answer not drained within ADMIN_FRAME_S included) closes the
        connection: its EOF clears everything, never a table left as the failure left it nor a client left waiting."""
        if prev is not None and not prev.done():
            await asyncio.wait({prev})
        try:
            answer = await self._admin_answer(conn.epoch, rid, op, args)
            if answer is None or conn.epoch != self.admin_epoch:
                return   # stale, or refused by the shutdown latch: no answer
            data = encode_admin(answer)
            async with conn.wlock:
                if conn.epoch != self.admin_epoch:
                    return
                conn.writer.write(data)
                await asyncio.wait_for(conn.writer.drain(), ADMIN_FRAME_S)
        except Exception:
            # abort, not close: a close waits to flush what a client that stopped reading will never read, and
            # the read loop sees EOF only once the transport is gone
            conn.writer.transport.abort()
            raise

    async def _admin_answer(self, epoch: int, rid: int, op: str, args: dict) -> Optional[dict]:
        bad = check_admin_args(op, args)
        if bad is not None:
            return {"id": rid, "ok": False, "refused": bad}
        if op == "FRONT-REGISTER":
            out = await self.front_register(epoch, args["session"], args["front_uid"], args["pid"], args["start"],
                                            args["incarnation"], args["reg_token"])
        elif op == "FRONT-ARM":
            out = await self.front_arm(epoch, args["session"], args["reg_token"])
        elif op == "DROP-SESSION":
            out = await self.drop_session(epoch, args["session"])
        elif op == "STATUS":
            out = self.status()
        else:
            out = await self.reload(epoch, args["generation"], args["item_sha256"], keep=args["keep"])
        if out is None:
            return None
        if "refused" in out:
            return {"id": rid, "ok": False, **out}
        return {"id": rid, "ok": True, **out}

    # -- admission --

    def _lookup(self, uid: int, pid: int, start: int) -> Optional[Registration]:
        if self.single_uid is not None and uid == self.single_uid:
            hits = [r for r in self.regs.values() if r.pid == pid and r.start == start]
            return hits[0] if len(hits) == 1 else None
        if uid not in self.gw_uids:
            return None
        reg = self.regs.get(uid)
        if reg is None or reg.pid != pid or reg.start != start:
            return None
        return reg

    def _admit(self, peer: Tuple[int, int, int], writer: asyncio.StreamWriter) -> Optional[Conn]:
        """At accept, under state_lock, no await: which pool this connection is in, or None (close, no frame)."""
        if self.closing:
            return None
        reg = self._lookup(*peer)
        if reg is None:
            return None
        if reg.state == "armed" and reg.status is None:
            conn = Conn(writer, reg, "candidate")
            reg.status = conn
            return conn
        if reg.state == "claimed":
            if len(reg.pending) >= self.pending_per_peer or len(self.pool) >= self.max_exchange_conns:
                return None
            conn = Conn(writer, reg, "pending")
            reg.pending.add(conn)
            self.pool.add(conn)
            return conn
        return None   # REGISTERED and not armed, a STATUS candidate already pending, or LOST

    def _forget(self, conn: Conn) -> None:
        """The connection is closing: out of every set (idempotent; no lock, no await)."""
        reg = conn.reg
        reg.pending.discard(conn)
        reg.exchanges.discard(conn)
        if reg.status is conn:
            reg.status = None
        self.pool.discard(conn)

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.first_frame_s
        conn: Optional[Conn] = None
        try:
            try:
                peer = peer_of(writer.get_extra_info("socket"))
            except PeerUnknown:
                return
            assert self.state_lock is not None
            async with self.state_lock:
                conn = self._admit(peer, writer)
            if conn is None:
                return
            frames = FrameReader(reader)
            side = KeysSide()
            allowed = frozenset({b"H"}) if conn.kind == "candidate" else frozenset({b"Q"})
            got = await asyncio.wait_for(frames.read(allowed), max(0.0, deadline - loop.time()))
            if got is None or conn.detached:
                return
            ftype, payload = got
            side.got(ftype)
            if ftype == b"H":
                await self._status(conn, frames, payload)
            else:
                await self._exchange(conn, frames, side, payload)
        except (OSError, EOFError, WireError, asyncio.TimeoutError):
            pass   # the peer went away, sent what the wire does not allow, or nothing in time: closed with no frame
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: connection handler failed (%s)", type(e).__name__)
        finally:
            if conn is not None:
                self._forget(conn)
            writer.close()

    async def _status(self, conn: Conn, frames: FrameReader, h: dict) -> None:
        reg = conn.reg
        assert self.state_lock is not None
        async with self.state_lock:
            ok = (not conn.detached and not self.closing and self.regs.get(reg.front_uid) is reg and reg.state == "armed"
                  and reg.status is conn and h["session"] == reg.session and h["incarnation"] == reg.incarnation)
            if ok:
                reg.state = "claimed"
                conn.kind = "status"
                conn.writer.write(_A)
        if not ok:
            return
        try:
            await frames.read(frozenset())   # nothing is valid after `H`: EOF, or anything at all, ends it
        finally:
            if not conn.detached and self.regs.get(reg.front_uid) is reg:
                reg.state = "lost"   # the claim ended without keys' `K`: refused from now on
                _note(logging.WARNING, "levain gw-keys: front-lost session=%s", reg.session)
                # §4.5: CONTROL revokes the session. Never after keys' own `K` (a DROP-SESSION, a clear): the
                # connection is detached then, and this branch is not reached
                self._attach_send_task(self._front_lost(reg.session))

    async def _exchange(self, conn: Conn, frames: FrameReader, side: KeysSide, q: dict) -> None:
        """THE FRAME (design 1.2): the ONE admission check at `Q` and the epoch stamp, then entry into the
        registration's exchange set as the LAST statement under the lock; the outer `try` follows with no await
        between them, and its `finally` is the release, so phase 1 can always find the exchange until it is released."""
        reg = conn.reg
        assert self.state_lock is not None
        async with self.state_lock:
            reg.pending.discard(conn)
            if conn.detached:
                return
            admitted = (self.regs.get(reg.front_uid) is reg and reg.state == "claimed" and not self.closing
                        and reg.admin_epoch == self.admin_epoch and len(reg.exchanges) < self.exchanges_per_session
                        and (not self.attach or self.attached is not None))
            if admitted:
                conn.kind = "exchange"
                conn.admin_epoch = self.admin_epoch
                conn.rec = new_record(reg.session, q)
                reg.exchanges.add(conn)
        if not admitted:
            await self._refuse(conn, frames, side, "keys-unavailable", UNTRUSTED_DRAIN_CAP)
            return
        try:
            await self._serve_q(conn, frames, side, q)
        except StaleExchange:
            pass   # detached: nothing more went upstream, and no answer (phase 2 closes the front connection)
        finally:
            try:
                await release_exchange(conn)
            finally:
                self._log_exchange(conn)

    def _current(self, conn: Conn) -> bool:
        """Under state_lock: the exchange may still mutate shared state (its epoch current, not detached, its
        registration the current one for both its front and its session, not closing)."""
        reg = conn.reg
        return (not conn.detached and not self.closing and conn.admin_epoch == self.admin_epoch
                and self.regs.get(reg.front_uid) is reg and self.by_session.get(reg.session) is reg)

    async def _serve_q(self, conn: Conn, frames: FrameReader, side: KeysSide, q: dict) -> None:
        """The `Q` handler: the token state, then the route against the token's claims and keys' OWN policy, then the
        metered request path of R4 §1.7, in this order: (1) the `Q` re-checked against that policy, the credential and
        the session in `budget_stopped` (`_check_q`); (2) the body buffer's take for a declared length; (3) `A`,
        then the body collected into memory within `body_read_s` (`_collect_body`); (4)-(5) parse, check, the
        ceiling written in, keys' own serialization, the head, the estimate and the ledger's admit, last
        (`_check_request`, one synchronous call); (6)-(8) the upstream, the durable `reserved` line, the head and
        body through `Upstream.write`, and the relay (`_serve_held`). A stale or detached exchange ends with NO
        answer (its front connection is one of phase 1's victims, which phase 2 closes)."""
        started = _stamp(time.time())   # the request's start, as its lineage row says it (`at`)
        out = await self._token_state(conn, q["token"])
        if out is None or conn.detached:
            return
        if "refused" in out:
            await self._refuse(conn, frames, side, out["refused"], UNTRUSTED_DRAIN_CAP)
            return
        claims = out["answer"]["claims"]
        session = conn.reg.session   # the answer's session (`front_binding` held it equal): the `budget_stopped` key
        rec = conn.rec if conn.rec is not None else new_record(conn.reg.session, q)
        slot = claims.get("slot")
        rec["slot"] = slot if type(slot) is int else None
        try:
            pol = self._route_policy(claims, q["route"])
        except norm.Refused as r:
            self._note_refusal(session, claims.get("run"), r.name)   # a ticketed refusal's window (H6-10)
            await self._refuse(conn, frames, side, r.name, UNTRUSTED_DRAIN_CAP)
            return
        ff = FrontFrames(frames)
        # the body's bytes in the keys-wide buffer: returned on every exit of this handler (the refusal before its
        # drain), and by the last `finally`, when the exchange ends: the serialization lives through the relay
        buf = self.body_buffer.taker(conn.reg.session)
        try:
            observed = None
            if pol.credential is not None:
                # the ONE observation: a refresh-kind credential near its expiry waits here for its refresh (shielded:
                # cancelling this exchange never cancels it); the credential `fresh` checked is the one the request
                # is built with
                observed = await fresh(self.cred_lifecycle, self._cred_store(), pol.credential)
            req = self._check_q(claims, session, pol, q, observed)
            raw = await self._collect_body(conn, ff, side, q, pol, buf, rec)
            if raw is None:
                buf.give_back()
                ff.close()
                return   # detached, or the front aborted before its `E`: nothing was reserved, nothing sent
            if observed is not None:
                # the body read can take up to `body_read_s` after `fresh`'s observation: a token that expired
                # meanwhile is refused here, before anything is checked or held (the head's write checks it again)
                unexpired(self.cred_lifecycle, observed)
            head, hconn, cred, tpl, data, meter, hold = self._check_request(conn.reg, pol, req, raw, buf,
                                                                            claims.get("run"), at=started)   # admit last
        except norm.Refused as r:
            buf.give_back()
            self._note_refusal(session, claims.get("run"), r.name)   # no hold was admitted: the window records it
            try:
                await self._refuse(conn, ff, side, r.name, pol.body_cap)
            finally:
                ff.close()   # a refusal that raises or is cancelled still retrieves the front's read in flight
            return
        except BaseException:
            buf.give_back()
            ff.close()
            raise
        # The hold is resolved explicitly on every exit, before anything after the exchange's work runs (the refusal's
        # drain, `release_exchange`, the record): `end()` releases an unsent hold and books a sent one `uncertain
        # {ended}`, and is a no-op after the relay's settle. No statement runs between the admit above and this try
        # (a tripwire holds it), and no ledger call sits in a `finally`; the ledger's owner callback is the backstop.
        try:
            await self._serve_held(conn, ff, side, pol, rec, meter, hold, head, hconn, cred, tpl, data, buf)
        except (norm.Refused, UpstreamFailed) as r:
            hold.end(self._end_meta(conn, hold, r.name))
            if ff.responded:
                rec["refused"] = r.name   # after `R`: the front's connection is closed with the body cut short
                _note(logging.WARNING, "levain gw-keys: response cut short after its head (%s)", r.name)
            else:
                await self._refuse(conn, ff, side, r.name, pol.body_cap)
        except BaseException:
            hold.end(self._end_meta(conn, hold))   # a cancel, StaleExchange, any other raise: the same call, raised on
            raise
        else:
            hold.end(self._end_meta(conn, hold))   # every normal return (detached, connect lost, FrontAborted); a no-op
                                                   # after a settle
        finally:
            buf.give_back()
            ff.close()

    async def _serve_held(self, conn: Conn, ff: "FrontFrames", side: KeysSide, pol: RoutePolicy, rec: dict,
                          meter: "Tuple[codec.Codec, Price, Dict[str, str], str, bool]", hold: ledger.Hold, head: bytes,
                          hconn: "h11.Connection", cred: Optional[Credential], tpl: norm.Template, data: bytes,
                          buf: "BodyTake") -> None:
        """The exchange from its admitted hold on (R4 §1.7 steps 6-8): the upstream, the durable wait, the head behind
        the ledger's send gate, keys' own serialization of the body (`data`, the length the head declared) and the
        response, relayed unbuffered while the codec's usage reader reads it on the side. A completed relay settles
        from that usage (`_settle`); every other exit is resolved by `_serve_q`. Once `R` is sent no `F` may follow
        (the wire's order), so a failure after it ends the exchange with the front's connection closed mid-body
        (`ff.responded`)."""
        rec["template"] = tpl.path
        reader: Optional[codec.UsageReader] = None
        scanner = Scanner([cred.secret.encode("latin-1")] if cred is not None else [])
        try:
            if conn.detached:   # a detached exchange answers nothing
                return
            # from here keys reads the front while it connects, so an abort that arrives before the head is written
            # stops the request
            up = await self._open_upstream(conn, ff, side, pol, rec)
            if up is None:
                return   # the front aborted (`X`) or went away during the connect: nothing went upstream
            # the hold's `reserved` line durable before the head can go (its flush overlaps the connect)
            try:
                await hold.durable()
            except ledger.LedgerUnavailable as e:
                raise norm.Refused("keys-unavailable", e.detail) from None

            async def send_head() -> None:
                if ff.aborted():   # checked with no await before the write: an abort read by now stops the head
                    raise FrontAborted()
                if cred is not None:
                    # an access token that expired during the resolve, connect or TLS is never sent (no await
                    # between this check and the write)
                    unexpired(self.cred_lifecycle, cred)
                try:
                    hold.send_gate()   # the ledger's gate: a durable hold, the ledger healthy and open
                except ledger.LedgerUnavailable as e:
                    raise norm.Refused("keys-unavailable", e.detail) from None
                except ledger.LedgerMisuse as e:
                    _note(logging.WARNING, "levain gw-keys: ledger misuse at the send gate (%s)", e)
                    raise norm.Refused("keys-unavailable", "ledger misuse") from None
                up.write(head)     # the gate: a refused or failed write handed nothing upstream
                hold.mark_sent()   # from here the head may have reached the upstream
                await up.drain()

            async def work() -> bool:
                """The head, the upload, the response head and the relay; True when the relay completed."""
                nonlocal reader
                try:
                    await upstream_step(send_head())
                except FrontAborted:
                    return False
                resp = await self._upload_and_head(up, hconn, data, rec)
                r_frame = self._response_frame(resp, pol, cred, scanner)
                if conn.detached:
                    return False
                conn.writer.write(r_frame)
                ff.responded = True
                rec["status"] = resp.status_code
                await conn.writer.drain()
                reader = meter[0].usage(_header(resp.headers, b"content-type"), meter[2])
                conn.usage = reader   # a cut relay's lineage row reads what the response said (H6-6)
                await self._relay_body(conn, up, hconn, scanner, rec, reader,
                                       errors=resp.status_code >= 400
                                       and _json_media(_header(resp.headers, b"content-type")))
                return not conn.detached   # fenced mid-relay: cut short, so `_serve_q`'s `end()` books it

            if not await self._against_front(ff, side, up, work()):
                return   # the front aborted, or the exchange was cut short: `_serve_q`'s `end()` resolves the hold
            assert reader is not None   # a completed relay made one
            self._settle(conn.reg, meter[1], hold, reader, route=pol.route, model=meter[3], unproven=meter[4],
                         status=rec["status"] if type(rec["status"]) is int else 0,
                         checks_model=meter[0].checks_answered_model)
        finally:
            rec["redactions"] = scanner.count

    def _settle(self, reg: Registration, price: Price, hold: ledger.Hold, reader: codec.UsageReader, *,
                route: str = "", model: str = "", unproven: bool = False, status: int = 0,
                checks_model: bool = False) -> None:
        """A completed relay's resolution (R4 §1.2, §1.4): its reported usage priced by class, settled in full (the
        ledger books what passes the hold as `overrun`); with none, `uncertain` charged its hold, the reason the
        reader's (`usage-over-cap` when a cap stopped it, else `no-usage`). A cost above the hold stops the session: it
        enters `budget_stopped`, which `_check_q` and `_admit_hold` read, so every later `Q` of the session is
        `budget-overrun` until DROP-SESSION. It enters only while `reg`, the registration the exchange started on, is
        still the session's (a dropped or replaced one stops no later registration of the id), and BEFORE the ledger's
        settle, so a settle that raises still leaves the session stopped. Nothing in flight is touched: each other
        exchange settles from its own usage. The stop is then reported: `SPEND-OVERRUN {session, route, model, ref,
        overrun, unproven}` on the attachment (R4 §1.4 step 2), sent after the settle and whether or not it raised;
        CONTROL records it for the card and revokes nothing.

        A ticketed exchange's resolution carries keys' whole `/3` row (H6-28; `lineage_row`): `uncertain` with the
        call's metadata and no counts, or `settled: "yes"` with its counts, its cost and, past the hold, `overrun`
        (H6-7). `status` is the upstream's (the relayed `R`).

        3c-9 (R4 §5 CARRIED), on a codec that checks it (`checks_model`: the exchange's
        :attr:`levain.broker.codec.Codec.checks_answered_model`, head ruling 1026+1): a response that says another model
        ran than `model`, the checked one (:func:`levain.broker.codec.answered_other`), is never priced at `model`'s
        row: it is charged its whole hold, or the cost the response reports when that is more, and the session stops as
        for an overrun. With no usage it is `uncertain` at its hold, as any call, unless the cost it reports is past the
        hold: then that cost is settled (no counts; verify r2 R2), since `uncertain` charges only the hold. Such a
        stop is reported whatever its overrun, 0 included (L3 r1 Q5): `SPEND-OVERRUN` with `reason: "model-mismatch"`,
        so CONTROL records it for the card; an overrun stop of the checked model's own usage is reported as before."""
        usage = reader.result()
        md = reader.metadata()
        other = checks_model and bool(model) and codec.answered_other(md, model)
        current = self.by_session.get(reg.session) is reg
        if usage is None and not (other and (md.cost_reported or 0) > hold.amount):
            if other and current:
                self.budget_stopped.add(reg.session)
            try:
                hold.uncertain(reader.reason, self._row_meta(hold, "uncertain", reader, status))
            finally:
                if other and current:
                    self._report_stop(reg, hold, route, model, 0, unproven, other=True)
            return
        if other:
            cost = max(hold.amount, md.cost_reported or 0)
        else:
            assert usage is not None
            cost = codec.price_usage(usage, price)
        if (other or cost > hold.amount) and current:
            self.budget_stopped.add(reg.session)
        try:
            hold.settle(cost, dict(usage.meta() if usage is not None else {},
                                   **self._row_meta(hold, "yes", reader, status, usage, cost)))
        finally:
            if current and (other or cost > hold.amount):
                self._report_stop(reg, hold, route, model, max(0, cost - hold.amount), unproven, other=other)

    def _report_stop(self, reg: Registration, hold: ledger.Hold, route: str, model: str, overrun: int,
                     unproven: bool, *, other: bool) -> None:
        """`SPEND-OVERRUN` for a stop `_settle` made (R4 §1.4 step 2), on the attachment when there is one; an
        other-model stop carries `reason: "model-mismatch"` (L3 r1 Q5)."""
        if not self.attach:
            return
        ledger_id, epoch, seq = hold.ref
        args = {"session": reg.session, "route": route, "model": model, "ref": [ledger_id, epoch, seq],
                "overrun": overrun, "unproven": unproven}
        if other:
            args["reason"] = "model-mismatch"
        self._attach_send_task(self._spend_overrun(args))

    @staticmethod
    def _row_meta(hold: ledger.Hold, settled: str, reader: Optional[codec.UsageReader], status: int,
                  usage: Optional[codec.Usage] = None, cost: Optional[int] = None) -> dict:
        """`{"lineage": <keys' row>}` for a resolution of a ticketed hold after its call (H6-28), `{}` for an
        interactive one (Q9: no lineage)."""
        if hold.base is None:
            return {}
        md = reader.metadata() if reader is not None else codec.Metadata()
        return {"lineage": lineage_row(hold.base, settled=settled, ended_at=_stamp(time.time()), md=md, usage=usage,
                                       cost=cost, overrun=(cost - hold.amount) if cost is not None else 0,
                                       status=status)}

    def _end_meta(self, conn: Conn, hold: ledger.Hold, refused: str = "") -> Optional[dict]:
        """The `meta` of `hold.end()` (H6-28): for a ticketed hold that `end` will resolve, keys' row: a SENT hold is
        `uncertain {ended}` (the call happened: its metadata so far, `conn.usage`, and the upstream status if `R` was
        relayed), an unsent one `released` (`settled: "no"`, the call's facts null, H6-21); `refused` is the refusal
        that ended the exchange, if one did. None for an interactive hold or an `end` that is a no-op. NEVER raises
        (it is evaluated in `except BaseException` before the raise goes on): a failure builds the row from the base
        alone, as the ledger would, and a failure of that (or of the hold's own reads, r1 F8) is None."""
        try:
            st = hold.state
            if hold.base is None or st not in (ledger.PENDING, ledger.RESERVED, ledger.SENT):
                return None
            now = _stamp(time.time())
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: a hold's end could not be read (%s)", type(e).__name__)
            return None
        try:
            if st is ledger.SENT:
                rec = conn.rec or {}
                status = rec.get("status")
                md = conn.usage.metadata() if conn.usage is not None else codec.Metadata()
                row = lineage_row(hold.base, settled="uncertain", ended_at=now, md=md,
                                  status=status if type(status) is int else 0, refused=refused)
            else:
                row = lineage_row(hold.base, settled="no", ended_at=now, md=None, refused=refused)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: a lineage row was built from its base (%s)", type(e).__name__)
            try:
                row = dict(hold.base, settled="uncertain" if st is ledger.SENT else "no", ended_at=now)
            except Exception:
                return None
        return {"lineage": row}

    async def _open_upstream(self, conn: Conn, ff: "FrontFrames", side: KeysSide, pol: RoutePolicy,
                             rec: dict) -> Optional[Upstream]:
        """The exchange's `Upstream`, or None when the front aborted first: the resolve, the connect and the TLS
        handshake run as one task, raced with the front's next frame. `X`, EOF or a bad frame cancels them (a
        connection already made is stored on the exchange, which the release aborts). The body's `E` was read before
        the admit (R4 §1.7), so the only frame the wire still allows is `X`."""
        assert pol.upstream is not None   # a brokered route always names one
        host = pol.upstream.host
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout_s

        async def connect() -> Upstream:
            # resolve, connect and TLS share the one connect deadline (design 1.6); a resolver thread may outlive a
            # resolve past it, the exchange does not. A connection that lands as the deadline expires is still stored
            # on the exchange by open_exchange, so the release aborts it.
            addr = await upstream_step(asyncio.wait_for(pick_address(host, UPSTREAM_PORT),
                                                        max(0.0, deadline - loop.time())))
            rec["upstream_address"] = addr
            return await upstream_step(asyncio.wait_for(open_exchange(addr, UPSTREAM_PORT, host, conn),
                                                        max(0.0, deadline - loop.time())))

        task = asyncio.ensure_future(connect())
        try:
            await asyncio.wait({task, ff.pending(side.allowed())}, return_when=asyncio.FIRST_COMPLETED)
            if ff.aborted():
                return None
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)   # a race lost to the abort: its outcome is dropped

    async def _against_front(self, ff: "FrontFrames", side: KeysSide, up: Upstream, work: Awaitable[bool]) -> bool:
        """`work` (the exchange from its request head on) raced against the front's next frame, the ONE read
        `FrontFrames` keeps in flight. The body's `E` was read before the admit, so the only frame the wire still
        allows is `X`, and any completed read (`X`, EOF, a frame the wire refuses) is the front's abort: `work` is
        cancelled, the upstream connection aborted (nothing it queued is flushed), and False returned, so the hold is
        resolved by `_serve_q`'s exit (`end()`: released if unsent, `uncertain {ended}` once sent). A `work` that
        finished first wins whatever the front did; its raise goes on unchanged."""
        task = asyncio.ensure_future(work)
        try:
            await asyncio.wait({task, ff.pending(side.allowed())}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            if ff.aborted():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)   # its outcome is dropped: the front has gone
                up.abort()
                return False
            return await task   # no frame but `X` is allowed after `E`, so this is unreached; the work alone
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _upload_and_head(self, up: Upstream, hconn: "h11.Connection", data: bytes,
                               rec: dict) -> object:
        """The request body (keys' buffered serialization, `data`) up and the response head down, AT ONCE: an upstream
        may answer before it has read the body (a 401 or a 413) and stop reading, so the final head ends the upload.
        Returns that head. The head's deadline starts at the request's last byte (design 1.6); while the body is
        still going up, the head waits as long as the upload does."""
        upload = asyncio.ensure_future(self._send_body(up, hconn, data, rec))
        head = asyncio.ensure_future(upstream.read_head(up, hconn, None))
        try:
            await asyncio.wait({upload, head}, return_when=asyncio.FIRST_COMPLETED)
            if upload.done():
                # an upstream that answers (a 413) and then resets fails the upload too; when its head has already
                # been read, the head wins over that failure (keys' own refusal still stands)
                if (isinstance(upload.exception(), UpstreamFailed) and head.done() and not head.cancelled()
                        and head.exception() is None):
                    return head.result()
                upload.result()   # the upload's failure raises here
                if not head.done():
                    await upstream_step(asyncio.wait_for(asyncio.shield(head), self.head_timeout_s))
            return await upstream_step(head)
        finally:
            for t in (upload, head):
                if not t.done():
                    t.cancel()
            await asyncio.gather(upload, head, return_exceptions=True)

    def _response_frame(self, resp: "h11.Response", pol: RoutePolicy, cred: object, scanner: Scanner) -> bytes:
        """The `R` frame for a final response head, or the refusal (design 1.6): a redirect is `upstream-redirect`;
        a `content-encoding` other than identity, or a status past 599, is `upstream-error`; a 401 on a refreshing
        credential asks for a refresh and is `credential-unavailable` (never relayed, never replayed). Only the
        route's `responses.headers` names pass, each value scanned as one block."""
        status = resp.status_code
        if status in REDIRECTS:
            raise norm.Refused("upstream-redirect", str(status))
        if status > 599:
            raise norm.Refused("upstream-error", "status")
        if status == 401 and cred is not None:
            # before any header is processed: a refreshing credential's 401 wants its refresh whatever its headers
            on_upstream_401(self.cred_lifecycle, self.creds, cred, cred.generation)   # type: ignore[attr-defined]
            if getattr(cred, "kind", None) == "oauth2-refresh":
                raise norm.Refused("credential-unavailable", "upstream 401")
        for raw_name, raw_value in resp.headers:
            if (bytes(raw_name).lower() == b"content-encoding"
                    and bytes(raw_value).strip().lower() not in (b"", b"identity")):
                raise norm.Refused("upstream-error", "encoding")
        out = self._relayed_headers(resp.headers, pol, scanner)
        try:
            return gwwire.encode(b"R", {"v": 1, "status": status, "headers": out})
        except WireError:
            raise norm.Refused("upstream-error", "response head over the frame cap") from None

    def _relayed_headers(self, headers: Sequence[Tuple[bytes, bytes]], pol: RoutePolicy,
                         scanner: Scanner) -> List[List[str]]:
        """The response headers relayed to the front (in `R`, or in `U`): only the route's `responses.headers`
        names, each value scanned as one block, a value outside the wire's grammar dropped (noted)."""
        allow = set(pol.response_headers)
        out: List[List[str]] = []
        for raw_name, raw_value in headers:
            name = bytes(raw_name).decode("ascii", "replace").lower()
            if name not in allow:
                continue
            value, _ = scanner.value(bytes(raw_value))
            text = value.decode("latin-1")
            if _HVALUE.match(text) is None:
                _note(logging.WARNING, "levain gw-keys: response header dropped (value outside the wire grammar) %s",
                      name)
                continue
            out.append([name, text])
        return out

    async def _relay_body(self, conn: Conn, up: Upstream, hconn: "h11.Connection", scanner: Scanner,
                          rec: dict, reader: "codec.UsageReader", errors: bool = False) -> None:
        """`b`* `e`: each body read relayed at once through the scanner (no buffering but the held possible start of a
        secret), each frame drained before the next read, so a slow front pauses the upstream's reader. Each read is
        handed to the usage `reader` first, which keeps its own bounded side copy and never changes or holds the
        relayed bytes. A detached exchange relays nothing more: `detached` is checked before every frame, after each
        read and each drain. `errors` (an error status with a JSON body) keeps a side copy of the body up to
        UPSTREAM_ERROR_BODY_MAX bytes for the record's `upstream_error`; a longer body has none."""
        w = conn.writer
        side: Optional[bytearray] = bytearray() if errors else None
        while True:
            part = await upstream_step(upstream.read_body(up, hconn, self.idle_s))
            if part is not None:
                reader.feed(part)
                if side is not None:
                    side += part
                    if len(side) > UPSTREAM_ERROR_BODY_MAX:
                        side = None
            out = scanner.feed(part) if part is not None else scanner.flush()
            for i in range(0, len(out), BODY_FRAME):
                if conn.detached:
                    return
                piece = out[i:i + BODY_FRAME]
                w.write(gwwire.encode(b"b", piece))
                rec["bytes_down"] += len(piece)
                await w.drain()
            if part is None:
                break
        if side is not None:
            rec["upstream_error"] = upstream_error_of(bytes(side), scanner)
        if conn.detached:
            return
        w.write(gwwire.encode(b"e"))
        await w.drain()

    def _log_exchange(self, conn: Conn) -> None:
        """The exchange's gw.jsonl record (design 1.9), written once at its end; never a header value, a body byte or a
        token. A failing log never touches the exchange's outcome."""
        rec, conn.rec = conn.rec, None
        if rec is None or self.gw_log is None:
            return
        try:
            out = {k: v for k, v in rec.items() if k != "t0"}
            out["ms"] = int((time.monotonic() - rec["t0"]) * 1000)
            out["kind"] = "exchange"
            out["at"] = _at(time.time())
            self.gw_log.write(out)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: the exchange record was not written (%s)", type(e).__name__)

    def _route_policy(self, claims: dict, route: str) -> RoutePolicy:
        """keys' OWN policy for `route`: it must be one of the token's `routes`, and the object comes from keys' load
        of the base map (then the dispatch seam), never from the front or session.json. A route that is not brokered
        is no `Q` route."""
        routes = claims.get("routes")
        if type(routes) is not list or route not in routes:
            raise norm.Refused("route-not-allowed", "not in the token's routes")
        pol = self._base_route(route)
        ids = frozenset() if pol is None or self.routes is None else self.routes.route_ids()
        if ids is None or route in ids:
            # the collision rule AT THE POINT OF USE: base.json is live-reloaded, so a base id added after the RELOAD
            # that checked the map would otherwise route a pinned dispatch session by the base policy, unpinned. The
            # ids are every served generation's; when they are unknown, every base route fails closed.
            why = "the dispatch route ids are unknown" if ids is None else "a base route and a dispatch route"
            _note(logging.WARNING, "levain gw-keys: route %s refused: %s", route, why)
            raise norm.Refused("keys-unavailable", f"route-collision: {route}: {why}")
        run = claims.get("run")
        if run is not None:
            # a dispatch run (a ticketed session) calls its run's route only, and that route is a dispatch-map route:
            # a run named on any base-map route (passthrough, egress or interactive brokered) is refused here, keys'
            # own refusal beside CONTROL's `ticket-field:route` at mint
            if route != run.get("route"):
                raise norm.Refused("route-not-allowed", "not the run's route")
            if pol is not None:
                raise norm.Refused("passthrough-for-dispatch", route)
        if pol is None:
            pol = self.dispatch_policy(route, routemaps.pin_of(claims))
        if pol is None or pol.type != "brokered" or pol.upstream is None:
            raise norm.Refused("route-not-allowed", "no brokered route")
        return pol

    def status(self) -> dict:
        """`STATUS` (3c-9 session D): read-only, no upstream connection, never a credential's bytes. `ledger_bytes`:
        the reservation ledger's size (None when keys has none or cannot stat it). `routes`: the brokered `[upstream
        host, credential name]` pairs keys would serve now, sorted (`served_pairs`). `routes_problem`: why a part of
        that set cannot be told (that part is then left out, as keys refuses it), else None. `limits` (D1): one entry
        per OpenRouter credential among those pairs, sorted by name, as the limit refresher last read it
        (`KeyLimit.entry`; never read yet is `age_s` and `failed` both None), from memory only. `limits_truncated`
        and `routes_truncated`: how many entries past STATUS_ANSWER_MAX were left off the end of each list, `limits`
        given the room first."""
        size = None
        if self.ledger is not None:
            try:
                size = os.stat(self.ledger.path).st_size
            except OSError:
                size = None
        pairs, problems = self.served_pairs()
        now = time.monotonic()
        limits = [self.key_limits.get(c, KeyLimit()).entry(c, now) for c in _openrouter_names(pairs)]
        out = {"ledger_bytes": size, "limits": limits, "limits_truncated": 0,
               "routes": sorted([h, c] for h, c in pairs), "routes_problem": "; ".join(problems) or None,
               "routes_truncated": 0}
        return _within_budget(out, [("limits", "limits_truncated"), ("routes", "routes_truncated")])

    def served_pairs(self) -> Tuple[Set[Tuple[str, str]], List[str]]:
        """The brokered `(upstream host, credential name)` pairs keys would serve now, and why a part of that set
        cannot be told: the base map's (a base id that is also a dispatch id is refused at use by `_route_policy`,
        so it is not one) and each dispatch generation's whose pin rule (`KeysRoutes.policy`) holds. Local reads only
        (the base map's stat-keyed parse)."""
        pairs: Set[Tuple[str, str]] = set()
        problems: List[str] = []
        ids = None if self.routes is None else self.routes.route_ids()
        if self.routes is not None and ids is None:
            problems.append("the dispatch route ids are unknown, so keys refuses every base route and the dispatch "
                            "routes cannot be told")
        elif self.lib_root is not None:
            try:
                self._base = routemaps.read_base_map(self.lib_root, self._base, parse=parse_base_map)
                base = self._base[1]
            except FileNotFoundError:
                base = {}
            except (OSError, ValueError, ContractError, RecursionError) as e:
                problems.append(f"the base map is unusable ({type(e).__name__}), so keys refuses every base route")
                base = {}
            pairs |= _brokered(p for rid, p in base.items() if rid not in (ids or ()))
        if self.routes is not None and ids is not None:
            for gen, m in list(self.routes.maps.items()):
                for rid in list(m.routes):
                    try:
                        pol = self.routes.policy(rid, routemaps.Pin(gen, m.digests[rid]))
                    except (RouteRefused, KeyError):
                        continue
                    pairs |= _brokered([pol])
        return pairs, problems

    # -- the OpenRouter limit refresher (D1, 1025+40 rulings) --

    def start_limits(self) -> None:
        """Start the limit refresher once (`_serve` calls it after the listeners are served, and nothing else in keys
        does, so an in-process Keys opens no upstream connection unless its caller starts this): a pass at once, then
        one every `key_limit_every_s`, until the shutdown latch (`_close` cancels it)."""
        self._in_loop()
        if self._limit_task is None and not self.closing:
            self._limit_task = asyncio.ensure_future(self._limit_loop())

    async def _limit_loop(self) -> None:
        while not self.closing:
            try:
                await self.limit_pass()
            except Exception as e:   # a defect in one pass never ends the refresher
                _note(logging.WARNING, "levain gw-keys: limit pass failed (%s)", type(e).__name__)
            await asyncio.sleep(self.key_limit_every_s)

    async def limit_pass(self) -> None:
        """Read the limit of each OpenRouter credential keys serves now, one at a time, and forget the credentials it
        no longer serves."""
        names = _openrouter_names(self.served_pairs()[0])
        for gone in set(self.key_limits) - set(names):
            del self.key_limits[gone]
        for name in names:
            if self.closing:
                return
            try:
                got = await self._read_limit(name)
            except Exception as e:   # one credential's defect never starves the rest of the pass (L3 r1 E2)
                _note(logging.WARNING, "levain gw-keys: limit read for %s failed (%s)", name, type(e).__name__)
                got = "internal"
            self.key_limits.setdefault(name, KeyLimit()).record(got, time.monotonic())

    async def _read_limit(self, name: str) -> Union[str, Tuple[Optional[int], Optional[int], Optional[str],
                                                                   Optional[bool]]]:
        """One read of `name`'s limit: `(limit, remaining, reset, byok)` (microdollars, `_limit_values`), or the
        failure's token from LIMIT_FAILURES. The credential is observed (never refreshed: `unexpired`, not `fresh`)
        and must be bound to `policy.OPENROUTER_HOST`; the whole call is bounded by `key_limit_s`."""
        try:
            cred = self._cred_store().observe(name)
        except norm.Refused:
            return "no-credential"
        if codec.OPENROUTER_HOST not in cred.hosts:
            return "host-mismatch"
        try:
            unexpired(self.cred_lifecycle, cred)
        except norm.Refused:
            return "expired"
        # the root interpreter's floor is 3.9, which has no `asyncio.timeout`: the call runs as a task that a timer
        # cancels at `key_limit_s`. `expired` tells that cancel from any other, and a timeout the call itself raises
        # (the kernel's, an inner `wait_for`'s) stays what it was, a failure of the connection
        expired = False
        task = asyncio.ensure_future(upstream.limit_get(cred.header_pair()))

        def expire() -> None:
            nonlocal expired
            expired = True
            task.cancel()
        timer = asyncio.get_running_loop().call_later(self.key_limit_s, expire)
        try:
            resp = await task
        except asyncio.CancelledError:
            if expired:
                return "timeout"
            raise
        except norm.Refused as r:
            return "address-refused" if r.name == "address-refused" else "bad-credential"
        except upstream.BadResponse:
            return "bad-body"
        except (OSError, EOFError, asyncio.TimeoutError):   # a refused, reset or kernel-timed-out connect, a TLS failure, a lost connection; on 3.9 asyncio.TimeoutError is no OSError
            return "unreachable"
        finally:
            timer.cancel()
        return _limit_values(resp)

    def _dispatch_policy(self, route: str, pin: Optional["routemaps.Pin"]) -> Optional[RoutePolicy]:
        """The dispatch map's policy for `route` under the session's `pin` (R4 §2's three-part rule,
        `KeysRoutes.policy`); a refusal names the rule that failed."""
        if self.routes is None:
            return None
        try:
            return self.routes.policy(route, pin)
        except RouteRefused as e:
            raise norm.Refused(e.name, e.detail) from None

    # -- RELOAD (S1 3c-4; 3c design r3 §2.3 "Load at keys", R4 §2) --

    async def reload(self, epoch: int, generation: int, item_sha256: str, *, keep: Sequence[int]) -> Optional[dict]:
        """`RELOAD {generation, item_sha256, keep}`: under the RELOAD lock, the file work off the loop (`current`, the
        file and its digest, every item check, the order against keys' own head on disk, then that head written durably,
        all inside `KeysRoutes.txn`), then the generation served on the loop, then the ack
        `{generation, item_sha256, served: [generation]}`. Head ruling H5 (1024+8): the head written drops every
        `served` entry below it that neither `keep` nor a `run` in keys' cache names, and `served` is what the
        written head still lists (CONTROL deletes a generation's files only once this list leaves it out). A
        refusal leaves every dispatch route refused until a valid RELOAD. A request from a stale epoch, or after the
        shutdown latch, is not answered; a RELOAD that completed before its epoch went stale still stands (it was
        verified whole).

        The file work is awaited to COMPLETION, a cancel of this task included (a preemption cancels the task; the
        executor thread cannot be cancelled): its outcome is published in memory (served or refused, so memory matches
        the disk head it left) before the lock is released, and only then is the cancel re-raised."""
        if self.closing or epoch != self.admin_epoch:
            return None
        rm = self.routes
        if rm is None:
            return {"refused": "routemap-verifier"}
        assert self.reload_lock is not None
        loop = asyncio.get_running_loop()
        async with self.reload_lock:
            held = frozenset(keep) | self._cached_run_generations()
            try:
                fut = loop.run_in_executor(None, self._reload_files, rm, generation, item_sha256, held)
            except Exception as e:   # the executor took no work (shut down, cannot schedule): a failed RELOAD
                rm.refuse("keys-unavailable")
                _note(logging.WARNING, "levain gw-keys: RELOAD %s failed (%s)", generation, type(e).__name__)
                return {"refused": "keys-unavailable"}
            cancelled = False
            while True:
                try:
                    await asyncio.shield(fut)
                    break
                except asyncio.CancelledError:
                    if not fut.cancelled():
                        cancelled = True   # this task was cancelled (a preemption): the work is waited out all the same
                    if fut.done():
                        break
                except Exception:
                    break                  # the work raised: read below
            out: dict
            exc = None if fut.cancelled() else fut.exception()
            if fut.cancelled():            # never the work's own outcome (the thread cannot be cancelled); fail closed
                rm.refuse("keys-unavailable")
                out = {"refused": "keys-unavailable"}
            elif isinstance(exc, RouteRefused):
                rm.refuse(exc.name)
                _note(logging.WARNING, "levain gw-keys: RELOAD %s refused (%s)", generation, exc.name)
                out = {"refused": exc.name}
            elif exc is not None:   # the head's write failed: nothing new is served, and the refusal is a fault
                rm.refuse("keys-unavailable")
                _note(logging.WARNING, "levain gw-keys: RELOAD %s failed (%s)", generation, type(exc).__name__)
                out = {"refused": "keys-unavailable"}
            else:
                checked, head = fut.result()
                rm.accept(checked, head)
                out = {"generation": generation, "item_sha256": item_sha256,
                       "served": [e["generation"] for e in head["served"]]}
        if cancelled:
            raise asyncio.CancelledError()
        return out

    @staticmethod
    def _reload_files(rm: "routemaps.KeysRoutes", generation: int, item_sha256: str,
                      keep: "frozenset[int]") -> "Tuple[routemaps.Checked, dict]":
        """The synchronous RELOAD transaction, one at a time (`rm.txn`): the checks, then the head pruned to `keep`
        (H5) and settled."""
        with rm.txn:
            checked, head = rm.check_reload(generation, item_sha256)
            head = rm.pruned(head, keep)
            rm.commit(head)
        return checked, head

    def _cached_run_generations(self) -> "frozenset[int]":
        """The route-map generations the `run`s in keys' cache are pinned to (H5: never pruned while cached)."""
        out = set()
        for entry in self.cache.values():
            run = entry.get("claims", {}).get("run") if isinstance(entry, dict) else None
            if isinstance(run, dict) and type(run.get("routemap_generation")) is int:
                out.add(run["routemap_generation"])
        return frozenset(out)

    def _base_route(self, route: str) -> Optional[RoutePolicy]:
        """`route`'s object in `L/routes/base.json`, opened at every `Q` and parsed (`policy.parse_base_map`: `/1`
        as main read it, `/2` with its `prices`) whenever the opened file's (st_dev, st_ino, st_mtime_ns, st_size)
        differs from the last map that parsed whole, or None when the map has none. EVERY entry is parsed before one
        is selected: a map keys cannot read, any entry that does not parse, or any route id named twice anywhere in
        the map refuses `keys-unavailable` (logged), never a pass for the route asked about; a refused map is never
        cached. The read is `routemaps.read_base_map`, the one reader `levain doctor` shares."""
        if self.lib_root is None:
            raise norm.Refused("keys-unavailable", "no lib root")
        try:
            self._base = routemaps.read_base_map(self.lib_root, self._base, parse=parse_base_map)
            return self._base[1].get(route)
        except (OSError, ValueError, ContractError, RecursionError) as e:
            _note(logging.WARNING, "levain gw-keys: the base route map is unusable (%s)", type(e).__name__)
            raise norm.Refused("keys-unavailable", "base map") from None

    def _cred_store(self) -> CredStore:
        if self.creds is None:
            if self.lib_root is None:
                raise norm.Refused("credential-unavailable", "no lib root")
            try:
                fd = self._open_cred_dir()
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: the credential directory is unreadable (%s)", type(e).__name__)
                raise norm.Refused("credential-unavailable", "no credential directory") from None
            self.creds = CredStore(fd)
        return self.creds

    def _open_cred_dir(self) -> int:
        return os.open(os.path.join(self.lib_root, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def open_cred_store(self) -> bool:
        """At keys' start, before `_serve` counts the fixed fds: open the reader's credential directory now, so its fd
        is a counted one. False (nothing opened, no failure) when it cannot be: `cred/` is made by the first
        `set-credential`, so it may not exist yet; the first exchange that needs a credential opens it then
        (TRANSIENT_NAMED budgets that case)."""
        if self.creds is None and self.lib_root is not None:
            try:
                self.creds = CredStore(self._open_cred_dir())
            except OSError as e:
                if e.errno != errno.ENOENT:
                    _note(logging.WARNING, "levain gw-keys: the credential directory is not open at start (%s)",
                          type(e).__name__)
        return self.creds is not None

    def _check_q(self, claims: dict, session: str, pol: RoutePolicy, q: dict,
                 observed: Optional[Credential]) -> tuple:
        """R4 §1.7 step 1: keys' own re-check of the `Q` against ITS policy (the front's verdict is never taken), with
        no reservation and no request head. Order: a route with no price table (a base `/1` map's) is
        `model-not-allowed` (a ticketed run's model is checked against its ticket once the body is parsed,
        `_check_request`); a websocket upgrade is `body-unbounded:websocket` (one exchange carries many responses,
        no one usage settles it); target and template; headers (strict: a forbidden, consumed or off-list name is
        `policy-header`); the declared length against `body_cap`; the credential and its host binding; the session in
        `budget_stopped` (`budget-overrun`). Returns (method, path, query, the re-checked headers, the template,
        the credential or None). The credential is `observed`, the one `fresh` observed and checked for the route's
        `credential`: it is not observed again."""
        if pol.prices is None:
            raise norm.Refused("model-not-allowed", "route is not priced")
        if q["upgrade"] == "websocket":
            codec.refuse_upgrade()
        try:
            method, path = q["method"].encode("ascii"), q["path"].encode("ascii")
            query = q["query"].encode("ascii") or None
        except UnicodeEncodeError:
            raise norm.Refused("malformed", "target") from None
        tpl = norm.match_template(pol.templates, method, path, query)
        fwd = norm.check_headers([(n, v) for n, v in q["headers"]], pol.headers or {}, upgrade_ok=False, strict=True)
        body = q["body"]
        if body.startswith("length:") and int(body[len("length:"):]) > pol.body_cap:
            raise norm.Refused("body-cap", "length")
        cred = None
        if pol.credential is not None:
            if observed is None or observed.name != pol.credential:
                raise norm.Refused("credential-unavailable", "not observed")
            cred = observed
            assert pol.upstream is not None
            check_host(cred, pol.upstream.host, pol.route)
            if any(n == cred.header for n, _ in fwd):
                raise norm.Refused("policy-header", cred.header)
        if session in self.budget_stopped:
            raise norm.Refused("budget-overrun", "the session is stopped")
        if claims.get("run") is not None and self.ledger is not None \
                and self.ledger.outbox_committed >= self.lineage_outbox_max:
            # H6-11: a full lineage outbox refuses new TICKETED `Q`s until acks drain (an interactive session writes
            # no lineage, so it is never refused for it). r1 F1: the measure counts the payloads already owed (lines
            # not yet durable, ticketed holds not yet resolved), so concurrent admits cannot pass the bound
            raise norm.Refused("keys-unavailable", "the lineage outbox is full")
        return method, path, query, fwd, tpl, cred

    def _check_request(self, reg: Registration, pol: RoutePolicy, req: tuple, raw: "_OneShot",
                       buf: "BodyTake", run: Optional[dict] = None, at: Optional[str] = None
                       ) -> Tuple[bytes, "h11.Connection", object, norm.Template, bytes,
                                  Tuple[codec.Codec, Price, Dict[str, str], str, bool], ledger.Hold]:
        """R4 §1.7 steps 4-5, ONE synchronous call (no `await` anywhere in it): the body codec's parse of the buffered
        body, its `check` (the model has a price entry; on a dispatch-shaped route, one with no per-session cap, the
        unboundable shapes are `body-unbounded:<what>`), the ceiling written in where absent and, beside it, a streamed
        request's usage asked for where the codec's provider sends it only when asked, the estimate, keys' own
        serialization (the bytes that go upstream; past `body_cap` it is `body-cap`, and `buf` is made to cover its
        length), the request head (`content-length` = that serialization's length, whatever framing the `Q`
        declared), and the ledger's admit LAST (a hold is admitted only for a request that
        goes out). A `RecursionError` anywhere in these steps is `body-json`, with no hold taken. Returns the request
        head, the h11 client that wrote it (it frames the body and parses the response), the credential (or None),
        the matched template, the body bytes, the codec, the price entry, the request's `call_names` the settle
        reads, the priced model and whether the hold is unproven (the meter), and the admitted hold. `req` is
        `_check_q`'s answer. For a ticketed run (`run`, from CONTROL's resolve): the checked model must be the run's
        `model_requested` (`model-not-allowed`), and the hold is the run's (`_admit_hold`). On a dispatch route to
        OpenRouter, the route's `body_policy` is written in as the request's `provider` object, replacing the client's
        (`codec.write_body_policy`), before the estimate and the serialization."""
        method, path, query, fwd, tpl, cred = req
        c = codec.CODECS[pol.body_codec]   # a priced route's codec is never `none` (`policy.parse_base_map`)
        obj = c.parse(raw.take())   # the raw bytes are unreferenced once parsed: only `data` below is kept
        dispatch = pol.max_session_microdollars is None
        checked = codec.guarded(c.check, obj, pol.prices, dispatch=dispatch,
                                model_requested=None if run is None else run["model_requested"])
        if dispatch and pol.upstream is not None and pol.upstream.host == codec.OPENROUTER_HOST:
            codec.guarded(codec.write_body_policy, obj, pol.body_policy or {})
        codec.guarded(c.write_ceiling, obj, checked.price)
        codec.guarded(c.write_stream_usage, obj)
        estimate = codec.guarded(c.estimate, obj, checked.price)
        names = codec.guarded(c.call_names, obj, checked.price)   # the settle names a billed call as the hold did
        data = codec.guarded(codec.serialize, obj)
        if len(data) > pol.body_cap:   # what is written in can grow a body at the cap past it
            raise norm.Refused("body-cap", "serialized")
        buf.cover(len(data))   # the buffer holds at least the serialization keys keeps until the upload
        head, hconn = request_head(pol, method, path, query, fwd, "length:%d" % len(data), cred)
        # an interactive route admits content the estimate cannot see, marked on its `reserved` line (R4 §1.6)
        meta = {"bound": "unproven"} if checked.unproven else {}
        meter = (c, checked.price, names, checked.model, bool(checked.unproven))
        hold = self._admit_hold(reg, pol, estimate, checked.model, meta, run, at=at,
                                unproven=bool(checked.unproven))   # the last step: see `_serve_q`
        return head, hconn, cred, tpl, data, meter, hold

    def _admit_hold(self, reg: Registration, pol: RoutePolicy, estimate: int, price_ref: str,
                    meta: dict, run: Optional[dict] = None, *, at: Optional[str] = None,
                    unproven: bool = False) -> ledger.Hold:
        """The ledger's synchronous admit of `estimate` (R4 §1.2's estimate, never shrunk: a hold that does not fit the
        room is `budget-exhausted`) against the route's per-session cap, scope ("session-route", session, route), or,
        for a ticketed run, the run's `reserved_microdollars`, scope ("run", run_id) (R4 §1.2);
        `price_ref` is the priced model. The session's place in `budget_stopped` is read again as the statement right
        before the admit (no `await` between the two), because the body read after `_check_q`'s read can take up
        to `body_read_s`. The ledger's own errors become refusals: `budget-exhausted` (402), and
        `keys-unavailable` (503) for an unhealthy or closing ledger and, noted, for a misuse.

        A ticketed run's `reserved` line carries its `lineage_base` (S1 3c-6c, R4 §3: the record is on disk before it
        is sent): the whole `/3` record but its outcome, `at` the request's start (`_serve_q`'s), `hold_microdollars`
        the hold admitted (`bound` returns the estimate, never shrunk)."""
        if self.ledger is None:
            raise norm.Refused("budget-exhausted", "no ledger")
        if run is not None:
            scope: Tuple[str, ...] = ("run", run["run_id"])
            cap = run["reserved_microdollars"]
            meta = dict(meta, lineage_base=lineage_base(run, at if at is not None else _stamp(time.time()), estimate,
                                                        unproven))
        else:
            scope = ("session-route", reg.session, pol.route)
            cap = pol.max_session_microdollars
        if cap is None:   # a dispatch route with no run: a session with no ticket has no dispatch scope
            raise norm.Refused("budget-exhausted", "no scope")

        def bound(room: int) -> int:
            return estimate   # above the room, the ledger refuses it `budget-exhausted`

        if run is not None and self.ledger.outbox_committed >= self.lineage_outbox_max:
            # r1 F1: `_check_q`'s outbox check read again before the admit (the body read sits between them; no
            # `await` from here to the admit)
            raise norm.Refused("keys-unavailable", "the lineage outbox is full")
        if reg.session in self.budget_stopped:
            raise norm.Refused("budget-overrun", "the session is stopped")
        try:
            return self.ledger.admit(scope, cap, bound, price_ref=price_ref, meta=meta)
        except ledger.Refused as e:
            raise norm.Refused(e.name, e.detail) from None
        except ledger.LedgerUnavailable as e:
            raise norm.Refused("keys-unavailable", e.detail) from None
        except ledger.LedgerMisuse as e:
            _note(logging.WARNING, "levain gw-keys: ledger misuse at admit (%s)", e)
            raise norm.Refused("keys-unavailable", "ledger misuse") from None

    async def _collect_body(self, conn: Conn, ff: "FrontFrames", side: KeysSide, q: dict, pol: RoutePolicy,
                            buf: "BodyTake", rec: dict) -> Optional[_OneShot]:
        """R4 §1.7 steps 2-3: the buffer budget, `A`, then the front's `B` frames into memory until `E` (the body) or
        `X` / EOF / a frame the wire refuses (None, nothing reserved); None too for a detached exchange (no `A`). A
        declared length is taken from `buf` before `A`, a chunked body per `B` frame as it arrives (`keys-unavailable`
        when the take cannot be granted). keys counts the bytes itself: past `body_cap` is `body-cap`, a `length:n`
        body of any other size, or any body on a `none` request, is `malformed`; the whole body must arrive within
        `body_read_s` of `A`, else `body-timeout`."""
        body = q["body"]
        want = int(body[len("length:"):]) if body.startswith("length:") else None
        if want is not None:
            buf.take(want)
        if conn.detached:   # a detached exchange answers nothing
            return None
        # `A` once the exchange is accepted (design 1.3): the front's body follows it
        conn.writer.write(_A)
        await conn.writer.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.body_read_s
        parts: List[bytes] = []
        got = 0
        while True:
            left = deadline - loop.time()
            if left <= 0:
                raise norm.Refused("body-timeout", "body read")
            try:
                fr = await asyncio.wait_for(ff.read(side.allowed()), left)
            except asyncio.TimeoutError:
                raise norm.Refused("body-timeout", "body read") from None
            except (WireError, asyncio.IncompleteReadError, ConnectionError):
                return None   # a read that failed is the front's abort (`FrontFrames.aborted`)
            if fr is None:
                return None
            ftype, payload = fr
            side.got(ftype)
            if ftype == b"X":
                return None
            if ftype == b"E":
                break
            assert isinstance(payload, bytes)
            got += len(payload)
            if body == "none" or (want is not None and got > want):
                raise norm.Refused("malformed", "body length")
            if got > pol.body_cap:
                raise norm.Refused("body-cap", "body")
            if want is None:
                buf.take(len(payload))
            parts.append(payload)
        if want is not None and got != want:
            raise norm.Refused("malformed", "body length")
        return _OneShot(b"".join(parts))

    async def _send_body(self, up: Upstream, hconn: "h11.Connection", data: bytes, rec: dict) -> None:
        """`data` through `up.write` in `UPLOAD_CHUNK` pieces, each drained before the next is written (so a cancel
        from an early response head stops the upload between chunks), framed as the head declared (`content-length`
        = its length), then h11's end of message; an upstream failure raises UpstreamFailed. `rec["bytes_up"]` counts
        the body bytes handed to the transport: a chunk is counted as its send starts (`upstream_send` writes, then
        drains, and a drain that never returns still left the chunk queued), so a write that fails over-counts by at
        most one chunk."""
        for i in range(0, len(data), UPLOAD_CHUNK):
            piece = data[i:i + UPLOAD_CHUNK]
            rec["bytes_up"] += len(piece)
            await upstream_send(up, h11_bytes(hconn, h11.Data(data=piece)))
        tail = h11_bytes(hconn, h11.EndOfMessage())   # nothing for a content-length body; kept for the framing's sake
        if tail:
            await upstream_send(up, tail)

    async def _token_state(self, conn: Conn, token: str) -> Optional[dict]:
        """`{"answer": {"session", "claims"}}`, `{"refused": name}`, or None for a stale exchange (no answer). A
        cached digest and one in flight take no permit; a dropped one is `token-revoked` without asking; otherwise
        the first `Q` (the owner) starts the table-owned resolve and every `Q` awaits it through a shield, within
        RESOLVE_S of its own arrival. The answer must bind to the asking registration (`front_binding`) before
        anything else; only a bound answer enters the cache, under state_lock, with the asking registration still
        current and the digest not dropped, so no answer of another session, and none that a drop overtook, is ever
        cached."""
        if TOKEN_RE.match(token) is None:
            return {"refused": "token-invalid"}
        digest = norm.digest_of_token(token)
        reg = conn.reg
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESOLVE_S
        assert self.state_lock is not None and conn.admin_epoch is not None
        async with self.state_lock:
            if not self._current(conn):
                return None
            hit = self.cache.get(digest)
            if hit is None and digest in self.dropped:
                return {"refused": "token-revoked"}
            if hit is None:
                key = (conn.admin_epoch, reg, digest)
                task = self.resolving.get(key)
                if task is None:
                    task = asyncio.ensure_future(self._resolve(key, reg, deadline))
                    self.resolving[key] = task
                    task.add_done_callback(_resolve_done)
        if hit is None:
            try:
                res = await asyncio.wait_for(asyncio.shield(task), max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                return {"refused": "keys-unavailable"}
            if res[0] == "failed":
                return {"refused": "keys-unavailable"}
            if res[0] == "refused":
                return {"refused": RESOLVE_REFUSALS[res[1]]}
            if not front_binding(res[1], reg):
                return {"refused": "front-mismatch"}
            async with self.state_lock:      # the claims enter the cache only as a fenced mutation
                if not self._current(conn):
                    return None
                if digest in self.dropped:
                    return {"refused": "token-revoked"}
                self.cache[digest] = res[1]
            return {"answer": res[1]}
        if not front_binding(hit, reg):
            return {"refused": "front-mismatch"}
        return {"answer": hit}

    async def _resolve(self, key: Tuple[int, Registration, str], reg: Registration, deadline: float) -> tuple:
        """The table-owned resolve task: its TYPED outcome is `("answer", {"session", "claims"})`,
        `("refused", "token-unknown" | "token-revoked" | "run-unknown")` or `("failed",)` (any Exception, the deadline
        included); it never raises one to its awaiters. It holds the permits itself, per-session FIRST, global second, each
        released on every exit; its `finally` pops its table entry."""
        try:
            if reg.resolve_sem is None:
                reg.resolve_sem = asyncio.Semaphore(RESOLVE_PER_SESSION)
            assert self.resolve_sem is not None
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(self._resolve_permitted(reg.resolve_sem, key[2]),
                                          max(0.0, deadline - loop.time()))
        except Exception:
            return ("failed",)
        finally:
            if self.resolving.get(key) is asyncio.current_task():
                del self.resolving[key]

    async def _resolve_permitted(self, mine: asyncio.Semaphore, digest: str) -> tuple:
        assert self.resolve_sem is not None
        async with mine:                     # the per-session permit FIRST: no global permit is held while waiting
            async with self.resolve_sem:
                return await self._token_resolve(digest)

    async def _token_resolve(self, digest: str) -> tuple:
        """`TOKEN-RESOLVE {token_digest}` on its own connection to CONTROL's socket: one request, one answer."""
        if self.control_path is None:
            return ("failed",)
        reader, writer = await asyncio.open_unix_connection(self.control_path)
        try:
            writer.write(encode_admin({"v": 1, "op": "TOKEN-RESOLVE", "id": 1, "token_digest": digest}))
            await writer.drain()
            ans = await read_control_answer(reader)
        finally:
            writer.transport.abort()
        if ans.get("id") != 1:
            return ("failed",)
        return resolve_outcome(ans)

    async def _refuse(self, conn: Conn, frames: Union[FrameReader, "FrontFrames"], side: KeysSide, name: str,
                      cap: int) -> None:
        """The `F` path: `F`, SHUT_WR, then read and discard the front's frames until `E`, `X`, EOF, `cap` body bytes
        read after the `F`, or DRAIN_S (no drain once the front's `E` was read); the caller closes. So `F` is never
        cut off by a close with unread bytes. A detached exchange is answered nothing (phase 2 closes its
        connection)."""
        if conn.detached:
            return
        if conn.rec is not None and conn.rec["refused"] is None:
            try:
                status: Optional[int] = norm.status_of(name)   # reads a parameterised name (`body-unbounded:<what>`)
            except KeyError:
                status = None
            conn.rec["refused"], conn.rec["status"] = name, status
        w = conn.writer
        w.write(gwwire.encode(b"F", {"refused": name}))
        if w.can_write_eof():
            w.write_eof()
        if b"E" not in side.allowed():
            return   # the front's request already ended (§1.7 reads the body before any upstream step): nothing to drain
        loop = asyncio.get_running_loop()
        deadline, got = loop.time() + DRAIN_S, 0
        while got < cap:
            left = deadline - loop.time()
            if left <= 0:
                return
            try:
                fr = await asyncio.wait_for(frames.read(side.allowed()), left)
            except asyncio.TimeoutError:
                return   # DRAIN_S is up: the caller closes (a stalled body, `body-timeout`, ends here)
            if fr is None:
                return
            ftype, payload = fr
            side.got(ftype)
            if ftype in (b"E", b"X"):
                return
            got += len(payload)


def _within_budget(out: dict, lists: Sequence[Tuple[str, str]]) -> dict:
    """`out` with each list `out[key]` of `lists` `(key, count)` cut from its end until the whole answer, framed as the
    largest `id` and `ok` would frame it, is within STATUS_ANSWER_MAX; the lists are given the room in their order,
    and `out[count]` is how many of that list were cut."""
    empty = {k: [] for k, _ in lists}
    counts = {c: len(out[k]) for k, c in lists}
    used = len(canon.canonical_bytes({**out, "id": JSON_INT_MAX, "ok": True, **empty, **counts}))
    res = dict(out)
    for key, count in lists:
        items, kept = out[key], 0
        for it in items:
            if used + len(canon.canonical_bytes(it)) + 1 > STATUS_ANSWER_MAX:   # the item and its comma
                break
            used += len(canon.canonical_bytes(it)) + 1
            kept += 1
        res[key], res[count] = items[:kept], len(items) - kept
        if kept < len(items):
            used = STATUS_ANSWER_MAX   # a list cut: nothing after it is kept, so a later short item never jumps it
    return res


def _openrouter_names(pairs: Set[Tuple[str, str]]) -> List[str]:
    """The credential names among `pairs` whose upstream host is `policy.OPENROUTER_HOST`, sorted."""
    return sorted({c for h, c in pairs if h == codec.OPENROUTER_HOST})


class KeyLimit:
    """One OpenRouter credential's limit as the refresher last read it: the values and the monotonic time of the last
    GOOD read (None: never read well), and the latest attempt's failure token (None: it succeeded, or none ran)."""

    __slots__ = ("good_at", "values", "failed")

    def __init__(self) -> None:
        self.good_at: Optional[float] = None
        self.values: Tuple[Optional[int], Optional[int], Optional[str], Optional[bool]] = (None, None, None, None)
        self.failed: Optional[str] = None

    def record(self, got: object, now: float) -> None:
        if isinstance(got, str):
            self.failed = got if got in LIMIT_FAILURES else "bad-body"
        else:
            self.values, self.good_at, self.failed = got, now, None   # type: ignore[assignment]

    def entry(self, credential: str, now: float) -> dict:
        """STATUS's entry: `age_s` (whole seconds since the last good read, None if none), the last good read's
        values, and the latest failure."""
        limit, remaining, reset, byok = self.values
        age = None if self.good_at is None else max(0, int(now - self.good_at))
        return {"credential": credential, "age_s": age, "limit_microdollars": limit,
                "remaining_microdollars": remaining, "reset": reset, "byok": byok, "failed": self.failed}


def _microdollars(v: object, *, signed: bool = False) -> Optional[int]:
    """OpenRouter's dollar amount `v` (a JSON number or null) as integer microdollars; `ValueError` when it is not a
    number (a bool is not), not finite, past LIMIT_MICRODOLLARS_MAX in size, or negative unless `signed`."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    try:
        f = float(v)
    except OverflowError:
        raise ValueError("out of range") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("not finite")
    if abs(f) > LIMIT_MICRODOLLARS_MAX / 1_000_000:   # bounded BEFORE the product, which overflows to inf (L3 r1 E1)
        raise ValueError("out of range")
    m = round(f * 1_000_000)
    if not (-LIMIT_MICRODOLLARS_MAX if signed else 0) <= m <= LIMIT_MICRODOLLARS_MAX:
        raise ValueError("out of range")
    return m


def _limit_values(resp: "upstream.RefreshResponse") -> Union[str, Tuple[Optional[int], Optional[int], Optional[str],
                                                                         Optional[bool]]]:
    """`GET /api/v1/key`'s answer as `(limit, remaining, reset, byok)`, or a failure token: a status other than 200
    is its class (`http-4xx`), a 200's body is `key_status_values`'."""
    if resp.status != 200:
        cls = resp.status // 100
        return f"http-{cls}xx" if 2 <= cls <= 5 else "http-other"
    return key_status_values(resp.body)


def key_status_values(body: bytes) -> Union[str, Tuple[Optional[int], Optional[int], Optional[str], Optional[bool]]]:
    """THE validation of an OpenRouter key status body, shared by keys' refresher and doctor's root read (L3 r1 E3):
    `(limit, remaining, reset, byok)` in microdollars, or `bad-body` for a body that is not `{"data": {...}}` with
    `limit` and `limit_remaining` present, or `bad-value` for an amount `_microdollars` refuses (a negative
    `limit_remaining` is a reading: an overdrawn key) or a `limit_reset` / `include_byok_in_limit` of another type.
    The rest of the body is dropped."""
    try:
        data = json.loads(body.decode("utf-8"))["data"]   # Infinity / NaN parse: _microdollars refuses them
        limit, left = data["limit"], data["limit_remaining"]
        reset, byok = data.get("limit_reset"), data.get("include_byok_in_limit")
    except Exception:
        return "bad-body"
    try:
        lim, rem = _microdollars(limit), _microdollars(left, signed=True)
    except ValueError:
        return "bad-value"
    if reset is not None and not (isinstance(reset, str) and LIMIT_RESET_RE.match(reset)):
        return "bad-value"
    if byok is not None and type(byok) is not bool:
        return "bad-value"
    return lim, rem, reset, byok


def _brokered(policies: object) -> Set[Tuple[str, str]]:
    """The `(upstream host, credential name)` of each brokered policy among `policies`."""
    return {(p.upstream.host, p.credential) for p in policies     # type: ignore[attr-defined]
            if p.type == "brokered" and p.upstream is not None and p.upstream.host and p.credential}


def request_head(pol: RoutePolicy, method: bytes, path: bytes, query: Optional[bytes], fwd: Sequence[Tuple[str, str]],
                 body: str, cred: object) -> Tuple[bytes, "h11.Connection"]:
    """The request head keys writes through `Upstream.write`, built from keys' own policy by an h11 client, and that
    client (it frames the body and parses the response): target = `upstream.base_path` + path (+ `?query`), `host`,
    the re-checked headers, `accept-encoding: identity`, the framing given in `body` (keys passes its serialization's
    `length:n`), then the ONE credential header. A head h11 refuses to write is `malformed`."""
    assert pol.upstream is not None
    target = pol.upstream.base_path.encode("ascii") + path + (b"?" + query if query is not None else b"")
    headers = [(b"host", pol.upstream.host.encode("ascii"))]
    headers += [(n.encode("ascii"), v.encode("ascii")) for n, v in fwd]
    headers.append((b"accept-encoding", b"identity"))
    if body.startswith("length:"):
        headers.append((b"content-length", body[len("length:"):].encode("ascii")))
    elif body == "chunked":
        headers.append((b"transfer-encoding", b"chunked"))
    if cred is not None:
        headers.append(cred.header_pair())   # type: ignore[attr-defined]
    conn = h11.Connection(our_role=h11.CLIENT)
    try:
        data = h11_bytes(conn, h11.Request(method=method, target=target, headers=headers))
    except h11.LocalProtocolError:
        raise norm.Refused("malformed", "request head") from None
    return data, conn


async def release_exchange(exchange: Conn) -> None:
    """The exchange's release: a graceful close of an undetached exchange's `Upstream` bounded by GRACE, then an
    abort on every exit (a False abort is noted), then the leave from the exchange set and the drop of the
    `Upstream`, which run even if the abort is cut short. A detached exchange's `Upstream` is never closed
    gracefully (a close would flush what phase 2's abort discards)."""
    up = exchange.upstream
    try:
        if up is not None and not exchange.detached:
            up._graceful_close()
            try:
                await asyncio.wait_for(up.wait_closed(), GRACE)
            except Exception:
                _note(logging.DEBUG, "levain gw-keys: graceful close ended early")
    finally:
        try:
            if up is not None:
                if not up.abort():
                    _note(logging.WARNING, "levain gw-keys: dropped an unclosed transport")
        finally:
            exchange.reg.exchanges.discard(exchange)
            exchange.upstream = None


# --- the process ----------------------------------------------------------------------------------------------------

def _gw_uids() -> frozenset:
    out = set()
    for n in range(STATUS_CONNS):
        try:
            out.add(pwd.getpwnam(GW_ACCOUNT.format(n)).pw_uid)
        except KeyError:
            pass
    return frozenset(out)


def _account_uid(name: str) -> Optional[int]:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _process_uids() -> Tuple[int, ...]:
    """(real, effective, saved) where `getresuid` exists; macOS has none, so (real, effective) there: its saved uid
    is not readable from Python."""
    if hasattr(os, "getresuid"):
        return tuple(os.getresuid())
    return (os.getuid(), os.geteuid())


def single_uid_refusal(keys_uid: Optional[int], ids: Sequence[int]) -> Optional[str]:
    """Why `--test-single-uid` is refused for a process whose uids are `ids` ((real, effective[, saved])), or None.
    Refused when the `_levain_keys` account's uid is ANY of them (a setuid or `sudo -u` launch holds that authority
    whichever slot it sits in), and when the effective uid is 0."""
    if ids[1] == 0:
        return "the effective uid is root"
    if keys_uid is not None and keys_uid in ids:
        return "this process holds a real install's keys account uid"
    return None


async def _serve(keys: Keys, front_path: str, gid: Optional[int], admin_path: str, admin_gid: Optional[int]) -> int:
    # the triggers are armed BEFORE the first listen(): a connect the kernel queues once front.sock listens can be
    # followed at once by a SIGINT, and until the loop's handler is installed that signal is a KeyboardInterrupt. A
    # trigger that fires before or during start is the "nothing is served" path below.
    keys.arm_triggers()
    socks = bind_listeners([(front_path, gid), (admin_path, admin_gid)])
    started = False
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        fixed = count_open_fds()
        need = fixed + bounded()
        print(f"levain gw-keys: start pid={os.getpid()} nofile_soft={soft} nofile_hard={hard} "
              f"fixed_measured={fixed} bounded={bounded()} need={need}", file=sys.stderr, flush=True)
        if soft != resource.RLIM_INFINITY and soft < need:
            print(f"levain gw-keys: refused nofile-low (soft {soft} < fixed_measured {fixed} + bounded {bounded()})",
                  file=sys.stderr, flush=True)
            return 3
        if keys.ledger is not None:
            keys.ledger.attach(asyncio.get_running_loop())   # its writer thread starts here, before anything is served
        started = True   # from here start() owns the sockets: it closes what it does not serve, even on a failure
        await keys.start(socks[:1], admin=socks[1])
        if keys.servers and not keys.closing:   # a trigger before or during start: nothing is served
            print(f"levain gw-keys: ready front={front_path} admin={admin_path}", file=sys.stderr, flush=True)
            keys.start_attach()
            keys.start_limits()
        await keys.wait_done()
        print("levain gw-keys: stopped", file=sys.stderr, flush=True)
        return 0
    finally:
        try:
            if not started:
                for s in socks:
                    s.close()
        finally:
            if keys.ledger is not None:
                # the open refusal windows' lines first (H6-10), queued ahead of the close
                keys.flush_refusals()
                # after wait_done: every live hold resolved by the close column, the writer drained for at most
                # SHUTDOWN_WAIT_S; lines past it are replay's (design 3c-1 §2.7). The one ledger call in a `finally`.
                await keys.ledger.aclose(SHUTDOWN_WAIT_S)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.keys")
    ap.add_argument("--runtime-root", required=True, help="R: keys serves R/keys/front.sock and R/keys/admin.sock")
    ap.add_argument("--lib-root", required=True, help="L: the route policy and the credentials live under it")
    ap.add_argument("--control", required=True, help="CONTROL's socket, which TOKEN-RESOLVE opens")
    ap.add_argument("--test-single-uid", action="store_true",
                    help="TEST ONLY: admit peers of keys' own uid, matched to a registration by (pid, start); "
                         "refused as root or as " + KEYS_ACCOUNT)
    a = ap.parse_args(argv)
    for name, v in (("--runtime-root", a.runtime_root), ("--lib-root", a.lib_root), ("--control", a.control)):
        if not os.path.isabs(v):
            ap.error(f"{name} must be an absolute path")
    if os.geteuid() == 0:
        print("levain gw-keys: refusing to run as root (it runs as _levain_keys)", file=sys.stderr)
        return 2
    single_uid: Optional[int] = None
    gid: Optional[int] = None
    admin_gid: Optional[int] = None
    if a.test_single_uid:
        why = single_uid_refusal(_account_uid(KEYS_ACCOUNT), _process_uids())
        if why is not None:
            print(f"levain gw-keys: --test-single-uid refused: {why}", file=sys.stderr)
            return 2
        # the effective uid: the one the kernel reports to this process's peers, and the one its own fronts carry;
        # the fixture's CONTROL runs as it too
        single_uid, gw_uids = os.geteuid(), frozenset()
        control_uid: Optional[int] = single_uid
    else:
        import grp
        gw_uids = _gw_uids()
        if not gw_uids:
            print("levain gw-keys: no _levain_gw<n> account exists; nothing may connect", file=sys.stderr)
            return 2
        control_uid = _account_uid(CONTROL_ACCOUNT)
        if control_uid is None:
            print(f"levain gw-keys: no {CONTROL_ACCOUNT} account exists; nothing may administer keys", file=sys.stderr)
            return 2
        for group in (FRONTS_GROUP, ADMIN_GROUP):
            try:
                g = grp.getgrnam(group).gr_gid
            except KeyError:
                print(f"levain gw-keys: the {group} group does not exist", file=sys.stderr)
                return 2
            if group == FRONTS_GROUP:
                gid = g
            else:
                admin_gid = g
    # L3 r1 Q1: a routes-trust --replace that did not finish (its intent marker in L/routes) refuses the start before
    # the ledger or a route map is read: keys' head may be half moved
    problem = replace_intent_problem(os.path.join(a.lib_root, "routes"))
    if problem is not None:
        print(f"levain gw-keys: refused: routes-trust-replace-unfinished: {problem}", file=sys.stderr)
        return 3
    raise_nofile()
    # the ONE credential writer (design 1.8): its executor made here at start, its thread at the first commit; it is
    # the credential layer shutdown's latch and step (3) call. Closed after the loop, whatever ended it.
    cred_writer = CredWriter(os.path.join(a.lib_root, "keys", "cred"))
    try:
        # the ledger: locked, replayed and flushed before anything is bound, and its fd a fixed one when _serve counts
        # them; any refusal is a refused start (the gw.jsonl pattern). A ledger that never reaches `_serve`'s attach
        # holds only its fd, which process exit drops.
        try:
            led = ledger.Ledger.open(os.path.join(a.lib_root, *LEDGER))
        except ledger.LedgerOpenError as e:
            print(e.line, file=sys.stderr)
            return 3
        keys = Keys(gw_uids=gw_uids, single_uid=single_uid, control_path=a.control, lib_root=a.lib_root,
                    control_uid=control_uid, ledger=led, cred_lifecycle=cred_writer, attach=True)
        try:
            keys.gw_log = install_log(a.lib_root)   # before _serve counts the fds: the log's fd is a fixed one
        except OSError as e:
            print(f"levain gw-keys: refused: gw.jsonl cannot be opened ({type(e).__name__})", file=sys.stderr)
            return 3
        cred_writer.open_dir()   # before _serve counts the fds: the writer's directory fd is a fixed one when it exists
        keys.open_cred_store()   # and the reader's
        if keys.routes is not None:
            # keys' head and every generation it served, each re-verified, before KEYS-ATTACH states what keys serves
            keys.routes.install(*keys.routes.load())
            gen, _sha = keys.routes.stated()
            print(f"levain gw-keys: route map generation={gen if gen is not None else 'none'}"
                  f"{'' if keys.routes.problem is None else ' refused=' + keys.routes.problem}",
                  file=sys.stderr, flush=True)
        front_path = os.path.join(a.runtime_root, "keys", "front.sock")
        admin_path = os.path.join(a.runtime_root, "keys", "admin.sock")
        try:
            return asyncio.run(_serve(keys, front_path, gid, admin_path, admin_gid))
        except FileExistsError as e:
            print(f"levain gw-keys: {e}", file=sys.stderr)
            return 3
    finally:
        cred_writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
