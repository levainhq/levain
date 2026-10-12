# xcheck: the frozen contract vectors against upstream Go

`main.go` reads every directory under `../frozen/`, runs each vector whose `expect.upstream` names a Go
implementation through that implementation (no levain policy around the call), and fails on:

- an unrecognised vector directory, or a directory below its minimum count (`minCount` in `main.go`);
- any vector whose Go verdict differs from its recorded `expect.upstream.verdict`;
- a recorded upstream version that is not the version this module is built with.

Pinned modules (`go.mod`, `go.sum` committed): `golang.org/x/mod v0.42.0` (`sumdb/note`),
`github.com/transparency-dev/formats v0.1.1`, `github.com/transparency-dev/merkle v0.0.2`. F5's oracle,
`github.com/transparency-dev/witness`'s `NewAddCheckpointHandler`, lives in `witnessoracle/`, its own module,
because the witness module requires newer `formats` and `merkle` versions than the ones pinned here.

Run locally (with a Go toolchain from https://go.dev/dl/ on `PATH`):

    cd tests/golden/contracts/xcheck && go run . -emit /tmp/go_emitted.json ../frozen
    LEVAIN_REQUIRE_XCHECK=1 LEVAIN_XCHECK_EMIT=/tmp/go_emitted.json pytest -q tests/test_contracts_gogen.py

`-emit` writes vectors made by upstream Go (signed notes, a team checkpoint, cosignatures, Merkle proofs) for
`tests/test_contracts_gogen.py` to verify in Python.

CI: the `xcheck` job in `.github/workflows/contracts-xcheck.yml` runs both commands on every push to `main` and
`seat/**`. It does not use actions/setup-go: it downloads the `go1.27.2.linux-amd64` tarball from https://go.dev/dl/,
checks it with `sha256sum --check --strict` against `go-toolchain.sha256` (committed here; the value was fetched once
from https://go.dev/dl/?mode=json on 2026-10-12) before unpacking it, and runs with `GOTOOLCHAIN=local` (T35).
`tests/test_contracts_ci.py` pins the order and that the pin's version is both modules' `go` directive.

`ssh-keygen -lf` (F7), `ssh-keygen -Y check-novalidate` (F12) and `no_upstream` vectors are counted, not run, here:
the Python tests check them.

F10's `receipt_checkpoint` vectors are levain's own rule (`no_upstream`), but every anchor levain accepts has its
F1-F3 layers run through upstream Go (`receiptLayers` in `main.go`): the checkpoint note opens under the vector's
vkey (`sumdb/note`), parses (`formats/log`), and the consistency proof from the same origin's previous accepted
anchor verifies (`merkle/proof`).
