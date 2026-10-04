"""team.toml (who is on the project and who owns the canon) and a pack's judgment.toml (path-shaped rules)."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .entry import HANDLE_RE, MODES


class RolesError(ValueError):
    pass


@dataclass
class Team:
    project: str
    owner: str
    members: dict[str, str]                 # handle -> email
    client_owners: list[str] = field(default_factory=list)
    mode: str = "ask-once"
    fetch_interval: int = 300

    def handle_for_email(self, email: str) -> str | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        for handle, mail in self.members.items():
            if mail.strip().lower() == email:
                return handle
        return None

    def owner_ok(self, owner: str) -> bool:
        """Is ``owner`` a name a ruling may carry? ``lead``, a member handle, or ``client:<name>``."""
        if owner == "lead" or owner in self.members:
            return True
        if owner.startswith("client:"):
            name = owner[len("client:"):].strip()
            return bool(name) and (not self.client_owners or name in self.client_owners)
        return False

    def owns(self, handle: str | None, owner: str) -> bool:
        """Does the acting engineer own this call? ``lead`` is the team's canon owner."""
        if not handle:
            return False
        return owner == handle or (owner == "lead" and handle == self.owner)


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def dump_team(t: Team) -> str:
    lines = [f"project = {_q(t.project)}", f"owner = {_q(t.owner)}", f"mode = {_q(t.mode)}",
             f"fetch_interval = {int(t.fetch_interval)}",
             "client_owners = [" + ", ".join(_q(c) for c in t.client_owners) + "]", "", "[members]"]
    lines += [f"{_q(h)} = {_q(m)}" for h, m in t.members.items()]
    return "\n".join(lines) + "\n"


def parse_team(text: str, where: str = "team.toml") -> Team:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RolesError(f"{where} is not valid TOML: {exc}") from None
    try:
        t = Team(project=str(raw["project"]), owner=str(raw["owner"]),
                 members={str(k): str(v) for k, v in dict(raw.get("members", {})).items()},
                 client_owners=raw.get("client_owners", []),
                 mode=str(raw.get("mode", "ask-once")), fetch_interval=int(raw.get("fetch_interval", 300)))
    except (KeyError, TypeError, ValueError) as exc:
        raise RolesError(f"{where} is missing or has a bad field: {exc}") from None
    validate_team(t, where)
    return t


def validate_team(t: Team, where: str = "team.toml") -> None:
    if t.mode not in MODES:
        raise RolesError(f"{where}: mode must be one of {', '.join(MODES)}")
    if t.fetch_interval < 0:
        raise RolesError(f"{where}: fetch_interval must be >= 0 seconds")
    for h, mail in t.members.items():
        if not HANDLE_RE.fullmatch(h):
            raise RolesError(f"{where}: member handle {h!r} must match [A-Za-z0-9][A-Za-z0-9._-]*")
        if "@" not in mail:
            raise RolesError(f"{where}: member {h!r} needs an email (it maps git's user.email to the handle)")
    if not isinstance(t.client_owners, list) or not all(isinstance(c, str) for c in t.client_owners):
        raise RolesError(f"{where}: client_owners must be a list of names")
    folded: dict[str, str] = {}
    for h in t.members:
        if h.lower() in folded:
            raise RolesError(f"{where}: handles {folded[h.lower()]!r} and {h!r} differ only by case "
                             "(their ledger folders would collide on a case-insensitive disk)")
        folded[h.lower()] = h
    seen: dict[str, str] = {}
    for h, mail in t.members.items():
        key = mail.strip().lower()
        if key in seen:
            raise RolesError(f"{where}: members {seen[key]!r} and {h!r} share the email {mail!r}; "
                             "the email is how a git identity maps to a handle, so it must be unique")
        seen[key] = h
    if t.owner not in t.members:
        raise RolesError(f"{where}: owner {t.owner!r} is not in [members]")


def load_team(path: Path) -> Team:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RolesError(f"no team.toml at {path}: run `levain team init` or `levain team join`") from None
    return parse_team(text, str(path))


# ---- a pack's judgment.toml ----------------------------------------------------------------------------------

JUDGMENT_FILE = "judgment.toml"


@dataclass
class Rule:
    id: str
    paths: list[str]
    kind: str
    owner: str
    words: str
    reason: str = ""
    recheck: str = ""
    mode: str = ""

    def fingerprint(self) -> str:
        return json.dumps([self.paths, self.kind, self.owner, self.words, self.reason, self.recheck, self.mode],
                          sort_keys=True)


@dataclass
class Judgment:
    pack: str
    version: str
    rules: list[Rule]

    @property
    def author(self) -> str:
        return f"pack:{self.pack}"


def load_judgment(path: Path) -> Judgment:
    """[pack] name/version + [[rule]] id/paths/kind/owner/words/reason/recheck/mode.

    Judgment that is not path-shaped stays prose in the pack's seed files; a rule here must name paths.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RolesError(f"{path.name} is not valid TOML: {exc}") from None
    meta = raw.get("pack") or {}
    if not isinstance(meta, dict) or not meta.get("name") or not meta.get("version"):
        raise RolesError(f"{path.name} needs [pack] name and version")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(meta["name"])):
        raise RolesError(f"{path.name}: [pack] name must be [A-Za-z0-9._-]+")
    rules, seen = [], set()
    raw_rules = raw.get("rule", [])
    if not isinstance(raw_rules, list):
        raise RolesError(f"{path.name}: rules are [[rule]] tables")
    for i, r in enumerate(raw_rules, 1):
        rid = str(r.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", rid):
            raise RolesError(f"rule {i}: id must be [A-Za-z0-9._-]+")
        if rid in seen:
            raise RolesError(f"rule {rid}: duplicate id")
        seen.add(rid)
        paths = r.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
            raise RolesError(f"rule {rid}: paths must be a non-empty list of globs (path-free judgment stays prose)")
        kind = r.get("kind", "ruling")
        if kind not in ("ruling", "practice"):
            raise RolesError(f"rule {rid}: kind must be ruling or practice")
        if kind == "ruling" and (not str(r.get("owner", "")).strip() or not str(r.get("words", "")).strip()):
            raise RolesError(f"rule {rid}: a ruling needs owner and words")
        mode = str(r.get("mode", ""))
        if mode and (mode not in MODES or kind != "ruling"):
            raise RolesError(f"rule {rid}: mode must be one of {', '.join(MODES)}, on a ruling only")
        rules.append(Rule(id=rid, paths=[str(p) for p in paths], kind=kind, owner=str(r.get("owner", "lead")),
                          words=str(r.get("words", "")), reason=str(r.get("reason", "")),
                          recheck=str(r.get("recheck", "")), mode=mode))
    return Judgment(pack=str(meta["name"]), version=str(meta["version"]), rules=rules)
