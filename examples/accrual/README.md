# Accrual demo

This is Levain's proof artifact. One script reads a continuity file's git history at four snapshots and renders a growth timeline.

The rendered output ([`growth_timeline.md`](growth_timeline.md)) is a single file you can scan in 30 seconds and see what *ship the seed that grows a practice, not the practice* means in practice.

## What it shows

`flow`'s own `global/continuity.md` at week 1, week 4, week 12, and now (≈5 months in). At each snapshot: the line count, the section list, the State banner. Where new architecture appears (Proven, Foundation, etc.), it's marked.

The closing tells you the trajectory: where most of the growth happened, and where the structure stabilized.

## Why it's the right proof artifact

A new operator running `levain init` gets the first snapshot — a small continuity, the basic shape. Not a finished foreign personality to inherit. The timeline shows what comes next: not as a target, but as evidence the engine works.

The README claim *ship the seed that grows a practice* would otherwise be a take-it-on-faith assertion. This is the empirical answer.

## Run it

```
python render_timeline.py
```

Defaults to `~/Documents/flow` + `global/continuity.md`. Override:

```
python render_timeline.py --repo /path/to/repo --file path/to/continuity.md --out timeline.md
```

Pure stdlib (subprocess + re + dataclasses + datetime). Python 3.11+.

The script reads git history from a real working repo — point `--repo` at the local repo whose continuity you want to render (your own, once you have a few weeks of history). Levain's defaults render flow's history because that's the public-evidence case.

## Anchors

Snapshot dates are passed via `--snapshots LABEL=YYYY-MM-DD ...`. Defaults track flow's own history; for a different repo, anchor to the first commit of its continuity file and pick three later points.
