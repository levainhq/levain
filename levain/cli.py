"""Levain CLI — `levain init`, `levain doctor`, `levain verify-hooks`.

The CLI that `levain.launch.main` (the entry point declared by `pyproject.toml`, [project.scripts]
levain) hands over to.
Subcommand handlers live in sibling modules; this file is dispatch only.
Lazy imports keep `levain --help` fast and isolate import errors per command.
"""

from __future__ import annotations

import argparse
import math
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from levain import __version__
from levain.chat import DEFAULT_TURN_SECONDS


def _positive_int(text: str) -> int:
    """An argparse type for a count that must be at least 1 (the SDK refuses 0 and below only when a
    session opens, long after the server has bound)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _positive_seconds(text: str) -> float:
    """An argparse type for a wall-clock bound: a finite number of seconds above 0 that a thread can
    wait for. ``nan`` and ``inf`` are refused because both read as a bound and neither ever fires;
    a value past ``threading.TIMEOUT_MAX`` because the watcher's wait raises on it instead of
    waiting, which would leave the turn with no bound at all."""
    import threading

    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds above 0, got {text!r}")
    if value > threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be at most {threading.TIMEOUT_MAX:.0f} seconds (the longest a thread can wait), "
            f"got {text!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="levain",
        description=(
            "A consent queue and memory harness for Claude Code and Codex: permission prompts "
            "answered by signed standing grants or signed human answers, with an audit trail, "
            "plus memory that persists across sessions."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"levain {__version__}",
    )

    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", required=True
    )

    init_p = subparsers.add_parser(
        "init",
        help="Scaffold a new install: interview, render templates, init store.",
        description=(
            "Scaffold a new Levain install at PATH (default: cwd). Runs a "
            "scripted interview to fill the seed templates, resolves "
            "environment-dependent placeholders, lays down the chosen "
            "adapter(s), initializes the anneal-memory store."
        ),
    )
    init_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    init_p.add_argument(
        "--adapter",
        # ⚠ THIS LIST IS A DUPLICATE OF `install.KNOWN_ADAPTERS` AND IS PINNED TO IT BY
        # TEST, NOT BY IMPORT (2026-08-03). Deriving it here is the obvious move and it
        # is WRONG: this module's docstring promises "lazy imports keep `levain --help`
        # fast", parser construction runs on every invocation including --help, and
        # `levain.install` measures 18.5 ms to import against a 20-30 ms --help — so
        # deriving would roughly double the cost of the one path the laziness exists to
        # protect. `install.py:290` is the real gate and produces the better error;
        # argparse `choices=` only buys --help display and shell completion.
        # The drift is closed by `test_cli_adapter_choices_match_KNOWN_ADAPTERS`, which
        # pays the import at TEST time where it is free.
        choices=["claude-code", "codex", "openhands"],
        help=(
            "Harness adapter to install. Prompts if omitted. `claude-code` and "
            "`codex` wire a hosted harness via hooks; `openhands` scaffolds a "
            "sovereign, runnable entity (its own store + seed, NO hooks — the "
            "condenser is the activation) that you drive with `levain run`. "
            "v1 installs one adapter per install — to use both, create two "
            "separate installs."
        ),
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Allow installing into a non-empty directory. Default refuses "
            "to avoid clobbering an existing install."
        ),
    )
    init_p.add_argument(
        "--pack",
        action="append",
        type=Path,
        dest="pack",
        default=None,
        metavar="DIR",
        help=(
            "Pack-layer directory (pack.toml + seed/) to compose ON TOP of the "
            "base templates. Repeatable; higher pack `order` wins on a filename "
            "collision. Works with --web (the browser interview composes the pack "
            "too) and the terminal interview alike."
        ),
    )
    init_p.add_argument(
        "--web",
        action="store_true",
        help=(
            "Run onboarding in the browser instead of the terminal: serve a "
            "one-page, pre-filled form on localhost (loopback-only). The form "
            "collects the same interview the CLI does and runs the identical "
            "install on submit."
        ),
    )
    init_p.add_argument(
        "--port",
        type=int,
        default=7430,
        help="Port for `--web` to bind on localhost (default: 7430).",
    )
    init_p.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Loopback address for `--web` to bind (default: 127.0.0.1). "
            "Onboarding is loopback-only — a non-loopback address is refused."
        ),
    )
    init_p.add_argument(
        "--no-open",
        action="store_true",
        dest="no_open",
        help="With `--web`, do not open a browser tab on startup.",
    )
    init_p.add_argument(
        "--answers",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Run the interview NON-INTERACTIVELY from a JSON answer file: an "
            "object keyed by SLOT NAME (never an ordered list — answers are "
            "matched by name, so field order is not something you have to know). "
            "Requires --adapter, since the adapter menu would otherwise prompt. "
            "Every slot must be present; \"\" is accepted on every slot except "
            "OPERATOR_NAME and ENTITY_NAME, which require an answer (matching "
            "the validator — the terminal interview and the web form also accept "
            "blanks elsewhere). Get a blank one with --answers-template."
        ),
    )
    init_p.add_argument(
        "--answers-template",
        action="store_true",
        dest="answers_template",
        help=(
            "Print a blank --answers file (every slot this install's interview "
            "asks, mapped to \"\") to STDOUT, and the field guide to STDERR, then "
            "exit without writing anything. Composes with --pack. Usage: "
            "`levain init --answers-template > answers.json`."
        ),
    )
    init_p.add_argument(
        "--messaging",
        choices=["isolate", "governed", "off", "unmanaged"],
        default=None,
        help=(
            "Claude Code cross-session messaging (claude-code adapter only). `isolate` (the default): "
            "only isolatePeerMachines, so a send to a session beyond this machine asks; sends between "
            "sessions on this machine are not queued or recorded, and an incoming message gets Claude "
            "Code's own per-message default. `governed` (opt in): inbound messages are held and every "
            "outbound send asks. `off`: inbound refused, sending and "
            "listing denied. `unmanaged`: none of those keys written. Claude Code never counts a message "
            "from another session as your consent, whichever you choose. The choice is kept in "
            ".levain/config.json, so `levain update` and `init --force` keep it. To change it on an existing "
            "install, run `levain messaging set MODE` (it records the choice and runs `levain update`)."
        ),
    )
    init_p.add_argument(
        "--consent-hook",
        choices=["on", "off"],
        default=None,
        help=(
            "The consent hooks (claude-code and codex adapters). `on` (the default) wires levain's "
            "PermissionRequest hook, which answers only with a signed answer or a standing grant and otherwise "
            "lets the agent's own dialog ask you, and its gate on agent config and hook edits, which asks you "
            "through levain when a key can sign them and otherwise refuses them in any mode where the agent's "
            "dialog does not ask (`levain doctor` says how to allow them). `off` wires no consent hook, for a "
            "session that should run "
            "without one. The choice is kept in .levain/config.json, so `levain update` and `init --force` keep "
            "it; give --consent-hook again to change it."
        ),
    )
    init_p.add_argument(
        "--memory-load",
        action="append",
        default=None,
        metavar="MODE | ADAPTER=MODE",
        help=(
            "How the memory wrap procedure (seed/memory_workflow.md) reaches your agent. MODE is `eager` "
            "(the default: loaded with the rest of the seed, always in context) or `on-demand` (kept out of the "
            "always-on context; the carrier holds a short summary and the agent reads the file when it wraps). "
            "A bare MODE sets the install's default; ADAPTER=MODE (say `codex=on-demand`) sets one agent, and "
            "may be given again. The rest of the memory instructions (recording, recall) load either way. The "
            "choice is kept in .levain/config.json, so `levain update` and `init --force` keep it; give "
            "--memory-load again to change it."
        ),
    )
    init_p.set_defaults(func=_cmd_init)

    doc_p = subparsers.add_parser(
        "doctor",
        help="Loud, in-environment health check of an install.",
        description=(
            "Check that an install is wired correctly: interpreter resolves, "
            "MCP server is registered for the detected adapter(s), the store "
            "is reachable, the hook scripts are present and runnable. Exits "
            "nonzero on any failure so it composes with shell pipelines: 1 when "
            "something is wrong, 6 when every failure is a post-upgrade step not "
            "yet applied (until then the install keeps running what an earlier "
            "levain set up)."
        ),
    )
    doc_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory to check (default: cwd).",
    )
    doc_p.add_argument(
        "--invoke",
        action="store_true",
        help=(
            "After static checks, also invoke each hook script live (the "
            "verify-hooks dynamic check) to confirm hooks actually fire and "
            "emit valid output. Closes the 'doctor green but harness not "
            "invoking hooks' silent-skip class — particularly useful under "
            "Codex 0.132/0.133 where hook trust is per-content-hash."
        ),
    )
    doc_p.add_argument(
        "--repin-hooks",
        action="store_true",
        help=(
            "Record the agent hook entries and hook scripts as they are now, after you "
            "changed them on purpose. Doctor reports a changed pin until you do."
        ),
    )
    doc_p.set_defaults(func=_cmd_doctor)

    vh_p = subparsers.add_parser(
        "verify-hooks",
        help="Smoke-test the installed activation hooks for each adapter present.",
        description=(
            "Invoke each installed hook script with the JSON payload a "
            "harness would send and check the emitted `hookSpecificOutput` "
            "is well-formed and non-empty. Validates the script half of the "
            "hook contract independently of whether the harness actually "
            "invokes the hooks at runtime (notably useful for the Codex "
            "platform hook-reliability gap)."
        ),
    )
    vh_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory to verify (default: cwd).",
    )
    vh_p.set_defaults(func=_cmd_verify_hooks)

    dash_p = subparsers.add_parser(
        "dashboard",
        help="Read-only glance at the substrate from outside a session.",
        description=(
            "Render the install's anneal substrate — memory health, the "
            "association graph, crystallized patterns, open loops, and the "
            "State / Active Threads narrative — without opening a Claude Code "
            "or Codex session. Read-only: acts on nothing. --json emits the "
            "machine-readable SubstrateView (the shape the v2 MCP-App control-"
            "pane serves)."
        ),
    )
    dash_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    dash_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the SubstrateView as JSON instead of a terminal render.",
    )
    dash_p.set_defaults(func=_cmd_dashboard)

    tui_p = subparsers.add_parser(
        "tui",
        help="Interactive terminal control plane over the substrate.",
        description=(
            "Inspect and steer the install's substrate from a full-screen "
            "terminal UI — the Unix-terminal-native peer of `levain serve` (the "
            "browser surface). Navigate the Identity · Operate · Mind zones, read "
            "every panel, and (with the write verbs) edit Class-A operator inputs "
            "and run Class-B lifecycle verbs, through the same governed write seam "
            "the web-app uses. No server, no port, no browser; needs an "
            "interactive terminal (use `levain dashboard` for a non-interactive "
            "glance)."
        ),
    )
    tui_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    tui_p.add_argument(
        "--read-only",
        action="store_true",
        dest="read_only",
        help=(
            "Inspect-only: suppress every write verb so the footer advertises "
            "navigation only. A pure read-only control plane over the substrate "
            "(the mode a cockpit over a store with no governed write target uses)."
        ),
    )
    tui_p.add_argument(
        "--manifest",
        action="store_true",
        help=(
            "Render the shared cockpit manifest (the kernel's panels, order and groups) "
            "instead of the substrate view. Read-only."
        ),
    )
    tui_p.set_defaults(func=_cmd_tui)

    state_p = subparsers.add_parser(
        "state",
        help="Set / show your freeform state line — what's going on right now.",
        description=(
            "An optional, freeform line in your own words ('wiped, keep it light', "
            "'on the billing refactor') that every session reads at start and the "
            "cockpit shows, verbatim with its age. "
            "It is never parsed, scored or used to limit what the partner does; you are "
            "the authority on your own state. With TEXT, sets it; with no argument, shows "
            "it; --clear removes it. It expires after 12 hours: a stale state is dropped, "
            "not shown. Stored in .levain/context.json."
        ),
    )
    state_p.add_argument(
        "text", nargs="?", default=None,
        help="The state to set (omit to show the current one).",
    )
    state_p.add_argument(
        "--path", type=Path, default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    state_p.add_argument("--clear", action="store_true", help="Remove the state.")
    state_p.add_argument(
        "--source", default="cli",
        help="Provenance tag for who set it (default: cli).",
    )
    state_p.set_defaults(func=_cmd_state)

    from levain.authority import CUSTODIES as _CUSTODY_CHOICES

    _CUSTODY_HELP = ("How the key is held, declared in its enrol record and shown on every listing: touch-id "
                     "(a Secure Enclave key `levain cockpit key create` made), passphrase (an encrypted key file, "
                     "checked), tpm, fido, windows-hello, passkey (a WebAuthn passkey, display-phone keys only), or unstated. "
                     "A declaration, never a proof. "
                     "fido-attested is refused until attestation at enrol is built.")
    ck_p = subparsers.add_parser(
        "cockpit",
        help="Enroll, revoke and list the cockpit's signing keys, and initialise its authority log (a reported install).",
        description=(
            "The keys that sign the cockpit's T2/T3 writes, held as links in the signed authority log. "
            "`enroll` adds an OpenSSH public key (class laptop-presence signs T2 and T3, display-phone "
            "T2) by a link a laptop-presence key signs; `revoke` retires one by a link a key of at least "
            "its class signs (the last laptop-presence key only with --compromised-since); `keys` lists "
            "them. Each signing key is passed as --signing-key and may ask for a touch. The log's first "
            "link is `levain cockpit ledger init`'s. A governed install's keys are written by the root "
            "installer, so these refuse it."
        ),
    )
    ck_sub = ck_p.add_subparsers(dest="cockpit_cmd", required=True)

    def _ck_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--path", type=Path, default=Path.cwd(),
                        help="Install directory (default: cwd); the journal is its ledger's cockpit/.")
        sp.add_argument("--data-root", type=Path, default=None, dest="data_root",
                        help="The cockpit data root itself (an embedder's, e.g. its state/bridge/cockpit).")

    ck_enroll = ck_sub.add_parser("enroll", help="Enroll an OpenSSH public key.")
    ck_enroll.add_argument("key", type=Path, help="The public key file (e.g. ~/.ssh/id_ed25519.pub).")
    ck_enroll.add_argument("--class", dest="key_class", default="laptop-presence",
                           choices=("laptop-presence", "display-phone", "presence"),
                           help="What the key may sign (default: laptop-presence): laptop-presence "
                                "signs T2 and T3, display-phone T2, presence nothing yet.")
    ck_enroll.add_argument("--custody", dest="custody", default=None, choices=_CUSTODY_CHOICES,
                           help=_CUSTODY_HELP + " Default: the key's sidecar (`levain cockpit key create` "
                                "writes one), else unstated, which only a display-phone or presence key may be.")
    ck_enroll.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                           help="The enrolled laptop-presence key that signs this enrolment (its private "
                                "key file, or the .pub of a key an agent holds).")
    _ck_common(ck_enroll)
    ck_enroll.set_defaults(func=_cmd_cockpit_enroll)
    ck_revoke = ck_sub.add_parser("revoke", help="Revoke an enrolled key.")
    ck_revoke.add_argument("ref", help="The key's fingerprint (SHA256:...) or principal.")
    ck_revoke.add_argument("--compromised-since", dest="compromised_since", type=int, default=None,
                           help="The authority seq (as `levain cockpit keys` lists) from which the key "
                                "may have been in other hands; its fires decided at or after that seq read "
                                "\"signed by a revoked key\", and the last laptop-presence key can be revoked "
                                "with it. The seq is a position in the authority log, which moves only when "
                                "a link is appended (an enrol, a revoke, a policy change), so it marks every "
                                "fire since the log's head reached it, not a point in time.")
    ck_revoke.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                           help="An enrolled key of at least the revoked key's class that signs this "
                                "revocation (its private key file, or the .pub of a key an agent holds).")
    _ck_common(ck_revoke)
    ck_revoke.set_defaults(func=_cmd_cockpit_revoke)
    ck_key = ck_sub.add_parser("key", help="Make a signing key levain can vouch for the custody of.")
    ck_key_sub = ck_key.add_subparsers(dest="key_cmd", required=True)
    ck_create = ck_key_sub.add_parser(
        "create",
        help="Create a laptop-presence signing key: Touch ID (macOS) or a passphrase.",
        description=(
            "Create a signing key in a levain-owned 0700 directory, with a sidecar that names its custody. "
            "touch-id (macOS): a Secure Enclave key every signature of which asks Touch ID; the identity is "
            "confirmed biometric before anything else happens. passphrase (everywhere): an ed25519 key "
            "encrypted with a passphrase ssh-keygen asks on the terminal; every signature asks it, and "
            "nothing caches it. A FIDO key is an optional upgrade, enrolled with --custody fido; levain "
            "never requires one."
        ),
    )
    ck_create.add_argument("--custody", default=None, choices=("touch-id", "passphrase"),
                           help="touch-id (macOS, Secure Enclave + Touch ID) or passphrase. Default: touch-id on a "
                                "Mac where `bioutil -r` shows Touch ID configured for unlock, passphrase otherwise; "
                                "the command says which it chose and why.")
    ck_create.add_argument("--label", default=None,
                           help="The key's file name and, for touch-id, its CTK identity label "
                                "(default levain-<date>-<time>).")
    ck_create.add_argument("--dir", type=Path, default=None,
                           help="Where the key goes (default: <levain home>/keys, i.e. $LEVAIN_HOME/keys or "
                                "~/.levain/keys; made 0700).")
    ck_create.set_defaults(func=_cmd_cockpit_key_create)
    ck_policy = ck_sub.add_parser("policy", help="The standing policy, as signed links in the authority log.")
    ck_policy_sub = ck_policy.add_subparsers(dest="policy_cmd", required=True)
    ck_raise = ck_policy_sub.add_parser(
        "raise",
        help="Raise a verb's tier floor (it never lowers one).",
        description=(
            "Append a signed raise to the authority log: from the next request on, VERB shows and fires "
            "at TIER or above. The log classifies the change itself: a raise needs a display-phone or "
            "laptop-presence key, one that would lower a floor is a loosen (laptop-presence, and not this "
            "command), and one that changes nothing is refused. Every waiting pending goes stale, and "
            "every page rendered before it must render again."
        ),
    )
    ck_raise.add_argument("verb", help="The verb's name, as the manifest's verbs map lists it, or pending_cancel. "
                                       "A downstream verb must be in the served_verbs.json record the cockpit "
                                       "writes under its data root when it starts.")
    ck_raise.add_argument("tier", choices=("T2", "T3"), help="The verb's new floor (C1 is no floor).")
    ck_raise.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                          help="An enrolled display-phone or laptop-presence key that signs the raise (its "
                               "private key file, or the .pub of a key an agent holds).")
    _ck_common(ck_raise)
    ck_raise.set_defaults(func=_cmd_cockpit_policy_raise)
    ck_keys = ck_sub.add_parser("keys", help="List enrolled keys.")
    _ck_common(ck_keys)
    ck_keys.set_defaults(func=_cmd_cockpit_keys)
    ck_pending = ck_sub.add_parser("pending", help="List the cockpit's waiting pendings (read-only, no server).")
    _ck_common(ck_pending)
    ck_pending.set_defaults(func=_cmd_cockpit_pending)
    ck_sign = ck_sub.add_parser(
        "sign",
        help="Sign a waiting cockpit pending and send the signature to its running server.",
        description=(
            "Read the pending off the journal, print its record in full (the very bytes signed), sign them "
            "with --signing-key in the cockpit namespace (it may ask for a touch or a passphrase), and POST "
            "the signature to the running `levain serve --write` at --url, which verifies, decides and "
            "applies. The write token is read from $LEVAIN_WRITE_TOKEN, else asked for (never an argument). "
            "A key whose class does not cover the pending's tier is refused before anything is signed."
        ),
    )
    ck_sign.add_argument("pending_id", help="The pending's id (cp...), as `levain cockpit pending` lists it.")
    ck_sign.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                         help="An enrolled key whose class covers the pending's tier (its private key file, or "
                              "the .pub of a key an agent holds).")
    ck_sign.add_argument("--url", default="http://127.0.0.1:7420",
                         help="The running server (default: http://127.0.0.1:7420); loopback only.")
    _ck_common(ck_sign)
    ck_sign.set_defaults(func=_cmd_cockpit_sign)
    ck_ledger = ck_sub.add_parser("ledger", help="The authority log itself.")
    ck_ledger_sub = ck_ledger.add_subparsers(dest="ledger_cmd", required=True)
    ck_init = ck_ledger_sub.add_parser(
        "init",
        help="Write the authority log's first links.",
        description=(
            "Initialise the authority log, every link signed by --signing-key (a laptop-presence key; a "
            "touch-required key asks once per link). A fresh store gets the genesis: the signing key "
            "enrolled as laptop-presence, signed by itself (trust on first use). A store upgraded from "
            "levain-cockpit/2 lists the keys it held; each confirmed key is re-enrolled (a revoked one "
            "enrolled then revoked), a key whose type its class no longer accepts is skipped, and the rest "
            "are dropped. Every key enrolled is confirmed, the signing key included: at a terminal by a "
            "prompt per key, otherwise by --confirm."
        ),
    )
    ck_init.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                         help="The laptop-presence key that signs every link (its private key file, or the "
                              ".pub of a key an agent holds).")
    ck_init.add_argument("--custody", dest="custody", default=None, choices=_CUSTODY_CHOICES,
                         help=_CUSTODY_HELP + " For the signing key; default: its sidecar (`levain cockpit key "
                              "create` writes one), else this is required.")
    ck_init.add_argument("--confirm", dest="confirm", action="append", default=[], metavar="FINGERPRINT",
                         help="Keep this key (SHA256:...); repeatable. With --confirm or --drop-unlisted "
                              "nothing is asked.")
    ck_init.add_argument("--drop-unlisted", dest="drop_unlisted", action="store_true",
                         help="Drop every listed key not named by --confirm.")
    _ck_common(ck_init)
    ck_init.set_defaults(func=_cmd_cockpit_ledger_init)

    cs_p = subparsers.add_parser(
        "consent",
        help="The cross-agent consent queue: its standing grants and its hook canary.",
        description=(
            "Commands for the consent queue levain runs for Claude Code and Codex. A consent grant lets the "
            "queue answer an agent's permission prompt for you: this agent, these exact tools, inside this "
            "workspace, under these permission modes, until it expires (at most 30 days). It is a link in the "
            "cockpit's signed authority log. Shell commands (Bash) are never granted: they always reach you. "
            "Edits of agent config or hooks always reach you. `pending` lists the requests waiting on you and "
            "`answer` signs an allow or a deny for one; `canary` checks that an agent's hook fires."
        ),
    )
    cs_sub = cs_p.add_subparsers(dest="consent_cmd", required=True)
    _add_consent_canary_parser(cs_sub)
    _add_consent_ledger_parser(cs_sub)
    _add_consent_grant_parser(cs_sub, _ck_common)
    _add_consent_answer_parser(cs_sub)
    _add_consent_away_parser(cs_sub)
    _add_consent_rule_parser(cs_sub)
    _add_push_parser(subparsers)
    _add_band_parsers(subparsers)

    run_p = subparsers.add_parser(
        "run",
        help="Run a sovereign entity as an interactive partner on an open model.",
        description=(
            "Talk to an ISOLATED Levain entity (created with `levain init "
            "--adapter openhands`) as an interactive REPL — 'use it like Claude "
            "Code, but sovereign': it runs on an open model (Ollama by default), "
            "carries its OWN memory, and NEVER touches this laptop's flow store "
            "(the sovereignty guard fail-closes before the first turn). It has "
            "confined HANDS — a file editor plus (where an OS sandbox exists) a "
            "stateful bash — both fenced to a crown-jewels floor: they work on "
            "your real repos while ~/.anneal-memory/, sibling stores, and ~/.ssh "
            "key material stay off-limits (--no-tools for a pure conversational "
            "partner). Multi-line input is ONE message: a pasted block is kept "
            "whole, :paste … :end brackets a block explicitly, and piped/heredoc "
            "stdin is a single turn unless it uses :send separators — note that "
            "piped stdin is read to EOF, so a driver holding the pipe open waits, "
            "and that a terminal discards any single line over ~1023 characters "
            "before any program sees it (an OS limit, unchanged by this). "
            "Needs the OpenHands runtime: pip install 'levain[openhands]'."
        ),
    )
    run_p.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="The entity directory to run (default: cwd).",
    )
    run_p.add_argument(
        "--model",
        default="glm-5.2:cloud",
        help=(
            "The model to run on (default: glm-5.2:cloud). A bare or ollama/<name> name "
            "is served by local Ollama via its OpenAI-compatible /v1 endpoint with native "
            "tool-calling; a non-Ollama provider name (openai/…, anthropic/…) is used as-is."
        ),
    )
    run_p.add_argument(
        "--base-url",
        default="http://localhost:11434",
        dest="base_url",
        help="The model endpoint (default: http://localhost:11434, local Ollama).",
    )
    run_p.add_argument(
        "--api-key-file",
        default=None,
        dest="api_key_file",
        metavar="PATH",
        help="Read the endpoint's API key from PATH (refused if other users can read or write it, or if it is a symlink). "
             "Or set LEVAIN_API_KEY. Either keeps the key off the command line, where other "
             "processes can read it; the file is also denied to the entity's own hands.",
    )
    run_p.add_argument(
        "--api-key",
        default=None,
        dest="api_key",
        help="API key for the endpoint, if it needs one (local Ollama does not).",
    )
    run_p.add_argument(
        "--no-tools",
        action="store_false",
        dest="with_tools",
        help=(
            "Run as a pure conversational partner (no executor tools). Default: the entity gets "
            "confined hands (file editor + sandboxed bash) fenced to the crown-jewels floor."
        ),
    )
    run_p.add_argument(
        "--task",
        default=None,
        help=(
            "Run ONE task non-interactively and exit (no REPL, no tty needed) — the headless "
            "runner. Tool activity streams as it happens; the reply goes to stdout. The EXIT "
            "CODE reports what the harness observed, never what the agent claimed: 0 replied, "
            "1 completed with no reply, 2 startup/usage error, 3 the turn raised, 4 held at the "
            "efferent gate, 5 the wall-clock bound fired, 7 the reply was an unreadable tool call "
            "(it did not run; stdout stays empty). It does NOT "
            "assert the task succeeded — verify that against the world (run the tests, read "
            "the diff), because a confined entity cannot truthfully report on its own "
            "environment."
        ),
    )
    run_p.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "With --task: print ONLY the entity's reply on stdout (no banner, no activity "
            "stream) — for piping the reply as a payload. Errors still go to stderr."
        ),
    )
    run_p.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        dest="max_iterations",
        help=(
            "With --task: bound the turn to at most N agent steps, so an unattended run "
            "cannot spend forever on one message. Default: the SDK's own limit."
        ),
    )
    run_p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        dest="max_seconds",
        help=(
            "With --task: bound the run to N seconds of WALL-CLOCK time and exit 5 if it is "
            "exceeded. Different guarantee from --max-iterations, which counts STEPS and so "
            "cannot bound a turn that hangs inside ONE step (a stalled model call). Default: "
            "unbounded for a hand-run task; a scheduled seat always sets it."
        ),
    )
    run_p.add_argument(
        "--consolidate",
        action="store_true",
        help=(
            "With --task: after the turn, run the entity's own wrap if enough episodes have "
            "accumulated — as a SECOND, separately bounded phase (K4a [6]). Without this an "
            "unattended entity piles up raw episodes forever and never metabolizes them, which is "
            "worse than stateless: its recall degrades the longer it runs. Combined with "
            "--unattended the consolidate may metabolize but is REFUSED from crystallizing."
        ),
    )
    run_p.add_argument(
        "--consolidate-every",
        type=int,
        default=None,
        dest="consolidate_every",
        help=(
            "Episodes that must accumulate before --consolidate runs a wrap (default: the same "
            "threshold the human-facing wrap-nudge uses, so the seat and the nudge can never "
            "disagree about when a wrap is due)."
        ),
    )
    run_p.add_argument(
        "--consolidate-max-seconds",
        type=float,
        default=None,
        dest="consolidate_max_seconds",
        help=(
            "Wall-clock bound for the --consolidate phase, INDEPENDENT of --max-seconds so a long "
            "turn cannot starve the consolidate. Note the process can therefore live for the SUM "
            "of the two bounds; keep a seat's cadence above that. 0 disables it explicitly."
        ),
    )
    run_p.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "With --task: declare that NO HUMAN is in the loop at all — a scheduler invoked "
            "this and nobody will necessarily read the output (what `levain daemon install-seat` "
            "emits). With --consolidate, the consolidate may metabolize but is refused from "
            "crystallizing. By "
            "default the gate and the standard-credential denial (the stores the launch banner "
            "lists under the floor) already apply to any --task run; "
            "efferent_gate: \"ungated\" turns the gate off and deny_standard_creds: false lets the "
            "entity read those stores (both in .levain/confinement.json)."
        ),
    )
    run_p.set_defaults(func=_cmd_run)

    wrap_p = subparsers.add_parser(
        "wrap",
        help="Consolidate a sovereign entity's memory — metabolize its episodes into felt memory.",
        description=(
            "Run the CONSOLIDATE for an ISOLATED entity (created with `levain init "
            "--adapter openhands`): metabolize the raw episodes it captured while you talked to it "
            "into its lasting memory, so its identity COMPOUNDS across sessions. The "
            "entity's firing captures every turn but is forbidden to consolidate on its own — this "
            "command is the invocation that does it. A scheduled seat may also run it unattended "
            "(--unattended), in which case it may metabolize but is REFUSED from crystallizing "
            "into the always-loaded identity tier. The compose step runs on the "
            "entity's OWN open model by default (sovereign — it metabolizes its own memory with its "
            "own mind); --composer points that step at a stronger model for a higher-quality wrap. "
            "Reads/writes ONLY the entity's own store; NEVER touches this laptop's flow store. Needs "
            "the OpenHands runtime: pip install 'levain[openhands]'."
        ),
    )
    wrap_p.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="The entity directory to consolidate (default: cwd).",
    )
    wrap_p.add_argument(
        "--composer",
        default="glm-5.2:cloud",
        help=(
            "The model that composes the consolidated memory (default: glm-5.2:cloud — the "
            "sovereign default, the entity's own model). Point it at a stronger model for a "
            "higher-quality wrap. A bare or ollama/<name> name is served by local Ollama via its "
            "OpenAI-compatible /v1 endpoint; a non-Ollama provider name is used as-is."
        ),
    )
    wrap_p.add_argument(
        "--base-url",
        default="http://localhost:11434",
        dest="base_url",
        help="The compose-model endpoint (default: http://localhost:11434, local Ollama).",
    )
    wrap_p.add_argument(
        "--api-key-file",
        default=None,
        dest="api_key_file",
        metavar="PATH",
        help="Read the endpoint's API key from PATH (refused if other users can read or write it, or if it is a symlink). "
             "Or set LEVAIN_API_KEY. Either keeps the key off the command line, where other "
             "processes can read it; the file is also denied to the entity's own hands.",
    )
    wrap_p.add_argument(
        "--api-key",
        default=None,
        dest="api_key",
        help="API key for the endpoint, if it needs one (local Ollama does not).",
    )
    wrap_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show the consolidation package (what WOULD be composed) and change nothing.",
    )
    wrap_p.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Discard a prior wrap left in progress by a crashed consolidate, then wrap fresh. Your "
            "captured episodes are safe (they return to the next wrap)."
        ),
    )
    wrap_p.add_argument(
        "--affect-tag",
        default=None,
        dest="affect_tag",
        help=(
            "Optional emergent affective state during this consolidation (free-text, e.g. "
            "'engaged') — tags the Hebbian associations formed this wrap. Omit for no modulation."
        ),
    )
    wrap_p.add_argument(
        "--affect-intensity",
        type=float,
        default=0.5,
        dest="affect_intensity",
        help="How strongly the --affect-tag state was felt, 0.0-1.0 (default 0.5). Ignored without --affect-tag.",
    )
    wrap_p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        dest="max_seconds",
        help=(
            "Bound the whole consolidate to N seconds of WALL-CLOCK time and exit 5 if exceeded. "
            "Default: unbounded for a hand-run wrap (you are watching it); a scheduled seat always "
            "sets it, because a compose stalled against a dead endpoint would otherwise hang the "
            "seat forever. 0 disables it explicitly."
        ),
    )
    wrap_p.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "Declare that NO HUMAN is present for this consolidate (what a scheduled seat passes). "
            "The consolidate may then METABOLIZE — compose the working memory — but is structurally "
            "REFUSED from CRYSTALLIZING, i.e. promoting anything into the always-loaded identity "
            "tier, which only a human-present wrap may ratify. Also discards an orphaned wrap left "
            "by a crashed prior run instead of stopping to ask for --reset."
        ),
    )
    wrap_p.set_defaults(func=_cmd_wrap)

    web_p = subparsers.add_parser(
        "serve",
        help="Serve the substrate dashboard as a local web-app (localhost).",
        description=(
            "Run the substrate dashboard as a local web-app — your browser, your "
            "machine, no vendor host, no CDN, no account. Binds 127.0.0.1 only and "
            "serves a fresh SubstrateView snapshot on every request. READ-ONLY by "
            "default — it binds a socket, so read-only is the safe default (unlike "
            "`levain tui`, a local terminal, which defaults writable); pass --write for "
            "the GOVERNED WRITABLE cockpit (operate State / spores / Tray-Keep through "
            "the same governed seam `levain tui` uses; every write needs the write token "
            "the server prints at start). "
            "`serve-app` is the in-host MCP App alternative."
        ),
    )
    web_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    web_p.add_argument(
        "--port",
        type=int,
        default=7420,
        help="Port to bind on localhost (default: 7420).",
    )
    web_p.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Loopback address to bind (default: 127.0.0.1). `serve` is loopback-only: "
            "an install-bearing substrate's seed/config is operator-private, so a "
            "non-loopback (LAN / mesh) address is refused — there is no off-box `serve`."
        ),
    )
    web_p.add_argument(
        "--no-open",
        action="store_true",
        dest="no_open",
        help="Do not open a browser tab on startup.",
    )
    web_p.add_argument(
        "--write",
        action="store_true",
        help=(
            "Serve the GOVERNED WRITABLE cockpit instead of a read-only glance — "
            "enables State / spore touch/descend/ascend / Tray-Keep / episode-tombstone "
            "edits through the governed write seam. Every write needs the write token "
            "this server prints at start, with a link that carries it (loopback too: "
            "127.0.0.1 is reachable by every local user). Loopback-ONLY — there "
            "is no off-box writable serve (an install's seed/config is operator-private). "
            "Default is read-only."
        ),
    )
    web_p.add_argument(
        "--install-class",
        choices=["reported"],
        default=None,
        dest="install_class",
        help=(
            "Declare this install's class for the cockpit broker (T2/T3 writes). `reported` is "
            "the only class declared here: a write fired on the surface token is labelled "
            "'claims cockpit token', and signatures read 'signed · reported'. With no declaration "
            "and no governed marker, T2/T3 writes answer 'run the installer'."
        ),
    )
    web_p.add_argument(
        "--chat",
        action="append",
        type=Path,
        default=[],
        metavar="ENTITY",
        help=(
            "Also serve CHAT with the OpenHands entity at this directory (repeatable, one per "
            "entity). Conversations are held in server memory and driven through POST /chat/open "
            "and /chat/turn, polled at GET /chat/job.json. The server builds every agent itself "
            "from the entity; a client sends only text. Loopback-only. Nothing is persisted: a "
            "restart ends every conversation."
        ),
    )
    web_p.add_argument(
        "--model",
        default="glm-5.2:cloud",
        help="With --chat: the model the entities run on (default: glm-5.2:cloud, as `levain run`).",
    )
    web_p.add_argument(
        "--base-url",
        default="http://localhost:11434",
        dest="base_url",
        help="With --chat: the model endpoint (default: http://localhost:11434, local Ollama).",
    )
    web_p.add_argument(
        "--api-key-file",
        default=None,
        dest="api_key_file",
        metavar="PATH",
        help="With --chat: Read the endpoint's API key from PATH (refused if other users can read or write it, or if it is a symlink). "
             "Or set LEVAIN_API_KEY. Either keeps the key off the command line, where other "
             "processes can read it; the file is also denied to the entity's own hands.",
    )
    web_p.add_argument(
        "--api-key",
        default=None,
        dest="api_key",
        help="With --chat: API key for the endpoint, if it needs one.",
    )
    web_p.add_argument(
        "--max-iterations",
        type=_positive_int,
        default=None,
        dest="max_iterations",
        help="With --chat: bound each turn to at most N agent steps. Default: the SDK's own limit.",
    )
    web_p.add_argument(
        "--turn-seconds",
        type=_positive_seconds,
        default=None,
        dest="turn_seconds",
        help=(
            "With --chat: stop a turn that runs longer than N seconds of wall-clock time (default "
            f"{DEFAULT_TURN_SECONDS:g}). The stop lands at the next step boundary, so a model call or shell command "
            "already in flight finishes first; the turn is reported timed out, nothing from it is "
            "captured, and its session is closed."
        ),
    )
    web_p.set_defaults(func=_cmd_serve)

    serve_p = subparsers.add_parser(
        "serve-app",
        help="Serve the substrate dashboard as an in-host MCP App (stdio).",
        description=(
            "Run the read-only substrate dashboard as an MCP-Apps server over "
            "stdio, so a host (Claude desktop/web, ChatGPT, VS Code, Goose) can "
            "render it inside the chat. Read-only by construction: the server "
            "declares only read tools, so nothing it exposes can mutate the "
            "store. Needs the optional MCP SDK: pip install 'levain[app]'."
        ),
    )
    serve_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    serve_p.set_defaults(func=_cmd_serve_app)

    docs_p = subparsers.add_parser(
        "docs",
        help="Read the operator manual in the browser (base + installed pack chapters).",
        description=(
            "Serve the Levain operator manual as a local web page — the base "
            "'Driving Your Partner' guide COMPOSED with any installed pack's own "
            "chapters (the same multi-root layering as the seed roster). READ-ONLY "
            "and loopback-only (127.0.0.1); your browser, your machine, no vendor "
            "host, no CDN, no account. Base docs ship in the wheel; a pack's chapters "
            "were copied into the install at `levain init --pack` time, so the "
            "composed view is self-contained."
        ),
    )
    docs_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    docs_p.add_argument(
        "--port",
        type=int,
        default=7440,
        help="Port to bind on localhost (default: 7440).",
    )
    docs_p.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Loopback address to bind (default: 127.0.0.1). `docs` is loopback-only: "
            "the operator manual is a local read surface, refused off-box."
        ),
    )
    docs_p.add_argument(
        "--no-open",
        action="store_true",
        dest="no_open",
        help="Do not open a browser tab on startup.",
    )
    docs_p.set_defaults(func=_cmd_docs)

    daemon_p = subparsers.add_parser(
        "daemon",
        help="Install/manage the always-on `serve` autostart (login-start, crash-survive).",
        description=(
            "Make `levain serve --write` always-on — start on login and survive a crash — so "
            "the cockpit just works without an ad-hoc `nohup`. Per-user, no admin/root: a "
            "launchd user agent on macOS (systemd --user / Task Scheduler are planned "
            "pure-additions). An always-on serve is a 24/7 loopback-LOCAL write window; "
            "off-box serve is never daemonized (install-bearing serve is loopback-only)."
        ),
    )
    daemon_sub = daemon_p.add_subparsers(
        dest="daemon_action", metavar="<action>", required=True
    )

    d_install = daemon_sub.add_parser(
        "install", help="Install + start the autostart (idempotent).",
    )
    d_install.add_argument("--path", type=Path, default=Path.cwd(),
                           help="Install directory to serve (default: cwd).")
    d_install.add_argument("--port", type=int, default=7420,
                           help="Loopback port (default: 7420).")
    d_install.add_argument("--label", default="com.levainhq.levain",
                           help="Service label (default: com.levainhq.levain).")
    d_install.set_defaults(func=_cmd_daemon_install)

    d_seat = daemon_sub.add_parser(
        "install-seat",
        help="Install a GOVERNED SEAT — one entity running a task unattended on a schedule.",
        description=(
            "Install a scheduled governed seat (K4a): ONE sovereign entity that runs a bounded "
            "task unattended on a cadence, exits, and runs again. This is the PERIODIC sibling "
            "of `daemon install` (which supervises the always-on cockpit) — a seat is NOT "
            "kept-alive, because a kept-alive periodic job is relaunched the instant it exits "
            "and would run continuously instead of on its schedule. "
            "Every efferent action the seat takes fans in to you: with no human present the K3 "
            "gate HALTS the turn before the action runs and reports it (exit 4). The full tool "
            "activity stream — including a gated halt — goes to the seat's log, which is your "
            "review surface. The seat is NOT started at install time: installing a schedule is "
            "not a request to spend a model turn right now. Force a turn with `daemon restart "
            "--label <label>`; manage it with `daemon status` / `uninstall` and the same label."
        ),
    )
    d_seat.add_argument("--path", type=Path, default=Path.cwd(),
                        help="The entity directory the seat runs (default: cwd).")
    d_seat.add_argument("--task", required=True,
                        help="The task spec the seat runs each turn (required).")
    # NOTE: these literals mirror daemon.DEFAULT_SEAT_INTERVAL / DEFAULT_SEAT_LABEL rather than
    # importing them (the daemon module is imported lazily, inside the command funcs, so `levain
    # --help` stays cheap — the same reason the labels above are literals). A drift-lock test
    # pins CLI defaults to the module constants so a divergence fails the suite instead of
    # silently shipping two different defaults.
    d_seat.add_argument("--interval", type=int, default=3600,
                        help="Seconds between turns (default: 3600 = hourly).")
    d_seat.add_argument("--label", default="com.levainhq.levain.seat",
                        help="Service label (default: com.levainhq.levain.seat).")
    d_seat.add_argument("--model", default=None,
                        help="Model to run on (default: `levain run`'s own default).")
    d_seat.add_argument("--max-iterations", type=int, default=None, dest="max_iterations",
                        help=(
                            "Bound each unattended turn to at most N agent steps (default: a "
                            "finite built-in bound — an unattended turn with no bound can spend "
                            "indefinitely with nobody watching; 0 = the SDK's own limit)."
                        ))
    d_seat.add_argument("--max-seconds", type=float, default=None, dest="max_seconds",
                        help=(
                            "Bound each turn to N seconds of WALL-CLOCK time (default: a finite "
                            "built-in bound). REQUIRED for a seat to be restartable: a turn hung "
                            "inside one step is unbounded by --max-iterations, and because launchd "
                            "coalesces per label the seat would then never run again. 0 disables "
                            "it — do not, unless something else bounds the process."
                        ))
    d_seat.add_argument("--no-consolidate", action="store_true", dest="no_consolidate",
                        help=(
                            "Do NOT let the seat metabolize its own memory. Off by default because "
                            "a seat that only ever accumulates raw episodes is WORSE THAN "
                            "STATELESS — its recall degrades the longer it runs. Use this only if "
                            "you wrap the entity yourself on some other schedule."
                        ))
    d_seat.add_argument("--consolidate-every", type=int, default=None, dest="consolidate_every",
                        help=(
                            "Episodes that must accumulate before the seat consolidates (default: "
                            "the same threshold the human-facing wrap-nudge uses)."
                        ))
    d_seat.add_argument("--consolidate-max-seconds", type=float, default=None,
                        dest="consolidate_max_seconds",
                        help=(
                            "Wall-clock bound for the consolidate phase, SEPARATE from "
                            "--max-seconds so a long turn cannot starve it (default: a finite "
                            "built-in bound). The seat's worst-case run is therefore the SUM of "
                            "the two; keep --interval above it. 0 disables it — do not."
                        ))
    d_seat.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Show what would be installed + the true live state; change nothing.")
    d_seat.set_defaults(func=_cmd_daemon_install_seat)

    d_uninstall = daemon_sub.add_parser(
        "uninstall", help="Stop + remove the autostart (no-op if absent).",
    )
    d_uninstall.add_argument("--label", default="com.levainhq.levain",
                             help="Service label (default: com.levainhq.levain).")
    d_uninstall.set_defaults(func=_cmd_daemon_uninstall)

    d_status = daemon_sub.add_parser(
        "status", help="Report installed/running state.",
    )
    d_status.add_argument("--label", default="com.levainhq.levain",
                          help="Service label (default: com.levainhq.levain).")
    d_status.set_defaults(func=_cmd_daemon_status)

    d_would = daemon_sub.add_parser(
        "would-install",
        help="DRY-RUN: show what `install` would do + the true current state (changes nothing).",
        description=(
            "Report what `daemon install` WOULD do — diff the rendered unit against any on-disk "
            "unit and read the TRUE live state — WITHOUT writing a file or touching the service "
            "manager. The honesty floor: a unit file on disk is not proof the service is loaded."
        ),
    )
    d_would.add_argument("--path", type=Path, default=Path.cwd(),
                         help="Install directory to serve (default: cwd).")
    d_would.add_argument("--port", type=int, default=7420,
                         help="Loopback port (default: 7420).")
    d_would.add_argument("--label", default="com.levainhq.levain",
                         help="Service label (default: com.levainhq.levain).")
    d_would.set_defaults(func=_cmd_daemon_would_install)

    d_restart = daemon_sub.add_parser(
        "restart", help="Restart the running serve (pick up new code / a crash).",
    )
    d_restart.add_argument("--label", default="com.levainhq.levain",
                           help="Service label (default: com.levainhq.levain).")
    d_restart.set_defaults(func=_cmd_daemon_restart)

    upd_p = subparsers.add_parser(
        "update",
        help="Update the known-good version set together (anneal + schema + migrations) "
             "and refresh the activation tree and adapter files after an upgrade.",
        description=(
            "Reconcile this install's composed stack to the declared known-good "
            "SET in one ordered, fail-safe operation: bring anneal-memory to the "
            "tested version, re-run the partnership schema if the store drifted, "
            "surface anneal's `migrate check` instruction proposals for you to "
            "apply under review, and record the composed set. After a levain "
            "upgrade it also refreshes the activation tree and adapter files "
            "(hooks, posture.md, recency_directives.md, CLAUDE.md / AGENTS.md, "
            ".claude/settings.json, .mcp.json, codex hooks): a file you edited is "
            "kept, and where the new release changed it too, its new version is "
            "staged under .levain/pending/ and listed once. The fix for "
            "version-drift — a new anneal feature landing as a CONFLICT with stale "
            "methodology instructions instead of an addition. The env-mutating pip "
            "step is gated (it prompts; --yes auto-confirms, --no-pip skips it)."
        ),
    )
    upd_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    upd_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show the reconcile plan and change NOTHING (a plan is not a result).",
    )
    upd_p.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm the env-mutating pip step (non-interactive).",
    )
    upd_p.add_argument(
        "--no-pip",
        action="store_true",
        dest="no_pip",
        help=(
            "Do not run pip — reconcile only the store-side steps and print the "
            "exact pip command for your own package manager."
        ),
    )
    upd_p.add_argument(
        "--ack",
        action="store_true",
        help=(
            "After surfacing the migration proposals, record your instruction "
            "files as reconciled (advances anneal's `migrate ack` marker). Use "
            "ONLY once you have applied the proposed edits — anneal never edits "
            "them for you."
        ),
    )
    upd_p.set_defaults(func=_cmd_update)

    msg_p = subparsers.add_parser(
        "messaging",
        help="Show or change the Claude Code cross-session messaging choice of an install.",
        description=(
            "`levain messaging` (or `messaging show`) prints the effective choice, whether it is recorded or the "
            "default (isolate), and whether the workspace's .claude/settings.json carries its controls. "
            "`levain messaging set MODE` records the choice in .levain/config.json and runs `levain update` "
            "(claude-code adapter only). MODE is isolate, governed, off or unmanaged."
        ),
    )
    msg_p.add_argument("--path", type=Path, default=Path.cwd(), help="Install directory (default: cwd).")
    msg_sub = msg_p.add_subparsers(dest="messaging_cmd")
    msg_show = msg_sub.add_parser("show", help="Print the effective choice (read-only).")
    msg_show.add_argument("--path", type=Path, default=argparse.SUPPRESS, help="Install directory (default: cwd).")
    msg_set = msg_sub.add_parser(
        "set",
        help="Record the choice and run `levain update`.",
        description=("Records the choice in .levain/config.json and runs `levain update` for the install, which "
                     "renders the controls, re-pins and writes the receipt. An update that errors restores the "
                     "previous choice; a settings.json held for review keeps the record."),
    )
    msg_set.add_argument("mode", choices=("isolate", "governed", "off", "unmanaged"))
    msg_set.add_argument("--path", type=Path, default=argparse.SUPPRESS, help="Install directory (default: cwd).")
    msg_set.add_argument("--yes", action="store_true", help="Auto-confirm update's pip step.")
    msg_set.add_argument("--no-pip", action="store_true", dest="no_pip", help="Skip update's pip step.")
    msg_p.set_defaults(func=_cmd_messaging)

    adopt_p = subparsers.add_parser(
        "adopt-answers",
        help="Make your hand-edited seed files the interview record (.levain/answers.json).",
        description=(
            "Read the interview answers back OUT of the rendered seed files (world.md, "
            "origin.md, and any pack's rendered seeds) and record them as the install's "
            "answers, so a re-render from the record (`levain init --force`, a pack "
            "update) reproduces your edits instead of reverting them. All or nothing: "
            "every seed file must read back to an answer set that renders it again "
            "(where two would, the one closest to your current record is taken, and a "
            "tie refuses), or nothing is written and it exits 1. An edit outside the "
            "interview's fields (a changed heading, an added paragraph between "
            "sections) cannot become a record and is refused."
        ),
    )
    adopt_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    adopt_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be recorded and write nothing.",
    )
    adopt_p.set_defaults(func=_cmd_adopt_answers)

    iso_p = subparsers.add_parser(
        "setup-isolation",
        help="Create a dedicated unprivileged user for an entity's bash (one-time, needs sudo).",
        description=(
            "Create a dedicated user for this entity's bash, with no password and no login shell, "
            "a workspace of its own outside your home directory (/Users/Shared/levain on macOS, "
            "/var/lib/levain on Linux) that you can read and that you change through the entity "
            "(or ws-git, ws-put and ws-adopt), an ssh key of its own, and one sudoers rule that "
            "lets your account start commands as that user. A process running as that user cannot "
            "read the environment of your processes or files only you can read. On macOS, headless "
            "chat turns, --task runs and scheduled seats then execute bash as that user, and the file "
            "editor writes as it; the interactive `levain run` REPL keeps using your account. Needs "
            "root once: run it with sudo. --undo removes what it created."
        ),
    )
    iso_p.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Entity directory (default: cwd).",
    )
    iso_p.add_argument(
        "--undo",
        action="store_true",
        help=(
            "Retire the user (its account stays, disabled: no login, no password, no home, so its id "
            "is never given to another account) and remove its ssh key, its sudoers rule and the "
            "workspace permissions. Files it created keep its retired id as owner and become readable by "
            "your group (never yours, never root's: a git that owned them would trust a repository "
            "it filled); the workspace is removed only if empty."
        ),
    )
    iso_p.add_argument(
        "--reenable",
        action="store_true",
        help=(
            "Give this entity its hands back after an --undo: the retired hands user (undo keeps the "
            "account, disabled, so its id is never reused) is brought back with the same id."
        ),
    )
    iso_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print every step and change nothing (does not need sudo).",
    )
    iso_p.add_argument(
        "--egress-port",
        action="append",
        type=int,
        dest="egress_ports",
        metavar="PORT",
        help=(
            "Linux: a loopback TCP port the hands user may connect to (its model proxy); repeat for more. "
            "Every other connection the hands user opens is refused (an nftables rule on its uid). "
            "Omitted, a setup allows none and a repair run keeps what is recorded."
        ),
    )
    iso_p.set_defaults(func=_cmd_setup_isolation)

    wsg_p = subparsers.add_parser(
        "ws-git",
        help="Run git in the entity's workspace as the entity's user, with hooks and the like off.",
        description=(
            "Every repository in the workspace `levain setup-isolation` created belongs to the "
            "entity's user, and your own git refuses them. This runs git as that user, without "
            "hooks or any program a repository's config could name, and refuses a repository "
            "whose config sets anything beyond remotes, branches and core basics. It waits for "
            "the entity: while anything runs as the entity's user, it refuses."
        ),
    )
    wsg_p.add_argument("--path", type=Path, default=Path.cwd(), help="Entity directory (default: cwd).")
    wsg_p.add_argument("-C", dest="repo", type=Path, default=Path.cwd(),
                       help="Repository inside the workspace (default: cwd).")
    wsg_p.add_argument("git_args", nargs=argparse.REMAINDER, help="Arguments for git, e.g. status, log -5.")
    wsg_p.set_defaults(func=_cmd_ws_git)

    wsa_p = subparsers.add_parser(
        "ws-adopt",
        help="Import one of your repositories into the entity's workspace, owned by the entity's user.",
        description=(
            "Copies a repository of yours, from where it is, into the entity's workspace as a new "
            "repository that belongs to the entity's user. Every branch and tag comes across, and "
            "the branch list is checked against yours; remotes with an https or ssh URL are kept. "
            "Hooks are not copied. Your repository is not moved or changed; uncommitted changes "
            "and stashes stay only there."
        ),
    )
    wsa_p.add_argument("--path", type=Path, default=Path.cwd(), help="Entity directory (default: cwd).")
    wsa_p.add_argument("repo", type=Path, help="Your repository (outside the workspace).")
    wsa_p.add_argument("--as", dest="name", default=None,
                       help="Its folder name in the workspace (default: the repository's folder name).")
    wsa_p.set_defaults(func=_cmd_ws_adopt)

    wsp_p = subparsers.add_parser(
        "ws-put",
        help="Copy one file of yours into the entity's workspace, written by the entity's user.",
        description=(
            "Copies a regular file into the entity's workspace as plain data: the entity's user "
            "writes it, never as a program (mode 0644), and a link anywhere on the way stops the "
            "copy instead of redirecting it. Missing folders are created. Nothing in the workspace "
            "is run."
        ),
    )
    wsp_p.add_argument("--path", type=Path, default=Path.cwd(), help="Entity directory (default: cwd).")
    wsp_p.add_argument("src", type=Path, help="Your file.")
    wsp_p.add_argument("dest", help="Where it goes: a path inside the workspace, or relative to it.")
    wsp_p.set_defaults(func=_cmd_ws_put)

    ssa_p = subparsers.add_parser(
        "ssh-add",
        add_help=False,   # -h is ssh-add's destination flag here, as in ssh-add itself
        help="Add an ssh key to a running session's scoped agent, limited to the hosts you name.",
        description=(
            "Each levain session's bash gets its own ssh agent, never yours. This adds one key to it, "
            "from your own shell (it refuses inside a levain confinement), after checking that the "
            "process answering on the session's socket is the agent levain started. The key works "
            "only for the hosts named with -h, whose host keys must be in <levain home>/ssh/known_hosts "
            "(a file only you can write; seed it with `ssh-keyscan <host>` and compare the fingerprint with one "
            "obtained out of band, e.g. the one GitHub or GitLab publishes, never from ~/.ssh/known_hosts), "
            "and only for -t seconds. It prints the fingerprint of each manifest key a host is bound to."
        ),
    )
    ssa_p.add_argument("--help", action="help", help="Show this help and exit.")
    ssa_p.add_argument("key", type=Path, help="The private key file.")
    ssa_p.add_argument("-h", dest="hosts", action="append", default=[], metavar="HOST",
                       help="A host the key may be used for ([user@]host; hops joined by >). Repeatable.")
    ssa_p.add_argument("-t", dest="lifetime", type=int, default=None, metavar="SECS",
                       help="How long the agent keeps the key (default 8 hours, at most 24).")
    ssa_p.add_argument("--session", default=None,
                       help="The session's agent id, when more than one session is running.")
    ssa_p.add_argument("--no-destination-limit", dest="waive", action="store_true",
                       help="Add the key for ANY host (a waiver `levain run` and `levain doctor` print).")
    ssa_p.set_defaults(func=_cmd_ssh_add)

    from levain.team.cli import register as _register_team

    _register_team(subparsers)

    args = parser.parse_args(argv)
    from levain.launch import api_key_lifted

    if api_key_lifted() and not hasattr(args, "api_key"):
        # The re-exec lifts --api-key before argparse sees it; a command without that option would
        # otherwise drop the key silently instead of rejecting it (L3 r2).
        parser.error("unrecognized arguments: --api-key")
    return args.func(args)


def _cmd_init(args: argparse.Namespace) -> int:
    if args.answers_template:
        # A pure read: emit what this install WOULD ask and exit. Refuse the flag
        # combinations that would imply it also installs something, rather than
        # silently ignoring them — a flag that is quietly dropped is how an
        # operator ends up believing an install happened.
        if args.answers is not None:
            print(
                "levain init: --answers-template and --answers are mutually "
                "exclusive (one emits a blank file, the other consumes a filled one)."
            )
            return 1
        if args.web:
            print("levain init: --answers-template cannot be combined with --web.")
            return 1
        if args.messaging is not None:
            print("levain init: --answers-template cannot be combined with --messaging (it installs "
                  "nothing, so there is no posture to set).")
            return 1
        if args.consent_hook is not None:
            print("levain init: --answers-template cannot be combined with --consent-hook (it installs "
                  "nothing).")
            return 1
        if args.memory_load is not None:
            print("levain init: --answers-template cannot be combined with --memory-load (it installs "
                  "nothing).")
            return 1
        from levain.install import run_answers_template

        return run_answers_template(packs=args.pack)

    if args.consent_hook is not None and args.web:
        print("levain init: --consent-hook cannot be combined with --web; drop --web, or run "
              "`levain init --consent-hook ...` in the terminal.")
        return 1

    if args.memory_load is not None and args.web:
        print("levain init: --memory-load cannot be combined with --web; drop --web, or run "
              "`levain init --memory-load ...` in the terminal.")
        return 1

    if args.messaging is not None and args.web:
        print("levain init: --messaging cannot be combined with --web; drop --web, or run "
              "`levain init --messaging ...` in the terminal.")
        return 1

    if args.answers is not None and args.web:
        # --web IS the interactive onboarding surface; pairing it with a file that
        # exists to avoid interaction is a contradiction, not a preference to resolve.
        print(
            "levain init: --answers cannot be combined with --web (--web runs the "
            "browser interview; --answers runs no interview at all).\n"
            "  Drop --web to install non-interactively."
        )
        return 1

    if args.web and args.adapter == "openhands":
        # The browser onboarding surface (init_server) is claude-code/codex only for
        # now; a sovereign-entity scaffold is a terminal-only path this slice. Fail
        # clean rather than letting init_server reject it with a generic bad_adapter.
        print(
            "levain init: --web onboarding does not support --adapter openhands yet.\n"
            "  Run it in the terminal:  levain init --adapter openhands "
            f"--path {args.path}"
        )
        return 1
    if args.web:
        from levain.init_server import run_init_web

        return run_init_web(
            path=args.path,
            adapter=args.adapter,
            force=args.force,
            packs=args.pack,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )

    from levain.install import run_init

    return run_init(
        path=args.path,
        adapter=args.adapter,
        force=args.force,
        packs=args.pack,
        answers_file=args.answers,
        messaging=args.messaging,
        consent_hook=args.consent_hook,
        memory_load=args.memory_load,
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    from levain.doctor import run_doctor

    return run_doctor(path=args.path, invoke=args.invoke, repin_hooks=args.repin_hooks)


def _add_consent_canary_parser(consent_sub: argparse._SubParsersAction) -> None:
    """`levain consent canary` (Q5), under the one `consent` parser; `levain consent grant` sits beside it."""
    canary_p = consent_sub.add_parser(
        "canary", help="Check that an agent's consent hook fires and its denial reaches the agent.",
        description=(
            "Run one real agent turn in a scratch workspace whose consent hook denies one sentinel write "
            "(never allows anything). PASS only when the hook fired on it, the agent reported the denial and "
            "the file was not written. Claude Code runs headless; Codex runs interactively under a pty "
            "(it cannot fire PermissionRequest headless). It spends one short model turn."))
    canary_p.add_argument("--agent", required=True, choices=("claude_code", "codex"))
    canary_p.add_argument("--workspace", type=Path, default=None,
                          help="Scratch workspace: a new or empty directory (default: a new temporary one).")
    canary_p.set_defaults(func=_cmd_consent_canary)


def _cmd_consent_canary(args: argparse.Namespace) -> int:
    from levain.consent.canary import cli_canary

    return cli_canary(args.agent, args.workspace)


def _add_consent_ledger_parser(consent_sub: argparse._SubParsersAction) -> None:
    """`levain consent ledger counts|digest|window|reconcile` (Q15): the authority ledger, read-mostly."""
    ledger_p = consent_sub.add_parser(
        "ledger", help="The authority ledger: which tool calls ran, and on whose authority.",
        description=(
            "One row per tool call an agent made, with an authority stamp (grant, answer, ruling, own_rules, "
            "classifier, bypass, dialog_unsigned, hook, sandbox, unknown, pending). Digests only: no command "
            "text or file content is stored. `counts` counts by agent and stamp; `digest` lists the rows a "
            "standing grant or a late ruling answered (the unseen digest); `window` lists one session's rows "
            "between two times (a gap is the window from its undecided time to its ruling); `reconcile` re-reads "
            "the agent's own record for every pending row and stamps it."))
    sub = ledger_p.add_subparsers(dest="ledger_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                       help="The consent directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
        p.add_argument("--json", dest="as_json", action="store_true", help="Print JSON.")

    def span(p: argparse.ArgumentParser, *, since_required: bool) -> None:
        p.add_argument("--since", required=since_required, default=None,
                       help="Start: an ISO time, a date, or an age such as 7d, 36h, 90m.")
        p.add_argument("--until", default=None, help="End (default: now), same forms.")

    counts_p = sub.add_parser("counts", help="Count rows by agent and stamp.")
    span(counts_p, since_required=False)
    common(counts_p)
    digest_p = sub.add_parser("digest", help="List the rows a grant or a late ruling answered.")
    span(digest_p, since_required=True)
    common(digest_p)
    window_p = sub.add_parser("window", help="List one session's rows between two times.")
    window_p.add_argument("--agent", required=True, choices=("claude_code", "codex"))
    window_p.add_argument("--session", required=True, help="The agent's session id.")
    span(window_p, since_required=True)
    common(window_p)
    recon_p = sub.add_parser("reconcile", help="Re-read the agent's record for every pending row and stamp it.")
    recon_p.add_argument("--give-up-after", dest="give_up_after", default="1h",
                         help="Stamp a row `unknown` when its source still has no answer this long after the call "
                              "(an age such as 90m, 1h, 2d; `never` keeps it pending). Default 1h.")
    common(recon_p)
    for p in (counts_p, digest_p, window_p, recon_p):
        p.set_defaults(func=_cmd_consent_ledger)


def _cmd_consent_ledger(args: argparse.Namespace) -> int:
    import json
    import re
    import sqlite3
    from dataclasses import asdict

    from levain.consent.ledger import SUBAGENT_MARK, WINDOW_SCOPE, Ledger, parse_when, reconcile
    from levain.consent.queue import QueueStoreError

    qdir = Path(os.path.abspath(args.queue_dir.expanduser())) if args.queue_dir is not None else None
    try:
        ledger = Ledger(qdir)
        if args.ledger_cmd == "reconcile":
            if args.give_up_after != "never" and not re.fullmatch(r"[0-9]{1,9}[dhm]", args.give_up_after, re.ASCII):
                raise ValueError(f"--give-up-after {args.give_up_after!r} is not an age such as 90m, 1h, 2d, or `never`")
            now = datetime.now(timezone.utc)
            gua = None if args.give_up_after == "never" else now - parse_when(args.give_up_after, now=now)
            result = reconcile(ledger, give_up_after=gua)
            print(json.dumps(result, sort_keys=True) if args.as_json else
                  f"filled {result['filled']} ({result['unknown']} unknown), still pending {result['still_pending']}"
                  + (f", already stamped elsewhere {result['already_stamped']}" if result["already_stamped"] else ""))
            return 0
        if args.ledger_cmd == "counts":
            rows = ledger.counts(args.since, args.until)
            if args.as_json:
                print(json.dumps([{"agent": a, "stamp": s, "outcome": o, "rows": n} for a, s, o, n in rows]))
            else:
                for a, s, o, n in rows:
                    print(f"{a:12} {s:16} {o:9} {n}")
                if not rows:
                    print("no rows")
            return 0
        calls = (ledger.digest(args.since, args.until) if args.ledger_cmd == "digest"
                 else ledger.window(args.agent, args.session, args.since, args.until))
    except (OSError, ValueError, OverflowError, QueueStoreError, sqlite3.Error) as e:
        print(f"levain consent ledger: {e}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps([asdict(c) for c in calls]))
    else:
        for c in calls:
            root, mark, sub = c.agent_session_id.partition(SUBAGENT_MARK)
            print(f"{c.received_at}  {c.agent:11} {c.stamp:16} {c.outcome:9} {c.phase:14} {c.tool_name}  "
                  f"{c.tool_input_digest[:20]}  {root[:8]}" + (f"  subagent {sub}" if mark else ""))
        if not calls:
            print("no rows")
        if args.ledger_cmd == "window":
            print(WINDOW_SCOPE)
    return 0


def _cmd_verify_hooks(args: argparse.Namespace) -> int:
    from levain.verify import run_verify_hooks

    return run_verify_hooks(path=args.path)


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from levain.dashboard import run_dashboard

    return run_dashboard(path=args.path, as_json=args.as_json)


def _cmd_tui(args: argparse.Namespace) -> int:
    from levain.tui import run_tui

    return run_tui(path=args.path, read_only=args.read_only, manifest=args.manifest)


def _cmd_state(args: argparse.Namespace) -> int:
    from levain.dashboard import run_state

    return run_state(
        path=args.path, text=args.text, source=args.source, clear=args.clear
    )


def _resolve_api_key(args: argparse.Namespace) -> bool:
    """Settle ``args.api_key`` from, in order: ``--api-key`` (in-process callers; the console entry
    lifts it out of the command line, see :mod:`levain.launch`), the value so lifted,
    ``--api-key-file``, then ``LEVAIN_API_KEY`` (removed from the environment once read, so no child
    inherits it). False, with the reason printed, when the key cannot be used."""
    from levain.launch import API_KEY_ENV, add_secret_file, take_lifted_api_key

    given = getattr(args, "api_key", None) or take_lifted_api_key()
    path = getattr(args, "api_key_file", None)
    env_key = os.environ.pop(API_KEY_ENV, None)
    if given is not None and path is not None:
        print("levain: give the API key once: --api-key or --api-key-file, not both.", file=sys.stderr)
        return False
    if path is not None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise OSError("not a regular file")
                if st.st_mode & 0o077:
                    print(f"levain: {path} is readable or writable by other users "
                          f"(mode {stat.S_IMODE(st.st_mode):o}); run `chmod 600 {path}`. The key was "
                          "not used.", file=sys.stderr)
                    return False
                with os.fdopen(fd, encoding="utf-8") as fh:
                    fd = -1
                    given = fh.read().strip()
            finally:
                if fd >= 0:
                    os.close(fd)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"levain: could not read the API key file {path}: {exc}", file=sys.stderr)
            return False
        if not given:
            print(f"levain: the API key file {path} is empty.", file=sys.stderr)
            return False
        add_secret_file(path)   # the floor denies it to the entity, both ways
    args.api_key = given if given is not None else env_key
    return True


def _cmd_run(args: argparse.Namespace) -> int:
    if not _resolve_api_key(args):
        return 2
    # Two drivers over ONE session (the K1 seam, `levain.session`): `--task` is the
    # non-interactive runner (one spec, exit-when-done, an exit code a caller can branch on);
    # without it, the interactive REPL. `--quiet`/`--max-iterations` only shape a task run.
    task = getattr(args, "task", None)
    # `--unattended` without `--task` is REFUSED rather than ignored. It is a security-relevant
    # DECLARATION (no human in the loop → the consolidate may metabolize, never crystallize),
    # and a REPL is definitionally attended, so honouring it there is impossible while dropping it
    # silently would leave the operator believing a posture they did not get. A governance claim
    # the run does not enforce is the exact failure class this keystone exists to make impossible.
    if getattr(args, "unattended", False) and task is None:
        print(
            "levain run: --unattended requires --task. It declares that NO HUMAN is in the loop, "
            "which cannot be true of an interactive REPL session — and silently ignoring it would "
            "leave you believing the credential floor was tightened when it was not.",
            file=sys.stderr,
        )
        return 2
    # ⚠ SAME REFUSAL, SAME PLACE — OUTSIDE `if task is not None` (fixed 2026-07-30, Diogenes).
    # The consolidate-flag refusal below lives INSIDE the task branch, so on the REPL path
    # --consolidate / --consolidate-every / --consolidate-max-seconds were SILENTLY IGNORED and
    # the call fell straight through to run_entity. Its `--unattended` sibling above is
    # deliberately outside for exactly this reason, and the two make the identical argument:
    # a governance-relevant declaration that the run does not honour is worse than a refusal.
    # --consolidate is the flag that PERMITS THE ENTITY TO REWRITE ITS OWN MEMORY — daemon.py
    # calls it "the single most governance-relevant fact about the unit" — so an operator
    # believing they enabled or bounded it when they did not is the exact failure class this
    # keystone exists to make impossible. guard_scoped_by_symptom_misses_the_class: the rule was
    # written for the tuning flags and missed the flag that actually carries the authority.
    if task is None and (
        getattr(args, "consolidate", False)
        or getattr(args, "consolidate_every", None) is not None
        or getattr(args, "consolidate_max_seconds", None) is not None
    ):
        print(
            "levain run: --consolidate / --consolidate-every / --consolidate-max-seconds require "
            "--task. They govern a self-memory-rewrite phase that only a task run performs, so "
            "honouring them in an interactive REPL is impossible — and silently ignoring them "
            "would leave you believing the entity metabolizes its memory when it does not.",
            file=sys.stderr,
        )
        return 2
    if task is not None:
        from levain.run import run_task

        max_seconds = getattr(args, "max_seconds", None)
        if _reject_bad_max_seconds(max_seconds, command="run"):
            return 2
        consolidate_max_seconds = getattr(args, "consolidate_max_seconds", None)
        if _reject_bad_max_seconds(consolidate_max_seconds, command="run",
                                   flag="--consolidate-max-seconds"):
            return 2
        # A consolidate TUNING flag with no --consolidate is REFUSED, not ignored — the same call
        # `--unattended`-without-`--task` makes above, for the same reason. Silently dropping it
        # leaves an operator believing they bounded a consolidate that is not going to happen, and
        # on a seat that belief would persist unfalsified for as long as the seat runs.
        if not getattr(args, "consolidate", False) and (
            getattr(args, "consolidate_every", None) is not None
            or consolidate_max_seconds is not None
        ):
            print(
                "levain run: --consolidate-every / --consolidate-max-seconds require "
                "--consolidate. They tune a phase that is off by default, so honouring them "
                "without it would leave you believing the entity metabolizes its memory when it "
                "does not.",
                file=sys.stderr,
            )
            return 2
        return run_task(
            path=args.path,
            task=task,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            with_tools=args.with_tools,
            quiet=getattr(args, "quiet", False),
            max_iterations=getattr(args, "max_iterations", None),
            # 0 means "explicitly unbounded" — normalize it to None at the boundary so exactly one
            # representation of "no bound" reaches the deadline.
            max_seconds=None if max_seconds == 0 else max_seconds,
            unattended=getattr(args, "unattended", False),
            consolidate=getattr(args, "consolidate", False),
            consolidate_every=getattr(args, "consolidate_every", None),
            consolidate_max_seconds=_resolve_unattended_consolidate_bound(
                consolidate_max_seconds,
                unattended=getattr(args, "unattended", False),
                consolidate=getattr(args, "consolidate", False),
            ),
        )

    from levain.run import run_entity

    return run_entity(
        path=args.path,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        with_tools=args.with_tools,
    )


def _reject_bad_max_seconds(value: float | None, *, command: str, flag: str = "--max-seconds") -> bool:
    """Validate a wall-clock bound. Returns ``True`` (and explains) when it must be refused.

    ⛔ ``flag`` EXISTS BECAUSE THIS VALIDATOR IS SHARED AND USED TO LIE. It hardcoded
    ``--max-seconds`` in all three messages, and ``levain run`` also routes
    ``--consolidate-max-seconds`` through it — so an operator who typed a bad value for the
    consolidate bound was told ``levain run: --max-seconds must be >= 0``, naming a flag they
    never set and sending them to fix the wrong option. Being one validator is the right call
    (a second copy is a second place to forget the edge cases); saying the same thing about
    every caller was not.

    ONE validator for every command that takes the flag (``run``, ``wrap``, ``daemon install-seat``),
    because the two edge cases below were each found by an L3 lineage and a second copy of this
    check is a second place for them to be forgotten. The K4a [6] slice added ``wrap`` to the list of
    callers, which is exactly when a duplicated guard starts to drift.

    A NEGATIVE bound is refused rather than normalized: ``TurnDeadline`` treats non-positive as
    DISARMED, so ``--max-seconds -1`` would read to an operator as "bound it tightly" and deliver
    "not bounded at all" (``guard_scoped_by_symptom_misses_the_class``).

    NON-FINITE IS CHECKED FIRST, because ``nan`` slips through every comparison guard (codex L3,
    HIGH): ``nan < 0`` is False so a ``< 0`` check passes it, and ``nan > 0`` is also False so the
    deadline then arms NOTHING — an unbounded run whose own banner prints ``nans wall-clock``.
    ``inf`` gets as far as ``setitimer`` and raises ``OverflowError``. Both die at the door.
    """
    if value is None:
        return False
    if not math.isfinite(value):
        print(
            f"levain {command}: {flag} must be a finite number of seconds, got "
            f"{value!r}. Use 0 to run unbounded — deliberately and visibly.",
            file=sys.stderr,
        )
        return True
    if value < 0:
        print(
            f"levain {command}: {flag} must be >= 0, got {value:g}. "
            f"Use 0 to disable the wall-clock bound explicitly.",
            file=sys.stderr,
        )
        return True
    return False


def _resolve_unattended_consolidate_bound(
    explicit: float | None, *, unattended: bool, consolidate: bool
) -> float | None:
    """Give an UNATTENDED consolidate a finite bound even when the operator did not name one.

    THE FOOT-GUN THIS REMOVES (codex L3, LOW): ``daemon install-seat`` supplies finite defaults, so
    the supported path was covered — but a direct ``levain run --task … --unattended --consolidate``
    accepted "declare that no human is present" and "run an unbounded model call" in the SAME
    command. That combination is precisely what the slice exists to make impossible, and it was
    reachable by OMISSION rather than by choice, which is the worst way to reach it.

    Defaulting rather than refusing, because the two flags are not in conflict — the operator asked
    for something coherent and simply did not say how long to allow. Binding to the absence of the
    human is the axis the gate and the credential floor already use, so this is that same rule
    applied to a bound. ``0`` remains an explicit, deliberate "unbounded"; it is honoured untouched.
    """
    if explicit is not None or not (unattended and consolidate):
        return None if explicit == 0 else explicit
    from levain.daemon import DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS

    print(
        f"levain: an unattended consolidate with no wall-clock bound named — defaulting it to "
        f"{DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS:g}s.\n"
        f"  An unattended compose with no bound can hang this process forever, and a scheduled "
        f"caller would never run again. Pass 0 to opt out deliberately.",
        file=sys.stderr,
    )
    return DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS


def _cmd_wrap(args: argparse.Namespace) -> int:
    from levain.wrap import wrap_entity

    if not _resolve_api_key(args):
        return 2

    max_seconds = getattr(args, "max_seconds", None)
    if _reject_bad_max_seconds(max_seconds, command="wrap"):
        return 2

    return wrap_entity(
        path=args.path,
        composer=args.composer,
        base_url=args.base_url,
        api_key=args.api_key,
        dry_run=args.dry_run,
        reset=args.reset,
        affect_tag=args.affect_tag,
        affect_intensity=args.affect_intensity,
        # 0 means "explicitly unbounded" — normalized at the boundary so exactly ONE representation
        # of "no bound" reaches the deadline, matching `run`'s handling of the same flag. And an
        # unattended wrap with no bound named gets the finite default rather than running unbounded
        # by omission — same reasoning as the `run` path (codex L3, LOW): both commands accept the
        # "no human present" declaration, so both must refuse to pair it with an unbounded compose.
        max_seconds=_resolve_unattended_consolidate_bound(
            max_seconds,
            unattended=getattr(args, "unattended", False),
            consolidate=True,  # `levain wrap` IS the consolidate
        ),
        unattended=getattr(args, "unattended", False),
    )


def _cockpit_root(args: argparse.Namespace) -> Path:
    """The broker's data root: ``--data-root``, else the one ``levain serve --write`` uses for
    ``--path`` (its write scope's ledger root's ``cockpit/``)."""
    if args.data_root is not None:
        return args.data_root
    from levain.dashboard import _resolve_source

    scope = _resolve_source(args.path).write_scope
    if scope is None:
        raise SystemExit(f"{args.path} has no writable install; pass --data-root")
    return scope.ledger_root / "cockpit"


_KEY_FILE_MAX = 16 * 1024


def _cockpit_refusals() -> tuple[type[BaseException], ...]:
    """What a key command answers as a refusal, never a traceback: its own refusals, the authority
    log's (a refused link, an unusable log, the verifier), an unreadable file, and the journal's (busy
    while the broker writes, not a cockpit journal, corrupt)."""
    import sqlite3

    from levain.authority import AuthorityError
    from levain.cockpit.firejournal import CockpitFormatError
    from levain.cockpit.keys import KeyError_

    return (KeyError_, AuthorityError, OSError, UnicodeDecodeError, sqlite3.Error, CockpitFormatError)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cmd_cockpit_enroll(args: argparse.Namespace) -> int:
    from levain.cockpit.custody import declared_custody
    from levain.cockpit.keys import KeyError_, enroll, ssh_signer

    try:
        with args.key.expanduser().open("rb") as fh:
            raw = fh.read(_KEY_FILE_MAX + 1)
        if len(raw) > _KEY_FILE_MAX:
            raise KeyError_(f"{args.key} is larger than a public key file ({_KEY_FILE_MAX} bytes)")
        # checked before anything is signed: the custody table, the sidecar, the encrypted file, the CTK identity
        custody = declared_custody(args.key, args.custody, args.key_class, raw.decode("utf-8"))
        signer_fp, sign = ssh_signer(args.signing_key)
        row, created, warning = enroll(_cockpit_root(args), raw.decode("utf-8"), args.key_class,
                                       custody=custody, signer_fp=signer_fp, sign=sign, clock=_utc_now)
    except _cockpit_refusals() as exc:
        print(f"enroll refused: {exc}", file=sys.stderr)
        return 1
    print(f"{'enrolled' if created else 'already enrolled'}: {row.fingerprint} as {row.key_class}, custody "
          f"{row.custody}, at seq {row.enrolled_seq} (principal {row.principal})")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_cockpit_revoke(args: argparse.Namespace) -> int:
    from levain.cockpit.keys import revoke, ssh_signer

    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        row, warning = revoke(_cockpit_root(args), args.ref, signer_fp=signer_fp, sign=sign, clock=_utc_now,
                              compromised_since=args.compromised_since)
    except _cockpit_refusals() as exc:
        print(f"revoke refused: {exc}", file=sys.stderr)
        return 1
    print(f"revoked: {row.fingerprint} at seq {row.revoked_seq}"
          + (f" (compromised since seq {row.compromised_since})" if row.compromised_since else ""))
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_cockpit_policy_raise(args: argparse.Namespace) -> int:
    from levain.cockpit.keys import policy_raise, ssh_signer

    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        tier, seq, warning = policy_raise(_cockpit_root(args), args.verb, args.tier, signer_fp=signer_fp,
                                          sign=sign, clock=_utc_now)
    except _cockpit_refusals() as exc:
        print(f"raise refused: {exc}", file=sys.stderr)
        return 1
    print(f"raised: {args.verb} at {tier} or above, at seq {seq}")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _add_consent_grant_parser(cs_sub: argparse._SubParsersAction, common: object) -> None:
    """``levain consent grant create|revoke|list`` under the ``consent`` subparsers ``cs_sub``; ``common`` adds
    the cockpit commands' ``--path`` / ``--data-root``."""
    cs_grant = cs_sub.add_parser("grant", help="Create, revoke and list consent grants.")
    cs_grant_sub = cs_grant.add_subparsers(dest="grant_cmd", required=True)
    cs_create = cs_grant_sub.add_parser(
        "create",
        help="Sign a consent grant (a laptop-presence key).",
        description=(
            "Append a signed create_grant link: from the next prompt on, a permission prompt from AGENT for one "
            "of the named tools, under one of the named permission modes, whose working directory and every "
            "file it targets resolve inside WORKSPACE, is allowed without asking until the grant expires. A "
            "tool with no file target (WebFetch, an MCP tool without path arguments) is matched on its working "
            "directory alone. The log refuses a shell tool, a tool levain does not know for AGENT, an MCP tool "
            "other than the filesystem server's read tools (read_text_file, list_directory...) whose fields the "
            "grant does not declare, a workspace of / or your home directory, and a life over 30 days, before "
            "anything is signed. MCP does not say which of a tool's fields are paths, so for any other MCP tool "
            "you declare every field it may take (--path-field, --mcp-field; the grant is then of the consent-mcp "
            "kind, which levain 0.11.4 and older cannot read): a request with any other field, or a value of "
            "another kind, goes to you, and every path and paths field must resolve inside WORKSPACE. A field you "
            "declare str is never read as a path: declare as path every field the server reads one from. A "
            "declaration on a listed read tool replaces levain's own schema for it."
        ),
    )
    cs_create.add_argument("--agent", required=True, choices=("claude_code", "codex"))
    cs_create.add_argument("--tool", dest="tools", action="append", required=True, metavar="TOOL",
                           help="An exact tool name, one levain knows for that agent (Claude Code: Edit, Write, "
                                "MultiEdit, NotebookEdit, Read, Grep, Glob, LS, NotebookRead, WebFetch, SendMessage, "
                                "an MCP read tool; Codex: apply_patch, an MCP read tool; either: an MCP tool "
                                "whose fields you declare); repeatable. No globs; never a "
                                "shell tool (Bash, PowerShell, Codex's write_stdin).")
    cs_create.add_argument("--workspace", type=Path, required=True,
                           help="The project directory the grant covers (resolved through symlinks).")
    cs_create.add_argument("--posture", dest="postures", action="append", required=True, metavar="MODE",
                           help="A permission mode the agent reports (Claude Code: default, acceptEdits, plan, "
                                "auto, dontAsk, bypassPermissions; Codex: default); repeatable.")
    cs_create.add_argument("--expires", required=True,
                           help="A duration (30m, 12h, 7d; at most 30d) or a UTC time (2026-10-20T12:00:00Z).")
    cs_create.add_argument("--path-field", dest="path_fields", action="append", default=[], metavar="TOOL:FIELD",
                           help="Declare FIELD of the MCP tool TOOL (one of --tool) a target path; repeatable.")
    cs_create.add_argument("--mcp-field", dest="mcp_fields", action="append", default=[], metavar="TOOL:FIELD=KIND",
                           help="Declare FIELD of the MCP tool TOOL (one of --tool) of KIND: path, paths (a list of "
                                "paths), str, strs (a list of strings), int or bool; repeatable.")
    cs_create.add_argument("--id", dest="grant_id", default=None, help="The grant's id (default: cg-<random>).")
    cs_create.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                           help="An enrolled laptop-presence key that signs the grant (its private key file, or "
                                "the .pub of a key an agent holds).")
    common(cs_create)
    cs_create.set_defaults(func=_cmd_consent_grant_create)
    cs_revoke = cs_grant_sub.add_parser(
        "revoke", help="Revoke a consent grant (any enrolled key).",
        description="Append a signed revoke_grant link. From the next prompt on the grant answers nothing, and "
                    "an answer it already gave is no longer delivered.")
    cs_revoke.add_argument("grant_id", help="The grant's id, as `levain consent grant list` shows it.")
    cs_revoke.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                           help="An enrolled key that signs the revoke.")
    common(cs_revoke)
    cs_revoke.set_defaults(func=_cmd_consent_grant_revoke)
    cs_list = cs_grant_sub.add_parser("list", help="List the consent grants the authority log holds.")
    common(cs_list)
    cs_list.set_defaults(func=_cmd_consent_grant_list)


