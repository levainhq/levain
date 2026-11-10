"""Build the legacy desk-store fixture with the CURRENT (pre-rename) coordinator code.

Run once, from the repo root, BEFORE the coordinator -> desk rename; the output is committed:

    python scripts/make_desk_legacy_fixture.py

Writes ``tests/fixtures/desk_legacy_store/``: ``store/`` (the store directory: dispatch.db and what the opener makes),
``expected.json`` (the normalised stdout and exit code of four read verbs against a COPY of ``store/``) and
``schema.sql.txt`` (every sqlite_master sql of a FRESH store, sorted). After the rename the same verbs must print the
same text against this old store and a fresh store must have this same schema: stored data keeps the old word forever.

The builder's own clock is fixed, so every timestamp in the store is deterministic; ids come from ``secrets`` and are
fixed once by committing the built store, never rebuilt.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "desk_legacy_store"
T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# the read verbs, as argv after the CLI word; {store} is the store directory. The first word is the role verb
VERBS = {
    "status": ["{verb}", "status", "--queue-dir", "{store}"],
    "lease": ["{verb}", "status", "--json", "--queue-dir", "{store}"],
    "drops": ["drops", "list", "--queue-dir", "{store}"],
    "review": ["{verb}", "review", "list", "--all", "--queue-dir", "{store}"],
}


def schema_text(db: Path) -> str:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    finally:
        conn.close()
    return "\n".join(f"{t} {n}\n{s};" for t, n, s in sorted(rows)) + "\n"


def fresh_schema(verb: str) -> str:
    """The schema a fresh store gets when the CLI opens it (the verb's own opener, which adds the envelope tables)."""
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "fresh"
        store.mkdir(mode=0o700)
        p = subprocess.run([sys.executable, "-m", "levain", verb, "take", "--queue-dir", str(store)],
                           capture_output=True, text=True, cwd=ROOT, timeout=120)
        if p.returncode != 0:
            raise SystemExit(p.stderr)
        return schema_text(store / "dispatch.db")


def build(dest: Path) -> None:
    from levain.coordinator import review
    from levain.coordinator.store import CoordStore

    now = [T0]
    store = CoordStore(dest, clock=lambda: now[0])

    def tick(seconds: int = 60) -> None:
        now[0] = now[0] + timedelta(seconds=seconds)

    # a lease: taken, requested a rotation launch (role='coordinator'), beaten, released (lease_events by='operator'
    # and 'coordinator')
    token = store.coord_take(timedelta(minutes=30))
    tick()
    store.launch_request(token, harness="claude", role="coordinator", cwd="/tmp/legacy-fixture")
    tick()
    store.coord_beat(token, by="coordinator")
    tick()
    store.coord_release(token, by="coordinator")
    tick()
    # a coord_settings row
    store.coord_set_agent("claude", model="sonnet", cwd="/tmp/legacy-fixture")
    tick()
    # a drop (its words are data); with no holder the drop may ask for a coordinator launch
    store.accept_drop("legacy store fixture: one drop written before the rename", surface="cli",
                      outbox_id="ob-legacy-fixture-0001", sent_at=None)
    tick()
    # a review item
    review.file_item(store, "know", {"text": "legacy fixture review item"}, dedupe_key="legacy-fixture-1")


def run_verbs(store_dir: Path, verb: str) -> dict:
    out = {}
    for name, argv in VERBS.items():
        cmd = [sys.executable, "-m", "levain"] + [a.format(verb=verb, store=store_dir) for a in argv]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=120)
        out[name] = {"argv": [a.replace(str(store_dir), "{store}") for a in cmd[3:]],
                     "exit": p.returncode, "stdout": normalise(p.stdout, store_dir)}
    return out


def normalise(text: str, store_dir: Path) -> str:
    """Strip what varies by run: the store path, wall-clock stamps, and relative ages."""
    import re
    text = text.replace(str(store_dir), "{store}")
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<TS>", text)
    # `review list` runs the feeds, which file a fresh receipt item with a random id on every run (see the return note)
    text = re.sub(r"rv-[0-9a-f]{32}", "rv-<ID>", text)
    return text


def verb_word() -> str:
    for verb in ("desk", "coord"):
        p = subprocess.run([sys.executable, "-m", "levain", verb, "status", "--help"], capture_output=True, cwd=ROOT)
        if p.returncode == 0:
            return verb
    raise SystemExit("neither levain desk nor levain coord exists")


def main() -> None:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    FIXTURE.mkdir(parents=True)
    verb = verb_word()
    with tempfile.TemporaryDirectory() as d:
        built = Path(d) / "built"
        built.mkdir(mode=0o700)   # a store directory must be 0700; git keeps no modes, so the test chmods its copy
        build(built)
        shutil.copytree(built, FIXTURE / "store")
        copy = Path(d) / "run"
        shutil.copytree(built, copy)
        copy.chmod(0o700)
        expected = run_verbs(copy, verb)
    (FIXTURE / "expected.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    (FIXTURE / "schema.sql.txt").write_text(fresh_schema(verb))
    print(f"built {FIXTURE} with verb {verb!r}")


if __name__ == "__main__":
    main()
