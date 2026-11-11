"""levain.intention_legacy: the ONE module that spells the old name, "spore", as code.

Pure data. A store, a signed record and an installed template are append-only history: what an older release wrote under
the old name keeps its spelling, and a reader of history has to recognise it. Nothing here is a shim: no writer, CLI
command, verb id or wire kind accepts an old spelling as a NEW request (an old spelling arriving new is refused with a
pointer to the new one, by the code that refuses it). The reader logic that uses these constants (the policy replay, the
journal recovery and display, the label key, the seed-file retirement) is not in this module.

Every entry says why it exists. An entry the code no longer needs is deleted, not kept "just in case".
"""
from __future__ import annotations

# The per-install store file. anneal creates it (`<db-stem>.spores.json`) and 1.0.7 keeps the name, so levain cannot rename
# it alone: the entity's MCP server would keep writing the old name while levain read another one. It is the one place the
# rename keeps the word on disk, and the codemod routes every plain literal of it (outside the standalone hook templates,
# which cannot import levain) through this constant.
STORE_FILE_NAME = 'memory.spores.json'

# The suffix form (`<db-stem>` + suffix), for the one f-string that derives the store beside `memory.db`.
STORE_FILE_SUFFIX = '.spores.json'

# The document keys of that file, anneal-owned: raw reads of the document (`data.get(...)`) use them.
STORE_DOC_KEY_OPEN = 'spores'
STORE_DOC_KEY_RESOLVED = 'resolved'

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
# prefixes for good (ruling E1); nothing mints this prefix any more.
OLD_ID_PREFIX = 'spore-'

# The keep-migrate plan files an old release wrote under `.levain/migrations/`. They keep their persisted keys so an old
# plan still applies and reverts (ruling H10).
KEEP_PLAN_STORE_KEY = 'spores_json'
KEEP_PLAN_ID_KEY = 'spore_id'
KEEP_PLAN_DEST_KIND = 'spore'
KEEP_EPISODE_META_KEY = 'spore'

# The seed document an old install holds. `levain update` recognises the old copy by its released hashes, which are keyed
# by the old path (ruling H6).
OLD_SEED_DOC = 'seed/spore_instructions.md'

# The READY schema v1 shared with flow: the key of `sources` and the path flow emits are flow's words until flow's own
# producer flips (ruling H11). The levain consumer accepts what flow emits today.
READY_SOURCE_KEY = 'spores'
READY_REF_PREFIX = 'state/spores.json#'