def _add_consent_away_parser(cs_sub: argparse._SubParsersAction) -> None:
    """``levain consent away [show|on|off|auto]`` under the ``consent`` subparsers ``cs_sub``."""
    away = cs_sub.add_parser(
        "away", help="Show or set away mode: whether the consent hook holds a request for a signed answer.",
        description="Away mode off: the hook answers only by a standing grant and otherwise leaves "
                    "the agent's own dialog to ask you at once, so a signed answer has nothing to land on. On: the "
                    "hook holds each request no grant answers for up to --hold seconds so you can answer it with "
                    "`levain consent answer` or your phone. Claude Code shows its own dialog during the hold too "
                    "(an allow from the hook dismisses it); Codex shows no dialog until the hold ends, so for Codex "
                    "away means away. `on` and `off` are your word and stand until `auto` clears it; with no word "
                    "(the default `levain init` writes; a settings file from before auto-away keeps its on or off "
                    "as your word), away mode is on while this Mac has had no keyboard, mouse or trackpad input for "
                    "--idle-after seconds (default 600) and an enrolled key could sign an answer; there is no input-"
                    "idle source on other platforms, so there only `on` turns it on. A held request ends early when "
                    "away mode turns off (your input, or `off`), leaving the agent's own dialog to ask. `on` refuses "
                    "when no enrolled key in the install's cockpit could sign an answer. `on` also records an away "
                    "mark in the consent queue store, which the hook reads as well as the settings file: leaving it "
                    "(`off` or `auto`) takes a leave signed by a key enrolled in the install's cockpit "
                    "(--signing-key), and rewriting the away words in the settings file does not end it. Revoking the "
                    "key that signed a leave puts away mode back on. Any enrolled key may sign a leave, whatever its "
                    "class. A consent store that cannot be read is never read as no mark: `off` and `auto` refuse "
                    "until it is repaired or moved aside. A process running as you is beyond what levain "
                    "stops without OS confinement: it can rewrite the store, or point the settings file's "
                    "cockpit_data_root at a cockpit holding its own key (doctor warns on a foreign cockpit). "
                    "Input-idle away needs no signature to end: your input ends it. Changes only "
                    "away_mode, away_word, away_hold_seconds and away_idle_seconds in <queue dir>/claude_code.json "
                    "and codex.json; grants nothing (every answer still verifies as a signature). `show` says which "
                    "source decides now.")
    away.add_argument("action", nargs="?", choices=("show", "on", "off", "auto"), default="show")
    away.add_argument("--agent", choices=("claude_code", "codex", "all"), default="all",
                      help="Which agent's settings (default: every agent that has a settings file).")
    away.add_argument("--hold", type=float, default=None,
                      help="Seconds to hold a request in away mode, more than 0 and at most 840, the longest the "
                           "hook waits (default: the file's hold, else 300 for Claude Code and 120 for Codex).")
    away.add_argument("--idle-after", dest="idle_after", type=float, default=None,
                      help="With `auto`: seconds of no input after which away mode turns on (default: the file's, "
                           "else 600).")
    away.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                      help="With `off` or `auto`: an enrolled key that signs leaving an away mode `on` set (its "
                           "private key file, or the .pub of a key an agent holds). With --agent all, one "
                           "signature per marked agent, in turn: a failure part way leaves the agents before it "
                           "left and exits non-zero. Not needed when no `on` stands, but whenever it is given it "
                           "is checked (the file and its .pub readable) before anything changes.")
    away.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                      help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    away.set_defaults(func=_cmd_consent_away)


