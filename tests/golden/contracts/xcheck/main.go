// Command xcheck runs levain's frozen contract vectors (tests/golden/contracts/frozen/) through the pinned
// upstream Go implementations and asserts each vector's expect.upstream, with no levain policy wrapped around
// the upstream call (S1_DESIGN §1.0). It also emits Go-generated vectors for Python to verify (-emit).
//
// Usage: go run . [-emit FILE] ../frozen
//
// It fails on an unrecognised vector directory, on a directory below its minimum vector count, on any vector
// whose upstream verdict differs from the recorded one, and on a recorded upstream version that is not the
// version this module is built against.
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"time"

	fmtlog "github.com/transparency-dev/formats/log"
	fnote "github.com/transparency-dev/formats/note"
	"github.com/transparency-dev/merkle/proof"
	"github.com/transparency-dev/merkle/rfc6962"
	"github.com/transparency-dev/merkle/testonly"
	"golang.org/x/mod/sumdb/note"
)

// minCount is the minimum number of vectors each frozen directory must hold. A directory missing from this
// table is unrecognised and fails the run; S1-b adds its own directories here when it lands them.
var minCount = map[string]int{
	"grammar":            30,
	"merkle":             100,
	"ed25519":            12,
	"note":               15,
	"checkpoint":         20,
	"cosig":              10,
	"witness":            5,
	"origin":             20,
	"fingerprint":        7,
	"canon":              40,
	"sshsig":             13,
	"entry":              80,
	"line":               40,
	"scope":              45,
	"receipt_checkpoint": 25,
}

type vector struct {
	ID     string                     `json:"id"`
	Kind   string                     `json:"kind"`
	Op     string                     `json:"op"`
	Input  map[string]json.RawMessage `json:"input"`
	Expect struct {
		Levain   map[string]json.RawMessage `json:"levain"`
		Upstream json.RawMessage            `json:"upstream"`
	} `json:"expect"`
}

type upstream struct {
	Implementation string `json:"implementation"`
	Version        string `json:"version"`
	Verdict        string `json:"verdict"`
}

type tally struct{ checked, noUpstream, notGo int }

var failures []string

func fail(format string, a ...any) { failures = append(failures, fmt.Sprintf(format, a...)) }

func str(v *vector, k string) string {
	var s string
	must(json.Unmarshal(v.Input[k], &s), v.ID+"."+k)
	return s
}

func u64(v *vector, k string) uint64 {
	n, err := strconv.ParseUint(string(v.Input[k]), 10, 64)
	must(err, v.ID+"."+k)
	return n
}

func hexs(v *vector, k string) [][]byte {
	var ss []string
	must(json.Unmarshal(v.Input[k], &ss), v.ID+"."+k)
	out := make([][]byte, len(ss))
	for i, s := range ss {
		b, err := hex.DecodeString(s)
		must(err, v.ID+"."+k)
		out[i] = b
	}
	return out
}

func hx(v *vector, k string) []byte {
	b, err := hex.DecodeString(str(v, k))
	must(err, v.ID+"."+k)
	return b
}

func levStr(v *vector, k string) string {
	var s string
	must(json.Unmarshal(v.Expect.Levain[k], &s), v.ID+".expect."+k)
	return s
}

func must(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "xcheck: %s: %v\n", what, err)
		os.Exit(2)
	}
}

func verdict(ok bool) string {
	if ok {
		return "accept"
	}
	return "refuse"
}

func ruleLeaves(n uint64) [][]byte {
	out := make([][]byte, n)
	for i := uint64(0); i < n; i++ {
		out[i] = bytes.Repeat([]byte{byte(i % 256)}, int(i+1))
	}
	return out
}

func treeRoot(leaves [][]byte) []byte {
	t := testonly.New(rfc6962.DefaultHasher)
	t.AppendData(leaves...)
	return t.Hash()
}

func noteBytes(v *vector) []byte {
	if _, ok := v.Input["note_hex"]; ok {
		return hx(v, "note_hex")
	}
	return []byte(str(v, "note"))
}

