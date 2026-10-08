"""Tenure: the team as it really was, derived from the ledger branch's own signed history.

Every v2 ledger is STRICT: its genesis commit carries ``tenure.toml``, signed by the owner's key it lists. From there
the team is never read off the tip. It is DERIVED, oldest commit first, along ``walk()``: a team or tenure change
counts only if the commit carrying it is signed by a key in force for the role that may make that change, and names
(``Levain-Base``) the last counted change before it. A ledger line is enforced only if the commit that added it is
signed by a key in force, at that position, for the member it is filed under (the owner, for ``pack-*``).

The design is ``tenure_design_1005.md`` (r22) and ``tenure_signing_regenesis_1007.md`` (flow,
projects/levain/reference). This module holds the derivation; ``transport`` holds the git wire and the writes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import entry as E
from . import roles as R
from . import signing as S
from .transport import _NO_HOOKS, _SCRUB_ENV, TeamError

TENURE_FILE = "tenure.toml"
TEAM_FILE = "team.toml"
RULES = 1                                  # the derivation-rules version a genesis names; a release derives only its own
BASE_TRAILER = "Levain-Base"
PAIR_TRAILER = "Levain-Pair"
_TRAILER_RE = re.compile(r"^(Levain-Base|Levain-Pair): *(\S+) *$", re.M)


class Unjudgeable(TeamError):
    """This clone cannot judge the ledger at all (a signature it cannot check, history it cannot read)."""


# ---- tenure.toml ------------------------------------------------------------------------------------------


@dataclass
class Tenure:
    rules: int = RULES
    past_owner_links: bool = True                         # G (ruled yes 10-05)
    offer: dict | None = None                             # {handle, email, keys: [..]} while a hand-off waits
    vetoes: list[dict] = field(default_factory=list)      # {handle, role, since, links}
    keys: dict[str, list[str]] = field(default_factory=dict)          # handle -> public key lines in force
    pending_keys: dict[str, list[str]] = field(default_factory=dict)  # handle -> proposed, not yet proven
    revokes: list[dict] = field(default_factory=list)     # {key: fingerprint, after: commit}


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def dump_tenure(t: Tenure) -> str:
    """Canonical text: sorted, every string JSON-quoted, so two writers of one state write one byte sequence."""
    out = [f"rules = {int(t.rules)}", f"past_owner_links = {'true' if t.past_owner_links else 'false'}"]
    if t.offer:
        keys = ", ".join(_q(k) for k in t.offer.get("keys", []))
        out.append(f"offer = {{ handle = {_q(t.offer['handle'])}, email = {_q(t.offer.get('email', ''))}, "
                   f"keys = [{keys}] }}")
    for title, table in (("keys", t.keys), ("pending_keys", t.pending_keys)):
        rows = {h: v for h, v in table.items() if v}
        if rows:
            out += ["", f"[{title}]"]
            out += [f"{_q(h)} = [" + ", ".join(_q(k) for k in sorted(v)) + "]" for h, v in sorted(rows.items())]
    for v in sorted(t.vetoes, key=lambda v: (v["handle"], v["role"], v["since"])):
        out += ["", "[[veto]]", f"handle = {_q(v['handle'])}", f"role = {_q(v['role'])}", f"since = {_q(v['since'])}",
                f"links = {'true' if v.get('links', False) else 'false'}"]
    for r in sorted(t.revokes, key=lambda r: (r["key"], r["after"])):
        out += ["", "[[revoke]]", f"key = {_q(r['key'])}", f"after = {_q(r['after'])}"]
    return "\n".join(out) + "\n"


def parse_tenure(text: str, where: str = TENURE_FILE) -> Tenure:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise R.RolesError(f"{where} is not valid TOML: {exc}") from None
    try:
        t = Tenure(rules=int(raw.get("rules", 0)), past_owner_links=bool(raw.get("past_owner_links", True)))
        off = raw.get("offer")
        if off is not None:
            if not isinstance(off, dict) or not isinstance(off.get("handle"), str):
                raise ValueError("offer needs a handle")
            t.offer = {"handle": off["handle"], "email": str(off.get("email", "")),
                       "keys": [str(k) for k in off.get("keys", [])]}
        for title in ("keys", "pending_keys"):
            table = raw.get(title, {})
            if not isinstance(table, dict):
                raise ValueError(f"[{title}] must be a table")
            setattr(t, title, {str(h): [str(k) for k in v] for h, v in table.items() if isinstance(v, list)})
        for v in raw.get("veto", []):
            t.vetoes.append({"handle": str(v["handle"]), "role": str(v["role"]), "since": str(v["since"]),
                             "links": bool(v.get("links", False))})
        for r in raw.get("revoke", []):
            t.revokes.append({"key": str(r["key"]), "after": str(r["after"])})
    except (KeyError, TypeError, ValueError) as exc:
        raise R.RolesError(f"{where} is missing or has a bad field: {exc}") from None
    return t


# ---- the flat field view: what a commit changes is the set of fields whose value differs ---------------------


def _fp_or_none(line: str) -> str | None:
    try:
        return S.fingerprint(line)
    except S.SigningError:
        return None


def flat(team: R.Team | None, ten: Tenure | None) -> dict[tuple, object]:
    """Every counted field as one key. ``tenure_design_1005.md`` §3b's field list, plus the signing doc's."""
    out: dict[tuple, object] = {}
    if team is not None:
        out[("project",)] = team.project
        out[("owner",)] = team.owner
        out[("mode",)] = team.mode
        out[("fetch_interval",)] = int(team.fetch_interval)
        for c in team.client_owners:
            out[("client_owner", c)] = True
        for h, mail in team.members.items():
            out[("member", h)] = mail
    if ten is not None:
        out[("rules",)] = int(ten.rules)
        out[("past_owner_links",)] = bool(ten.past_owner_links)
        if ten.offer:
            out[("offer",)] = json.dumps(ten.offer, sort_keys=True)
        for v in ten.vetoes:
            out[("veto", v["handle"], v["role"], v["since"])] = bool(v.get("links", False))
        for title, kind in (("keys", "key"), ("pending_keys", "pending")):
            for h, lines in getattr(ten, title).items():
                for line in lines:
                    fp = _fp_or_none(line)
                    if fp:
                        out[(kind, h, fp)] = line.strip()
        for r in ten.revokes:
            out[("revoke", r["key"])] = r["after"]
    return out


def unflat(f: dict[tuple, object]) -> tuple[R.Team, Tenure]:
    members = {k[1]: str(v) for k, v in f.items() if k[0] == "member"}
    team = R.Team(project=str(f.get(("project",), "")), owner=str(f.get(("owner",), "")), members=members,
                  client_owners=sorted(k[1] for k in f if k[0] == "client_owner"), mode=str(f.get(("mode",), "ask-once")),
                  fetch_interval=int(f.get(("fetch_interval",), 300)))
    ten = Tenure(rules=int(f.get(("rules",), RULES)), past_owner_links=bool(f.get(("past_owner_links",), True)))
    if ("offer",) in f:
        ten.offer = json.loads(str(f[("offer",)]))
    for k, v in f.items():
        if k[0] == "veto":
            ten.vetoes.append({"handle": k[1], "role": k[2], "since": k[3], "links": bool(v)})
        elif k[0] == "key":
            ten.keys.setdefault(k[1], []).append(str(v))
        elif k[0] == "pending":
            ten.pending_keys.setdefault(k[1], []).append(str(v))
        elif k[0] == "revoke":
            ten.revokes.append({"key": k[1], "after": str(v)})
    return team, ten


def key_fps(ten: Tenure, handle: str) -> set[str]:
    return {fp for fp in (_fp_or_none(k) for k in ten.keys.get(handle, [])) if fp}


# ---- git, read-only, never through replace objects ----------------------------------------------------------


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    env.update(LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def _git(top: Path, args: list[str], *, input_text: str | None = None, timeout: float = 120) -> str:
    try:
        cp = subprocess.run(["git", *_NO_HOOKS, *args], cwd=str(top), env=_env(), capture_output=True, text=True,
                            input=input_text, timeout=timeout,
                            stdin=None if input_text is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unjudgeable(f"git {args[0]} timed out") from None
    except FileNotFoundError:
        raise Unjudgeable("git is not on PATH") from None
    if cp.returncode != 0:
        raise Unjudgeable(f"git {args[0]} failed: {(cp.stderr or cp.stdout).strip()[-200:]}")
    return cp.stdout


def parents_of(top: Path, tip: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in _git(top, ["rev-list", "--parents", tip]).splitlines():
        shas = row.split()
        out[shas[0]] = shas[1:]
    return out


def effective_walk(parents: dict[str, list[str]], tip: str, accepted: dict[str, int]) -> list[str]:
    """``walk(tip)``, oldest first: first parents, except at an accepted merge, the parent its acceptance names."""
    chain, cur = [], tip
    seen: set[str] = set()
    while cur:
        if cur in seen:   # impossible in a DAG; a guard, not a rule
            break
        seen.add(cur)
        chain.append(cur)
        ps = parents.get(cur, [])
        if not ps:
            break
        n = accepted.get(cur, 1) if len(ps) > 1 else 1
        cur = ps[n - 1] if 0 < n <= len(ps) else ps[0]
    chain.reverse()
    return chain


@dataclass
class Meta:
    email: str
    message: str
    date: str

    def trailer(self, name: str) -> str | None:
        found = [v for k, v in _TRAILER_RE.findall(self.message) if k == name]
        return found[-1] if found else None


def metas(top: Path, shas: list[str]) -> dict[str, Meta]:
    if not shas:
        return {}
    text = _git(top, ["-c", "log.mailmap=false", "log", "--no-walk=unsorted", "--stdin", "--format=%x01%H%x00%ae%x00%aI%x00%B"],
                input_text="\n".join(shas) + "\n")
    out = {}
    for rec in text.split("\x01")[1:]:
        sha, mail, date, body = (rec.split("\x00", 3) + ["", "", ""])[:4]
        out[sha.strip()] = Meta(mail.strip(), body, date.strip())
    return out


_HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_PATHS = ["ledger/", TEAM_FILE, TENURE_FILE]


@dataclass
class Change:
    files: set[str] = field(default_factory=set)            # touched paths among ledger/*, team.toml, tenure.toml
    added: list[tuple[str, str]] = field(default_factory=list)   # (rel under ledger/, line)
    removed: list[tuple[str, str]] = field(default_factory=list)


def changes(top: Path, walk: list[str]) -> dict[str, Change]:
    """Every walk commit's diff against its EFFECTIVE parent, in one ``diff-tree --stdin`` call."""
    if not walk:
        return {}
    pairs = [walk[0]] + [f"{walk[i]} {walk[i - 1]}" for i in range(1, len(walk))]
    text = _git(top, ["-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "diff-tree", "--stdin", "--root",
                      "-r", "-p", "--unified=0", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
                      "--src-prefix=a/", "--dst-prefix=b/", "--", *_PATHS], input_text="\n".join(pairs) + "\n")
    out: dict[str, Change] = {}
    cur: Change | None = None
    src = dst = ""
    lines = text.split("\n")
    i = 0

    def rel(path: str, prefix: str) -> str:
        p = path[len(prefix):] if path.startswith(prefix) else ""
        return p

    while i < len(lines):
        line = lines[i]
        i += 1
        if _HEX.match(line):
            cur = out.setdefault(line, Change())
            continue
        if cur is None:
            continue
        if line.startswith("--- "):
            src = rel(line[4:], "a/")
            if src:
                cur.files.add(src)
        elif line.startswith("+++ "):
            dst = rel(line[4:], "b/")
            if dst:
                cur.files.add(dst)
        elif line.startswith("@@ "):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            n_old = int(m.group(1)) if m and m.group(1) is not None else 1
            n_new = int(m.group(2)) if m and m.group(2) is not None else 1
            body = []
            while i < len(lines) and len(body) < n_old + n_new:
                if not lines[i].startswith("\\ "):
                    body.append(lines[i])
                i += 1
            for h in body:
                if h.startswith("-") and src.startswith("ledger/") and src.endswith(".jsonl"):
                    cur.removed.append((src[len("ledger/"):], h[1:]))
                elif h.startswith("+") and dst.startswith("ledger/") and dst.endswith(".jsonl"):
                    cur.added.append((dst[len("ledger/"):], h[1:]))
    return out