def _cmd_consent_away(args: argparse.Namespace) -> int:
    from levain.consent import away
    from levain.consent.adapters.claude_code import away_mark_of, effective_away
    from levain.consent.auto_away import hold_seconds
    from levain.consent.queue import QueueStoreError, default_queue_dir

    # abspath, not resolve(): the queue directory's symlink check must see the path as given (L3 r1)
    qdir = Path(os.path.abspath(args.queue_dir.expanduser())) if args.queue_dir is not None else default_queue_dir()
    agents = list(away.AGENTS) if args.agent == "all" else [args.agent]
    if args.action == "show" and args.hold is not None:
        print("levain consent away: --hold goes with `on`, `off` or `auto`", file=sys.stderr)
        return 2
    if args.action != "auto" and args.idle_after is not None:
        print("levain consent away: --idle-after goes with `auto`", file=sys.stderr)
        return 2
    if args.action not in ("off", "auto") and args.signing_key is not None:
        print("levain consent away: --signing-key goes with `off` or `auto`", file=sys.stderr)
        return 2
    try:
        away.check_queue_dir(qdir)
    except (OSError, ValueError, QueueStoreError) as e:
        print(f"levain consent away: {e}", file=sys.stderr)
        return 1
    if args.action in ("off", "auto") and args.signing_key is None:
        # a missing key is refused before any agent changes; with a key, each marked agent takes its own signature
        # in the loop below, so one failing part way leaves the agents before it left (and exits non-zero)
        marked = []
        for agent in agents:
            try:
                if away.read(qdir, agent) is not None:
                    cur = away.settings(qdir, agent)
                    if cur.away_marked and not cur.away_unreadable:   # an unreadable store: the loop below says so
                        marked.append(agent)
            except (OSError, ValueError, QueueStoreError):
                pass   # the loop below reports it
        if marked:
            print(f"levain consent away: away mode was set by `levain consent away on` for {', '.join(marked)}, "
                  "and leaving it takes a signature: run it again with --signing-key <an enrolled key>; no agent "
                  "was changed", file=sys.stderr)
            return 1
    rc, seen = 0, 0
    for agent in agents:
        try:
            try:
                present = away.read(qdir, agent) is not None
            except ValueError as e:
                if args.action != "show" or "does not parse" not in str(e):
                    raise
                # the hook reads a file that does not parse as the defaults; show says both
                print(f"{agent}: away mode off (the hook reads the defaults: {e})")
                seen += 1
                continue
            if not present:
                msg = f"{agent}: no settings file in {qdir} (`levain init` with the consent hook on writes it)"
                if args.agent == "all":
                    print(f"{agent}: skipped, no settings file")
                else:
                    print(msg, file=sys.stderr)
                    rc = 1
                continue
            if args.action != "show":
                word = {"on": True, "off": False, "auto": None}[args.action]
                away.set_away(qdir, agent, word, args.hold, args.idle_after, signing_key=args.signing_key)
            got = away.settings(qdir, agent)  # what the hook reads, not the raw file and the store's mark
            seen += 1
        except (OSError, ValueError, QueueStoreError) as e:
            print(f"levain consent away: {agent}: {e}", file=sys.stderr)
            rc = 1
            continue
        on, source, why = effective_away(got)
        reason = away.why_unanswerable(got)
        if got.away_unreadable:
            print(f"{agent}: {got.away_unreadable}; repair it, or move it aside (a store moved aside reads as no "
                  "away mark, which ends away mode too)")
            rc = 1
            continue
        if on:
            hold = min(hold_seconds(got, agent, source), away.MAX_HOLD)
            state = " (signed to leave)" if got.away_marked else ""
            line = f"{agent}: away mode ON{state}, holds up to {hold:g}s"
            line += " (Claude Code's own dialog shows meanwhile)" if agent == "claude_code" else \
                " (Codex shows no dialog until the hold ends)"
        else:
            line = f"{agent}: away mode off"
        line += f"; WARN: {reason}" if reason else f"; answers verify against {got.cockpit_data_root}"
        line += f"; set by {why}"
        if got.away_marked:
            try:
                mark = away_mark_of(qdir, away.claude_code.settings_from(away.read(qdir, agent)), agent)
            except Exception:  # noqa: BLE001 - read a moment ago; say what the hook meets
                line += f"; {away.claude_code.store_unreadable(qdir)}"
            else:
                if mark is not None and mark.leave_by is not None and mark.leave_refusal is not None:
                    line += (f"; a leave signed by {mark.leave_by} does not verify ({mark.leave_refusal}), so the "
                             "mark stands")
                line += "; `levain consent away off --signing-key <key>` leaves it"
        elif source == "word" and not (got.legacy and not on):  # a legacy off's reason already names the command
            line += "; `levain consent away auto` hands it back to input idle"
        elif source == "idle" and not on:
            line += f"; on after {got.away_idle_seconds:g}s of no keyboard, mouse or trackpad input"
        print(line)
    if seen == 0 and rc == 0:
        print(f"levain consent away: no consent settings in {qdir}; `levain init` with the consent hook on writes "
              "them", file=sys.stderr)
        return 1
    return rc