// run returns the upstream verdict for one vector, checking outputs where the vector's levain side is an accept.
func run(dir string, v *vector, up upstream) string {
	hasher := rfc6962.DefaultHasher
	switch v.Op {
	case "decimal":
		_, err := strconv.ParseUint(str(v, "text"), 10, 64)
		return verdict(err == nil)
	case "b64":
		b, err := base64.StdEncoding.DecodeString(str(v, "text"))
		if err == nil && up.Verdict == "accept" && v.Kind == "spec" && hex.EncodeToString(b) != levStr(v, "bytes") {
			fail("%s/%s: decoded bytes differ", dir, v.ID)
		}
		return verdict(err == nil)
	case "verify":
		return verdict(ed25519.Verify(ed25519.PublicKey(hx(v, "pub_key")), hx(v, "message"), hx(v, "signature")))
	case "root", "root_explicit":
		var leaves [][]byte
		if v.Op == "root" {
			leaves = ruleLeaves(u64(v, "size"))
		} else {
			leaves = hexs(v, "leaves")
		}
		if got := hex.EncodeToString(treeRoot(leaves)); got != levStr(v, "root") {
			fail("%s/%s: Go root %s", dir, v.ID, got)
		}
		return "accept"
	case "inclusion":
		err := proof.VerifyInclusion(hasher, u64(v, "index"), u64(v, "size"), hx(v, "leaf_hash"), hexs(v, "proof"), hx(v, "root"))
		return verdict(err == nil)
	case "consistency":
		err := proof.VerifyConsistency(hasher, u64(v, "m"), u64(v, "n"), hexs(v, "proof"), hx(v, "old_root"), hx(v, "new_root"))
		return verdict(err == nil)
	case "check_advance":
		var held, nw fmtlog.Checkpoint
		if _, err := held.Unmarshal([]byte(str(v, "held"))); err != nil {
			return "refuse"
		}
		if _, err := nw.Unmarshal([]byte(str(v, "new"))); err != nil {
			return "refuse"
		}
		return verdict(proof.VerifyConsistency(hasher, held.Size, nw.Size, hexs(v, "proof"), held.Hash, nw.Hash) == nil)
	case "verify_note":
		var vkeys []string
		must(json.Unmarshal(v.Input["known"], &vkeys), v.ID)
		var vs []note.Verifier
		for _, k := range vkeys {
			ver, err := note.NewVerifier(k)
			must(err, v.ID+" vkey")
			vs = append(vs, ver)
		}
		n, err := note.Open(noteBytes(v), note.VerifierList(vs...))
		if err == nil && v.Kind == "spec" {
			var want []string
			must(json.Unmarshal(v.Expect.Levain["verified"], &want), v.ID)
			var got []string
			for _, s := range n.Sigs {
				got = append(got, s.Name)
			}
			if strings.Join(got, ",") != strings.Join(want, ",") {
				fail("%s/%s: Go verified %v, levain %v", dir, v.ID, got, want)
			}
		}
		return verdict(err == nil)
	case "verify_checkpoint":
		ver, err := note.NewVerifier(str(v, "log_vkey"))
		must(err, v.ID+" vkey")
		raw := []byte(str(v, "note"))
		origin, _, _ := strings.Cut(string(raw), "\n") // upstream with no levain policy: the text's own origin
		cp, _, _, err := fmtlog.ParseCheckpoint(raw, origin, ver)
		if err == nil && v.Kind == "spec" {
			var size uint64
			must(json.Unmarshal(v.Expect.Levain["size"], &size), v.ID)
			if cp.Origin != levStr(v, "origin") || cp.Size != size || hex.EncodeToString(cp.Hash) != levStr(v, "root") {
				fail("%s/%s: Go parsed %q %d %x", dir, v.ID, cp.Origin, cp.Size, cp.Hash)
			}
		}
		return verdict(err == nil)
	case "verify_cosignatures":
		var vkeys []string
		must(json.Unmarshal(v.Input["trusted"], &vkeys), v.ID)
		var vs []note.Verifier
		for _, k := range vkeys {
			ver, err := fnote.NewVerifierForCosignatureV1(k)
			must(err, v.ID+" vkey")
			vs = append(vs, ver)
		}
		n, err := note.Open([]byte(str(v, "note")), note.VerifierList(vs...))
		if err == nil && v.Kind == "spec" {
			var want []struct {
				Name      string `json:"cosigner_name"`
				KeyID     string `json:"key_id"`
				Timestamp uint64 `json:"timestamp"`
			}
			must(json.Unmarshal(v.Expect.Levain["records"], &want), v.ID)
			if len(want) != len(n.Sigs) {
				fail("%s/%s: Go verified %d cosignatures, levain %d", dir, v.ID, len(n.Sigs), len(want))
			} else {
				for i, s := range n.Sigs {
					raw, _ := base64.StdEncoding.DecodeString(s.Base64)
					ts := binary.BigEndian.Uint64(raw[4:12])
					if s.Name != want[i].Name || fmt.Sprintf("%08x", s.Hash) != want[i].KeyID || ts != want[i].Timestamp {
						fail("%s/%s: record %d differs: %s %08x %d", dir, v.ID, i, s.Name, s.Hash, ts)
					}
				}
			}
		}
		return verdict(err == nil)
	}
	fail("%s/%s: xcheck has no upstream call for op %q", dir, v.ID, v.Op)
	return ""
}

