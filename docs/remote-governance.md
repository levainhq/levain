# Remote governance: which terminal a band answer came from

A band answer typed at the terminal is signed with the laptop's Touch ID key (`levain/consent/terminal_sign.py`).
That sheet opens on the laptop, so an answer typed from a terminal the operator reaches over ssh, mosh or
`claude attach` must never raise it: nobody is there to touch it, and it would only time out. This page is the table of
which surfaces the code recognises as remote, what a band answer typed on each one does, and what is left ungoverned.

Every claim below carries the test that re-derives it (run from the repo root:
`python -m pytest tests/test_remote_detect_1105.py -q`) or a `[judged by ...]` tag naming who judged it against what.
Measurements were taken on loopback with a scratch sshd and mosh and stubs, never on a phone:
`[judged by levain 41, 2026-11-05, against project_memory/1105-31/RETURN_remotegov_m1.md]`.

## The detector

One function decides, `terminal_sign.remote_reason(env, presence)`, and `terminal_sign.is_remote(...)` is
`remote_reason(...) is not None`. Both live UserPromptSubmit hooks reach it with the same call: `levain.band_hook`
through `band_view._terminal_signer`, and flow's `scripts/termsign_hook.py`, which calls `ts.is_remote()` with no
arguments (`tests/test_remote_detect_1105.py::test_is_remote_without_arguments_reads_os_environ`,
`::test_is_remote_without_arguments_sees_the_ancestry`,
`::test_is_remote_without_arguments_sees_an_attach_client_of_the_environs_session`). A desk is remote when any leg fires:

| leg | what it reads | reason it names | test |
|---|---|---|---|
| env | exactly `SSH_CONNECTION`, `SSH_TTY` or `SSH_CLIENT` in the hook's environment (`SSH_AUTH_SOCK`, `P9K_SSH`, `_P9K_SSH_TTY` are not signals) | `env SSH_CLIENT` ... | `::test_the_three_exact_env_names_are_signals`, `::test_names_that_only_look_like_ssh_are_not_signals` |
| ancestry | an ancestor of the hook process whose argv[0] basename is exactly `sshd`, `sshd-session` or `mosh-server` (one trailing colon of a rewritten process title is dropped), within the hop cap of the one ancestry walker, `levain.consent.band._ancestors` | `ancestor process sshd-session` | `::test_ancestry_with_sshd_session_is_remote_with_env_scrubbed`, `::test_ancestry_with_mosh_server_is_remote_with_env_scrubbed`, `::test_only_the_exact_names_count_as_a_remote_ancestor`, `::test_local_terminal_ancestry_is_not_remote` |
| attach client | the hook env names its desk (`CLAUDE_CODE_SESSION_ID`) and a `ps` row is `claude attach <id>` with `<id>` a prefix of at least eight characters of that id, and that client's own ancestry holds one of the three names | `attach client 1e37cf45 reached from sshd-session` | `::test_attach_client_under_sshd_makes_the_session_remote`, `::test_attach_client_under_mosh_server_makes_the_session_remote`, `::test_attach_client_for_another_session_is_not_remote`, `::test_local_attach_client_is_not_remote` |
| agent view client | the hook env names its desk and `ps` shows a `claude agents` client (or an id-matching `claude attach`) on a real tty whose own ancestry holds one of the three names. The prompt is tied to its client by the keystroke: the client's tty and the session's tty are both read for one keystroke, so their access times sit within `TIE_S` (2 s: usually milliseconds, but the kernel defers an update made in the same second as the last one and stamps it at the next stat, measured up to 1.006 s off) of each other and the session's is under `TIE_RECENT_S` (60 s) old; one tied client decides, several tied read remote if ANY is remote, so a remote client read within that window for any reason makes the prompt remote. With no client tied the fallback is: a remote `claude agents` client is open AND the laptop keyboard and mouse have been idle for at least `IDLE_S` (60 s); an unreadable idle time cannot show the laptop in use, so it reads remote | `agent view client ttys002 reached from mosh-server, its input matched this session's to 1 ms`, or `fallback: remote agent view client ttys002 reached from mosh-server, laptop idle 120 s` (or `laptop activity unreadable`) | `tests/test_remote_detect_agentview_1106.py` |
| presence | the caller's presence text says remote (neither live hook passes one: [judged by levain 41, 2026-11-05, against `git grep -n "is_remote("` in levain and flow's `scripts/termsign_hook.py`]) | `presence says remote` | `::test_presence_remote_is_a_signal_and_other_presence_is_not` |

Routing only, never evidence: a `ps` that fails, times out or prints something unparseable reads as local for that
leg, and nothing raises (`::test_ps_that_fails_or_times_out_is_not_remote`, `::test_ps_garbage_is_not_remote`,
`::test_a_walker_that_raises_is_not_remote_and_does_not_raise`). The cost of the two errors is not equal: a false
"local" raises a sheet nobody touches, which times out, and a false "remote" costs one signature on the phone.

What a remote desk changes is the sheet and the words, nothing else. The answer is still recorded and the row still
closes, with sign state `unsigned/remote` (`::test_attached_from_ssh_desk_never_reaches_the_sheet_and_promises_no_card`;
the kernel principle, quoted in `band_view`, is that signing never blocks recording). The echo is unchanged, `recorded UNSIGNED (the desk is remote, so no Touch ID prompt was raised)`, and promises no phone card: a card for a Levain-store answer needs a producer and a consumer that do not exist yet (`::test_attached_from_ssh_desk_never_reaches_the_sheet_and_promises_no_card` pins the absence of the promise). The outcome's `detail` names the leg that fired, in process only: the answer's stored sidecar keeps the
reason (`remote`), not the detail.

## The surfaces

| surface | how it is detected | a band answer typed there | ungoverned |
|---|---|---|---|
| local terminal | no leg fires (`::test_local_terminal_ancestry_is_not_remote`) | Touch ID sheet on the laptop; signed if touched, else `unsigned/timeout`; recorded either way (`::test_the_same_desk_with_a_local_attach_client_calls_the_signer_once`) | nothing new: the signature is the only evidence, the typed line is the operator's |
| `claude` run over ssh | env leg (`SSH_CONNECTION`, `SSH_CLIENT`, `SSH_TTY` all reached the hook, `[judged by levain 41, 2026-11-05, against F2 in RETURN_remotegov_m1.md]`), and the ancestry leg when the env is scrubbed (`::test_ssh_run_with_the_env_scrubbed_never_reaches_the_sheet`) | no sheet; recorded `unsigned/remote`; the echo says only that it is unsigned and why | the answer is recorded unsigned, so the row closes without a signature |
| `claude` run over mosh | the same env names (inherited from the ssh bootstrap, stale after a roam: `[judged by levain 41, 2026-11-05, against F2]`) and the ancestry leg through `mosh-server`, which is reparented to launchd with no sshd in the chain (`::test_ancestry_with_mosh_server_is_remote_with_env_scrubbed`) | as above | as above; tmux or screen on top of either was not measured |
| `claude attach <id>` from ssh (the desk) | the attach-client leg: the hook runs in the daemon-spawned desk, whose environment and ancestry never mention the client, so the client is found by its `ps` row (`::test_attach_client_under_sshd_makes_the_session_remote`, end to end `::test_attached_from_ssh_desk_never_reaches_the_sheet_and_promises_no_card`) | as above | an attach by id from mosh reads the same way (`::test_attach_client_under_mosh_server_makes_the_session_remote`) but was not run for real; two clients on one desk read remote if ANY is remote (`::test_two_clients_one_local_one_remote_reads_remote`), so a laptop answer typed while an ssh client is also attached is routed to the phone |
| `claude attach <name>` | NOT detected: a client attached by name carries no session id in its argv (`::test_a_client_without_a_matching_id_in_argv_is_not_seen`) | read as local: the sheet opens on the laptop and times out; recorded `unsigned/timeout` | the whole surface: a name-attached remote client is as before this change |
| Claude mobile (Anthropic's remote control) | NOT detected by levain: no leg looks for it. Flow classifies a `queued` turn with `queueSkipSlashCommands` as remote control (flow decides; `[judged by levain 41, 2026-11-05, against the quoted peer_roster lines in RETURN_remotegov_m1.md F3]`) | not measured: whether such a turn reaches `levain.band_hook` as a prompt, and with what environment, is UNMEASURED | all of it; levain does not govern this input. The repo only lists `--remote-control` among the claude flags it passes through (`levain/consent/delivery.py`) |
| the cockpit on his phone | not a terminal: a click on a row goes through the cockpit, not the hook. A row carries a passkey sign card only where a producer filed a sign request (`Question.sign_request`; flow files them for its own band, levain files only `LEVAIN_SIGN_REQUESTS`' one review-clear grammar) `[judged by levain 41, 2026-11-05, against F4 in RETURN_remotegov_m1.md]` | Face ID passkey on the phone, where a card exists | a levain-store band row has no phone sign card today (no terminal-answer grammar in `LEVAIN_SIGN_REQUESTS`), so the echo for a remote answer does not mention a phone card; it would be true only for rows a producer has filed |

## Away and sleep: not this repo's half

Whether a typed turn ends the away or sleep presence is counted by flow, not here: flow's `peer_roster` stamps the
turn and `phone_bridge` publishes it to levain, which stores it and reads it back as `desk_msg_at`
(`levain/webpush/desk.py` `note_desk_message`, `levain/webpush/intent.py`). Before flow's `remote_stamp`, an ssh-, mosh- or
attach-typed turn into the desk session ended away and sleep, because nothing in the transcript tells those turns apart
from a laptop-typed one `[measured by levain 41, 2026-11-05, F3 and F5 in RETURN_remotegov_m1.md]`. Flow's own hook now
stamps each prompt this repo's detector (`terminal_sign.is_remote`) calls remote, keyed by the hook's `prompt_id`, which
equals the transcript's `promptId`, and `peer_roster` reads a stamped turn as `last_remote_terminal`, not the laptop's
keyboard `[judged by levain 41, 2026-11-05, against flow main 4eaa53fb: scripts/remote_stamp.py and scripts/termsign_hook.py]`.
That half is flow's and its tests are flow's; this repo's part is only that the detector it asks now sees an attached desk.

## Known edges

- A client attached by name is not seen (above); so is anything that runs the desk with no ssh, mosh or attach
  ancestry, such as a VPN-reached laptop session: that is a local terminal as far as the process tree can tell.
- Flow's hook calls `is_remote()` with no arguments, so it matches an attach client only by the session id in its own
  environment (`CLAUDE_CODE_SESSION_ID`); levain's band hook also passes the hook payload's id. A daemon-backed attach
  whose hook environment carries no session id is seen by levain's hook and not by flow's, so flow's away/sleep stamp
  can still read that turn as the laptop's keyboard `[judged by levain 43, 2026-11-05, against the L3 r2 codex finding
  in project_memory/1105-31/L3_remotegov_r2.txt]`; forwarding the payload id from flow's hook is flow's change.
- The detector reads the process tree as `ps` prints it. A build of sshd that stops rewriting its title, or a
  `claude` binary that renames the `attach` subcommand, stops matching; the failure is the status quo, a sheet that
  times out.
- Residue, no instrument here holds it: answering one band item from a real phone terminal over ssh, mosh and
  `claude attach` and seeing no sheet on the laptop and the new words in the echo.

### Known edge: the agent view peek panel (intent only, not measured)

A reply typed in the agent view peek panel (sent "without leaving agent view") may not travel as bytes read from the viewed session's tty, so no client's tty would tie. Whether it does has not been measured. The intent is that such a reply lands on the fallback (a remote agent view client is open and the laptop is idle); with the laptop busy it reads local and the sheet opens unseen. Real mosh and Blink were not run either: the tie rests on the measured laptop agent view and a pty experiment.

## The sign card (B2)

A band answer typed at a remote desk is recorded at once and unsigned (`sign_reason='remote'`); nothing delays or gates it.
After the write the seat hook files one card per typed line in the cockpit Signer's queue
(`levain/consent/answer_countersign.py`): levain's own plain request in the `levain-answer-countersign` namespace, whose
message is the ordinary `levain-terminal-answer/1` bytes plus a `countersign-of:` line naming the recorded answers. The
queue dir is `$LEVAIN_SIGN_QUEUE_DIR`, else the `sign_queue_dir` key of the hook queue's `claude_code.json`; with neither,
no card is filed and the echo says why. The echo promises "sign it on your phone" only when the card was filed, pointed at
and the push child started (which says nothing of arrival). The card is evidence only.

A band render reads the card back, with no poller: `countersigned on your phone <time>` only when the passkey signature
verifies over exactly the recorded answer; `remote, unsigned` while the card is open, expired or was never filed;
`DISOWNED on your phone` after a deny; `countersign FAILED: <reason>` for anything else. A bad signature, a missing
card or any error is never countersigned. The local Touch ID path files nothing and reads as before.

Known edges. The card's pointer write waits for the question store at most `HOOK_BUSY_MS`; a store held past that
leaves a filed card nothing points at (it lapses unsigned) and the echo says the store stayed busy. A push that could not
be started is not retried: the echo says so and the answer reads `remote, unsigned`. The badge finds the signing queue
from the reading process's settings, so moving `sign_queue_dir` after a card was filed makes that card read as gone
(`countersign FAILED`) or, with no queue named, `remote, unsigned`, never as signed; the queue path is not stored beside the answer on purpose, since the passkeys the
verifier trusts are read from that queue. Filing is idempotent only while the card is open; nothing refiles, because a
recorded answer is never answered again.

Residue, no instrument here holds it: a line typed from a real phone terminal, the push arriving on the phone, Face ID on
the sheet and the badge turning on the laptop; and the live `sign_queue_dir` key in the hook queue's `claude_code.json`.