def _add_consent_answer_parser(cs_sub: argparse._SubParsersAction) -> None:
    """``levain consent pending`` and ``levain consent answer`` under the ``consent`` subparsers ``cs_sub``."""
    queue_help = "The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent)."
    cs_pending = cs_sub.add_parser(
        "pending", help="List the consent requests waiting on an answer.",
        description="One line per pending request, oldest first: its id, agent, tool, working directory, the "
                    "paths it targets, its tier, when it was filed and how long its hold has left. Answers "
                    "nothing.")
    cs_pending.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=queue_help)
    cs_pending.set_defaults(func=_cmd_consent_pending)
    cs_answer = cs_sub.add_parser(
        "answer", help="Sign an allow or a deny for one pending request.",
        description="Show the pending request as the agent filed it, then sign the answer with --signing-key and "
                    "hand it to the queue, which verifies it against a fresh replay of the authority log (an "
                    "enrolled, unrevoked key; for an allow, a key class that covers the request's tier) and "
                    "records it. The hook waiting on the request then delivers it to the agent. One request, "
                    "one signature; a deny is signed too. A request whose input is too long to show here in "
                    "full is not signed: the agent's own dialog stands.")
    cs_answer.add_argument("request_id", help="The request's id, as `levain consent pending` shows it.")
    cs_answer.add_argument("decision", choices=("allow", "deny"))
    cs_answer.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                           help="An enrolled key that signs the answer (its private key file, or the .pub of a "
                                "key an agent holds). An allow of an agent-config edit needs a laptop-presence "
                                "key.")
    cs_answer.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=queue_help)
    cs_answer.add_argument("--data-root", type=Path, default=None, dest="data_root",
                           help="The cockpit data root whose authority log verifies the answer. Default: the one "
                                "the queue's hook settings name for the request's agent (its cockpit_data_root); "
                                "a root other than that one is refused before anything is signed.")
    cs_answer.add_argument("--path", type=Path, default=None,
                           help="An install directory whose ledger's cockpit/ is that data root (as --data-root).")
    cs_answer.set_defaults(func=_cmd_consent_answer)
    cs_phone = cs_sub.add_parser(
        "phone", help="Answer consent requests from a phone with a passkey (serves the phone port).",
        description="Serve a small consent page on its own loopback port, for an https proxy you run (for example "
                    "`tailscale serve`) to put on your tailnet. From the phone you see what each agent asked and "
                    "answer it with a passkey, which the queue verifies like a terminal answer. A passkey is a "
                    "display-phone key: it can allow a T2 request and deny any; a T3 allow stays on the laptop. "
                    "The page also makes the phone's passkey and shows the laptop command that enrols it (a "
                    "passkey never enrols itself). Nothing else is served there: not the cockpit, not your "
                    "install. The printed link carries this server's token; anyone holding it can read the "
                    "waiting requests until it stops, and none can answer without an enrolled passkey.")
    cs_phone.add_argument("--name", default=None,
                          help="The https name your proxy serves the phone port on (it becomes the passkey's "
                               "name, so keep it: a passkey made on one name never answers on another).")
    cs_phone.add_argument("--tailscale", action="store_true",
                          help="Take --name from this machine's Tailscale name (`tailscale status`) and print the "
                               "`tailscale serve` line. Changes nothing in Tailscale itself.")
    cs_phone.add_argument("--port", type=int, default=None,
                          help=f"The loopback port (default {_phone_port_default()}).")
    cs_phone.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=queue_help)
    cs_phone.add_argument("--sign-namespace", dest="sign_namespaces", action="append", default=None,
                          type=_sign_namespace, metavar="NS",
                          help="Also list the sign requests filed in the queue's sign/ directory in namespace NS "
                               "(repeatable), for a user-verified passkey signature (Face ID, Touch ID or the device "
                               "passcode, as the authenticator reports it). levain checks user verification; a "
                               "caller's `ssh-keygen -Y verify -n NS` checks the key, the message and the namespace "
                               "only. The queue dir's claude_code.json must name the cockpit (cockpit_data_root). A "
                               "namespace beginning 'levain', one levain verifies in, or git, file or email is "
                               "refused.")
    cs_phone.set_defaults(func=_cmd_consent_phone)


