"""Tests for the pack-drift reconcile — the DOWNSTREAM pack axis.

`levain.manifest` pack provenance (hash / drift / lock round-trip), `levain.reconcile`
(verbatim 3-way, render-slot targeted re-prompt + edit-preservation), and the
`levain update` pack phase. The install's post-init state is constructed directly
(seed files + lock + answers.json) so the reconcile can be unit-tested without a live
anneal binary.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from levain import manifest, reconcile
from levain.install import _copy_pack_docs, read_answers, write_answers
from levain.interview import parse_template, render_template
from levain.packs import load_pack_manifest


def _write_pack(root: Path, *, name: str, order: int = 10, render=(), seed=None,
                activation=None, docs=None, version=None, changelog=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ver = f'version = "{version}"\n' if version else ""
    rnd = f"render = {list(render)!r}\n" if render else ""
    (root / "pack.toml").write_text(f'name = "{name}"\norder = {order}\n{rnd}{ver}', encoding="utf-8")
    for sub, files in (("seed", seed), ("activation", activation), ("docs", docs)):
        for fn, content in (files or {}).items():
            p = root / sub / fn
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    if changelog:
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def _install_from_pack(tmp_path: Path, pack: Path, answers: dict) -> Path:
    """Reproduce the post-init install state for one pack: render its render seeds,
    verbatim-copy the rest, record the lock (source + rendered hashes) + answers.json."""
    install = tmp_path / "install"
    (install / "seed").mkdir(parents=True)
    mf = load_pack_manifest(pack)
    render_names = set(mf.render)
    for f in sorted((pack / "seed").glob("*.md")):
        if f.name in render_names:
            (install / "seed" / f.name).write_text(
                render_template(parse_template(f), answers), encoding="utf-8"
            )
        else:
            (install / "seed" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    rendered = manifest.rendered_hashes(install, [f"seed/{n}" for n in mf.render])
    prov = manifest.pack_provenance(mf.name, pack, mf.version, rendered=rendered, render=mf.render)
    manifest.write_lock(
        install, manifest.CompatSet(levain="0.0.0", anneal="0.9.6", schema="partnership"),
        packs=[prov],
    )
    _copy_pack_docs(install, [(mf, pack)])  # compose the pack's docs, like a real init
    write_answers(install, answers, lambda s: None)
    return install


def _reconcile(install: Path, prompter=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        prov, drifted, review = reconcile.run_pack_reconcile(
            install, dry_run=False, emit=print, prompter=prompter
        )
    return buf.getvalue(), prov, drifted, review


ANSWER = lambda fields: {f.slot: f"A_{f.slot}" for f in fields}  # noqa: E731
REFUSE = lambda fields: None  # noqa: E731 — non-interactive


# --------------------------------------------------------------------------
# manifest — provenance + drift
# --------------------------------------------------------------------------

class TestPackProvenance:
    def test_hash_covers_the_right_files_and_excludes_pyc(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "x"},
                           activation={"posture.md": "p"}, docs={"c.md": "d"})
        (pack / "seed" / "__pycache__").mkdir()
        (pack / "seed" / "__pycache__" / "j.pyc").write_text("junk")
        h = manifest.hash_pack_source(pack)
        assert set(h) == {"pack.toml", "seed/a.md", "activation/posture.md", "docs/c.md"}

    def test_missing_source_hashes_empty(self, tmp_path):
        assert manifest.hash_pack_source(tmp_path / "nope") == {}

    def test_drift_unchanged_changed_missing(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "v1"})
        prov = manifest.pack_provenance("p", pack, None)
        assert manifest.compute_pack_drift([prov])[0].status == "unchanged"
        (pack / "seed" / "a.md").write_text("v2")
        d = manifest.compute_pack_drift([prov])[0]
        assert d.status == "changed" and d.modified == ("seed/a.md",)
        import shutil
        shutil.rmtree(pack)
        assert manifest.compute_pack_drift([prov])[0].status == "source_missing"

    def test_lock_round_trip_with_rendered(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", render=["w.md"], seed={"w.md": "{{X}}"})
        inst = tmp_path / "i"
        (inst / ".levain").mkdir(parents=True)
        prov = manifest.pack_provenance("p", pack, "1.0", rendered={"seed/w.md": "abc"})
        manifest.write_lock(inst, manifest.CompatSet("0", "0.9.6", "partnership"), packs=[prov])
        back = manifest.read_pack_locks(inst)
        assert len(back) == 1
        assert back[0].name == "p" and back[0].version == "1.0"
        assert back[0].rendered == {"seed/w.md": "abc"}
        # the engine lock still reads (packs key is additive)
        assert manifest.read_lock(inst).anneal == "0.9.6"


# --------------------------------------------------------------------------
# reconcile — verbatim
# --------------------------------------------------------------------------

class TestVerbatimReconcile:
    def test_fast_forward_unedited(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "v1\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "seed" / "a.md").write_text("v2\n")
        _out, _prov, drifted, review = _reconcile(inst)
        assert drifted and not review
        assert (inst / "seed" / "a.md").read_text() == "v2\n"

    def test_operator_edit_backed_up_then_applied(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "orig\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (inst / "seed" / "a.md").write_text("MY EDIT\n")
        (pack / "seed" / "a.md").write_text("upstream v2\n")
        _reconcile(inst)
        assert (inst / "seed" / "a.md").read_text() == "upstream v2\n"
        baks = list((inst / "seed").glob("a.md.bak.*"))
        assert len(baks) == 1 and baks[0].read_text() == "MY EDIT\n"

    def test_added_and_removed_seed(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "a\n", "b.md": "b\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "seed" / "c.md").write_text("new\n")   # added
        (pack / "seed" / "b.md").unlink()              # removed
        _reconcile(inst)
        assert (inst / "seed" / "c.md").read_text() == "new\n"
        assert not (inst / "seed" / "b.md").exists()

    def test_second_run_is_clean(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "v1\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "seed" / "a.md").write_text("v2\n")
        # persist the advanced provenance (what update does)
        _out, prov, _d, _r = _reconcile(inst)
        manifest.write_lock(inst, manifest.CompatSet("0", "0.9.6", "partnership"), packs=prov)
        _out2, _p2, drifted2, _r2 = _reconcile(inst)
        assert not drifted2


# --------------------------------------------------------------------------
# reconcile — render 3-way
# --------------------------------------------------------------------------

class TestRenderReconcile:
    def _pack(self, tmp_path, template, **kw):
        return _write_pack(tmp_path / "p", name="p", render=["role.md"],
                           seed={"role.md": template}, **kw)

    def test_new_slot_targeted_prompt_and_persist(self, tmp_path):
        pack = self._pack(tmp_path, "Focus: {{FOCUS}}\n",
                          changelog="## v2\n- added ESCALATION to sharpen sizing\n")
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (pack / "seed" / "role.md").write_text("Focus: {{FOCUS}}\nEsc: {{ESCALATION}}\n")
        out, _prov, _d, review = _reconcile(inst, ANSWER)
        role = (inst / "seed" / "role.md").read_text()
        assert "A_ESCALATION" in role and "hosting" in role  # new answered, old preserved
        assert read_answers(inst).get("ESCALATION") == "A_ESCALATION"  # persisted
        assert "added ESCALATION" in out  # CHANGELOG framed the re-prompt
        assert not review

    def test_new_slot_non_interactive_surfaces_no_blank_fill(self, tmp_path):
        pack = self._pack(tmp_path, "Focus: {{FOCUS}}\n")
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (pack / "seed" / "role.md").write_text("Focus: {{FOCUS}}\nX: {{NEWX}}\n")
        out, _prov, _d, review = _reconcile(inst, REFUSE)
        assert review  # surfaced
        assert "{{NEWX}}" not in (inst / "seed" / "role.md").read_text()  # not blank-rendered
        assert "interactively" in out

    def test_prose_only_change_fast_forwards(self, tmp_path):
        pack = self._pack(tmp_path, "Focus: {{FOCUS}}\n")
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (pack / "seed" / "role.md").write_text("Your focus is: {{FOCUS}}\n")
        _out, _prov, _d, review = _reconcile(inst, ANSWER)
        assert not review
        assert (inst / "seed" / "role.md").read_text() == "Your focus is: hosting\n"

    def test_edited_render_file_side_by_side_never_clobbers(self, tmp_path):
        pack = self._pack(tmp_path, "Focus: {{FOCUS}}\n")
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (inst / "seed" / "role.md").write_text("Focus: MY HAND EDIT\n")   # operator edit
        (pack / "seed" / "role.md").write_text("Focus v2: {{FOCUS}}\n")   # template change
        _out, _prov, _d, review = _reconcile(inst, ANSWER)
        assert "MY HAND EDIT" in (inst / "seed" / "role.md").read_text()  # untouched
        assert (inst / "seed" / "role.md.new").exists()                   # side-by-side
        assert review

    def test_no_persisted_answers_surfaces(self, tmp_path):
        pack = self._pack(tmp_path, "Focus: {{FOCUS}}\n")
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (inst / ".levain" / "answers.json").unlink()  # a pre-persist install
        (pack / "seed" / "role.md").write_text("Focus: {{FOCUS}}\nNew: {{NEW}}\n")
        _out, _prov, _d, review = _reconcile(inst, ANSWER)
        assert review  # can't re-prompt without the answer baseline


# --------------------------------------------------------------------------
# run_pack_reconcile — dry-run + docs
# --------------------------------------------------------------------------

class TestRunPackReconcile:
    def test_dry_run_writes_nothing(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "v1\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "seed" / "a.md").write_text("v2\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            reconcile.run_pack_reconcile(inst, dry_run=True, emit=print, prompter=ANSWER)
        assert (inst / "seed" / "a.md").read_text() == "v1\n"  # unchanged

    def test_docs_rebuilt_from_pulled_source(self, tmp_path):
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "a\n"}, docs={"ch.md": "# v1\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "docs" / "ch.md").write_text("# v2\n")
        _reconcile(inst)
        assert any("# v2" in p.read_text() for p in (inst / ".levain" / "docs").rglob("*.md"))

    def test_no_packs_recorded_is_noop(self, tmp_path):
        inst = tmp_path / "i"
        (inst / ".levain").mkdir(parents=True)
        manifest.write_lock(inst, manifest.CompatSet("0", "0.9.6", "partnership"))
        prov, drifted, review = reconcile.run_pack_reconcile(inst, dry_run=False, emit=lambda s: None)
        assert prov == [] and not drifted and not review


class TestHonestyFloorAndDocs:
    def test_read_pack_locks_status_distinguishes_absent_ok_corrupt(self, tmp_path):
        inst = tmp_path / "i"
        (inst / ".levain").mkdir(parents=True)
        lock = inst / ".levain" / "manifest.json"
        # absent: base-only lock (no packs key)
        manifest.write_lock(inst, manifest.CompatSet("0", "0.9.6", "partnership"))
        # write_lock always writes packs:[]; an empty list is "absent"-equivalent for our
        # purposes, but a lock with NO packs key at all is the true pre-pack case:
        import json
        lock.write_text(json.dumps({"levain": "0", "anneal": "0.9.6", "schema": "partnership"}))
        assert manifest.read_pack_locks_status(inst)[1] == "absent"
        # ok
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "x\n"})
        prov = manifest.pack_provenance("p", pack, None)
        manifest.write_lock(inst, manifest.CompatSet("0", "0.9.6", "partnership"), packs=[prov])
        assert manifest.read_pack_locks_status(inst)[1] == "ok"
        # corrupt: unparseable JSON
        lock.write_text("{not json")
        assert manifest.read_pack_locks_status(inst)[1] == "corrupt"
        # corrupt: a malformed entry (missing required field) -> corrupt, never dropped
        lock.write_text(json.dumps({"levain": "0", "anneal": "0.9.6", "schema": "partnership",
                                    "packs": [{"name": "p"}]}))  # no source/files
        assert manifest.read_pack_locks_status(inst)[1] == "corrupt"

    def test_corrupt_lock_returns_none_never_wipes(self, tmp_path):
        # A corrupt pack lock must return None provenance (the caller then REFUSES to
        # rewrite), never [] which would erase the baseline (complement L3 CRITICAL).
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "x\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (inst / ".levain" / "manifest.json").write_text("{corrupt")
        prov, drifted, review = _reconcile(inst)[1:]
        assert prov is None and review is True   # signal "don't write"; needs_review

    def test_docs_rebuild_failure_holds_docs_provenance(self, tmp_path):
        # If the wholesale docs rebuild fails, docs provenance must NOT advance — else
        # stale/gone docs are marked "resolved" and never retried (complement L3 #2).
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "a\n"}, docs={"ch.md": "# v1\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        recorded = manifest.read_pack_locks(inst)
        (pack / "docs" / "ch.md").write_text("# v2\n")  # docs drift
        with mock.patch("levain.reconcile._copy_pack_docs", side_effect=OSError("disk full")):
            prov, _d, _r = _reconcile(inst)[1:]
        # docs/ch.md hash held at the RECORDED (old) value, so it re-drifts next run
        assert prov[0].files["docs/ch.md"] == recorded[0].files["docs/ch.md"]


class TestMultiPack:
    def _install_two(self, tmp_path, packA, packB, answers):
        install = tmp_path / "install"
        (install / "seed").mkdir(parents=True)
        provs = []
        for pack in (packA, packB):
            mf = load_pack_manifest(pack)
            for f in sorted((pack / "seed").glob("*.md")):
                (install / "seed" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            provs.append(manifest.pack_provenance(mf.name, pack, mf.version, render=mf.render))
        manifest.write_lock(
            install, manifest.CompatSet("0", "0.9.6", "partnership"), packs=provs)
        write_answers(install, answers, lambda s: None)
        return install

    def test_two_packs_both_reconcile_in_one_run(self, tmp_path):
        a = _write_pack(tmp_path / "a", name="a", seed={"da.md": "a v1\n"})
        b = _write_pack(tmp_path / "b", name="b", order=20, seed={"db.md": "b v1\n"})
        inst = self._install_two(tmp_path, a, b, {})
        (a / "seed" / "da.md").write_text("a v2\n")
        (b / "seed" / "db.md").write_text("b v2\n")
        _out, prov, drifted, review = _reconcile(inst)
        assert drifted and not review
        assert (inst / "seed" / "da.md").read_text() == "a v2\n"
        assert (inst / "seed" / "db.md").read_text() == "b v2\n"
        assert len(prov) == 2  # both packs' provenance recorded


def _record(install, prov):
    """Persist reconcile provenance to the lock (what `levain update` does)."""
    manifest.write_lock(install, manifest.CompatSet("0", "0.9.6", "partnership"), packs=prov)


# --------------------------------------------------------------------------
# the provenance-advance model — the L1 regressions (must not trap / must gate)
# --------------------------------------------------------------------------

class TestProvenanceAdvance:
    def test_version_only_bump_advances_not_traps(self, tmp_path):
        # A version-only pack.toml bump is harmless sugar — it must ADVANCE (record the
        # new version), never trap the install in permanent needs_review (L1 #2).
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "x\n"}, version="1.0")
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "pack.toml").write_text('name = "p"\norder = 10\nversion = "1.1"\n')
        _out, prov, drifted, review = _reconcile(inst)
        assert drifted and not review          # not trapped
        assert prov[0].version == "1.1"        # version advanced
        _record(inst, prov)
        _o2, _p2, drifted2, _r2 = _reconcile(inst)
        assert not drifted2                    # clears on the next run

    def test_seed_set_add_gates_review_and_resurfaces(self, tmp_path):
        # Adding a seed reports needs_review (exit 1), NOT success — the adapter @import
        # isn't regenerated, so the new file is inert until re-onboard (L1 #1 / M2). And
        # it keeps flagging until then.
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "a\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        (pack / "seed" / "new.md").write_text("new doctrine\n")
        _out, prov, _d, review = _reconcile(inst)
        assert review is True                   # gated to needs_review (non-zero exit)
        assert (inst / "seed" / "new.md").is_file()  # on disk...
        _record(inst, prov)
        _o2, _p2, drifted2, review2 = _reconcile(inst)
        assert drifted2 and review2 is True     # ...but keeps flagging until re-onboard

    def test_hand_merged_render_clears_the_flag(self, tmp_path):
        # After the operator hand-merges an edited render file's .new, the next update
        # must ADVANCE (stop re-.new-ing it) — L1 #3.
        pack = _write_pack(tmp_path / "p", name="p", render=["r.md"], seed={"r.md": "F: {{FOCUS}}\n"})
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (inst / "seed" / "r.md").write_text("F: MY EDIT\n")         # operator edit
        (pack / "seed" / "r.md").write_text("F v2: {{FOCUS}}\n")    # template change
        _out, prov, _d, review = _reconcile(inst, ANSWER)
        assert review and (inst / "seed" / "r.md.new").exists()
        _record(inst, prov)
        # operator hand-merges: takes the .new content
        merged = (inst / "seed" / "r.md.new").read_text()
        (inst / "seed" / "r.md").write_text(merged)
        _o2, prov2, _d2, review2 = _reconcile(inst, ANSWER)
        assert not review2                             # cleared
        assert not (inst / "seed" / "r.md.new").exists()  # stale .new removed
        _record(inst, prov2)
        _o3, _p3, drifted3, _r3 = _reconcile(inst, ANSWER)
        assert not drifted3                            # fully settled

    def test_render_membership_flip_surfaces_not_silent(self, tmp_path):
        # A file flips verbatim -> render via pack.toml `render` with UNCHANGED seed
        # bytes — a source-hash diff misses it; it must SURFACE (re-onboard), never
        # silently leave `{{FOCUS}}` literal (codex L3 #1).
        pack = _write_pack(tmp_path / "p", name="p", seed={"role.md": "Focus: {{FOCUS}}\n"})
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        # only the manifest changes: role.md becomes a render file (bytes identical)
        (pack / "pack.toml").write_text('name = "p"\norder = 10\nrender = ["role.md"]\n')
        _out, _prov, drifted, review = _reconcile(inst, ANSWER)
        assert drifted and review is True   # surfaced, not a silent clean pass
        assert "{{FOCUS}}" in (inst / "seed" / "role.md").read_text()  # untouched, flagged

    def test_source_missing_gates_review_and_keeps_docs(self, tmp_path):
        # A vanished pack source cannot reconcile — needs_review (exit 1), NOT silent
        # success; and its previously-composed docs are NOT dropped (codex L3 #2).
        pack = _write_pack(tmp_path / "p", name="p", seed={"a.md": "a\n"}, docs={"ch.md": "# ch\n"})
        inst = _install_from_pack(tmp_path, pack, {})
        assert (inst / ".levain" / "docs").exists()
        import shutil
        shutil.rmtree(pack)
        _out, _prov, drifted, review = _reconcile(inst)
        assert drifted and review is True                      # not a green exit
        assert any((inst / ".levain" / "docs").rglob("*.md"))  # docs preserved

    def test_answer_persist_failure_holds_provenance(self, tmp_path):
        # If the new-answer write FAILS after a slot is answered, provenance must NOT
        # advance — else the manifest says "resolved" while answers.json lacks the slot
        # (codex L3 #3).
        pack = _write_pack(tmp_path / "p", name="p", render=["r.md"], seed={"r.md": "F: {{FOCUS}}\n"})
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        recorded = manifest.read_pack_locks(inst)
        (pack / "seed" / "r.md").write_text("F: {{FOCUS}}\nX: {{NEWX}}\n")
        with mock.patch("levain.reconcile.write_answers", return_value=False):
            _out, prov, _d, review = _reconcile(inst, ANSWER)
        assert review is True
        # provenance returned is the RECORDED baseline (not advanced) so it re-surfaces
        assert prov[0].files == recorded[0].files

    def test_partial_prompter_surfaces_never_blank_fills(self, tmp_path):
        # A custom prompter that returns a dict MISSING a new field must surface, never
        # render {{SLOT}} -> "" (L1 #6). The shipped tty_prompter is all-or-None anyway.
        pack = _write_pack(tmp_path / "p", name="p", render=["r.md"], seed={"r.md": "F: {{FOCUS}}\n"})
        inst = _install_from_pack(tmp_path, pack, {"FOCUS": "hosting"})
        (pack / "seed" / "r.md").write_text("F: {{FOCUS}}\nX: {{NEWX}}\n")
        _out, _prov, _d, review = _reconcile(inst, lambda fields: {})  # empty -> missing NEWX
        assert review
        assert "{{" not in (inst / "seed" / "r.md").read_text()  # not blank-rendered
        assert "X: " not in (inst / "seed" / "r.md").read_text()  # untouched original
