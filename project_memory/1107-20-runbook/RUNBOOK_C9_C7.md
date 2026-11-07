# Runbook for Sunday 11-08: C7 closed-lid test and C9 one real coordinator day

Signed np-161c6272 (a), folded into the friction day (np-a739498c). Written 11-07 by levain 59's runbook lane. **Nothing here has been run.** Every fact below was read from the checkout at `~/Briefcase/levain` (origin/main, v0.11.28 tagged, tree at 0.11.28.dev0), from `--help` output, or from the machine's current state, and each has its source named. Anything not derivable is marked **UNVERIFIED** with what settles it.

## The plain-words top

| | **C7 closed-lid test** | **C9 one real coordinator day** |
|---|---|---|
| What it proves | What Levain's "keep the host awake" really does when the lid is closed on battery. Levain's own code says it does NOT stop lid-close sleep (`awake.py` `LID_NOTE`: *"a closed lid can still sleep this Mac ... an idle-sleep assertion does not prevent lid-close sleep"*). This run settles that premise with a measurement. | Whether the coordinator carries a real day: drops from your phone split into threads, an envelope you sign once covers a build, one irreversible thing waits for your sign, an away thread waits for the morning card, and the review queue clears. It is a run, not a test (BUILD_PLAN: "C9 is a run, not a test"). |
| Your time | About 25 minutes, 10 of them lid closed. | A real working day with about 8 checkpoints of 1 to 3 minutes each. Everything between is you living your day. |
| You do | Unplug, close the lid, wait, open it, say what you saw. | Drop words from your phone, sign one envelope, sign one irreversible effect, say "heading out", read the morning card, clear three review items. |
| The desk does | All setup, the heartbeat recorder, the verdict reading from the logs. | All setup, starts the serve and the coordinator, watches the stores, writes the run log, files anything that failed. |
| Result shapes | PASS means the observation matches what doctor says (see C7 below). A lid that keeps the machine awake is also a result: it makes the doctor line wrong and the desk files the fix. | PASS means every line of the DONE list (C9 section) has its evidence in the run log. |

**Order on the day:** C7 first, before any seat is running, because closing the lid puts this laptop to sleep and with it flow's desk, seats and the serve. C9 after. If C7 fails to wake cleanly, C9 waits.

**One warning that governs both:** this laptop is flow's 24/7 host. Right now (11-07 ~15:00) `pmset -g` reports `sleep 0 (sleep prevented by Amphetamine, caffeinate, powerd, useractivityd, caffeinate, sharingd, caffeinate, coreaudiod, coreaudiod)`. **Amphetamine is running and holding the Mac awake.** A lid test with Amphetamine on measures Amphetamine, not Levain. It must be quit for C7 and relaunched after (C7 DESK PREP 3).

---

# Part 1. C7 closed-lid test

**Row:** `STATUS_1_0.md` COORD sub-table "C7 host awake | RELEASED v0.11.15 (code); closed-lid-on-battery RUN is HIS". BUILD_PLAN C7: "RUN: closed lid on battery under the assertion (unverified premise, gap 6)". Cut-gate list line 760.

## What is being observed, exactly

The mechanism (`levain/coordinator/awake.py`): whenever a coordinator thread of kind `work` is in state `working`, Levain starts `caffeinate -i -t 1800` (macOS idle-sleep assertion, `PreventUserIdleSystemSleep`) through `awake.sync`, which `store.py` calls after every committed coordinator write (`_keep_awake`). It is an idle-sleep assertion only. The module header says it does not claim to survive a closed lid.

The run answers one question: **with a work thread `working`, the assertion visibly held, on battery, no external display, lid closed for N minutes, does the machine keep running?**

Observable, measured three independent ways so none is trusted alone:

1. **A heartbeat file.** A tiny desk-written script appends a timestamp every 5 seconds to a log. A machine that sleeps stops the script, so a gap in the log is the sleep. No gap through the closed-lid window means it stayed awake. (The script is plain `date` in a shell loop; no model, no sandbox.)
2. **`pmset -g log`** after the fact: the Sleep / Wake / Clamshell Sleep lines with timestamps, and the assertion lines. Read-only.
3. **What you see on lid-open:** the screen is either already awake and logged in with the clock on the heartbeat tail still ticking, or it shows the lock screen after a wake. You say which, and roughly how long you waited.

## What the coordinator keeps or loses (and why nothing can be lost)

The test thread is a throwaway: no agent session is launched for it, no repo is touched, and its store is a scratch `LEVAIN_HOME` (`/tmp/c7-home`), not `~/.levain`. The coordinator's real store, lease and queue are never opened. If the Mac sleeps, the thread simply stays `working` in the scratch SQLite file, committed before sleep; on wake the file is intact (WAL SQLite commits are durable before the write returns). The `caffeinate` holder expires by itself after 1800 s (`HOLD_SECONDS`). So the worst case is: the scratch thread is left `working` and the scratch dir is deleted. No data of yours is in play.

