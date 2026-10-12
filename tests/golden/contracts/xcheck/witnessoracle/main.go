// Command witnessoracle drives levain's F5 vectors through github.com/transparency-dev/witness's own
// add-checkpoint handler (httptest), recording what the handler parsed (S1_DESIGN F5, rev 3 U10).
//
// It is a separate module from xcheck because the witness module requires newer formats/merkle versions than
// the ones xcheck pins for F1-F4. xcheck runs it: go run . <path to frozen/witness/vectors.json>
package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime/debug"

	"github.com/transparency-dev/witness/witness"
)

const module = "github.com/transparency-dev/witness"

type vec struct {
	ID    string `json:"id"`
	Kind  string `json:"kind"`
	Input struct {
		OldSize    uint64   `json:"old_size"`
		Proof      []string `json:"proof"`
		Checkpoint string   `json:"checkpoint"`
	} `json:"input"`
	Expect struct {
		Levain struct {
			Verdict string `json:"verdict"`
			Body    string `json:"body"`
		} `json:"levain"`
		Upstream json.RawMessage `json:"upstream"`
	} `json:"expect"`
}

func main() {
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var doc struct {
		Vectors []vec `json:"vectors"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	version := ""
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, d := range bi.Deps {
			if d.Path == module {
				version = d.Version
			}
		}
	}
	bad, checked := 0, 0
	for _, v := range doc.Vectors {
		if string(v.Expect.Upstream) == `"n/a"` {
			continue
		}
		var up struct{ Implementation, Version, Verdict string }
		if err := json.Unmarshal(v.Expect.Upstream, &up); err != nil || up.Version != version {
			fmt.Printf("FAIL %s: vector records %q, oracle built with %s\n", v.ID, up.Version, version)
			bad++
			continue
		}
		var gotOld uint64
		var gotProof [][]byte
		var gotCP []byte
		h := witness.NewAddCheckpointHandler(func(_ context.Context, oldSize uint64, newCP []byte, proof [][]byte) ([]byte, uint64, error) {
			gotOld, gotCP, gotProof = oldSize, newCP, proof
			return []byte("— oracle AAAAAA==\n"), 0, nil
		})
		rec := httptest.NewRecorder()
		h(rec, httptest.NewRequest(http.MethodPost, "/add-checkpoint", bytes.NewReader([]byte(v.Expect.Levain.Body))))
		verdict := "refuse"
		if rec.Code == http.StatusOK {
			verdict = "accept"
			ok := gotOld == v.Input.OldSize && string(gotCP) == v.Input.Checkpoint && len(gotProof) == len(v.Input.Proof)
			for i := 0; ok && i < len(gotProof); i++ {
				ok = hex.EncodeToString(gotProof[i]) == v.Input.Proof[i]
			}
			if !ok {
				fmt.Printf("FAIL %s: the handler parsed old=%d, %d proof lines, checkpoint %q\n", v.ID, gotOld, len(gotProof), gotCP)
				bad++
			}
		}
		if verdict != up.Verdict {
			fmt.Printf("FAIL %s: handler says %s (HTTP %d), vector records %s\n", v.ID, verdict, rec.Code, up.Verdict)
			bad++
		}
		checked++
	}
	fmt.Printf("%-12s %4d vectors: %d checked through %s@%s\n", "witness", len(doc.Vectors), checked, module, version)
	if bad > 0 || checked == 0 {
		os.Exit(1)
	}
}