// pinnedVersions maps each module path to the version this binary was built with.
func pinnedVersions() map[string]string {
	out := map[string]string{"go": runtime.Version()}
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, d := range bi.Deps {
			out[d.Path] = d.Version
		}
	}
	return out
}

func checkVersion(dir string, v *vector, up upstream, pinned map[string]string) {
	impl := up.Implementation
	want := ""
	switch {
	case strings.HasPrefix(impl, "golang.org/x/mod/"):
		want = pinned["golang.org/x/mod"]
	case strings.HasPrefix(impl, "github.com/transparency-dev/formats/"):
		want = pinned["github.com/transparency-dev/formats"]
	case strings.HasPrefix(impl, "github.com/transparency-dev/merkle/"):
		want = pinned["github.com/transparency-dev/merkle"]
	case strings.HasPrefix(impl, "github.com/transparency-dev/witness/"):
		return // the witness oracle is a separate module; it checks its own version
	default:
		want = pinned["go"]
	}
	if want != up.Version {
		fail("%s/%s: vector records %s %s, xcheck is built with %s", dir, v.ID, impl, up.Version, want)
	}
}

func goBinary() string {
	if p, err := exec.LookPath("go"); err == nil {
		return p
	}
	return filepath.Join(runtime.GOROOT(), "bin", "go")
}

func main() {
	emit := flag.String("emit", "", "write Go-generated vectors (JSON) to this file")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: xcheck [-emit FILE] <frozen dir>")
		os.Exit(2)
	}
	root := flag.Arg(0)
	entries, err := os.ReadDir(root)
	must(err, root)
	pinned := pinnedVersions()
	var dirs []string
	for _, e := range entries {
		if e.IsDir() {
			dirs = append(dirs, e.Name())
		}
	}
	sort.Strings(dirs)
	seen := map[string]bool{}
	for _, dir := range dirs {
		min, known := minCount[dir]
		if !known {
			fail("%s: unrecognised vector directory", dir)
			continue
		}
		seen[dir] = true
		raw, err := os.ReadFile(filepath.Join(root, dir, "vectors.json"))
		must(err, dir)
		var doc struct {
			Vectors []vector `json:"vectors"`
		}
		must(json.Unmarshal(raw, &doc), dir)
		if len(doc.Vectors) < min {
			fail("%s: %d vectors, below its minimum %d", dir, len(doc.Vectors), min)
		}
		var t tally
		for i := range doc.Vectors {
			v := &doc.Vectors[i]
			if string(v.Expect.Upstream) == `"n/a"` {
				t.noUpstream++
				continue
			}
			var up upstream
			must(json.Unmarshal(v.Expect.Upstream, &up), dir+"/"+v.ID)
			if strings.HasPrefix(up.Implementation, "ssh-keygen ") || strings.HasPrefix(up.Implementation, "github.com/transparency-dev/witness/") {
				t.notGo++ // ssh-keygen (-lf, -Y check-novalidate) is checked by the Python tests; the witness oracle runs below
				continue
			}
			checkVersion(dir, v, up, pinned)
			got := run(dir, v, up)
			if got != up.Verdict {
				fail("%s/%s (%s): Go says %s, the vector records %s", dir, v.ID, v.Kind, got, up.Verdict)
			}
			t.checked++
		}
		fmt.Printf("%-12s %4d vectors: %d checked against Go, %d no_upstream, %d by another oracle\n",
			dir, len(doc.Vectors), t.checked, t.noUpstream, t.notGo)
	}
	for dir := range minCount {
		if !seen[dir] {
			fail("%s: directory missing", dir)
		}
	}

	// F5's oracle: the witness module's own add-checkpoint handler, in its own module (witnessoracle/), because
	// its go.mod requires newer formats/merkle than the versions pinned above.
	abs, err := filepath.Abs(filepath.Join(root, "witness", "vectors.json"))
	must(err, "witness path")
	cmd := exec.Command(goBinary(), "run", ".", abs)
	cmd.Dir = "witnessoracle"
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		fail("witness oracle: %v", err)
	}

	fmt.Printf("receipt_checkpoint: %d accepted anchors' F1-F3 layers checked against Go\n", receiptLayers(root))

	if *emit != "" {
		must(emitVectors(*emit), "emit")
	}
	if len(failures) > 0 {
		for _, f := range failures {
			fmt.Fprintln(os.Stderr, "FAIL", f)
		}
		os.Exit(1)
	}
	fmt.Println("xcheck: OK")
}