What the user sees on lid-open is the part that is genuinely being asked: if the machine slept, expect the usual wake (lock screen, a second or two); if it did not, nothing changes. Either is a clean outcome.

## DESK PREP (all before you sit; none of it is the test)

1. **Pick the window.** Daytime, before seats start, with no dream due (the dream runs at 00:00) and Phill at the machine. The desk holds all seat launches until C7 ends. Check `python3 ~/Briefcase/flow/scripts/peer_roster.py` shows no live seat that must not sleep. UNVERIFIED: the exact command output shape; settle by running it.
2. **Battery.** The lid test must start on battery with enough charge: `pmset -g batt` must show `Now drawing from 'Battery Power'` and at least 60%. Currently 80% on AC. You unplug at the start of your steps; the desk checks it reads Battery Power before the thread is started.
3. **Quit everything that holds the Mac awake**, then verify. Quit Amphetamine (it is a menu-bar app; `osascript -e 'tell application "Amphetamine" to quit'` is the desk's quit, not a hand-driven UI click on his apps beyond this one named quit, and he can also quit it from the menu bar). Stop every `caffeinate` that is not Levain's: `pgrep -lf caffeinate` before; the desk stops the ones it owns. Then `pmset -g assertions` must list **no** `PreventUserIdleSystemSleep` and no `PreventSystemSleep` assertion other than those the OS itself owns (`powerd`, `useractivityd`, `sharingd`, `coreaudiod` appear in today's output; coreaudiod holds one only while audio is open, so also pause any audio). Write the before-snapshot to `~/Desktop/c9-c7-run/c7/assertions_before.txt`. UNVERIFIED: whether powerd / sharingd assertions (seen in `pmset -g` today) are the benign kind or themselves block lid sleep; settle by reading `pmset -g assertions` in full once with Amphetamine off. If a non-Levain assertion remains and cannot be stopped, record it, it is part of the result.
4. **No external display, no power adapter, no Bluetooth keyboard/mouse that wakes** (a wake from a peripheral muddies the log): unplug any external monitor and the adapter before the lid closes. A display attached with the lid closed is clamshell mode and a different test.
5. **Scratch home and a throwaway thread.** Commands (absolute paths; `L=/Users/phillipclapham/Briefcase/levain/.venv/bin/levain`):
   ```
   export LEVAIN_HOME=/tmp/c7-home
   mkdir -p /tmp/c7-home /Users/phillipclapham/Desktop/c9-c7-run/c7
   $L drop "C7 closed-lid test thread, throwaway" --json            # prints a drop id (the DROP below)
   $L drops split DROP --thread "work:c7 lid test" --by operator --json   # prints the thread id (THREAD)
   ```
   Do NOT run `thread state THREAD working` yet; that is the start of the test and it takes the assertion. `levain drop` with no coordinator agent set only queues (the machine's current `levain coord status` says `next drop: unset (no coordinator agent is set; drops queue for you)`), and under the scratch `LEVAIN_HOME` there is no agent at all, so nothing launches. UNVERIFIED: that `drops split --by operator` needs no token; settle by the desk running steps 5 once against a second scratch dir (`LEVAIN_HOME=/tmp/c7-rehearsal`) before Phill sits.
6. **Rehearse the hold with the lid open** (setup, not the test): in `/tmp/c7-rehearsal`, run `thread state THREAD working`, check `LEVAIN_HOME=/tmp/c7-rehearsal $L doctor` shows a `host sleep` row saying the hold is taken and `pgrep -lf "caffeinate -i -t 1800"` shows the holder, then `thread state THREAD done` and check the holder is gone (doctor's row says nothing holds it). This proves the assertion mechanism works on this machine before the lid is involved. UNVERIFIED: that doctor reads the scratch dir through `LEVAIN_HOME` (code path read: `doctor._check_host_awake` -> `_consent_queue_dir` -> `queue_dir_for(os.environ)` -> `$LEVAIN_HOME/consent`, so it should, but `drop`/`thread` must also be using `$LEVAIN_HOME/consent`; the rehearsal settles it).
7. **Heartbeat script** at `~/Desktop/c9-c7-run/c7/heartbeat.sh`: `while true; do date '+%s %F %T' >> ~/Desktop/c9-c7-run/c7/heartbeat.log; sleep 5; done` (write it with the Write tool, run with `bash`, not a heredoc into an interpreter). The desk starts it in the background at the start of Phill's step 3 and stops it at the end.
8. **Note the clock and the plan.** The lid-closed window is 10 minutes (shorter than the 30-minute hold, so the holder cannot expire and confound the result; `HOLD_SECONDS = 1800`, renewed only in its last half, so no sync is expected inside the window).

## YOUR STEPS (top to bottom, each with the thing you should see)

- [ ] **C7.1** Desk says READY (all DESK PREP done, Amphetamine quit, heartbeat not yet started). You see: desk message "C7 ready, unplug when I say". *Fail if:* Amphetamine still in the menu bar.
- [ ] **C7.2** Unplug the charger and any external display. You see: battery icon, no charge bolt. *Check:* `pmset -g batt` (desk reads it) says Battery Power.
- [ ] **C7.3** Desk starts the heartbeat, then runs `thread state THREAD working` under `LEVAIN_HOME=/tmp/c7-home`. You see: nothing visible. *Desk checks and logs:* doctor row says the hold is taken; `pgrep -lf "caffeinate -i -t 1800"` shows the holder; `pmset -g assertions` now lists a `PreventUserIdleSystemSleep` assertion owned by caffeinate; copy to `assertions_during.txt`. **If the holder is not there, STOP: the premise of the test is not met; log it as a C7 setup FAIL and do not close the lid.**
- [ ] **C7.4** Note the time (the desk says it). **Close the lid.** Do not touch the machine. Do not play audio. Wait 10 minutes by a clock that is not this Mac (your phone).
- [ ] **C7.5** After 10 minutes open the lid. You see one of two things; say which:
   - (A) the screen comes up at once, no lock screen delay, nothing to wake. Say "stayed awake".
   - (B) a short wake: dark screen, then the lock screen. Say "slept".
   Say also: did the fans or any sound run while it was closed (you may not know; that is fine), and did anything on screen look different.
- [ ] **C7.6** Desk reads the logs and tells you the verdict (below). You only need to confirm what you saw in step 5.
- [ ] **C7.7** Plug the charger back in. Desk ends the thread (`thread state THREAD done`) and checks the holder is gone.

## PASS / FAIL and what the verdict means

The desk reads the heartbeat log: a gap across the lid-closed window longer than 30 s means the Mac slept. Cross-checked against `pmset -g log` (look for `Clamshell Sleep` / `Sleep` and the matching `Wake` lines inside the window) and against what Phill saw.

| Observation (all three agree) | Verdict |
|---|---|
| Heartbeat gap, pmset shows a sleep within seconds of the lid close, Phill saw a wake | **PASS, premise confirmed**: Levain's assertion does not hold the host through a closed lid, exactly as `LID_NOTE` and the module header say. C7 is DONE; the claim stays as written (no lid claim). The 1.0 docs must keep saying "idle sleep only". |
| No gap, no pmset sleep lines, Phill saw it already awake | **PASS, premise REFUTED in Levain's favour, but the doctor line is now wrong**: `LID_NOTE` and the module docstring say the opposite. The desk files a fix row to rewrite them, and a follow-up to learn why (UNVERIFIED mechanism: `caffeinate -i` alone is not documented to hold lid-close sleep; a leftover assertion from something else is the likelier cause, so the desk re-reads `assertions_during.txt` first and re-runs once with it clean). |
| The three disagree (gap but Phill saw it awake; or no gap but pmset shows a sleep) | **INCONCLUSIVE**: not a pass. The desk logs all three raw, does not interpret, and asks Phill whether to repeat. |
| Holder absent at C7.3 | **SETUP FAIL**, not a result. |

## What to capture (the desk copies into `~/Desktop/c9-c7-run/c7/`)

`assertions_before.txt`, `assertions_during.txt`, `heartbeat.log`, `pmset_log_window.txt` (`pmset -g log` filtered to the window, read-only), `batt_before.txt`, `batt_after.txt`, `doctor_during.txt`, and Phill's one-line observation. Then one episode: `episodic.py write --agent flow --type finding` with the verdict and the paths.

## Rollback / cleanup

`LEVAIN_HOME=/tmp/c7-home $L thread state THREAD done` (releases the holder; if the Mac is in a strange state the holder dies by itself within 1800 s); `rm -r /tmp/c7-home /tmp/c7-rehearsal`; stop the heartbeat; **relaunch Amphetamine** (it was holding the machine awake for flow's 24/7 duties; the desk does this and checks `pmset -g` says `sleep 0` again). Plug the charger back in. Nothing in `~/.levain` was touched.

## How a failure is reported

The desk files a band row (`needs_phill.py add`) only if a verdict needs a decision from Phill (the REFUTED row, or INCONCLUSIVE). A setup FAIL is logged in the run log and fixed first. A refuted premise also gets a levain seat brief for the doctor-line fix. Never a silent PASS: the verdict, the three sources and Phill's sentence go in one RESULT file `~/Desktop/c9-c7-run/c7/RESULT.md` and its path goes in the next desk message.

---

# Part 2. C9 one real coordinator day

**Row:** `STATUS_1_0.md` "C9 integration + one real day | HIS (a run) ... his day, with C4's panel on main/released"; cut-gate line 760. **BUILD_PLAN DONE line** (`project_memory/1020-coordinator/BUILD_PLAN.md`), which is the PASS bar, quoted:

> C9's run log shows each of: a drop split into >=2 threads with a receipt naming the split; an envelope signed at send covering a reversible build with no further prompt; one irreversible effect outside the envelope SIGNed from the phone; an away thread waiting UNDECIDED and shown on the morning card; a KNOW in the review queue cleared in each of the three modes. CI green on the exact sha; every fact-moving commit carries `Sibling-grep:`.

The last sentence (CI green, Sibling-grep) is the levain seat's discipline on any commit made during the day, not something Phill does; the desk checks it at the end.

## What the day looks like

You live Sunday normally. The coordinator is a Claude Code session Levain launches on a drop (`levain coord agent claude`); it splits what you drop into threads, works inside the envelope you sign, and escalates the rest. You drop from the phone Drop tab. The desk is the second pair of eyes on the stores and writes the run log. **Sunday is also the friction day** (np-a739498c): the prompts-logger session and the zero-prompts criterion (Q18 S-c) are a separate run on the same day, and the two share your attention, not the apparatus. Do not mix their logs; C9's log is `~/Desktop/c9-c7-run/c9/RUNLOG.md`.

**Use real work for the drops**, not toy tasks, or it is not a real day: the BUILD_PLAN says "one real day". Suggested real material the desk can have ready as a list in the DESK PREP (it must be his real items, never invented): UNVERIFIED which of his real items are drop-worthy Sunday; settle by asking him at C9.0 for 3 real things he would drop.

## DESK PREP (before you sit)

State today (read 11-07): `levain --version` -> `levain 0.11.28.dev0` from `~/Briefcase/levain/.venv/bin/levain` (`levain` is not on PATH); `levain daemon status` -> `com.levainhq.levain: installed=False running=False`; `levain coord status` -> `lease: nobody holds the coordinator role; next drop: unset (no coordinator agent is set)`; `~/.levain/coordinator` and `~/.levain/coordinator-shadow` exist (seed/activation/CLAUDE.md).

1. **Run the released build, not the dev tree.** The row says "on main/released". Tag `v0.11.28` exists (`git tag --sort=-creatordate` -> v0.11.28). The checkout is at `0.11.28.dev0`, i.e. AFTER the release. Install the released wheel into a clean venv the desk owns: `python3 -m venv ~/.levain-c9-venv && ~/.levain-c9-venv/bin/pip install levain==0.11.28`, and use `L9=~/.levain-c9-venv/bin/levain`. UNVERIFIED: that 0.11.28 is on PyPI (the neocortex says "levain 0.11.28 out"; settle with `pip index versions levain` or the install itself). If not on PyPI, `pip install` from the tag: `pip install "git+file:///Users/phillipclapham/Briefcase/levain@v0.11.28"`.
2. **No coordinator state leaks from a rehearsal.** Run the day on the real `~/.levain` (it IS the real day; the store is the one the phone talks to). Make a backup first: `cp -R ~/.levain ~/Desktop/c9-c7-run/c9/levain-home-backup` (small; excludes nothing; does not contain the key, which lives in the keychain, UNVERIFIED; settle with `ls ~/.levain/keys`).
3. **Choose the coordinator.** `$L9 coord agent claude` (once). It prints a line to trust the workspace `~/.levain/coordinator` in Claude Code: the desk runs `claude` in that dir once, or Phill accepts the trust prompt at C9.1. Source: `coord agent --help` and `C10_DESIGN.md` section 5 step 1.
4. **Serve as a launchd agent.** `$L9 daemon install` (the serve `--write` always-on; it also runs the coordinator watcher, C10 S1). Verify: `$L9 daemon status` -> installed=True running=True. If it is not wanted as a standing install, run `$L9 serve --write --no-open` in the background instead and say so in the log. UNVERIFIED: whether `daemon install` needs flags for the write token / port; settle with `$L9 daemon would-install` (dry run).
5. **Phone pairing and reach.** The cockpit pairs with `$L9 cockpit pair --short` (code valid 10 minutes). `serve` is loopback-only (`--host` refuses non-loopback), so the phone cannot reach it directly. UNVERIFIED: how the phone reaches this serve (Tailscale proxy? the cockpit relay?); settle by reading `~/Briefcase/levain/docs/cockpit-contract.md` and `project_memory/1029-9/C10_DESIGN.md` and running `$L9 cockpit pair --list` to see whether a phone is already paired. If a phone is already paired (he signed from the iPhone on 11-06 per the neocortex), no new pairing is needed.
6. **Away mode starts OFF** (`$L9 consent away` shows state). The away step turns it ON; leaving it takes his signature, so the desk checks the starting state and notes it.
7. **Run-log skeleton** at `~/Desktop/c9-c7-run/c9/RUNLOG.md` with the six DONE lines as empty rows (below) and a timestamp column. The desk fills each row from store reads, never from memory: `$L9 drops list`, `$L9 thread list --all`, `$L9 envelope list THREAD`, `$L9 coord morning`, `$L9 coord review list`, `$L9 coord log`.
8. **Leave off during the day:** no `levain coord take` by hand (that is the operator holding the role himself and voids the launch path); no second coordinator; no seat stands in for the coordinator.
9. **A safe irreversible effect for step C9.5** (see there): pick a real, reversible-in-practice but outside-the-envelope action so a SIGN is exercised without harm; the cleanest is a `levain push` of a throwaway branch of a scratch repo to a scratch remote the desk creates (`git init --bare /tmp/c9-remote.git`). The envelope in C9.3 names the real repo only, so the push is outside it. Source: `levain push --help` ("never a force, a delete or a tag"). Cleanup is `rm -r /tmp/c9-remote.git`.

## YOUR STEPS (top to bottom)

- [ ] **C9.0** You name 3 real things to drop today (one build-ish and reversible, one that includes an irreversible edge, one note). Desk writes them in the log. *Sees:* the list in the desk message.
- [ ] **C9.1** From your phone Drop tab, drop **one message containing two separate intents** (for example a build and a question about it). *Expected:* a drop receipt on the phone; the coordinator session starts (a pending launch is started by the watcher: `coord status` shows a launch `launching` then `held`); no prompt reaches you for that. *DONE-line 1 evidence:* `drops show DROP` lists >=2 threads and a drop receipt naming the split. *Fail if:* no coordinator starts within about 2 minutes (the watcher runs every 30 s), or the receipt does not name a split.
- [ ] **C9.2** Open the phone (or cockpit) Needs/Review view. *Expected:* the envelope for the build thread arrives as a worded envelope to sign (`envelope echo`), not a diff of the plan: "sign the envelope, not the plan".
- [ ] **C9.3** **Sign the envelope** with Face ID on the phone. *Expected:* the coordinator then does a reversible build step (commit on a thread branch, run tests) **with no further prompt** for the covered work. *DONE-line 2 evidence:* `envelope show` is live and signed; the run log shows the coordinator's covered steps with no consent request in `consent` for them. *Fail if:* any permission prompt interrupts a covered reversible step (that is also a friction finding; log it for Q18).
- [ ] **C9.4** Let it work while you do your day. At two points the desk asks nothing of you.
- [ ] **C9.5** The irreversible effect: tell the coordinator (by a second drop) to push the scratch branch to the scratch remote, outside the envelope. *Expected:* a SIGN request arrives on your phone as a Needs card; the coordinator does not push before you sign. **Sign it from the phone** (Face ID). *Expected:* the push lands (`git -C <scratch> log` on the bare remote shows the commit) and the card clears. *DONE-line 3 evidence:* `consent` ledger shows the request answered by a signed phone answer. *Fail if:* it pushes without a sign, or the card never reaches the phone.
- [ ] **C9.6** Say it in a drop: "heading out for the night" (the away word). *Expected:* away turns ON (`coord away` reads the drop as your word; "Leaving away takes his signature"); a KNOW is filed saying so; from now on every SIGN waits UNDECIDED. Then have the desk place one more SIGN-class request (another push) and **do not sign it**. *DONE-line 4 evidence:* that request sits UNDECIDED and appears on `coord morning`.
- [ ] **C9.7** Next morning (or after a deliberate wait of at least 30 minutes if the day is the only slot, flagged as such), open the morning card on the phone (Review tab) or `coord morning`. *Expected:* away, what waits UNDECIDED (the unsigned push), thread budgets, what waits on you, all on the card. *DONE-line 4 evidence completes here.*
- [ ] **C9.8** Clear three review-queue KNOWs, one in each mode: **manual** (you clear it), **fresh** (you ask a fresh session to look and it clears), **coordinator** (the coordinator's own mode; refused for an authority-relevant item, so use a non-authority one). *Expected:* each clears, and the refusal appears if you try coordinator mode on an authority item (a good negative check). *DONE-line 5 evidence:* `coord review list` shows three cleared items with their three modes. Source: `coord review --help`. UNVERIFIED: how "fresh" is invoked from the CLI; settle with `$L9 coord review clear --help`.

## PASS / FAIL

PASS = every one of the five run-log lines has evidence read from the stores, plus the sixth (CI green on the exact sha, each fact-moving commit carries `Sibling-grep:`) checked by the desk with `git log` and the CI run for any commit the day made. FAIL = any line missing. A partial day is reported as "C9 n of 5 lines", not as a pass. A step that failed gets the DONE-line it blocks named.

## What to capture

`RUNLOG.md` (one row per line: evidence command, its output, time), `drops_list.txt`, `thread_list.txt`, `envelope_show.txt`, `coord_morning.txt`, `coord_review_list.txt`, `coord_log.txt` and a `coord status` before and after, plus any permission prompt that interrupted a covered step. Then episodes: `finding` for each line's result, `observation` (tagged `felt`) for anything Phill says about how it felt, in his words.

## Rollback / cleanup

`$L9 coord release` (ends the lease), `$L9 coord agent none`, stop or uninstall the serve if installed only for the day (`$L9 daemon uninstall`), `$L9 envelope kill ENV` for any live envelope, `rm -r /tmp/c9-remote.git`, delete the scratch branch, `$L9 consent away` check that away is back to its starting state **only by his signature** (the desk never turns it off), restore `~/Desktop/c9-c7-run/c9/levain-home-backup` only if the store is corrupted (compare first). The venv `~/.levain-c9-venv` may stay.

## How a failure is reported

Each failed line: the desk writes the row in `RUNLOG.md` with the exact output, files a levain seat brief named for the DONE-line it blocks (not a summary of Phill's mood), writes one band row if it needs Phill to decide (for example whether C9 repeats), and an episode. A coordinator that stops three times files ONE band row by design (C10 S4); that is expected and is itself C10's residue check. The desk never edits `STATUS_1_0.md` rows from this run without the stores' evidence in hand, and never marks C9 or C7 done on a partial run.

# Part 3. Levain hands checks (folded in 11-07 16:08)

Signed np-4fe004e5 (a), 11-07 16:08: "Fold the hands checks into Sunday's friction day (np-a739498c) and keep every default and word as built". These are the Levain rows that only his hands, eyes or sign-ins can close. Source rows: `~/Briefcase/levain/project_memory/STATUS_1_0.md` LANE MENU rows 9, 20, 33, 69 and 104 (`awk 'index($0,"69. ")==1' STATUS_1_0.md` after line 562 prints each) [judged by levain 60 lane, 11-07, against those rows]. **Nothing here has been run.** Where a step is not derivable it says **UNVERIFIED** and what settles it. Order matters only for item H4 (it waits on an install) and H2 (it waits on Z2b being built and installed).

State at 11-07 (derived): origin/main is tagged `v0.11.28` (`git -C ~/Briefcase/levain tag --sort=-creatordate | head -1`); Z2b is NOT on main (`git -C ~/Briefcase/levain merge-base --is-ancestor seat/1107-20-z2b origin/main` -> exit 1, "z2b-not-on-main"); one VM named `vend-real-7a6895a7` is running (`tart list`).

## Levain hands checks (folded in 11-07 16:08)

- [ ] **H1. The VM sitting: Claude then Codex sign-ins on one re-staged VM** (rows 69 and 104).\
  *Desk stages first:* clones `p72-vm` into a `vend-` named VM with `~/Briefcase/levain/project_memory/1105-17/vmend/vmend.py` (it refuses any non-`vend-` name), tree = origin/main at the time, opens the VM window on your screen, and puts the two launchers on the guest desktop (`A1_Sign_in_Claude.command`, `A2_Sign_in_Codex.command`) [row 69 V1 brief, `1105-36/RETURN_remaining_a.md`; launcher names from `1106-16/v1/SIGNIN_STEPS.txt`]. Desk first runs `tart list` and `check_signins.sh` (`1106-16/v1/`) and tells you whether the 11-06 VM `vend-real-7a6895a7` already shows both signed in; if it does, the desk says so and asks you whether to re-stage anyway (row 69 says "re-staged VM").\
  *You do (at the laptop, inside the VM window):* (1) double-click `A1_Sign_in_Claude.command`; (2) type `/login`, Enter, Enter (option 1, Claude account), cmd-click the URL (opens Safari in the VM), sign in, copy the short code, paste it into the Terminal, Enter; (3) when it says logged in, type `/exit` and answer yes at Levain's identity confirm; (4) double-click `A2_Sign_in_Codex.command`, press Down once ("Sign in with Device Code"), Enter; (5) cmd-click the URL, sign in to ChatGPT in the VM Safari, type the one-time code shown in the Terminal (15 minute limit); (6) press Ctrl-C to end Codex, answer yes at Levain's confirm; (7) tell the desk "signed in" and leave the VM window open [steps copied from `1106-16/v1/SIGNIN_STEPS.txt`, the 11-06 sitting's instructions; UNVERIFIED: whether the launcher text and menu wording are unchanged on a fresh re-stage].\
  *Pass looks like:* desk runs `~/Briefcase/levain/project_memory/1106-16/v1/check_signins.sh` and it prints `RESULT: both signed in (probes passed)` (the fixed script, `1106-16/v1/RETURN_row54.md`), Claude in slot 8, Codex in slot 9.\
  *Write down:* the Codex slot e9 label. UNVERIFIED which label that is: `1106-16/v1/RETURN_row54.md` says the baseline on 11-06 was `"observed_id":"not-a-check"` and that this is by design for Codex (Levain reads no Codex account), while row 104 says the 11-06 driver "ended asking him to record the slot's label"; you say what label you want recorded and the desk writes it, with the `slots/e9/baseline` contents beside it. Also write down the time, and anything the VM asked you that is not in step list above.\
  *Desk afterwards (not yours):* the rows this sitting closes if it passes are 7, 39, 51, 53 and 54 and the identity gate (row 69); the desk runs the X1 real Codex END and the C1p / identity-step rows with raw output [row 69 V1 brief].

- [ ] **H2. A throwaway read-only GitHub token for the Z2b VM check** (row 104). **Blocked until Z2b is built and installed**: Z2b is `seat/1107-20-z2b`, not on main (command above). If it is still not installed Sunday, skip this item and write "H2 not reached: Z2b not installed" in the log; do NOT mint a token for a command that does not exist yet (row 104 D8: "he mints the token only when Z2b is built").\
  *Desk stages first:* confirms `levain seat gh-token --help` exists in the installed levain, then says which levain home the tokens go under (`seat_lease._levain_home()`); on the VM that is the VM's home, UNVERIFIED which home Z2b's VM check is meant to use (the frame does not say; settles it: the desk's Z2b return, `1107-15-voice/RETURN_z2b_build.md` UNVERIFIED item 3, and your word).\
  *You do (github.com in your browser, then one terminal):* open github.com/settings/personal-access-tokens and mint TWO fine-grained tokens, each read-only, ONE repository each, 90 days: (a) resource owner `levainhq`, repository `levain` (levainhq is an organisation; UNVERIFIED that it is, read the owner field on the mint page: if levainhq and phillipclapham are the same owner one token suffices, `Z2B_FRAME.md` line 27); (b) resource owner `phillipclapham`, repository `flow`. Repository permissions for both: Contents, Metadata, Pull requests, Issues, Actions, each Read-only; nothing else. Then at the hidden prompt, paste each token: `levain seat gh-token set --owner levainhq --expires YYYY-MM-DD` and `levain seat gh-token set --owner phillipclapham --expires YYYY-MM-DD` (the date on the mint page; used only if GitHub's reply carries no expiry) [commands and permission list from `~/Briefcase/.levain-1107-20-z2b/project_memory/1107-15-voice/RETURN_z2b_build.md`, "Phill's commands"]. Never paste a token into chat or an argument.\
  *Pass looks like:* `levain seat gh-token status` prints a fingerprint and an expiry for each owner and never the token; each `set` printed its confirmation after the GET from the host. A bad token should refuse with fixed words ("seat gh-token refused: ...", `cli.py` on the Z2b branch); UNVERIFIED what the refusal text says for each case.\
  *Write down:* the two expiry dates, each token's name on the mint page (so it can be revoked), and whether the expiry came from GitHub's header or from your `--expires`. Revoke later with `levain seat gh-token clear --owner <owner>` and delete the token on GitHub. The "VM half" itself (what the seat does on the VM with the door) is UNVERIFIED: the Z2b return lists the live checks it owes (a real token against api.github.com, the seat loading `levain_gh`, the sandbox denying `seat_gh`); the desk writes the exact command when Z2b is installed.

- [ ] **H3. First real Room chat, then a glass look at the voice pieces** (row 20, V1-0 and V1-5..V1-8).\
  *Desk stages first:* installs main (the release you are on, `v0.11.28` or later) and starts the room: `levain serve --write --room <dir>` [`1104-voice/V1_DESIGN.md` line 44, V1-0]. UNVERIFIED: the exact running URL and `<dir>`; the desk prints the tailnet URL for the iPhone (`python3 ~/Briefcase/flow/scripts/review_url.py`) before you start. Desk confirms a voice key is on file if you want premium voice (premium needs a Signer today, row 20 / row 50, UNVERIFIED whether that has since changed) and that the free voice works without one.\
  *You do:* (1) on the laptop type a line to Claude in the Room, read the reply, tap Stop once; (2) the same on the iPhone; (3) then the glass look at the voice pieces, per the V1 design's hands column: autoplay after Send; plays with the silent switch on; interrupt halfway; the "Tap to hear" degrade after a remount; AirPods; whether the Nova voice sounds right; push-to-talk: mic permission re-asked per launch on the iPhone and transcript quality in your voice; open a card from a notification with the setting on and see whether it plays without a page tap or shows "Tap to hear this card" [`V1_DESIGN.md` rows V1-5, V1-6, V1-7]. UNVERIFIED: which of these pieces are in the installed release (row 20 says landing 9e is on main and the Deepgram picker and speed pin too, but the default provider change, row 62, is not on main as of the row); the desk lists what is installed before you start (UNVERIFIED: the command that lists it) and skips the rest.\
  *Pass looks like:* a real Claude answer to a typed line, read by you, on both devices (V1-0's own pass line: "he typed a line on the laptop and on the iPhone, read the reply and tapped Stop"), and for each voice piece you say works / does not / did not try.\
  *Write down:* what you typed and what you saw, per device; the felt response in your words (the desk logs it as a `felt` episode); anything you want changed on the page.

- [ ] **H4. iPhone band-push tap, warm and cold** (row 33). **Blocked on the install.** Both levain-side gaps are on main but UNRELEASED as of the row; this item runs only after a release that contains them is installed in the server you tap. Settles whether it is: the desk runs `git -C ~/Briefcase/levain merge-base --is-ancestor f752261e v0.11.28` and the same for `8c1fe768` (a `y` for both means the tag carries them) and `diff -rq` the installed levain against that tag [method from row 33]; if not installed, skip and write "H4 not reached: not installed".\
  *Desk stages first:* re-pairs the iPhone app first (write token) [row 33], files ONE test band row for the tap, and has `/tap_trace` readable.\
  *You do (iPhone):* (1) tap the test band-push notification with the app in the background (warm); (2) swipe the app away and send a second test row, then tap that notification (cold) [row 33].\
  *Pass looks like:* each tap lands on the centre focus view on that row inside the app (not on the home page), and `/tap_trace` shows the tap [row 33: "success = the centre focus view on that row inside the app, read `/tap_trace`"].\
  *Write down:* warm result, cold result, and the question row 33 carries: did you mean the center item or the sign sheet by "the card" (your answer decides which the push should open).

- [ ] **H5. Your go on the 3b-8a cleanup-journal VM run** (row 9). This is your word, not hands work: the cleanup journal and the VM driver are on main and the driver's first VM run was PARTIAL (`1106-16/RETURN_vm_driver.md`: RESULT PASS with host-side adapters the lane wrote, because `scripts/vm_harness/driver.py` has no real adapters in the repo) [row 9].\
  *Desk stages first:* names the VM it would clone (`p72-vm` is the one that worked; `vdis-mac` carries a stranded HELD session and refused `install`, `RETURN_vm_driver.md` finding), states which of the two harness defects (rig seed writes no identity seed; rig ships an old fake) are fixed first, and says the run clones and deletes its own `vend-` VM and leaves `tart list` as it found it.\
  *You do:* say "go" or "not yet" in the one message the desk sends. Nothing else.\
  *Pass looks like:* (after your go) `RESULT.txt` shows a header line then `RESULT: PASS` (the shape in `1106-16/vm_driver/driver_run1/RESULT.txt`), with the adapters being the real ones if they exist by then, otherwise it is again PARTIAL; UNVERIFIED whether the three interface decisions of `1029-5/CLEANUP_SCOPE.md` are settled by the driver (row 9 says not re-derived).\
  *Write down:* "go" or "not yet" with the time, in your words.

---

# Open questions the run cannot answer from here (all UNVERIFIED above, collected)

1. Whether `powerd` / `sharingd` assertions block lid-close sleep (settled by reading the full `pmset -g assertions` with Amphetamine off).
2. Whether `LEVAIN_HOME=/tmp/c7-home` is honoured end to end by `drop`, `drops`, `thread` and `doctor` (settled by the rehearsal in C7 DESK PREP 6).
3. Whether `drops split --by operator` needs a token (the same rehearsal).
4. Whether 0.11.28 is on PyPI (`pip index versions levain`).
5. How the phone reaches the loopback-only serve (`docs/cockpit-contract.md`, `cockpit pair --list`).
6. How `coord review clear` invokes the "fresh" mode (`coord review clear --help`).
7. Whether `daemon install` needs flags (`daemon would-install`).
8. Which of Phill's real items Sunday are drop-worthy (his answer at C9.0).
9. (Part 3, H1) Which Codex slot e9 label to record, and whether a fresh re-stage repeats the 11-06 launcher wording.
10. (Part 3, H2) Whether levainhq is an organisation distinct from phillipclapham; which levain home Z2b's VM check uses; the exact VM-half command; the refusal text per bad-token case.
11. (Part 3, H3) The room URL and directory; which voice pieces the installed release carries; whether premium voice still needs a Signer.
12. (Part 3, H4) Whether the installed release carries f752261e and 8c1fe768 (blocked on the install).
13. (Part 3, H5) Whether the driver has real adapters yet; whether the three `CLEANUP_SCOPE.md` interface decisions are settled.
