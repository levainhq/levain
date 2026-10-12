"""The shared contracts: the wire and cryptographic substrate every levain log speaks (S1, frozen).

Merkle math, signed notes, checkpoints, cosignatures, witness submissions, origins, key fingerprints and
canonical JSON leaf framing. A frozen byte here changes only under a new version tag, never by an edit;
the golden vectors in ``tests/golden/contracts/frozen/`` pin every one.

Layering: this package imports only the standard library and ``cryptography`` (in ``ed25519.py`` alone).
Nothing here imports ``levain.team`` or any other levain module; ``team`` imports ``contracts``.
"""

CONTRACTS = "levain-contracts/1"


class ContractError(ValueError):
    """An input was refused by a frozen contract rule. The message names the rule."""
