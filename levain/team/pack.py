"""Seed a pack's ``judgment.toml`` into the ledger, so a pack's path-shaped judgment is enforced like a ruling.

Each rule becomes an entry authored ``pack:<name>`` with ``pack = "<name>@<version>"`` and ``rule_id``.
Re-seeding after a pack upgrade appends only what changed: a changed rule supersedes its older entry, a rule
the pack no longer carries is retired, an unchanged rule writes nothing. Only the team owner seeds (a pack
change is a reviewed change to the team's doctrine).
"""
from __future__ import annotations

from pathlib import Path

from . import entry as E
from . import roles as R
from .transport import GitLedger, TeamError


def _fingerprint_entry(e: dict) -> str:
    return R.Rule(id=e.get("rule_id", ""), paths=list(e.get("paths", [])), kind=e.get("kind", ""),
                  owner=e.get("owner", ""), words=e.get("words", ""), reason=e.get("reason", ""),
                  recheck=e.get("recheck", ""), mode=e.get("mode", "")).fingerprint()


def plan(judgment: R.Judgment, existing: dict[str, dict],
         retired: dict[str, dict] | None = None) -> list[dict]:
    """The entries a seed would append (unsealed), given the in-force entries this pack seeded before.

    ``retired``: rules the team (not the pack) retired or replaced. Re-seeding the SAME rule text does not
    bring one back; only a pack version that changes the rule offers it again.
    """
    author = judgment.author
    tag = f"{judgment.pack}@{judgment.version}"
    retired = retired or {}
    out = []
    for rule in judgment.rules:
        gone = retired.get(rule.id)
        if rule.id not in existing and gone is not None and _fingerprint_entry(gone) == rule.fingerprint():
            continue
        old = existing.get(rule.id)
        if old is not None and _fingerprint_entry(old) == rule.fingerprint():
            continue
        out.append(E.build(author, "constraint", kind=rule.kind, mode=rule.mode or None, paths=rule.paths,
                           owner=rule.owner, words=rule.words or None, reason=rule.reason or None,
                           recheck=rule.recheck or None, supersedes=[old["id"]] if old else None,
                           agent="pack", pack=tag, rule_id=rule.id))
    carried = {r.id for r in judgment.rules}
    for rid, old in sorted(existing.items()):
        if rid not in carried:
            out.append(E.build(author, "retire", supersedes=[old["id"]], agent="pack", pack=tag, rule_id=rid,
                               words=f"pack {tag} no longer carries rule {rid}"))
    return out


def seed(gl: GitLedger, pack_dir: Path, *, push: bool = True) -> list[dict]:
    path = pack_dir / R.JUDGMENT_FILE
    if not path.is_file():
        raise TeamError(f"{pack_dir} has no {R.JUDGMENT_FILE}")
    judgment = R.load_judgment(path)
    team = gl.team()
    handle = gl.handle(team)
    if handle != team.owner:
        raise TeamError(f"only the team owner ({team.owner}) seeds pack rules; you are {handle or 'not a member'}")
    bad = [r.id for r in judgment.rules if not team.owner_ok(r.owner)]
    if bad:
        raise TeamError(f"rule owner(s) not allowed by team.toml (client_owners / members): {', '.join(bad)}")
    # One pack-sync at a time: two that plan from the same ledger would both supersede the same old rule.
    with gl.lock(name="pack", timeout=60):
        led = gl.ledger(team)
        todo = plan(judgment, led.pack_rules(judgment.pack), led.retired_by_others(judgment.pack))
        written = []
        for i, e in enumerate(todo):
            written.append(gl.append(e, push=push and i == len(todo) - 1))
    return written