def _add_push_parser(subparsers: argparse._SubParsersAction) -> None:
    """``levain push``: the gated push (``levain.consent.push_gate``)."""
    from levain.consent.push_gate import add_parser

    add_parser(subparsers)


def _add_band_parsers(subparsers: argparse._SubParsersAction) -> None:
    """``levain ask`` and ``levain question``: the question band (``levain.consent.band``)."""
    from levain.consent.band import add_parsers

    add_parsers(subparsers)


def _phone_port_default() -> int:
    from levain.phone_server import DEFAULT_PHONE_PORT

    return DEFAULT_PHONE_PORT


def _sign_namespace(text: str) -> str:
    from levain.consent.sign import namespace_problem

    problem = namespace_problem(text)
    if problem is not None:
        raise argparse.ArgumentTypeError(problem)
    return text


def _cmd_consent_phone(args: argparse.Namespace) -> int:
    from levain.phone_server import DEFAULT_PHONE_PORT, run_phone_server

    from levain.consent.queue import default_queue_dir

    # the phone port may start before any agent has filed a request: a queue with no database yet lists nothing
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    return run_phone_server(qdir, name=args.name, tailscale=args.tailscale,
                            port=DEFAULT_PHONE_PORT if args.port is None else args.port,
                            sign_namespaces=tuple(dict.fromkeys(args.sign_namespaces or ())))


def _consent_queue_dir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import DB_NAME, default_queue_dir

    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    if not (qdir / DB_NAME).is_file():
        raise FileNotFoundError(f"no consent queue at {qdir} (no {DB_NAME}); pass --queue-dir")
    return qdir


def _reading_queue(qdir: Path) -> object:
    """The queue in ``qdir`` for reading: its authority source is the adapters' no-authority one (the queue
    directory, which sqlite cannot open as a log), so nothing read through it can ever verify."""
    from levain.consent.queue import ConsentQueue, authority_source

    return ConsentQueue(qdir, authority=authority_source(qdir, anchor=None))