// receiptLayers runs the F1-F3 layers inside every receipt_checkpoint anchor levain ACCEPTS through upstream Go
// (S1_DESIGN §1 "Independent cross-check": F10 is levain's own rule, kind no_upstream, and Go checks the layers
// inside it): the checkpoint note opens under the vector's vkey (sumdb/note), parses as a checkpoint
// (formats/log), and the consistency proof from the same origin's previous accepted anchor verifies (merkle/proof).
// A levain-refused anchor asserts nothing here. Returns the number of anchors checked.
func receiptLayers(root string) int {
	raw, err := os.ReadFile(filepath.Join(root, "receipt_checkpoint", "vectors.json"))
	must(err, "receipt_checkpoint")
	var doc struct {
		Vectors []vector `json:"vectors"`
	}
	must(json.Unmarshal(raw, &doc), "receipt_checkpoint")
	checked := 0
	for i := range doc.Vectors {
		v := &doc.Vectors[i]
		var leaves []struct {
			Data string `json:"data_hex"`
			Vkey string `json:"vkey"`
		}
		must(json.Unmarshal(v.Input["leaves"], &leaves), v.ID)
		var verdicts []string
		must(json.Unmarshal(v.Expect.Levain["verdicts"], &verdicts), v.ID)
		type anchor struct {
			size uint64
			root []byte
		}
		prev := map[string]anchor{}
		for j, l := range leaves {
			if verdicts[j] != "accept" {
				continue
			}
			b, err := hex.DecodeString(l.Data)
			must(err, v.ID)
			var leaf struct {
				Checkpoint  string   `json:"checkpoint"`
				OldSize     uint64   `json:"old_size"`
				Consistency []string `json:"consistency"`
			}
			must(json.Unmarshal(b, &leaf), v.ID)
			ver, err := note.NewVerifier(l.Vkey)
			must(err, v.ID+" vkey")
			origin, _, _ := strings.Cut(leaf.Checkpoint, "\n")
			cp, _, _, err := fmtlog.ParseCheckpoint([]byte(leaf.Checkpoint), origin, ver)
			if err != nil {
				fail("receipt_checkpoint/%s leaf %d: Go refuses the checkpoint levain accepts: %v", v.ID, j, err)
				continue
			}
			old, ok := prev[origin]
			if !ok {
				old = anchor{0, rfc6962.DefaultHasher.EmptyRoot()}
			}
			var pr [][]byte
			for _, s := range leaf.Consistency {
				h, err := base64.StdEncoding.DecodeString(s)
				must(err, v.ID)
				pr = append(pr, h)
			}
			if leaf.OldSize != old.size {
				fail("receipt_checkpoint/%s leaf %d: old_size %d, previous anchor %d", v.ID, j, leaf.OldSize, old.size)
			}
			switch {
			case old.size == 0:
				if len(pr) != 0 {
					fail("receipt_checkpoint/%s leaf %d: a proof from size 0", v.ID, j)
				}
			case old.size == cp.Size:
				if len(pr) != 0 || !bytes.Equal(old.root, cp.Hash) {
					fail("receipt_checkpoint/%s leaf %d: equal sizes, roots or proof differ", v.ID, j)
				}
			default:
				if err := proof.VerifyConsistency(rfc6962.DefaultHasher, old.size, cp.Size, pr, old.root, cp.Hash); err != nil {
					fail("receipt_checkpoint/%s leaf %d: Go refuses the proof levain accepts: %v", v.ID, j, err)
				}
			}
			prev[origin] = anchor{cp.Size, cp.Hash}
			checked++
		}
	}
	return checked
}

// ---- Go-generated vectors, for tests/test_contracts_gogen.py --------------------------------------------

type goNote struct {
	Note string   `json:"note"`
	Keys []string `json:"vkeys"`
	Now  int64    `json:"now,omitempty"`
}

type goProof struct {
	Size  uint64   `json:"size"`
	M     uint64   `json:"m,omitempty"`
	Index uint64   `json:"index,omitempty"`
	Leaf  string   `json:"leaf_hash,omitempty"`
	Proof []string `json:"proof"`
	Old   string   `json:"old_root,omitempty"`
	Root  string   `json:"root"`
}

