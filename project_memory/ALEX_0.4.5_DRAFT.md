# DRAFT — message to Alex, for Phill to send (or not). NOT SENT.

Written 2026-09-06 by `0906+17 levain-seat` after the 0.4.5 publish verified.
⛔ Draft only. Nothing has been sent to anyone.

---

Alex —

0.4.5 is up. The thing that ate your hook patch is fixed.

Short version: `levain init --force` used to rewrite the whole `activation/` tree
and only copy two markdown files aside first, so a patched hook went in the bin
with no backup and nothing printed. That's the command our own upgrade
instructions tell you to run, which is the part that actually bothered me. Now
anything under `activation/` that the install can't rebuild from its own
templates gets copied to `.levain/backups/activation/<timestamp>/` before the
tree is replaced. Hooks included, files you added yourself included. It decides
by whether it can reproduce the bytes, not by a list of filenames, so it doesn't
need updating every time the tree grows a file.

Two honest limits, because I'd rather you hear them from me than find them.

It preserves, but it doesn't always tell you it did. There's one line in the hook
that `init` rewrites itself on every run — the resolved `anneal-memory` path — and
if your edit is confined to that line, we genuinely can't tell it apart from our
own output. You get the copy either way. You just don't get the "operator-edited"
notice, because claiming you edited something when we can't know would be worse
than staying quiet about it.

Symlinks aren't handled at all. If you've symlinked anything under `activation/`,
copy it aside yourself before upgrading. That one's written up and open, not
quietly ignored.

If you do upgrade: `doctor` will go red immediately afterward, on every 0.4.x
upgrade, not just this one. Two failures, `hook freshness` and `compat: levain`.
Both are correct and neither is your install being broken — they turn red because
the version moved. The two commands they name clear them. That was already true
of 0.4.4 and we didn't say so clearly enough, which is fixed in the changelog now.

One other thing in this release you'd care about specifically: `init --adapter
codex` used to repoint Codex's global MCP memory registration silently. That
registration is machine-wide, so running init from a second install moved every
Codex session onto that store with nothing printed. It now names what it's leaving
and what it's adopting, and backs the config up first.

No pressure either way — I know you're running your own harness now. Mostly I
wanted you to know it got fixed properly rather than patched around, since you're
the reason we found it.

— Phill

---

## Notes for the fan-in (NOT part of the message)

⛔ **Deliberately absent:** any request that he check his install or audit what he
lost. The loss already happened and he knows; asking him to re-audit is work
created for him by our defect, twice.
⛔ **No promises about `spore-861`, `spore-860` or `spore-866`** — all open. The
symlink gap is named as a current limit with a workaround, not as a coming fix.
⛔ **`spore-866` (the TOML escaping) is not mentioned at all.** It needs a
backslash or quote in an install path, which is not his situation, and naming it
would be the "makes him re-check unnecessarily" failure the brief warned about.
⚠ **Past tense throughout, and only written after the publish verified** — index,
clean-venv install from site-packages, doctor on the published wheel on both
adapters, and the falsification control at exit 1.
⚠ **The apology is structural, not performed:** "that's the command our own
upgrade instructions tell you to run, which is the part that actually bothered
me." One clause, no grovelling. He is an operator, not a customer to be soothed.