def file_texts(top: Path, specs: list[str]) -> dict[str, str | None]:
    """``<commit>:<path>`` -> text (None when missing), through one ``cat-file --batch``."""
    if not specs:
        return {}
    try:
        cp = subprocess.run(["git", *_NO_HOOKS, "cat-file", "--batch"], cwd=str(top), env=_env(), capture_output=True,
                            input="\n".join(specs).encode() + b"\n", timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise Unjudgeable(f"git cat-file failed: {exc}") from None
    data, out, pos = cp.stdout, {}, 0
    for spec in specs:
        nl = data.index(b"\n", pos)
        head = data[pos:nl].decode()
        pos = nl + 1
        if head.endswith(" missing") or head.endswith(" ambiguous"):
            out[spec] = None
            continue
        size = int(head.split()[2])
        out[spec] = data[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return out


# ---- derivation ----------------------------------------------------------------------------------------------


@dataclass
class Spell:
    handle: str
    role: str          # "owner" | "member"
    start: int         # walk position of the counted commit that gave the role
    start_sha: str
    end: int | None = None


@dataclass
class Derivation:
    tip: str
    walk: list[str]
    team: R.Team
    tenure: Tenure
    counted_head: str                               # the last counted team/tenure commit (the next Levain-Base)
    files: dict[str, list[str]]                     # ledger/<rel> -> every line added, in walk order (chain set)
    unenforced: dict[str, set[str]]                 # rel -> hashes of chain lines that are not enforced
    line_pos: dict[str, int]                        # entry hash -> walk position of the commit that added it
    spells: list[Spell]
    problems: list[str]
    judged: str = "full"                            # "full" | "partial"
    frozen_at: str | None = None                    # the anchor the verdicts are frozen at, when partial
    frozen_why: str = ""
    waiting: int = 0                                # lines after the frozen point (listed, not enforced)
    role_changes: list[tuple[str, str, str]] = field(default_factory=list)   # (sha, date, text)
    void: set[str] = field(default_factory=set)
    touched: dict[tuple, str] = field(default_factory=dict)   # field -> the last counted commit that changed it
    state: dict[tuple, object] = field(default_factory=dict)  # the counted flat state at the tip

    def owner_authority(self, entry: dict) -> bool:
        """Did ``entry``'s author hold owner authority at its own line (§3c rule 2, G and vetoes applied)?"""
        pos = self.line_pos.get(str(entry.get("hash")))
        author = entry.get("author")
        if pos is None or not author:
            return False
        runs = _owner_runs(self.spells, author)
        for start, end, first_sha in runs:
            if start <= pos and (end is None or pos < end):
                if end is None:
                    return True
                if not self.tenure.past_owner_links:
                    return False
                return not any(v["handle"] == author and v["role"] == "owner" and v["since"] == first_sha
                               and not v.get("links", False) for v in self.tenure.vetoes)
        return False


def _owner_runs(spells: list[Spell], handle: str) -> list[tuple[int, int | None, str]]:
    """Consecutive ownership spells of one handle merged into one run (§3d: a key change never splits authority)."""
    runs: list[list] = []
    for s in sorted((s for s in spells if s.role == "owner" and s.handle == handle), key=lambda s: s.start):
        if runs and runs[-1][1] == s.start:
            runs[-1][1] = s.end
        else:
            runs.append([s.start, s.end, s.start_sha])
    return [(a, b, c) for a, b, c in runs]


@dataclass
class Clone:
    """What this clone trusts: its pin, its anchor, its own operator acts. Never pushed."""
    pinned_root: str
    anchor: str | None = None
    accepted: dict[str, int] = field(default_factory=dict)    # merge sha -> followed parent
    distrust: set[str] = field(default_factory=set)


def derive(top: Path, tip: str, clone: Clone, cache: S.SigCache) -> Derivation:
    """The derivation for ``tip`` as this clone judges it. Raises ``Unjudgeable`` when a signature cannot be checked."""
    parents = parents_of(top, tip)
    walk = effective_walk(parents, tip, clone.accepted)
    if not walk or walk[0] != clone.pinned_root:
        # a rewrite: the tip's chain does not reach the pinned genesis. Judge the anchor's own chain, frozen.
        if clone.anchor and clone.anchor in parents_of_safe(top, clone.anchor):
            d = derive(top, clone.anchor, clone, cache)
            d.tip, d.judged = tip, "partial"
            d.frozen_at, d.frozen_why = clone.anchor, "the ledger history was rewritten (its chain no longer reaches the pinned genesis)"
            return d
        raise Unjudgeable("the ledger's history does not reach this clone's pinned genesis and there is no earlier "
                          "derivation to keep")
    pos = {sha: i for i, sha in enumerate(walk)}
    freeze_end = len(walk)                    # positions >= freeze_end are WAITING
    why = ""
    anchor_pos = pos.get(clone.anchor) if clone.anchor else None
    if clone.anchor and anchor_pos is None:
        freeze_end, why = -1, "the ledger history was rewritten since this clone's last derivation"
    for i, sha in enumerate(walk):
        if len(parents.get(sha, [])) > 1 and sha not in clone.accepted:
            cut = anchor_pos + 1 if anchor_pos is not None and anchor_pos < i else i
            if cut < freeze_end or freeze_end == -1:
                freeze_end, why = cut, f"merge {sha[:10]} on the ledger is not accepted (`levain team accept-merge`)"
            break
    if freeze_end == -1:
        if clone.anchor and clone.anchor in parents_of_safe(top, clone.anchor):
            d = derive(top, clone.anchor, clone, cache)
            d.tip, d.judged, d.frozen_at, d.frozen_why = tip, "partial", clone.anchor, why
            return d
        raise Unjudgeable(why)
    void = set(clone.distrust)
    for _ in range(64):
        d = _derive_once(top, tip, walk, parents, freeze_end, void, cache)
        new = set(clone.distrust) | d.void
        if new == void:
            break
        void = new
    if freeze_end < len(walk):
        d.judged, d.frozen_at, d.frozen_why = "partial", walk[freeze_end - 1], why
    return d


def parents_of_safe(top: Path, sha: str) -> dict[str, list[str]]:
    try:
        return parents_of(top, sha)
    except Unjudgeable:
        return {}


def _signers(top: Path, shas: list[str], cache: S.SigCache) -> dict[str, str | None]:
    verdicts = cache.verify(top, shas)
    out: dict[str, str | None] = {}
    for sha in shas:
        v = verdicts[sha]
        if v.kind == "indeterminate":
            raise Unjudgeable(f"cannot verify the signature of {sha[:10]}: {v.reason}")
        out[sha] = v.fingerprint if v.kind == "signed" else None
    return out


def _derive_once(top: Path, tip: str, walk: list[str], parents: dict[str, list[str]], freeze_end: int,
                 void: set[str], cache: S.SigCache) -> Derivation:
    live = walk[:freeze_end]
    where = {s: i for i, s in enumerate(walk)}
    ch = changes(top, walk)
    meta = metas(top, live)
    signer = _signers(top, live, cache)
    problems: list[str] = []
    touching = [sha for sha in live if ch.get(sha) and ch[sha].files & {TEAM_FILE, TENURE_FILE}]
    texts = file_texts(top, [f"{sha}:{name}" for sha in touching for name in (TEAM_FILE, TENURE_FILE)])

    # genesis
    g = walk[0]
    gteam_txt, gten_txt = texts.get(f"{g}:{TEAM_FILE}"), texts.get(f"{g}:{TENURE_FILE}")
    if gten_txt is None or gteam_txt is None:
        raise Unjudgeable("not a strict ledger: its genesis has no tenure.toml (levain 0.6.x ledgers are not read)")
    try:
        team, ten = R.parse_team(gteam_txt, "genesis team.toml"), parse_tenure(gten_txt, "genesis tenure.toml")
    except R.RolesError as exc:
        raise Unjudgeable(f"not a strict ledger: {exc}") from None
    if ten.rules != RULES:
        raise Unjudgeable(f"this ledger uses derivation rules {ten.rules}; this levain derives rules {RULES}")
    if signer.get(g) is None or signer[g] not in key_fps(ten, team.owner):
        raise Unjudgeable("not a strict ledger: its genesis is not signed by its own owner's key")
    state = flat(team, ten)
    spells: list[Spell] = [Spell(team.owner, "owner", 0, g)] + [Spell(h, "member", 0, g) for h in team.members]
    base = g
    file_state = {TEAM_FILE: gteam_txt, TENURE_FILE: gten_txt}
    lines: dict[str, list[str]] = {}
    unenforced: dict[str, set[str]] = {}
    line_pos: dict[str, int] = {}
    role_changes: list[tuple[str, str, str]] = []
    new_void: set[str] = set()
    revoked: dict[str, str] = {}             # fp -> after (counted revocations)
    touched: dict[tuple, str] = {k: g for k in state}

    def keys_of(f: dict, handle: str) -> set[str]:
        return {k[2] for k in f if k[0] == "key" and k[1] == handle}

    def ok(sha: str, f: dict, handle: str) -> bool:
        fp = signer.get(sha)
        return fp is not None and sha not in void and fp in keys_of(f, handle)

    for i, sha in enumerate(walk):
        c = ch.get(sha, Change())
        if i >= freeze_end:
            for rel, text in c.added:
                lines.setdefault(rel, []).append(text)
                h = _hash_of(text)
                if h:
                    unenforced.setdefault(rel, set()).add(h)
            continue
        owner = str(state[("owner",)])
        # 1. lines, judged at the parent's state (a commit's own lines come before its own role changes)
        for rel, text in c.added:
            lines.setdefault(rel, []).append(text)
            h = _hash_of(text)
            if not h:
                continue
            line_pos.setdefault(h, i)
            top_dir = rel.split("/", 1)[0]
            handle = next((k[1] for k in state if k[0] == "member" and E.safe_handle(k[1]) == top_dir), None)
            if i == 0:
                # a genesis's own tree (a re-genesis carries the old history there) is judged by the genesis: the
                # root of trust each joiner confirms. A member's folder and pack folders are enforced.
                good = top_dir.startswith("pack-") or handle is not None
            elif top_dir.startswith("pack-"):
                good = ok(sha, state, owner)
            else:
                good = handle is not None and ok(sha, state, handle)
            if not good:
                unenforced.setdefault(rel, set()).add(h)
        gone = [(r, t) for r, t in c.removed if t.strip() and (r, t) not in c.added]
        if gone:
            problems.append(f"{len(gone)} ledger line(s) removed or rewritten in {sha[:10]}; the ledger is append-only, "
                            "so the original lines still count")
        if i == 0 or not (c.files & {TEAM_FILE, TENURE_FILE}):
            continue
        # 2. a team/tenure change
        if len(parents.get(sha, [])) > 1:
            continue
        m = meta.get(sha) or Meta("", "", "")
        new_team_txt = texts.get(f"{sha}:{TEAM_FILE}")
        new_ten_txt = texts.get(f"{sha}:{TENURE_FILE}")
        try:
            nteam = R.parse_team(new_team_txt or "", "team.toml")
            nten = parse_tenure(new_ten_txt or "", "tenure.toml")
        except R.RolesError as exc:
            problems.append(f"{sha[:10]} by {m.email}: {exc}; the counted team stands")
            file_state = {TEAM_FILE: new_team_txt or "", TENURE_FILE: new_ten_txt or ""}
            continue
        try:
            before = flat(R.parse_team(file_state[TEAM_FILE], "x"), parse_tenure(file_state[TENURE_FILE], "x"))
        except R.RolesError:
            before = dict(state)
        file_state = {TEAM_FILE: new_team_txt or "", TENURE_FILE: new_ten_txt or ""}
        after = flat(nteam, nten)
        if m.trailer(BASE_TRAILER) != base:
            if after != state:
                problems.append(f"{sha[:10]} by {m.email} changes team.toml/tenure.toml without naming the last counted "
                                f"change ({base[:10]}) as its Levain-Base: not counted (write team changes with the "
                                "levain CLI)")
            continue
        fp = signer.get(sha)
        if fp is None:
            problems.append(f"{sha[:10]} by {m.email} is not validly signed: its team changes do not count")
            continue
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        applied, refused = _apply(state, before, after, changed, fp, sha, i, revoked, problems)
        if refused:
            problems.append(f"{sha[:10]} by {m.email}: not in force: {', '.join(refused[:6])}"
                            + (" ..." if len(refused) > 6 else ""))
        # COUNTED (it advances the Levain-Base chain) iff its signer is the owner in force, or at least one of its
        # changes was its signer's to make: a stranger's commit must not stale the owner's next one. A distrusted
        # or revoked commit stays a link (its base is consumed) with its fields void (§3b, signing doc §2).
        if sha in void:
            if fp in keys_of(state, owner) or applied:
                base = sha
            problems.append(f"{sha[:10]} by {m.email} is void (distrusted or signed by a revoked key): "
                            "its changes do not count")
            continue
        if fp in keys_of(state, owner) or applied:
            base = sha
        if not applied:
            continue
        nt, _ = unflat(applied)
        try:
            R.validate_team(nt)
        except R.RolesError as exc:
            problems.append(f"{sha[:10]} by {m.email}: the resulting team would be invalid ({exc}); not counted")
            continue
        # spells and role changes
        prev_owner = str(state[("owner",)])
        prev_members = {k[1] for k in state if k[0] == "member"}
        for k in set(state) | set(applied):
            if state.get(k) != applied.get(k):
                touched[k] = sha
        state = applied
        cur_owner = str(state[("owner",)])
        cur_members = {k[1] for k in state if k[0] == "member"}
        if cur_owner != prev_owner:
            for s in spells:
                if s.role == "owner" and s.end is None:
                    s.end = i
            spells.append(Spell(cur_owner, "owner", i, sha))
            role_changes.append((sha, m.date, f"owner: {prev_owner} -> {cur_owner}"))
        for h in sorted(prev_members - cur_members):
            for s in spells:
                if s.role == "member" and s.handle == h and s.end is None:
                    s.end = i
            role_changes.append((sha, m.date, f"removed: {h}"))
        for h in sorted(cur_members - prev_members):
            spells.append(Spell(h, "member", i, sha))
            role_changes.append((sha, m.date, f"added: {h}"))
        # an offer dies with its offeree's membership
        off = state.get(("offer",))
        if off is not None and json.loads(str(off))["handle"] not in cur_members:
            del state[("offer",)]
        # revocations newly counted void earlier commits signed by that key after `after`
        for fpk, after_sha in revoked.items():
            if after_sha in where:
                for j in range(where[after_sha] + 1, i):
                    if signer.get(walk[j]) == fpk:
                        new_void.add(walk[j])

    t, n = unflat(state)
    return Derivation(tip=tip, walk=walk, team=t, tenure=n, counted_head=base, files=lines, unenforced=unenforced,
                      line_pos=line_pos, spells=spells, problems=problems, role_changes=role_changes,
                      waiting=sum(len(ch.get(s, Change()).added) for s in walk[freeze_end:]), void=new_void,
                      touched=touched, state=dict(state))


def _hash_of(text: str) -> str | None:
    try:
        obj = E.parse_line(text)
    except E.LineError:
        return None
    h = obj.get("hash") if isinstance(obj, dict) else None
    return h if isinstance(h, str) else None


def _apply(state: dict, before: dict, after: dict, changed: set, fp: str, sha: str, pos: int,
           revoked: dict[str, str], problems: list[str]) -> tuple[dict | None, list[str]]:
    """Apply each changed field the signer may change (signing doc §3), field by field. Returns (new state, refused)."""
    new = dict(state)
    refused: list[str] = []
    owner = str(state[("owner",)])
    members = {k[1] for k in state if k[0] == "member"}

    def holds(handle: str) -> bool:
        return fp in {k[2] for k in state if k[0] == "key" and k[1] == handle}

    is_owner = holds(owner)
    offer = json.loads(str(state[("offer",)])) if ("offer",) in state else None
    offeree = offer is not None and fp in {_fp_or_none(k) for k in offer.get("keys", [])} \
        and offer["handle"] in members
    any_applied = False
    for k in sorted(changed, key=lambda k: tuple(map(str, k))):
        old, val = state.get(k), after.get(k)
        if old == val:
            continue      # a no-op against the counted state (a restore, an ignored field)
        kind = k[0]
        allowed = False
        if kind in ("project", "mode", "fetch_interval", "client_owner", "past_owner_links", "veto"):
            allowed = is_owner
        elif kind == "member":
            h = k[1]
            if val is None:
                allowed = is_owner and h != owner
            elif old is None:
                allowed = is_owner
            else:
                allowed = is_owner or holds(h)          # an email is display: the owner, or the member themself
        elif kind == "owner":
            allowed = offeree and val == offer["handle"]
        elif kind == "offer":
            allowed = is_owner or (offeree and val is None)
        elif kind == "pending":
            h = k[1]
            allowed = (is_owner or holds(h)) if val is not None else (is_owner or holds(h) or fp == k[2])
            if val is not None and any(kk[0] in ("key", "pending") and kk[2] == k[2] and kk[1] != h for kk in state):
                allowed = False     # one fingerprint, one handle
        elif kind == "key":
            h = k[1]
            if val is not None:
                # added: a confirm (signed by that very pending key), or an accept setting the offeree's offered keys
                allowed = (fp == k[2] and ("pending", h, k[2]) in state) or (
                    offeree and h == offer["handle"] and k[2] in {_fp_or_none(x) for x in offer.get("keys", [])})
                if any(kk[0] in ("key",) and kk[2] == k[2] and kk[1] != h for kk in state):
                    allowed = False
            else:
                remaining = {kk[2] for kk in new if kk[0] == "key" and kk[1] == h} - {k[2]}
                if h == owner:
                    allowed = is_owner and bool(remaining)
                else:
                    allowed = is_owner or holds(h)
        elif kind == "revoke":
            target_owner = k[1] in {kk[2] for kk in state if kk[0] == "key" and kk[1] == owner}
            allowed = is_owner and val is not None and old is None and not target_owner
            if allowed:
                revoked[k[1]] = str(val)
        elif kind == "rules":
            allowed = False
        if allowed:
            if val is None:
                new.pop(k, None)
            else:
                new[k] = val
            any_applied = True
        else:
            refused.append(_label(k, old, val))
    # a confirm moves the key: drop its pending entry in the same step
    for k in list(new):
        if k[0] == "key" and ("pending", k[1], k[2]) in new and ("key", k[1], k[2]) not in state:
            new.pop(("pending", k[1], k[2]), None)
    # an accept spends the offer
    if offeree and new.get(("owner",)) == offer["handle"] and new.get(("owner",)) != owner:
        new.pop(("offer",), None)
    return (new if any_applied else None), refused


def _label(k: tuple, old: object, val: object) -> str:
    name = ".".join(str(x)[:16] for x in k)
    return f"{name} ({'removed' if val is None else 'added' if old is None else 'changed'})"
