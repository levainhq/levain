"""Generate provisional/receipt_origin/origins.json (P12 fixture; see STATUS). stdlib + cryptography only."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ed_key_blob, pub, seed  # noqa: E402

OUT = Path(__file__).resolve().parent / "origins.json"
SCOPE = "0123456789abcdef0123456789abcdef"   # a fixture team id
devices = {label: seed(f"receipt-log-{label}") for label in ("device-a", "device-b")}
doc = {"status": "provisional (P12)", "production": "levain-receipt/<scope id>/<fp_hex of the device key blob>",
       "origins": {label: f"levain-receipt/{SCOPE}/{hashlib.sha256(ed_key_blob(pub(sd))).hexdigest()}"
                   for label, sd in devices.items()},
       "seed_labels": {label: f"receipt-log-{label}" for label in devices}}
OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
print(OUT)
