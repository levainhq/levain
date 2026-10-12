"""T35 (S1_DESIGN rev 11): the contracts-xcheck CI job checks the Go toolchain tarball's sha256 against a value
committed in this repo BEFORE unpacking it, and runs that toolchain only. The pin was fetched once from
https://go.dev/dl/?mode=json; the version is the xcheck modules' `go` directive and the vectors' recorded Go.

Text checks over the workflow (no YAML dependency in the dev extra): ordering is by position in the file.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XCHECK = ROOT / "tests" / "golden" / "contracts" / "xcheck"
PIN = XCHECK / "go-toolchain.sha256"
WORKFLOW = (ROOT / ".github" / "workflows" / "contracts-xcheck.yml").read_text(encoding="utf-8")


def _go_directive(mod: Path) -> str:
    return re.search(r"^go (\S+)$", mod.read_text(encoding="utf-8"), re.M).group(1)


def _pin() -> tuple[str, str, str]:
    lines = PIN.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "one tarball, one line, in `sha256sum -c` format"
    m = re.fullmatch(r"([0-9a-f]{64})  (go(\d+\.\d+\.\d+)\.linux-amd64\.tar\.gz)", lines[0])
    assert m, lines[0]
    return m.group(1), m.group(2), m.group(3)


def test_the_pin_names_the_modules_go_version_and_the_vectors_go():
    _, _, version = _pin()
    assert _go_directive(XCHECK / "go.mod") == version
    assert _go_directive(XCHECK / "witnessoracle" / "go.mod") == version
    genlib = (ROOT / "tests" / "golden" / "contracts" / "_genlib.py").read_text(encoding="utf-8")
    assert f'GO = "go{version}"' in genlib


def test_the_job_does_not_let_setup_go_choose_the_toolchain():
    assert "actions/setup-go" not in WORKFLOW
    assert re.search(r"GOTOOLCHAIN: *\"?local\"?", WORKFLOW), "Go must never fetch another toolchain itself"


def test_the_job_verifies_the_pinned_sha256_before_unpacking():
    _, tarball, _ = _pin()
    fetch = WORKFLOW.find("\"https://go.dev/dl/$tarball\"")
    check = WORKFLOW.find('sha256sum --check --strict "$GITHUB_WORKSPACE/tests/golden/contracts/xcheck/go-toolchain'
                          '.sha256"')
    unpack = WORKFLOW.find("tar -C")
    assert -1 not in (fetch, check, unpack), (fetch, check, unpack)
    assert fetch < check < unpack
    # the tarball name is read from the pin file, never typed a second time in the workflow
    assert tarball not in WORKFLOW
    assert "go-toolchain.sha256" in WORKFLOW[fetch - 400:fetch]


def test_the_job_runs_the_unpacked_toolchain_and_says_which():
    assert "/go/bin\" >> \"$GITHUB_PATH\"" in WORKFLOW
    assert WORKFLOW.find("go version") > WORKFLOW.find("tar -C")
