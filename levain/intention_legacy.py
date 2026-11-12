"""levain.intention_legacy: the ONE module that spells the old name, "spore", as code.

Pure data, and the pure readers of the journal's recorded history (the block at the end). A store, a signed record and an
installed template are append-only history: what an older release wrote under the old name keeps its spelling, and a reader
of history has to recognise it. Nothing here is a shim: no writer, CLI command, verb id or wire kind accepts an old spelling
as a NEW request (an old spelling arriving new is refused with a pointer to the new one, by the code that refuses it). The
reader logic that uses these constants beyond the journal's field readers below (the policy replay, the journal recovery and
display, the label key, the seed-file retirement) is not in this module.

The journal's history readers live here, not in the operator-side ``levain.cockpit.effects``, because the fire journal is
ROOT-RUN code: it may import only what the root closure (``tests/ci/root_closure.py``) already holds. This module imports
nothing from ``levain.cockpit`` and nothing outside the standard library, so it stays pure.

Every entry says why it exists. An entry the code no longer needs is deleted, not kept "just in case".
"""
from __future__ import annotations

from typing import Any, Mapping

# The per-install store file. anneal creates it (`<db-stem>.spores.json`) and 1.0.7 keeps the name, so levain cannot rename
# it alone: the entity's MCP server would keep writing the old name while levain read another one. It is the one place the
# rename keeps the word on disk, and the codemod routes every plain literal of it (outside the standalone hook templates,
# which cannot import levain) through this constant.
STORE_FILE_NAME = 'memory.spores.json'

# The document key of the open records in that file, anneal-owned: a raw read of the document (`data.get(...)`) uses it.
STORE_DOC_KEY_OPEN = 'spores'

# The old verb ids, to their new ids. A signed authority-log `raise` link names a verb by its id, and the policy replay maps
# a recorded old id to the new one so a floor an operator signed keeps binding (ruling H1). `descend` becomes `close` and
# `ascend` becomes `promote` (the verb map; the anneal 1.0.7 tools carry the same names). Total over the old cockpit
# registry: the eight verbs below are every VerbSpec the old release served.
OLD_VERB_TO_NEW = {
    'spore_touch': 'intention_touch',
    'spore_descend': 'intention_close',
    'spore_surface_at': 'intention_surface_at',
    'spore_update': 'intention_update',
    'spore_retarget': 'intention_retarget',
    'spore_set_disposition': 'intention_set_disposition',
    'spore_ascend': 'intention_promote',
    'spore_seed': 'intention_seed',
}

# The old audit-ledger kinds (`edits.jsonl`) and effect kinds the old release recorded: the eight verbs plus the two undo
# kinds. The ledger is append-only history that the Edits panel still renders and that undo still refuses (ruling A6).
OLD_KIND_TO_NEW = {
    **OLD_VERB_TO_NEW,
    'spore_undo_edit': 'intention_undo_edit',
    'spore_undo_seed': 'intention_undo_seed',
}

# The old row / resource-key prefix and the old journal effect store, as signed and journalled by the old release. A
# DECIDED or COMMITTED_INCOMPLETE operation across the update must still complete, and terminal rows must still render
# (ruling H2).
OLD_ROW_PREFIX = 'spore:'
OLD_EFFECT_STORE = 'spores'

# The same row prefix without its colon (the kind of a `<kind>:<id>` resource key a journal row carries), and the key an
# old `/edit` body named its row by. The journal is history and the policy replay reads the first; the second is an old
# spelling arriving as a NEW request, which is refused with a pointer.
OLD_ROW_KIND = 'spore'
OLD_ROW_ID_KEY = 'spore_id'

# The old CLI command word. `levain spores ...` is refused with a pointer to `levain intentions ...` (ruling H10).
OLD_CLI_COMMAND = 'spores'

# The old id prefix: `spore-NNN` ids live in every store and, cited, in immutable history. The id resolver reads both
# prefixes for good (ruling E1): `levain.consent.questions.INTENTION_ID` builds its old arm from this. Nothing mints it any more.
OLD_ID_PREFIX = 'spore-'