# What the terminal must never receive raw from an agent-sourced string: every C0/C1 control and DEL (Cc), every
# format control, the bidi controls among them (Cf), a lone surrogate (Cs), and the line and paragraph separators
# (Zl, Zp). Each is printed as its escape, \uXXXX (\UXXXXXXXX past the BMP).
_TERM_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _term_safe(text: object) -> str:
    import unicodedata

    return "".join((f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
                   if unicodedata.category(c) in _TERM_UNSAFE else c for c in str(text))


def __getattr__(name: str) -> object:
    # the shared "display too long to read back" sentinel keeps its old name here, imported on first use (lazy)
    if name == "_UNSHOWN":
        from levain.consent.listing import UNSHOWN

        return UNSHOWN
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _filed_input(request: object) -> object:
    """The tool input as filed (``levain.consent.listing.filed_input``, shared with the cockpit page)."""
    from levain.consent.listing import filed_input

    return filed_input(request)


def _consent_targets(request: object) -> str:
    """The paths a grant's criteria would read from the request (``levain.consent.listing.consent_targets``,
    shared with the cockpit page). A display only: nothing is decided from it."""
    from levain.consent.listing import consent_targets

    return consent_targets(request)


def _time_left(expires_at: str, now: datetime) -> str:
    from levain.cockpit.consent import parse_stamp

    left = int((parse_stamp(expires_at) - now).total_seconds())
    if left <= 0:
        return "expiring"
    return f"{left // 60}m{left % 60:02d}s left" if left >= 60 else f"{left}s left"


def _cmd_consent_pending(args: argparse.Namespace) -> int:
    import sqlite3

    from levain.cockpit.consent import ConsentError
    from levain.consent.dialog import LABELS
    from levain.consent.listing import read_listing
    from levain.consent.queue import QueueStoreError

    try:
        qdir = _consent_queue_dir(args)
        pending = _reading_queue(qdir).pending()
        if not pending:
            print("no pending consent requests")
        now = _utc_now()
        for r in pending:
            print(_term_safe(f"{r.id}  {r.agent}  {r.tool_name}  cwd {r.cwd}  targets {_consent_targets(r)}  "
                             f"{r.tier}  filed {r.received_at}  {_time_left(r.expires_at, now)}"))
        # what the agent's own dialog did with the requests levain left to it: unsigned, never an answer
        left = [e for e in read_listing(qdir, now=now).recent if e.state in ("withdrawn", "expired")]
        if left:
            print("left to the agent's own dialog (last 24 h, UNSIGNED: not answers levain verified):")
            for e in left:
                print(_term_safe(f"  {e.request.id}  {e.request.agent}  {e.request.tool_name}  "
                                 f"{e.decided_at}  {LABELS.get(e.dialog, LABELS[None])}"))
    except (OSError, QueueStoreError, ConsentError, sqlite3.Error, *_cockpit_refusals()) as exc:
        print(f"pending: {_term_safe(exc)}", file=sys.stderr)
        return 1
    return 0


def _answer_root(args: argparse.Namespace, qdir: Path, request: object) -> Path:
    """The cockpit data root an answer to ``request`` is verified against (``levain.consent.answering.answer_root``,
    shared with the phone port's passkey route); ``--data-root`` / ``--path`` must name that same directory."""
    from levain.consent.answering import answer_root

    named = None if args.data_root is None and args.path is None else Path(_cockpit_root(args)).expanduser()
    return answer_root(qdir, request, named)


def _cmd_consent_answer(args: argparse.Namespace) -> int:
    from levain.authority import anchor_path
    from levain.cockpit.consent import ConsentError, utc_stamp
    from levain.cockpit.keys import ssh_signer
    from levain.consent.answering import refuse_cut_display
    from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source
    from levain.consent.record import ANSWER_NAMESPACE, answer_message, answer_payload

    try:
        qdir = _consent_queue_dir(args)
        got = _reading_queue(qdir).get(args.request_id)      # the one read: expires it first if its time is up
        if got is None:
            raise AnswerRefused(f"no sealed request {args.request_id!r}")
        request, state = got
        if state != "pending":
            raise AnswerRefused(f"request {request.id} is already {state}")
        refuse_cut_display(request, "from the terminal")
        root = _answer_root(args, qdir, request)
        queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))
        signer_fp, sign = ssh_signer(args.signing_key, namespace=ANSWER_NAMESPACE)
        payload = answer_payload(queue_id=queue.queue_id, request=request, decision=args.decision,
                                 signer_fp=signer_fp, signed_at=utc_stamp(_utc_now()))
        print(_term_safe(f"answering {request.id}: {args.decision.upper()}"))
        for label, value in (("agent", f"{request.agent} (session {request.agent_session_id})"),
                             ("tool", request.tool_name),
                             ("input as filed", request.display[len(request.tool_name) + 1:]),
                             ("cwd", request.cwd), ("tier", request.tier),
                             ("filed", f"{request.received_at}, hold ends {request.expires_at}"),
                             ("input digest", request.tool_input_digest),
                             ("request digest", payload["request_digest"]), ("signer", signer_fp)):
            print(f"  {label:<15} {_term_safe(value)}")
        sys.stdout.flush()
        signature = sign(answer_message(payload))
        decision = queue.answer(request.id, payload, signature)
    except KeyboardInterrupt:
        print("answer cancelled", file=sys.stderr)
        return 1
    except (AnswerRefused, OSError, QueueStoreError, ConsentError, *_cockpit_refusals()) as exc:
        print(f"answer refused: {_term_safe(exc)}", file=sys.stderr)
        return 1
    print(_term_safe(f"{'allowed' if decision.decision == 'allow' else 'denied'}: {decision.request_id} by "
                     f"{decision.signer_fp} at {decision.decided_at}"))
    return 0


def _add_consent_rule_parser(cs_sub: argparse._SubParsersAction) -> None:
    """``levain consent rule`` (a late ruling on an undecided request, R-a) and ``levain consent deliver`` under the
    ``consent`` subparsers ``cs_sub``."""
    queue_help = "The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent)."
    rule = cs_sub.add_parser(
        "rule", help="Sign a late ruling (allow or deny) on an undecided request, by its gap number.",
        description="A request whose hold ended with no answer is UNDECIDED and numbered per agent session (gap "
                    "#N); the agent was told it is open. This signs your ruling on it with --signing-key, in the "
                    "ruling namespace (never an answer's), and hands it to the queue, which verifies it against a "
                    "fresh replay of the authority log. The ruling is then delivered: a session still running gets it "
                    "at its next step; an ended one is resumed with it as its prompt, in the directory and permission "
                    "mode it was started in. The ledger rows of the gap's window are printed beside it. With no "
                    "gap, lists the undecided requests.")
    rule.add_argument("gap", nargs="?", default=None,
                      help="The gap: its number (3 or #3) when no other recent session has that number, else with "
                           "its session handle as the pings and the listing show it (3@a1b2c3).")
    rule.add_argument("decision", nargs="?", choices=("allow", "deny"), default=None)
    rule.add_argument("--session", default=None,
                      help="The agent session the gap belongs to (its id, or a unique start of it), as an "
                           "alternative to the gap's @handle.")
    rule.add_argument("--agent", default=None, choices=("claude_code", "codex"), help="The agent, likewise.")
    rule.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                      help="An enrolled key that signs the ruling (as for `levain consent answer`). An allow of an "
                           "agent-config edit needs a laptop-presence key.")
    rule.add_argument("--no-deliver", dest="deliver", action="store_false",
                      help="Record the ruling only; `levain consent deliver` delivers it later.")
    rule.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=queue_help)
    rule.add_argument("--data-root", type=Path, default=None, dest="data_root",
                      help="The cockpit data root whose authority log verifies the ruling (as for `levain consent "
                           "answer`).")
    rule.add_argument("--path", type=Path, default=None,
                      help="An install directory whose ledger's cockpit/ is that data root (as --data-root).")
    rule.set_defaults(func=_cmd_consent_rule)
    dlv = cs_sub.add_parser(
        "deliver", help="Deliver every late ruling whose text has not reached its session yet.",
        description="For a ruling left to a session's next step that never came (the session ended first): a session "
                    "that has ended is resumed with the ruling as its prompt; one still running keeps waiting. Safe "
                    "to run from cron or launchd: a ruling's text reaches its session once.")
    dlv.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=queue_help)
    dlv.set_defaults(func=_cmd_consent_deliver)


def _rule_target(qdir: Path, gap_text: str, session: str | None, agent: str | None) -> object:
    """The one undecided request named by ``gap_text`` (``3``, ``#3``, ``3@a1b2c3``; of ``session`` / ``agent`` when
    named), or raise ``AnswerRefused`` naming the candidates. Gap numbers start at 1 in every session, so a bare
    number is refused when that number names a gap in more than one session, ruled ones of any age included: a gap
    already ruled must never let the number fall through to another session's gap."""
    from levain.consent.queue import AnswerRefused, gap_label, session_handle

    if session is not None and not session.strip():
        raise AnswerRefused("--session is empty: it would name every session")
    number, at, handle = gap_text.strip().lstrip("#").partition("@")
    if not number.isdigit() or (at and not handle):
        raise AnswerRefused(f"{gap_text!r} is not a gap (3, #3 or 3@a1b2c3)")
    gap = int(number)
    found = [g for g in _reading_queue(qdir).gapped(agent) if g.gap == gap
             and (not handle or session_handle(g.request.agent_session_id) == handle
                  or g.request.agent_session_id.startswith(handle))
             and (session is None or g.request.agent_session_id.startswith(session))]
    named = f"gap #{gap}" + (f"@{handle}" if handle else "") + (f" in session {session}" if session else "")
    if not found:
        raise AnswerRefused(f"no request has {named}; `levain consent rule` with no gap lists the undecided ones")
    if len(found) > 1:
        names = "; ".join(f"{gap_label(g.gap, g.request.agent_session_id)} ({g.request.agent}, {g.state})"
                          for g in found)
        raise AnswerRefused(f"{named} names {len(found)} sessions' gaps ({names}); name it with its @handle")
    if found[0].state != "undecided":
        raise AnswerRefused(f"{gap_label(gap, found[0].request.agent_session_id)} is already ruled "
                            f"({found[0].state})")
    return found[0]


def _print_gap_window(qdir: Path, gapped: object, ruled_at: str) -> None:
    """The ledger rows of the gap's session from its undecided time to the ruling (DESIGN §3.6, the record side)."""
    from levain.consent.ledger import DB_NAME, SUBAGENT_MARK, WINDOW_SCOPE, Ledger, reconcile

    if not (qdir / DB_NAME).is_file():
        print("  gap window: no ledger in this queue directory (no rows recorded)")
        return
    ledger = Ledger(qdir)
    root = gapped.request.agent_session_id.split(SUBAGENT_MARK, 1)[0]
    try:
        reconcile(ledger, give_up_after=None, agent_session_id=root, subagents=True)
    except Exception as exc:  # noqa: BLE001 - a stamp left pending is shown as pending, never hidden
        print(f"  (stamps not reconciled: {_term_safe(exc)})")
    rows = ledger.window(gapped.request.agent, gapped.request.agent_session_id, gapped.undecided_at, ruled_at)
    print(f"  gap window {gapped.undecided_at} .. {ruled_at}: {len(rows)} ledger row(s) in this session and its "
          "subagents")
    for c in rows:
        sub = c.agent_session_id.partition(SUBAGENT_MARK)[2]
        print(_term_safe(f"    {c.received_at}  {c.phase:14} {c.stamp:16} {c.outcome:9} {c.tool_name}  "
                         f"{c.tool_input_digest[:20]}" + (f"  subagent {sub}" if sub else "")))
    print(f"  ({WINDOW_SCOPE})")


def _print_delivery(d: object) -> None:
    print(_term_safe(f"  delivery: {d.route}: {d.reason}"))
    if d.argv:
        import shlex

        shown = [*d.argv[:-1], d.argv[-1] if len(d.argv[-1]) <= 60 else d.argv[-1][:57] + "..."]
        print(_term_safe(f"  resume: (cd {shlex.quote(d.cwd or '')} && {shlex.join(shown)})"))


def _delivery_queue(qdir: Path, request: object) -> object:
    """The queue whose authority source is the cockpit the request's hook verifies against (delivery re-verifies
    each ruling it claims)."""
    from levain.authority import anchor_path
    from levain.consent.answering import answer_root
    from levain.consent.queue import ConsentQueue, authority_source

    root = answer_root(qdir, request)
    return ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))


def _cmd_consent_rule(args: argparse.Namespace) -> int:
    from levain.authority import anchor_path
    from levain.cockpit.consent import ConsentError
    from levain.cockpit.keys import ssh_signer
    from levain.consent.answering import refuse_cut_display
    from levain.consent.delivery import agent_notify, deliver
    from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source, gap_label
    from levain.consent.record import RULING_NAMESPACE, ruling_message

    try:
        qdir = _consent_queue_dir(args)
        if args.gap is None:
            rows = _reading_queue(qdir).undecided_for(args.agent)
            for g in rows:
                r = g.request
                print(_term_safe(f"gap {gap_label(g.gap, r.agent_session_id)}  {r.agent}  session "
                                 f"{r.agent_session_id}  {r.tool_name}  targets {_consent_targets(r)}  {r.tier}  "
                                 f"undecided since {g.undecided_at}"))
            if not rows:
                print("no undecided requests")
            return 0
        if args.decision is None or args.signing_key is None:
            raise AnswerRefused("a ruling needs allow|deny and --signing-key")
        gapped = _rule_target(qdir, args.gap, args.session, args.agent)
        request = gapped.request
        if args.decision == "allow":
            refuse_cut_display(request, "from the terminal")   # a deny is signed even over a cut display
        root = _answer_root(args, qdir, request)
        queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))
        signer_fp, sign = ssh_signer(args.signing_key, namespace=RULING_NAMESPACE)
        payload = queue.ruling_payload(request.id, args.decision, signer_fp)
        print(_term_safe(f"ruling on gap {gap_label(gapped.gap, request.agent_session_id)}: "
                         f"{args.decision.upper()} ({request.id})"))
        for label, value in (("agent", f"{request.agent} (session {request.agent_session_id})"),
                             ("tool", request.tool_name),
                             ("input as filed", request.display[len(request.tool_name) + 1:]),
                             ("cwd", request.cwd), ("tier", request.tier),
                             ("undecided since", gapped.undecided_at),
                             ("request digest", payload["request_digest"]), ("signer", signer_fp)):
            print(f"  {label:<15} {_term_safe(value)}")
        sys.stdout.flush()
        ruling = queue.answer_late(request.id, payload, sign(ruling_message(payload)))
    except KeyboardInterrupt:
        print("ruling cancelled", file=sys.stderr)
        return 1
    except (AnswerRefused, OSError, QueueStoreError, ConsentError, *_cockpit_refusals()) as exc:
        print(f"ruling refused: {_term_safe(exc)}", file=sys.stderr)
        return 1
    print(_term_safe(f"ruled {ruling.ruling}: gap {gap_label(ruling.gap, request.agent_session_id)} "
                     f"({ruling.request_id}) by {ruling.signer_fp} at "
                     f"{ruling.decided_at}"))
    try:
        _print_gap_window(qdir, gapped, ruling.decided_at)
    except Exception as exc:  # noqa: BLE001 - the ruling stands; the window is a report
        print(f"  gap window: not read ({_term_safe(exc)})")
    if not args.deliver:
        print("  delivery: not attempted (--no-deliver); `levain consent deliver` delivers it")
        return 0
    try:
        got = deliver(queue, ruling.request_id, notify=agent_notify(qdir, request.agent))
    except Exception as exc:  # noqa: BLE001 - the ruling stands; its next hook or `consent deliver` delivers it
        print(f"  delivery: failed ({_term_safe(type(exc).__name__)}: {_term_safe(exc)}); the ruling stands and "
              "lands at the session's next step, or `levain consent deliver`")
        return 1
    _print_delivery(got)
    return 1 if got.route == "failed" else 0


def _cmd_consent_deliver(args: argparse.Namespace) -> int:
    from levain.cockpit.consent import ConsentError
    from levain.consent.delivery import agent_notify, deliver
    from levain.consent.queue import AnswerRefused, QueueStoreError, gap_label

    try:
        qdir = _consent_queue_dir(args)
        waiting = _reading_queue(qdir).undelivered_rulings()
    except (AnswerRefused, OSError, QueueStoreError, ConsentError, *_cockpit_refusals()) as exc:
        print(f"deliver: {_term_safe(exc)}", file=sys.stderr)
        return 1
    if not waiting:
        print("no ruling waits for delivery")
    failed = 0
    for g in waiting:
        print(_term_safe(f"gap {gap_label(g.gap, g.request.agent_session_id)} ({g.request.agent} session "
                         f"{g.request.agent_session_id})"))
        try:
            queue = _delivery_queue(qdir, g.request)
            if queue.ruling_delivered(g.request.id) is not None:
                print("  delivery: delivered: an earlier resume in this run carried it")
                continue
            d = deliver(queue, g.request.id, notify=agent_notify(qdir, g.request.agent))
        except Exception as exc:  # noqa: BLE001 - one ruling's error never stops the rest
            print(f"  delivery: failed ({_term_safe(type(exc).__name__)}: {_term_safe(exc)})")
            failed += 1
            continue
        _print_delivery(d)
        failed += d.route == "failed"
    return 1 if failed else 0


def _grant_expiry(text: str) -> object:
    """``--expires``: a duration (``30m``, ``12h``, ``7d``) as a timedelta, or a UTC time as an aware datetime."""
    import re
    from datetime import timedelta

    m = re.fullmatch(r"(\d{1,6})([mhd])", text.strip())
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return timedelta(**{unit: int(m.group(1))})
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        raise ValueError(f"--expires {text!r} is neither a duration (30m, 12h, 7d) nor a UTC time "
                         "(2026-10-20T12:00:00Z)")
    return when


def _grant_mcp_fields(path_fields: list[str], mcp_fields: list[str]) -> dict[str, dict[str, str]]:
    """``--path-field TOOL:FIELD`` and ``--mcp-field TOOL:FIELD=KIND`` as tool -> field -> kind (split at the last
    ``:``, then a field from its kind at the first ``=``); a field declared twice with two kinds is refused. The log
    judges the names and kinds (``levain.authority._validate_mcp_fields``)."""
    out: dict[str, dict[str, str]] = {}
    for spec, kind in [(f, "path") for f in path_fields] + [(f, None) for f in mcp_fields]:
        tool, sep, name = spec.rpartition(":")
        if kind is None:
            name, eq, kind = name.partition("=")
            if not eq:
                raise ValueError(f"--mcp-field {spec!r} is not TOOL:FIELD=KIND")
        if not sep or not tool or not name:
            raise ValueError(f"{spec!r} is not TOOL:FIELD")
        if out.setdefault(tool, {}).setdefault(name, kind) != kind:
            raise ValueError(f"{tool}:{name} is declared both {out[tool][name]} and {kind}")
    return out


def _grant_fields_text(declared: dict[str, dict[str, str]]) -> str:
    return "; ".join(f"{t} " + ",".join(f"{f}={k}" for f, k in sorted(fields.items()))
                     for t, fields in sorted(declared.items()))


