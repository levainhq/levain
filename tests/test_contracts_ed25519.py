"""F2's Ed25519 profile: ``contracts/ed25519.py`` reproduces the pinned Go/OpenSSL row of the speccheck table.

If a ``cryptography`` wheel changes any of the 12 verdicts, this file goes red. That is the detector for the
stated known edge (a ``cryptography`` built against another backend).
"""
import ast
import hashlib
from pathlib import Path

import pytest

from levain.contracts import ed25519 as E, keys as K
from tests._contracts_vectors import FROZEN, check_shape, h, ids, load

VECTORS = load("ed25519")
CASES_SHA256 = "08e47a36d9aead288664930505584f353fff113ab854f2800db1e4f5b3540450"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i = v["input"]
    got = E.verify(h(i["pub_key"]), h(i["message"]), h(i["signature"]))
    assert got == (v["expect"]["levain"]["verdict"] == "accept")


def test_the_row_is_the_pinned_row():
    row = tuple(v["expect"]["levain"]["verdict"] == "accept" for v in VECTORS)
    assert row == E.PROFILE_ROW
    assert " ".join("V" if x else "X" for x in row) == "V V V V X X X X X X X V"


def test_cases_json_is_the_measured_file():
    assert hashlib.sha256((FROZEN / "ed25519" / "cases.json").read_bytes()).hexdigest() == CASES_SHA256


def test_cryptography_version_is_in_the_measured_range():
    import cryptography
    major = int(cryptography.__version__.split(".")[0])
    assert major >= 48, cryptography.__version__


def test_cryptography_is_imported_only_by_ed25519_py():
    """§4: ``contracts`` imports the stdlib and ``cryptography``, the latter in ``ed25519.py`` alone."""
    pkg = ROOT / "levain" / "contracts"
    for py in pkg.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level == 0 and (node.module or "").startswith("levain") and not (
                        node.module or "").startswith("levain.contracts"):
                    pytest.fail(f"{py.name} imports {node.module}")
            for n in names:
                if n.split(".")[0] == "cryptography":
                    assert py.name == "ed25519.py", f"{py.name} imports cryptography"
                if n.split(".")[0] == "levain" and not n.startswith("levain.contracts"):
                    pytest.fail(f"{py.name} imports {n}")


def test_keys_lengths_equal_ed25519_lengths():
    assert (K.PUBLIC_KEY_LEN, K.SIGNATURE_LEN) == (E.PUBLIC_KEY_LEN, E.SIGNATURE_LEN)


def test_team_signing_does_not_load_cryptography():
    """keys.py keeps its own lengths so that team/signing.py, which imports it, stays free of cryptography."""
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", "import sys, levain.team.signing; print('cryptography' in sys.modules)"],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "False"


def test_sign_then_verify():
    seed = bytes(range(32))
    sig = E.sign(seed, b"m")
    assert E.verify(E.public_from_seed(seed), b"m", sig)
    assert not E.verify(E.public_from_seed(seed), b"n", sig)
    assert not E.verify(b"\x00" * 31, b"m", sig) and not E.verify(E.public_from_seed(seed), b"m", sig[:63])