func seedKey(name, label string) (string, string) {
	seed := sha256.Sum256([]byte("levain-contracts/xcheck/" + label))
	skey, vkey, err := note.GenerateKey(bytes.NewReader(seed[:]), name)
	must(err, "GenerateKey")
	return skey, vkey
}

func hexAll(bs [][]byte) []string {
	out := make([]string, len(bs))
	for i, b := range bs {
		out[i] = hex.EncodeToString(b)
	}
	return out
}

func emitVectors(path string) error {
	out := map[string]any{"generator": "xcheck/main.go (Go " + runtime.Version() + ")"}

	// signed notes: one and two signers
	skA, vkA := seedKey("go.example/a", "a")
	skB, vkB := seedKey("go.example/b", "b")
	sA, err := note.NewSigner(skA)
	must(err, "signer a")
	sB, err := note.NewSigner(skB)
	must(err, "signer b")
	var notes []goNote
	for _, text := range []string{"hello from Go\n", "two\nlines\n", "unicode é \U0001F35E\n"} {
		msg, err := note.Sign(&note.Note{Text: text}, sA, sB)
		must(err, "sign")
		notes = append(notes, goNote{Note: string(msg), Keys: []string{vkA, vkB}})
	}
	out["notes"] = notes

	// a levain-team checkpoint signed by Go, and Go cosignatures on it
	origin := "levain-team/" + strings.Repeat("0123456789abcdef", 2)
	skL, vkL := seedKey(origin, "log")
	sL, err := note.NewSigner(skL)
	must(err, "log signer")
	leaves := ruleLeaves(13)
	cp := fmtlog.Checkpoint{Origin: origin, Size: 13, Hash: treeRoot(leaves)}
	cpNote, err := note.Sign(&note.Note{Text: string(cp.Marshal())}, sL)
	must(err, "sign checkpoint")
	out["checkpoints"] = []goNote{{Note: string(cpNote), Keys: []string{vkL}}}

	var cosigKeys []string
	var cosigners []note.Signer
	for _, w := range []string{"go.example/w1", "go.example/w2"} {
		seed := sha256.Sum256([]byte("levain-contracts/xcheck/" + w))
		k := ed25519.NewKeyFromSeed(seed[:])
		skey := "PRIVATE+KEY+" + w + "+00000000+" + base64.StdEncoding.EncodeToString(append([]byte{0x04}, seed[:]...))
		s, err := fnote.NewSignerForCosignatureV1(skey)
		must(err, "cosigner")
		cosigners = append(cosigners, s)
		pub := append([]byte{0x04}, k.Public().(ed25519.PublicKey)...)
		h := sha256.Sum256(append([]byte(w+"\n"), pub...))
		cosigKeys = append(cosigKeys, fmt.Sprintf("%s+%08x+%s", w, binary.BigEndian.Uint32(h[:4]), base64.StdEncoding.EncodeToString(pub)))
	}
	cosigned, err := note.Sign(&note.Note{Text: string(cp.Marshal())}, append([]note.Signer{sL}, cosigners...)...)
	must(err, "cosign")
	out["cosigned"] = []goNote{{Note: string(cosigned), Keys: cosigKeys, Now: time.Now().Unix()}}

	// Merkle proofs from the upstream reference tree
	t := testonly.New(rfc6962.DefaultHasher)
	t.AppendData(ruleLeaves(37)...)
	var incl, cons []goProof
	for _, size := range []uint64{1, 2, 3, 16, 17, 37} {
		for _, idx := range []uint64{0, size / 2, size - 1} {
			p, err := t.InclusionProof(idx, size)
			must(err, "inclusion proof")
			incl = append(incl, goProof{Size: size, Index: idx, Leaf: hex.EncodeToString(t.LeafHash(idx)), Proof: hexAll(p), Root: hex.EncodeToString(t.HashAt(size))})
		}
		for _, m := range []uint64{1, size / 2, size} {
			if m == 0 {
				continue
			}
			p, err := t.ConsistencyProof(m, size)
			must(err, "consistency proof")
			cons = append(cons, goProof{Size: size, M: m, Proof: hexAll(p), Old: hex.EncodeToString(t.HashAt(m)), Root: hex.EncodeToString(t.HashAt(size))})
		}
	}
	out["inclusion"] = incl
	out["consistency"] = cons

	raw, err := json.MarshalIndent(out, "", " ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o644)
}