def _cmd_consent_grant_create(args: argparse.Namespace) -> int:
    from levain.authority import _CONSENT_MCP_TOOL, _consent_shell_name, consent_mcp_tool_problem
    from levain.cockpit.keys import ssh_signer
    from levain.consent.grants import create_grant

    declared: dict[str, dict[str, str]] | None = None     # None until the declarations parse
    try:
        expires = _grant_expiry(args.expires)
        declared = _grant_mcp_fields(args.path_fields, args.mcp_fields)
        stray = sorted(set(declared) - set(args.tools))
        if stray:
            raise ValueError(f"fields are declared for {stray[0]!r}, which is not among --tool")
        signer_fp, sign = ssh_signer(args.signing_key)
        grant, warning = create_grant(_cockpit_root(args), agent=args.agent, tools=args.tools,
                                      workspace=args.workspace, postures=args.postures, expires=expires,
                                      signer_fp=signer_fp, sign=sign, clock=_utc_now, grant_id=args.grant_id,
                                      mcp_fields=declared or None)
    except (ValueError, *_cockpit_refusals()) as exc:
        print(f"grant refused: {exc}", file=sys.stderr)
        # the declare hint is a remedy only for a well-formed MCP name that is not a shell: no declaration grants those
        undeclared = [] if declared is None else [t for t in args.tools if t not in declared
                                                  and _CONSENT_MCP_TOOL.fullmatch(t) and not _consent_shell_name(t)
                                                  and consent_mcp_tool_problem(t) is not None]
        if undeclared:
            print(f"  to grant {undeclared[0]}, declare every field it takes: --path-field {undeclared[0]}:FIELD "
                  f"for a path, --mcp-field {undeclared[0]}:FIELD=KIND for any other", file=sys.stderr)
        return 1
    print(f"granted: {grant.id} at seq {grant.seq}: {grant.agent} {', '.join(grant.tools)} in {grant.workspace} "
          f"under {', '.join(grant.postures)}, until {grant.expires_at}")
    if grant.mcp_fields:
        print(f"  declared fields ({grant.kind}): {_grant_fields_text(grant.mcp_fields)}")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_consent_grant_revoke(args: argparse.Namespace) -> int:
    from levain.cockpit.keys import ssh_signer
    from levain.consent.grants import revoke_grant

    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        seq, warning = revoke_grant(_cockpit_root(args), args.grant_id, signer_fp=signer_fp, sign=sign,
                                    clock=_utc_now)
    except _cockpit_refusals() as exc:
        print(f"revoke refused: {exc}", file=sys.stderr)
        return 1
    print(f"revoked: {args.grant_id} at seq {seq}")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_consent_grant_list(args: argparse.Namespace) -> int:
    from levain.cockpit.consent import utc_stamp
    from levain.consent.grants import grant_listing

    try:
        now = _utc_now()
        grants, inert = grant_listing(_cockpit_root(args), now=now)
    except _cockpit_refusals() as exc:
        print(f"grants: {exc}", file=sys.stderr)
        return 1
    if not grants and not inert:
        print("no consent grants")
    stamp = utc_stamp(now)
    for g in grants:
        state = "expired" if g.expires_at <= stamp else f"until {g.expires_at}"
        refused = [t for t in g.tools if g.tool_problem(t) is not None]
        if refused:
            state += f" (answers nothing for {','.join(map(str, refused))}: a tool a grant may not name)"
        print(f"{g.id}  {g.agent}  {','.join(map(str, g.tools))}  {g.workspace}  postures {','.join(map(str, g.postures))}  "
              f"{state}  "
              f"(seq {g.seq})")
        if g.mcp_fields:
            print(f"  declared fields ({g.kind}): {_grant_fields_text(g.mcp_fields)}")
    for gid, kind, expires_at, seq in inert:
        state = "expired" if expires_at <= stamp else f"until {expires_at}"
        print(f"{gid!r}  kind {kind}  {state}  (seq {seq}): a grant kind this levain does not know, held inert: it "
              "answers nothing here (upgrade levain to use it; `levain consent grant revoke` removes it)")
    return 0


def _cmd_cockpit_keys(args: argparse.Namespace) -> int:
    from levain.cockpit.custody import custody_note
    from levain.cockpit.keys import keys

    try:
        rows, note = keys(_cockpit_root(args), now=_utc_now())
        notes = custody_note(rows)
    except _cockpit_refusals() as exc:
        print(f"keys: {exc}", file=sys.stderr)
        return 1
    if note:
        print(note)
    if not rows:
        print("no keys enrolled")
    for r in rows:
        state = f"revoked at seq {r.revoked_seq}" if r.revoked_seq is not None else "active"
        if r.compromised_since is not None:
            state += f", compromised since seq {r.compromised_since}"
        print(f"{r.fingerprint}  {r.key_class:<16} custody {r.custody:<14} {state:<44} enrolled at seq {r.enrolled_seq}"
              + (f" ({r.enrolled_at})" if r.enrolled_at else "")
              + (f", first enrolled {r.legacy_enrolled_at}" if r.legacy_enrolled_at else "")
              + (f"; {notes[r.fingerprint]}" if r.fingerprint in notes else ""))
    return 0


def _sign_refusals() -> tuple[type[BaseException], ...]:
    from levain.autonomic.journal import JournalCorruptError
    from levain.cockpit.consent import ConsentError
    from levain.cockpit.sign_client import SignRefused

    return (SignRefused, JournalCorruptError, ConsentError, *_cockpit_refusals())


def _cmd_cockpit_pending(args: argparse.Namespace) -> int:
    from levain.cockpit.sign_client import journal

    try:
        now = _utc_now()
        rows = journal(_cockpit_root(args)).decision_rows(now)
    except _sign_refusals() as exc:
        print(f"pending: {_term_safe(exc)}", file=sys.stderr)
        return 1
    if not rows.pendings:
        print("no waiting pendings")
    for p in rows.pendings:
        where = "/".join(str(p.target.get(k)) for k in ("panel_id", "row_id") if p.target.get(k) is not None)
        print(_term_safe(f"{p.pending_id}  {p.verb}  {p.tier}  {where or '-'}  by {p.principal}  "
                         f"issued {p.issued_at}  {_time_left(p.expires_at, now)}"))
    if rows.skipped:
        print(f"note: {rows.skipped} corrupt op(s) skipped", file=sys.stderr)
    return 0


def _cmd_cockpit_sign(args: argparse.Namespace) -> int:
    import getpass
    import os

    from levain.autonomic.confirm import COCKPIT_NAMESPACE
    from levain.cockpit.keys import ssh_signer
    from levain.cockpit.firejournal import CockpitDB, UnknownPending
    from levain.cockpit.sign_client import (TOKEN_ENV, SignRefused, header_safe_token, journal, key_refusal,
                                            loopback_url, post_signature, render, server_listening, waiting)

    try:
        url = loopback_url(args.url)
        db = journal(_cockpit_root(args))
        now = _utc_now()
        view = waiting(db, args.pending_id, now)
        signer_fp, sign = ssh_signer(args.signing_key, namespace=COCKPIT_NAMESPACE)
        refusal, key_class = key_refusal(db, signer_fp, view.record["tier"], now=now)
        if refusal is not None:
            raise SignRefused(f"{refusal} (nothing signed; the pending stays WAITING)")
        token = os.environ.get(TOKEN_ENV)
        if not token:
            try:
                token = getpass.getpass(f"write token (or set {TOKEN_ENV}): ")
            except EOFError:      # no terminal and nothing on stdin
                token = ""
        if not token.strip():
            raise SignRefused(f"no write token: set {TOKEN_ENV} or type the one `levain serve --write` printed")
        token = header_safe_token(token)
        server_listening(url)
        for line in render(view, signer_fp=signer_fp, signer_class=key_class):
            print(_term_safe(line))
        sys.stdout.flush()
        signature = sign(view.record_bytes)
        server_listening(url)   # again after the touch, which takes seconds: the listener must still be this user's
        answer = post_signature(url, view.pending_id, signature, token)
    except KeyboardInterrupt:
        print("sign cancelled", file=sys.stderr)
        return 1
    except _sign_refusals() as exc:
        print(f"sign refused: {_term_safe(exc)}", file=sys.stderr)
        return 1
    body = answer.body
    if 300 <= answer.status < 400:
        print(_term_safe(f"sign refused: HTTP {answer.status} (a redirect, not followed): {answer.raw[:500]}"),
              file=sys.stderr)
        return 1
    if answer.status != 200 or body is None:
        if body is not None:
            print(_term_safe(f"sign refused: {body.get('error')}: {body.get('message')}"), file=sys.stderr)
        else:
            print(_term_safe(f"sign refused: HTTP {answer.status}: {answer.raw[:500]}"), file=sys.stderr)
        return 1
    print(_term_safe(f"{body.get('state')}" + (f" ({body.get('reason')})" if body.get("reason") else "")
                     + f": decision {body.get('decision_kind')}" + (f", {body.get('label')}" if body.get("label") else "")
                     + (" (a replay: this pending was already decided)" if body.get("replay") else "")))
    if not (body.get("replay") is False and body.get("decision_kind") == "ALLOWED" and body.get("state") == "COMMITTED"):
        return 1
    # the answer counts only when the journal the operator chose agrees: a listener that is not its server
    # (another install's, or none of ours) cannot make a success
    try:
        after = CockpitDB(db.data_root).load(view.pending_id)
    except (UnknownPending, *_sign_refusals()) as exc:
        print(f"sign unconfirmed: the journal could not be re-read ({_term_safe(exc)})", file=sys.stderr)
        return 1
    if after.state != "COMMITTED":
        print(_term_safe(f"sign unconfirmed: the server answered COMMITTED but {view.pending_id} is {after.state} "
                         f"in {db.path}: the server at {url} does not hold this journal"), file=sys.stderr)
        return 1
    return 0


