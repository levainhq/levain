"""Guest side (run as root in the VM clone, after setup): the fake-vendor slot's account record.

The product refuses a gated session whose slot has no ``keep/identity-seed.json`` (``identity-seed-missing:<tag>``), so the
rig's ``seed --mode fake`` must write it, in the product's own shape (``levain.slot.end.identity_seed_bytes``), root-owned
0600 like END's park. The 11-07 driver lane had to run a separate helper by hand because ``seed`` did not."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FAKE_ACCOUNT = {"emailAddress": "fake@example.invalid",
                "organizationUuid": "123e4567-e89b-12d3-a456-426614174000",
                "organizationName": None}
FAKE_SESSION = "s-0123456789abcdef"


def write_identity_seed(store: Path, tag: str, *, vendor: str = "claude", chown_root: bool = True) -> Path:
    """Write ``<store>/slots/<tag>/keep/identity-seed.json`` (O_EXCL, no symlink follow) and return its path. The account
    must match the one the rig's baseline digest is made from (``FAKE_ACCOUNT``)."""
    from levain.slot import end as E
    body = E.identity_seed_bytes(vendor=vendor, session=FAKE_SESSION, account=FAKE_ACCOUNT)
    p = Path(store) / "slots" / tag / "keep" / E.IDENTITY_SEED_NAME
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body)
        os.fchmod(fd, 0o600)
        if chown_root:
            os.fchown(fd, 0, 0)
    finally:
        os.close(fd)
    print("identity seed", p, len(body), "bytes", flush=True, file=sys.stdout)
    return p
