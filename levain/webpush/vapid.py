"""The VAPID key pair and contact, kept in the push store.

Apple's web push service refuses a request whose JWT subject claim is not a URL or a ``mailto:`` address
("The JWT subject claim isn't a URL or mailto" in its list of rejected requests), so a ``sub`` is always sent;
the default is a placeholder that satisfies the format, and ``levain ping keygen --sub`` sets a real contact.
"""
from __future__ import annotations

import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.webpush import ece
from levain.webpush.store import PushStore, now_stamp

DEFAULT_SUB = "mailto:levain@localhost"
_REUSE_FOR = 3600   # a stored token is reused while at least this long remains on it


def _valid_sub(sub: str) -> str:
    if not (sub.startswith("mailto:") and len(sub) > 8) and not sub.startswith("https://"):
        raise ValueError("the contact is a mailto: address or an https URL")
    return sub


def ensure_keys(queue_dir: Path | str, sub: str | None = None, *, rotate: bool = False) -> tuple[ec.EllipticCurvePrivateKey, str, str]:
    """(private key, public key as base64url, sub). Generates the pair on first need. ``sub`` sets the contact
    on the existing pair; ``rotate`` makes a new pair (every subscription made under the old key then fails and
    must be re-made on the phone)."""
    store = PushStore(queue_dir)
    if sub is not None:
        _valid_sub(sub)
    with store.write() as c:
        row = c.execute("SELECT private_der, sub FROM vapid WHERE id = 1").fetchone()
        if row is None or rotate:
            key = ec.generate_private_key(ec.SECP256R1())
            der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            use_sub = sub or (row["sub"] if row is not None else DEFAULT_SUB)
            c.execute("INSERT OR REPLACE INTO vapid (id, private_der, public_b64, sub, created) VALUES (1,?,?,?,?)",
                      (der, ece.b64e(ece.public_bytes(key)), use_sub, now_stamp()))
            c.execute("DELETE FROM vapid_tokens")
            return key, ece.b64e(ece.public_bytes(key)), use_sub
        key = serialization.load_der_private_key(bytes(row["private_der"]), None)
        if sub is not None and sub != row["sub"]:
            c.execute("UPDATE vapid SET sub = ? WHERE id = 1", (sub,))
            c.execute("DELETE FROM vapid_tokens")
        return key, ece.b64e(ece.public_bytes(key)), sub or row["sub"]


def authorization(queue_dir: Path | str, endpoint: str) -> dict[str, str]:
    """The VAPID ``Authorization`` header for ``endpoint``, reusing the stored token for its origin."""
    key, public, sub = ensure_keys(queue_dir)
    store = PushStore(queue_dir)
    aud = "https://" + endpoint.split("/", 3)[2]
    now = int(time.time())
    with store.write() as c:
        row = c.execute("SELECT jwt, exp FROM vapid_tokens WHERE aud = ?", (aud,)).fetchone()
        if row is not None and row["exp"] - now >= _REUSE_FOR:
            return {"Authorization": f"vapid t={row['jwt']}, k={public}"}
        header = ece.vapid_headers(key, endpoint, sub, _now=now)["Authorization"]
        jwt = header.split("t=", 1)[1].split(",", 1)[0]
        c.execute("INSERT OR REPLACE INTO vapid_tokens (aud, jwt, exp) VALUES (?,?,?)",
                  (aud, jwt, now + ece.VAPID_LIFETIME))
        return {"Authorization": header}