def _cmd_cockpit_key_create(args: argparse.Namespace) -> int:
    from levain.cockpit import custody as _custody
    from levain.cockpit.keys import fingerprint_of

    try:
        chosen = args.custody
        if chosen is None:
            chosen, why = _custody.default_custody()
            print(f"custody: {chosen} ({why}; pass --custody to choose)", flush=True)
        if chosen == "touch-id":
            made = _custody.create_touch_id(args.dir, args.label)
        else:
            made = _custody.create_passphrase(args.dir, args.label)
        fp = fingerprint_of(made.public_key)
    except _cockpit_refusals() as exc:
        print(f"key create refused: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("key create cancelled", file=sys.stderr)
        return 1
    print(f"created: {made.path} ({made.custody}, {fp})"
          + (f"; CTK identity {made.ctk_hash}, label {made.label}" if made.ctk_hash else ""))
    print(f"public key: {made.public_path}")
    print(_GUARANTEE[made.custody])
    print(f"next: `levain cockpit ledger init --signing-key {made.path}` for a new log, or `levain cockpit "
          f"enroll {made.public_path} --signing-key <an enrolled laptop-presence key>` (the custody is read "
          "from the key's sidecar)")
    return 0


# what each custody guarantees, as the CHANGELOG and README state it (DESIGN §3.2 rev 6 item 6)
_GUARANTEE = {
    "touch-id": "guarantee: a fingerprint per signature, on a key that cannot leave this Mac's Secure Enclave. "
                "Declared custody, not proven: no Mac key carries attestation.",
    "passphrase": "guarantee: a typed passphrase per signature, and nothing more: malware running as you can "
                  "keylog it or copy the file, so this is the lowest class.",
}


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _cmd_cockpit_ledger_init(args: argparse.Namespace) -> int:
    from levain.authority import LAPTOP
    from levain.cockpit.custody import declared_custody
    from levain.cockpit.keys import init_plan, ledger_init, signing_public_key, ssh_signer

    root = plan = None
    try:
        root = _cockpit_root(args)
        signer_pub = signing_public_key(args.signing_key)
        # checked before anything is signed: the custody table, the sidecar, the encrypted file, the CTK identity
        signer_custody = declared_custody(args.signing_key, args.custody, LAPTOP, signer_pub)
        signer_fp, sign = ssh_signer(args.signing_key)
        plan = init_plan(root, now=_utc_now())
        if plan.migrated:
            print(f"this store was upgraded from levain-cockpit/2 and held {len(plan.rows)} key(s):")
        else:
            print("a fresh store: its first link enrols the signing key as laptop-presence, signed by "
                  "itself (trust on first use)")
        if plan.seq:
            print(f"an earlier init was cut off with the log's head at seq {plan.seq}; this run resumes it. "
                  f"the log holds {len(plan.log_keys)} key(s), each kept only if confirmed:")
            for k in plan.log_keys:
                notes = ["the genesis" if k.fingerprint == plan.genesis else "enrolled"]
                if k.revoked_seq is not None:
                    notes.append(f"revoked at seq {k.revoked_seq}")
                if k.fingerprint == signer_fp:
                    notes.append("the signing key")
                if k.fingerprint not in {r.fingerprint for r in plan.rows}:
                    notes.append("no legacy row")
                print(f"  seq {k.enrolled_seq}  {k.fingerprint}  {k.key_class:<16} custody {k.custody} "
                      f"({'; '.join(notes)})")
            if plan.rows:
                print("the legacy keys:")
        for r in plan.rows:
            notes = ["revoked" + (", compromised" if r.compromised else "") if r.revoked else "active"]
            if r.fingerprint == signer_fp:
                notes.append("the signing key")
            if r.in_log is not None:
                notes.append(f"in the log at seq {r.in_log.enrolled_seq}")
            if r.problem:
                notes.append(f"SKIPPED: {r.problem}")
            custody = signer_custody if r.fingerprint == signer_fp else "unstated"
            print(f"  {r.fingerprint}  {r.key_class:<16} {r.key_type:<36} enrolled {r.enrolled_at or '?'}  "
                  f"custody {custody} ({'; '.join(notes)})")
        if not plan.seq and signer_fp not in {r.fingerprint for r in plan.rows}:
            print(f"  {signer_fp}  {'laptop-presence':<16} {signer_pub.split()[0]:<36} the signing key, custody "
                  f"{signer_custody} (new: the genesis)")
        if args.confirm or args.drop_unlisted:
            confirmed, drop = set(args.confirm), args.drop_unlisted
        elif sys.stdin is not None and sys.stdin.isatty():
            confirmed, drop = set(), True
            asks = [signer_fp] + [r.fingerprint for r in plan.rows
                                  if r.problem is None and r.fingerprint != signer_fp]
            if plan.genesis is not None and plan.genesis not in asks:
                asks.insert(0, plan.genesis)             # a resume keeps the log's genesis only if confirmed
            for fp in asks:
                what = (" (the log's genesis)" if fp == plan.genesis and fp != signer_fp else
                        " (the signing key, the genesis)" if fp == signer_fp and plan.genesis in (None, fp) else
                        " (the signing key)" if fp == signer_fp else "")
                if _ask(f"keep {fp}{what}? [y/N] "):
                    confirmed.add(fp)
        else:
            sys.stdout.flush()
            print("ledger init refused: stdin is not a terminal, so nothing can be confirmed; pass "
                  "--confirm <fingerprint> for each key to keep (the signing key included) and "
                  "--drop-unlisted to drop the rest", file=sys.stderr)
            return 1
        result = ledger_init(root, signer_public_key=signer_pub, signer_fp=signer_fp, sign=sign, clock=_utc_now,
                             confirmed=confirmed, drop_unlisted=drop, signer_custody=signer_custody)
    except _cockpit_refusals() as exc:
        sys.stdout.flush()                    # the listing above, before the refusal under it
        print(f"ledger init refused: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:                 # Ctrl-C at a keep prompt or during a signing touch
        sys.stdout.flush()
        where = ""
        try:
            # re-read only a store this run already opened (a /2 one is migrated by then, never here)
            seq = 0 if root is None or plan is None else init_plan(root, now=_utc_now()).seq
            if seq:
                where = f"; the log is at seq {seq}; the store stays uninitialised; re-run to resume"
        except _cockpit_refusals() + (KeyboardInterrupt,):
            pass                              # where the log stands is a courtesy, never a second traceback
        print(f"ledger init cancelled{where}", file=sys.stderr)
        return 1
    for line in result.links:
        print(line)
    for fp in result.dropped:
        print(f"dropped: {fp}")
    for fp in result.skipped:
        print(f"skipped: {fp}")
    print(f"initialised: store {result.store_id}, head at seq {result.seq}")
    if result.warning:
        print(f"warning: {result.warning}", file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from levain.web_server import run_web_server

    if not _resolve_api_key(args):
        return 2

    return run_web_server(
        path=args.path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        write=args.write,
        chat=args.chat,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        max_iterations=args.max_iterations,
        turn_seconds=args.turn_seconds,
        install_class=args.install_class,
    )


def _cmd_serve_app(args: argparse.Namespace) -> int:
    from levain.app_server import run_app_server

    return run_app_server(path=args.path)


def _cmd_docs(args: argparse.Namespace) -> int:
    from levain.docs_server import run_docs_web

    return run_docs_web(
        path=args.path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )


def _daemon_provider():
    """Resolve this OS's provider, or print the planned-addition message + signal exit 2."""
    from levain import daemon

    try:
        return daemon.select_provider(), None
    except NotImplementedError as exc:
        print(f"levain daemon: {exc}", file=sys.stderr)
        return None, 2


def _cmd_daemon_install(args: argparse.Namespace) -> int:
    from levain import daemon

    provider, err = _daemon_provider()
    if provider is None:
        return err
    spec = daemon.build_spec(install_path=args.path, port=args.port, label=args.label)
    try:
        result = provider.install(spec)
    except daemon.DaemonError as exc:
        print(f"levain daemon install failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    print(f"\n  serves http://127.0.0.1:{args.port}  (loopback, governed-writable)")
    print(f"  write token + unlocked link: the latest start's lines in {spec.stdout_log}")
    print(f"\n⚠ {daemon.THREAT_MODEL_NOTE}")
    return 0


def _seat_bounds_line(max_iterations: int | None, max_seconds: float | None) -> str:
    """Render the seat's two bounds as one line, naming what is UNBOUNDED as such.

    Both are shown together because they bound different failure modes and an operator reading one
    cannot infer the other: steps bound a turn that keeps working, wall-clock bounds a turn that
    STOPS working (a hung model call). "unbounded" is printed as a word rather than omitted — a
    missing bound rendered as a blank reads as a default, which is how an unbounded seat would look
    exactly like a bounded one.
    """
    steps = f"{max_iterations} steps" if max_iterations is not None else "steps UNBOUNDED"
    secs = f"{max_seconds:g}s wall-clock" if max_seconds is not None else "wall-clock UNBOUNDED"
    return f"{steps} · {secs}"


def _seat_consolidate_line(
    consolidate: bool, every: int | None, max_seconds: float | None
) -> str:
    """Render the seat's self-consolidate posture — the answer to "does this rewrite its own memory".

    Printed for BOTH states rather than only when enabled. An operator scanning the banner for
    "what does this thing do to itself while I am not here" must get an answer, and an absent line
    reads as "no such feature" rather than "the feature is off" — the same reason
    :func:`_seat_bounds_line` prints the word UNBOUNDED instead of omitting a missing bound.
    """
    if not consolidate:
        return (
            "OFF — this seat will accumulate raw episodes and never metabolize them, so its "
            "recall degrades as it runs"
        )
    thr = f"every {every} episodes" if every is not None else "at the default episode threshold"
    secs = f"{max_seconds:g}s bound" if max_seconds is not None else "UNBOUNDED"
    return f"ON ({thr}, {secs}) · may metabolize, may NEVER crystallize"


def _print_bound_cadence_notes(
    max_seconds: float | None,
    interval: int,
    *,
    consolidate: bool = False,
    consolidate_max_seconds: float | None = None,
) -> None:
    """Say what the two numbers MEAN TOGETHER — printed by the dry-run AND the real install.

    ⚠ ONE FUNCTION, TWO CALL SITES, deliberately. The first version of this lived only after
    `provider.install()`, so `--dry-run` — the surface whose entire purpose is *inspect before you
    commit* — showed `wall-clock UNBOUNDED` with no warning at all, while the real install warned
    loudly about the same config. Two surfaces disagreeing about one fact, with the silent one being
    the one the cautious operator uses. Found by running the dry-run and reading it, not by review;
    same shape as the K4a [7] finding where a pointer existed in two places and fixing one copy left
    the other lying (`guard_scoped_by_symptom_misses_the_class`).
    """
    # THE BOUND AND THE CADENCE INTERACT, AND THE OPERATOR CANNOT SEE IT FROM EITHER FLAG ALONE.
    # launchd coalesces per label, so a turn allowed to run longer than the interval necessarily
    # SKIPS intervals — the cadence silently becomes the turn's duration. NOT an error and not
    # refused: "poll as often as possible, one at a time" is a legitimate pattern (short interval,
    # long bound). But it must be SAID, because the operator asked for one cadence and will get
    # another, and a schedule that quietly means something else is how the original defect hid.
    #
    # ⚠ THE COMPARISON IS AGAINST THE SUM OF BOTH BOUNDS, NOT THE TURN'S ALONE (K4a [6]). What
    # occupies the label is the PROCESS, and a consolidating seat's process is turn THEN consolidate,
    # sequentially, each with its own budget. Checking only `max_seconds` would have printed a clean
    # "1800s < 3600s cadence, all good" for a seat whose real worst case is 2700s — and the operator
    # would have been told the schedule holds when it does not. Same class as the two banners that
    # answered one question with a resolved line and a static one.
    # ⚠ THESE THREE ARE INDEPENDENT `if`s, NOT AN elif CHAIN (fixed 2026-07-30, Diogenes).
    # They were chained, but the conditions they warn about CO-OCCUR: a turn bound >= interval
    # AND an unbounded consolidate is a legal, reachable configuration, and only the first branch
    # fired. Worse, its own arithmetic dropped the tail — `consolidate_max_seconds or 0` makes an
    # UNBOUNDED consolidate contribute ZERO — so it printed "the seat's worst-case run (4000s
    # turn)" for a seat whose worst case is UNBOUNDED, while the "TURN bounded but CONSOLIDATE is
    # not" silent-death warning was swallowed by the `elif`. The operator was told the schedule
    # holds, by the very banner that exists to tell them when it does not.
    # The suite structurally could not catch it: every test of that warning uses a 600s turn
    # against a 3600s interval, so the branches never compete.
    unbounded_tail = consolidate and consolidate_max_seconds is None
    occupancy = (max_seconds or 0) + (consolidate_max_seconds or 0 if consolidate else 0)
    # An unbounded consolidate means unbounded occupancy — it cannot be under ANY cadence.
    if max_seconds is not None and (unbounded_tail or occupancy >= interval):
        if unbounded_tail:
            parts = f"{max_seconds:g}s turn + an UNBOUNDED consolidate"
        else:
            parts = f"{max_seconds:g}s turn"
            if consolidate and consolidate_max_seconds is not None:
                parts += f" + {consolidate_max_seconds:g}s consolidate = {occupancy:g}s"
        print(
            f"\n  ⓘ the seat's worst-case run ({parts}) is >= the cadence ({interval}s). launchd "
            f"coalesces per label,\n"
            f"    so a long run will SKIP intervals — the effective cadence becomes the run's "
            f"own duration.\n"
            f"    Fine if that is what you want; lower the bounds or raise --interval if not."
        )
    if max_seconds is not None and consolidate and consolidate_max_seconds is None:
        # The turn is bounded and the consolidate is NOT. The turn bound alone looks reassuring, so
        # this asymmetry has to be named or the seat reads as bounded while owning an unbounded tail.
        print(
            "\n  ⚠ the TURN is bounded but the CONSOLIDATE is not (--consolidate-max-seconds 0). "
            "A compose stalled\n"
            "    against a dead endpoint would hang this seat forever behind a unit still "
            "reporting installed +\n"
            "    loaded — the same silent death the turn bound closes, through the other door."
        )
    if max_seconds is None:
        # The one genuinely dangerous configuration. It is opt-in, so say what it costs.
        print(
            "\n  ⚠ NO WALL-CLOCK BOUND (--max-seconds 0). A turn that hangs inside one step will "
            "never exit,\n"
            "    and because launchd coalesces per label this seat would then STOP RUNNING "
            "PERMANENTLY behind a\n"
            "    unit still reporting installed + loaded. Nothing will tell you. Set "
            "--max-seconds unless something\n"
            "    else bounds this process."
        )


def _cmd_daemon_install_seat(args: argparse.Namespace) -> int:
    from levain import daemon
    from levain.session import require_openhands_entity

    provider, err = _daemon_provider()
    if provider is None:
        return err

    # VALIDATE THE ENTITY HERE — daemon.build_seat_spec deliberately does not (it is a stdlib-only
    # leaf and must not import the run/confinement layer). Without this an unrunnable seat installs
    # happily and then fails IDENTICALLY every interval, forever, into a log nobody is watching —
    # the schedule turns a one-time usage error into a silent permanent one.
    entity = args.path.expanduser().resolve()
    problem = require_openhands_entity(entity)
    if problem:
        print(f"levain daemon install-seat: {problem}", file=sys.stderr)
        return 2

    # 0 is the operator's explicit "unbounded" (the SDK's own limit); None means "use our finite
    # default". Distinguishing them keeps the safe default finite while leaving the escape hatch.
    # A NEGATIVE bound is neither: it is a typo (`-1` for `1`) that serializes straight into the
    # unit's argv and is only "discovered" as whatever the SDK does with it, once per interval,
    # in a log nobody is watching — silently defeating the finite default the time-bound rests on.
    # Rejected at the gate. (Found INDEPENDENTLY by both L3 lineages — codex LOW + glm MEDIUM.)
    if args.max_iterations is not None and args.max_iterations < 0:
        print(
            f"levain daemon install-seat: --max-iterations must be >= 0, got "
            f"{args.max_iterations}. Use 0 for the SDK's own limit (explicitly unbounded), or "
            f"omit the flag for the finite default ({daemon.DEFAULT_SEAT_MAX_ITERATIONS}).",
            file=sys.stderr,
        )
        return 2
    max_iters = (
        daemon.DEFAULT_SEAT_MAX_ITERATIONS if args.max_iterations is None
        else (None if args.max_iterations == 0 else args.max_iterations)
    )

    # SAME THREE-VALUED TREATMENT FOR THE WALL-CLOCK BOUND — deliberately identical shape to the
    # step bound above rather than a second convention, because the two flags answer the same
    # question in different units and an operator should not have to learn each one separately.
    # Same non-finite guard as `levain run` — one flag, two doors, and the K2 defect was exactly an
    # invariant that only its strongest caller enforced. A `nan` here is worse than there: it
    # serializes into the PLIST as `--max-seconds nan`, so the seat looks bounded to anyone auditing
    # the unit file and is not.
    if args.max_seconds is not None and not math.isfinite(args.max_seconds):
        print(
            f"levain daemon install-seat: --max-seconds must be a finite number of seconds, got "
            f"{args.max_seconds!r}. Use 0 to install an explicitly unbounded seat.",
            file=sys.stderr,
        )
        return 2
    if args.max_seconds is not None and args.max_seconds < 0:
        print(
            f"levain daemon install-seat: --max-seconds must be >= 0, got "
            f"{args.max_seconds:g}. Use 0 to disable the wall-clock bound explicitly, or omit "
            f"the flag for the finite default ({daemon.DEFAULT_SEAT_MAX_SECONDS:g}s).",
            file=sys.stderr,
        )
        return 2
    max_secs = (
        daemon.DEFAULT_SEAT_MAX_SECONDS if args.max_seconds is None
        else (None if args.max_seconds == 0 else args.max_seconds)
    )

    # THE CONSOLIDATE PHASE — same three-valued treatment for its bound, and the same rejection of
    # a tuning flag whose feature is switched off.
    consolidate = not getattr(args, "no_consolidate", False)
    if _reject_bad_max_seconds(
        getattr(args, "consolidate_max_seconds", None), command="daemon install-seat",
        flag="--consolidate-max-seconds",
    ):
        return 2
    if getattr(args, "consolidate_every", None) is not None and args.consolidate_every < 1:
        print(
            f"levain daemon install-seat: --consolidate-every must be >= 1, got "
            f"{args.consolidate_every}. A threshold of 0 would consolidate on an EMPTY store, "
            f"burning a model call every interval to metabolize nothing.",
            file=sys.stderr,
        )
        return 2
    if not consolidate and (
        getattr(args, "consolidate_every", None) is not None
        or getattr(args, "consolidate_max_seconds", None) is not None
    ):
        print(
            "levain daemon install-seat: --consolidate-every / --consolidate-max-seconds "
            "contradict --no-consolidate. Refused rather than silently ignored: an operator who "
            "tuned a consolidate and installed a seat that never runs one would have no way to "
            "notice for as long as the seat lives.",
            file=sys.stderr,
        )
        return 2
    consolidate_secs = (
        None if not consolidate
        else daemon.DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS
        if args.consolidate_max_seconds is None
        else (None if args.consolidate_max_seconds == 0 else args.consolidate_max_seconds)
    )

    # Read the entity's DECLARED gate posture and resolve it exactly as the runtime will for an
    # unattended drive, so the output below states what will actually happen (see the print block).
    # Fail-safe: an unreadable/…invalid config must not silently produce a "governed" claim.
    try:
        spec = daemon.build_seat_spec(
            entity_path=entity, task=args.task, interval=args.interval,
            label=args.label, model=args.model, max_iterations=max_iters,
            max_seconds=max_secs,
            consolidate=consolidate,
            consolidate_every=getattr(args, "consolidate_every", None),
            consolidate_max_seconds=consolidate_secs,
        )
    except ValueError as exc:      # an incoherent schedule (e.g. --interval 0)
        print(f"levain daemon install-seat: {exc}", file=sys.stderr)
        return 2

    # Resolve the seat's GOVERNANCE POSTURE — after the spec, because the drive mode is read off
    # the spec's own argv rather than assumed.
    try:
        from levain.firing.confinement import load_confinement_config
        from levain.firing.drive import resolve_cred_floor
        from levain.firing.gate import resolve_gate_mode

        # str-typed on purpose: "unknown" is OUR third state (config unreadable), which is not
        # part of the runtime's GateMode literal — and collapsing it into either real state is
        # exactly the guess this branch exists to refuse.
        _cfg = load_confinement_config(entity)
        declared_gate: str = _cfg.efferent_gate
        gate_mode: str = resolve_gate_mode(declared_gate, human_present=False)
        # The FLOOR posture, resolved the same way and for the same reason. Until K4a the banner
        # carried one RESOLVED line (the gate) directly above one STATIC line (the floor),
        # answering the same operator question — and THAT ASYMMETRY WAS THE BUG.
        # DERIVED from the spec, never hardcoded: build_seat_spec emits --unattended today. Since
        # 2026-10-12 every drive resolves the same cred floor, so the derivation matters only if a
        # later policy splits them again (glm L3 LOW: a claim outliving its enforcement).
        # ⚠ NOT COVERED BY A TEST, deliberately: `build_seat_spec` always emits `--unattended`, so
        # hardcoding it here is presently INDISTINGUISHABLE from deriving it, and a mutation of
        # this line survives the suite. Recorded rather than papered over with a test that fakes a
        # second seat shape — the day one exists, that test becomes real.
        _seat_mode = "unattended" if "--unattended" in spec.argv else "headless"
        cred_floor: bool | None = resolve_cred_floor(_cfg.deny_standard_creds, mode=_seat_mode)
        cred_declared: bool | None = _cfg.deny_standard_creds
    except (OSError, ValueError, RuntimeError) as exc:
        # NARROW on purpose. A bare `except Exception` here caught a NameError from a mis-ordered
        # reference in this very function and reported it as "governance posture unknown" — a
        # coding error wearing a config error's clothes, which would have made the next typo
        # silently permanent. Config-read failures are OSError/ValueError (the loader raises
        # ConfinementError, a RuntimeError); a programming error must crash loudly instead.
        declared_gate, gate_mode = f"unreadable ({type(exc).__name__})", "unknown"
        cred_floor, cred_declared = None, None

    if gate_mode == "unknown":
        # The runtime loads this same config fail-closed, so a seat installed now would REFUSE to
        # start on every single interval — turning one config typo into recurring scheduled noise
        # in a log nobody is watching. Refuse at install, where a human is present to read it
        # (codex L3 LOW).
        print(
            f"levain daemon install-seat: cannot read this entity's confinement config "
            f"({declared_gate}). The runtime loads it fail-closed, so a seat installed now would "
            f"fail every {args.interval}s forever. Fix .levain/confinement.json (check it with "
            f"`levain doctor --path {entity}`) and re-run.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        plan = provider.would_install(spec)
        st = plan.current
        on_disk = ("yes (differs from rendered)" if plan.on_disk and plan.would_change
                   else "yes (matches rendered)" if plan.on_disk else "no")
        print(f"install-seat dry-run for {plan.label} (NOTHING is changed):")
        print(f"  entity:  {entity}")
        print(f"  task:    {args.task}")
        print(f"  cadence: every {spec.start_interval}s")
        print(f"  bounds:  {_seat_bounds_line(max_iters, max_secs)}")
        print(f"  memory:  {_seat_consolidate_line(consolidate, args.consolidate_every, consolidate_secs)}")
        print(f"  unit:    {plan.unit_path}")
        print(f"  on disk: {on_disk}")
        print(f"  live:    {st.load_state} — {st.detail}")
        print(f"  action:  {plan.action}")
        print("\n  honesty floor: a unit file on disk is NOT proof the service is loaded.")
        _print_bound_cadence_notes(
            max_secs, args.interval,
            consolidate=consolidate, consolidate_max_seconds=consolidate_secs,
        )
        return 0

    try:
        result = provider.install(spec)
    except daemon.DaemonError as exc:
        print(f"levain daemon install-seat failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    print(f"\n  seat:   {entity}")
    print(f"  task:   {args.task}")
    print(f"  bounds: {_seat_bounds_line(max_iters, max_secs)}")
    print(f"  memory: {_seat_consolidate_line(consolidate, args.consolidate_every, consolidate_secs)}")
    _print_bound_cadence_notes(
        max_secs, args.interval,
        consolidate=consolidate, consolidate_max_seconds=consolidate_secs,
    )
    # BOTH streams, and the .err one is named LAST because it carries the decision. launchd sends
    # stdout and stderr to SEPARATE files, and the gated-halt report ("HELD AT THE EFFERENT GATE"
    # + the pending-action list) is printed to STDERR. Naming only the stdout path sends the
    # operator to a file that shows the entity's ACTIVITY but never the fact that it was STOPPED —
    # it would break the exact fan-in this command claims to provide. (codex L3 HIGH.)
    print(f"  activity: {spec.stdout_log}")
    print(f"  DECISIONS + gated halts: {spec.stderr_log}   ← the fan-in surface")
    print("\n  manage it with this label (the daemon subcommands default to the COCKPIT label):")
    print(f"    levain daemon status    --label {args.label}")
    print(f"    levain daemon restart   --label {args.label}      # force a turn now")
    print(f"    levain daemon uninstall --label {args.label}")

    # TELL THE TRUTH ABOUT THE GATE — never assert a governance property without checking it.
    # An entity pinned `efferent_gate: "ungated"` runs its efferent actions with nobody present
    # and nothing halting them; claiming otherwise here is worse than silence, because this is the
    # last thing the operator reads before they stop watching. Resolved through the RUNTIME's own
    # resolve_gate_mode with human_present=False (how a seat is actually driven), so this claim
    # cannot drift from the behaviour it describes. (glm L3.)
    from levain.firing.confinement import cred_floor_label

    print()
    if cred_floor is True:
        why = ("pinned by deny_standard_creds: true" if cred_declared is True
               else "the default")
        print(f"  cred floor: {cred_floor_label()} are DENIED ({why}).")
    elif cred_floor is False:
        print(
            f"  ⚠ cred floor: {cred_floor_label()} are READABLE by this\n"
            "    seat — deny_standard_creds is explicitly false, so the default is\n"
            "    OVERRIDDEN. It can read those credentials with nobody watching; the gate stops\n"
            "    it SENDING them anywhere, but not reading them into memory that persists."
        )
    print()
    if gate_mode == "gated":
        print(
            "⚠ GOVERNED, NOT AUTONOMOUS: with no human present every efferent action HALTS at "
            "the K3 gate before it runs (exit 4) and is reported in the decisions log above — "
            "that log IS the fan-in, so a seat you never read is a seat you are not governing."
        )
    elif gate_mode == "unknown":
        # Could not read the posture. Say THAT — do not guess in either direction. A false
        # "governed" tells the operator to stop watching; a false "ungated" cries wolf.
        print(
            f"⚠ GATE POSTURE UNKNOWN — could not read this entity's confinement config\n"
            f"   ({declared_gate}). The seat is installed, but whether its efferent actions are\n"
            f"   halted for you CANNOT BE STATED HERE. Check with `levain doctor --path {entity}`\n"
            f"   and read the first turn's decisions log before trusting it unattended."
        )
    else:
        print(
            f"🚨 UNGATED SEAT — THIS ENTITY IS NOT GOVERNED. Its .levain/confinement.json pins\n"
            f"   efferent_gate: \"{declared_gate}\", so the K3 gate is DISARMED: every efferent\n"
            f"   action (bash, file writes) will EXECUTE unattended, on a schedule, with no human\n"
            f"   to fan in to and nothing to halt it. That is the ungoverned-autonomy posture\n"
            f"   Levain exists to refuse. Set efferent_gate to \"auto\" to restore the gate."
        )
    return 0


def _cmd_daemon_uninstall(args: argparse.Namespace) -> int:
    from levain import daemon

    provider, err = _daemon_provider()
    if provider is None:
        return err
    try:
        print(provider.uninstall(args.label))
    except daemon.DaemonError as exc:
        print(f"levain daemon uninstall failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    provider, err = _daemon_provider()
    if provider is None:
        return err
    st = provider.status(args.label)
    print(f"{args.label}: installed={st.installed} running={st.running}")
    print(f"  {st.detail}")
    return 0


def _cmd_daemon_would_install(args: argparse.Namespace) -> int:
    from levain import daemon

    provider, err = _daemon_provider()
    if provider is None:
        return err
    spec = daemon.build_spec(install_path=args.path, port=args.port, label=args.label)
    plan = provider.would_install(spec)
    st = plan.current
    on_disk = ("yes (differs from rendered)" if plan.on_disk and plan.would_change
               else "yes (matches rendered)" if plan.on_disk else "no")
    print(f"would-install dry-run for {plan.label} (NOTHING is changed):")
    print(f"  unit:    {plan.unit_path}")
    print(f"  on disk: {on_disk}")
    print(f"  live:    {st.load_state} — {st.detail}")
    print(f"  action:  {plan.action}")
    print("\n  honesty floor: a unit file on disk is NOT proof the service is loaded or running.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    from levain.update import run_update

    return run_update(
        path=args.path,
        dry_run=args.dry_run,
        yes=args.yes,
        no_pip=args.no_pip,
        ack=args.ack,
    )


def _cmd_messaging(args: argparse.Namespace) -> int:
    from levain.install import run_messaging_set, run_messaging_show

    if getattr(args, "messaging_cmd", None) == "set":
        return run_messaging_set(args.path, args.mode, yes=args.yes, no_pip=args.no_pip)
    return run_messaging_show(args.path)


def _cmd_adopt_answers(args: argparse.Namespace) -> int:
    from levain.install import run_adopt_answers

    return run_adopt_answers(args.path, dry_run=args.dry_run)


def _cmd_setup_isolation(args: argparse.Namespace) -> int:
    from levain.firing.hands import cmd_setup_isolation

    ports = tuple(args.egress_ports) if args.egress_ports is not None else None
    return cmd_setup_isolation(args.path, undo=args.undo, dry_run=args.dry_run, reenable=args.reenable,
                               egress_ports=ports)


def _cmd_ssh_add(args: argparse.Namespace) -> int:
    from levain.firing import ssh_agent

    try:
        print(ssh_agent.add_key(args.key.expanduser(), args.hosts,
                                lifetime=ssh_agent.DEFAULT_LIFETIME if args.lifetime is None else args.lifetime,
                                session=args.session, waive_destinations=args.waive))
    except ssh_agent.AgentError as exc:
        print(f"levain ssh-add: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_ws_git(args: argparse.Namespace) -> int:
    from levain.firing.ws_git import cmd_ws_git

    git_args = args.git_args[1:] if args.git_args[:1] == ["--"] else args.git_args
    return cmd_ws_git(args.path, args.repo, git_args)


def _cmd_ws_adopt(args: argparse.Namespace) -> int:
    from levain.firing.ws_git import cmd_ws_adopt

    return cmd_ws_adopt(args.path, args.repo, args.name)


def _cmd_ws_put(args: argparse.Namespace) -> int:
    from levain.firing.ws_git import cmd_ws_put

    return cmd_ws_put(args.path, args.src, args.dest)


def _cmd_daemon_restart(args: argparse.Namespace) -> int:
    from levain import daemon

    provider, err = _daemon_provider()
    if provider is None:
        return err
    try:
        print(provider.restart(args.label))
    except daemon.DaemonError as exc:
        print(f"levain daemon restart failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Through the console entry, so `python -m levain.cli` gets the launch re-exec too.
    from levain.launch import main as _launch_main

    sys.exit(_launch_main())