# The keep-migrate plan files an old release wrote under `.levain/migrations/`. They keep their persisted keys so an old
# plan still applies and reverts (ruling H10).
KEEP_PLAN_STORE_KEY = 'spores_json'
KEEP_PLAN_ID_KEY = 'spore_id'
KEEP_PLAN_DEST_KIND = 'spore'
KEEP_EPISODE_META_KEY = 'spore'

# The stored disposition an old release wrote, to the word that replaced it (ruling D2/H8). A signed op or a keep-migrate
# plan an old release made can name `seed`; the code that replays or applies it reads it as `someday`. A reader of history
# only: a NEW write naming `seed` is refused (`levain.intentions.RETIRED_SEED_MSG`).
OLD_DISPOSITION_TO_NEW = {'seed': 'someday'}

# The seed document an old install holds. `levain update` recognises the old copy by its released hashes, which are keyed
# by the old path (ruling H6).
OLD_SEED_DOC = 'seed/spore_instructions.md'

# The READY schema v1 shared with flow: the key under which the input of `levain.desk.ready.derive` carries the intention
# rows is flow's own word (flow's `ready.collect` and the shared conformance fixture name it), so it is read through this
# constant. The provenance `source` and `ref` the items carry are the new words (`intentions`, `state/intentions.json#`):
# flow flips its producer in the same landing step, and nothing reads the old pair.
READY_SOURCE_KEY = 'spores'

# The current effect store name and resource-key prefix of an intention. The readers below map a recorded old spelling to
# these; ``levain.cockpit.effects`` re-binds both by import, so there is one definition.
INTENTIONS = "intentions"
INTENTION_KEY_PREFIX = "intention:"


# --- readers of history ----------------------------------------------------------------------------
# A signed record, an effects row and a label row are append-only: what the release that said "spore" wrote keeps its
# spelling, and the journal holds ops across the update. These readers are the one place a recorded old spelling becomes
# the new one, on READ. None accepts an old spelling as a NEW request (the builders, ``_origin_key_of`` and the verb
# registry are new-only), and none writes: every new row carries the new spelling.

def read_recorded_store(store: str) -> str:
    """A recorded effect store name, as the broker now registers it."""
    return INTENTIONS if store == OLD_EFFECT_STORE else store


def read_recorded_key(resource_key: str) -> str:
    """A recorded resource key, with the intention prefix in place of the old one; any other key is returned as it is."""
    if resource_key.startswith(OLD_ROW_PREFIX):
        return INTENTION_KEY_PREFIX + resource_key[len(OLD_ROW_PREFIX):]
    return resource_key


def recorded_key_forms(resource_key: str) -> tuple[str, ...]:
    """Every spelling a journal row for this resource key may carry on disk, the current one first: an intention key was
    written under the old prefix by the release that said "spore" and under the new one since. Any other key has one."""
    key = read_recorded_key(resource_key)
    if key.startswith(INTENTION_KEY_PREFIX):
        return key, OLD_ROW_PREFIX + key[len(INTENTION_KEY_PREFIX):]
    return (key,)


def read_recorded_verb(verb: Any) -> Any:
    """A recorded verb id (a signed record's, a pending's), under its new name; anything else is returned as it is."""
    return OLD_KIND_TO_NEW.get(verb, verb) if isinstance(verb, str) else verb


def read_recorded_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded ``changes`` or ``projection`` of an intention effect, as a copy with the disposition an old release signed
    (``seed``) read as the word that replaced it (``someday``), so an op signed before the rename completes instead of
    ending refused at the write guard (ruling D2). The signed record keeps its bytes; the label a fire writes is computed
    over this read value (``someday``), so the row renders under its signer's label, as the same disposition under its new
    name. A disposition rewritten by ``levain update`` has no fire behind it and reads unverified."""
    out = dict(fields)
    disposition = out.get("disposition")
    if isinstance(disposition, str):
        out["disposition"] = OLD_DISPOSITION_TO_NEW.get(disposition, disposition)
    return out


def read_recorded_target(target: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded target with its ``row_id`` in the new prefix, as a copy: the signed record is never edited."""
    out = dict(target)
    row_id = out.get("row_id")
    if isinstance(row_id, str):
        out["row_id"] = read_recorded_key(row_id)
    return out
