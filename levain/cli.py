"""Levain CLI — `levain init`, `levain doctor`, `levain verify-hooks`.

The entry point declared by `pyproject.toml` ([project.scripts] levain).
Subcommand handlers live in sibling modules; this file is dispatch only.
Lazy imports keep `levain --help` fast and isolate import errors per command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from levain import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="levain",
        description=(
            "A portable cognitive-partnership memory + methodology kit. "
            "Ship the seed that grows a practice, not the practice."
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
            "Every slot must be present; \"\" is allowed only for an optional "
            "section (equivalent to answering the terminal's `Skip this section?` "
            "with y) or an optional-line field. Get a blank one with "
            "--answers-template."
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
    init_p.set_defaults(func=_cmd_init)

    doc_p = subparsers.add_parser(
        "doctor",
        help="Loud, in-environment health check of an install.",
        description=(
            "Check that an install is wired correctly: interpreter resolves, "
            "MCP server is registered for the detected adapter(s), the store "
            "is reachable, the hook scripts are present and runnable. Exits "
            "nonzero on any failure so it composes with shell pipelines."
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
    tui_p.set_defaults(func=_cmd_tui)

    focus_p = subparsers.add_parser(
        "focus",
        help="Set / show the operator's live focus — what you're working on now.",
        description=(
            "The operator's live, self-authored attention context — 'what I'm on "
            "right now' — that every session reads to orient (it travels across "
            "sessions, like the rest of the substrate). With TEXT, sets it; with no "
            "argument, shows the current focus + its freshness; --clear unsets it. "
            "Stored in the install's .levain/context.json and read into the "
            "dashboard / TUI / web cockpit. (A sensor app may write the same "
            "three-key contract into its own context file instead.)"
        ),
    )
    focus_p.add_argument(
        "text", nargs="?", default=None,
        help="The focus to set (omit to show the current one).",
    )
    focus_p.add_argument(
        "--path", type=Path, default=Path.cwd(),
        help="Install directory (default: cwd).",
    )
    focus_p.add_argument("--clear", action="store_true", help="Unset the focus.")
    focus_p.add_argument(
        "--source", default="cli",
        help="Provenance tag for who set it (default: cli).",
    )
    focus_p.set_defaults(func=_cmd_focus)

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
            "persistent bash — both fenced to a crown-jewels floor: they work on "
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
            "1 completed with no reply, 2 startup/usage error, 3 the turn raised. It does NOT "
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
        "--unattended",
        action="store_true",
        help=(
            "With --task: declare that NO HUMAN is in the loop at all — a scheduler invoked "
            "this and nobody will necessarily read the output (what `levain daemon install-seat` "
            "emits). Beyond the gate that --task already arms, this also folds the standard "
            "credential stores (~/.config/gh, ~/.aws/credentials, ~/.netrc) into the "
            "crown-jewels floor by default, because an unattended read can compound into "
            "always-loaded memory with nobody to notice. Override per-entity with "
            "deny_standard_creds in .levain/confinement.json."
        ),
    )
    run_p.set_defaults(func=_cmd_run)

    wrap_p = subparsers.add_parser(
        "wrap",
        help="Consolidate a sovereign entity's memory — metabolize its episodes into felt memory.",
        description=(
            "Run the human-gated CONSOLIDATE for an ISOLATED entity (created with `levain init "
            "--adapter openhands`): metabolize the raw episodes it captured while you talked to it "
            "into its lasting 6-section memory, so its identity COMPOUNDS across sessions. The "
            "entity's firing captures every turn but is forbidden to consolidate on its own — this "
            "command is the explicit operator gate that does it. The compose step runs on the "
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
            "the same governed seam `levain tui` uses, under localhost-sovereign auth). "
            "This is the sovereign v2 control surface; the in-host `serve-app` MCP App "
            "is the parked alternative."
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
            "edits through the governed write seam. Loopback-sovereign (the localhost "
            "bind + Host/CSRF guards are the auth; no token) and loopback-ONLY — there "
            "is no off-box writable serve (an install's seed/config is operator-private). "
            "Default is read-only."
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
        help="Update the known-good version set together (anneal + schema + migrations).",
        description=(
            "Reconcile this install's composed stack to the declared known-good "
            "SET in one ordered, fail-safe operation: bring anneal-memory to the "
            "tested version, re-run the partnership schema if the store drifted, "
            "surface anneal's `migrate check` instruction proposals for you to "
            "apply under review, and record the composed set. The fix for "
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

    args = parser.parse_args(argv)
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
        from levain.install import run_answers_template

        return run_answers_template(packs=args.pack)

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
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    from levain.doctor import run_doctor

    return run_doctor(path=args.path, invoke=args.invoke)


def _cmd_verify_hooks(args: argparse.Namespace) -> int:
    from levain.verify import run_verify_hooks

    return run_verify_hooks(path=args.path)


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from levain.dashboard import run_dashboard

    return run_dashboard(path=args.path, as_json=args.as_json)


def _cmd_tui(args: argparse.Namespace) -> int:
    from levain.tui import run_tui

    return run_tui(path=args.path, read_only=args.read_only)


def _cmd_focus(args: argparse.Namespace) -> int:
    from levain.dashboard import run_focus

    return run_focus(
        path=args.path, text=args.text, source=args.source, clear=args.clear
    )


def _cmd_run(args: argparse.Namespace) -> int:
    # Two drivers over ONE session (the K1 seam, `levain.session`): `--task` is the
    # non-interactive runner (one spec, exit-when-done, an exit code a caller can branch on);
    # without it, the interactive REPL. `--quiet`/`--max-iterations` only shape a task run.
    task = getattr(args, "task", None)
    # `--unattended` without `--task` is REFUSED rather than ignored. It is a security-relevant
    # DECLARATION (no human in the loop → the standard cred stores join the crown-jewels floor),
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
    if task is not None:
        from levain.run import run_task

        max_seconds = getattr(args, "max_seconds", None)
        # A NEGATIVE bound is refused, not normalized. Both L3 lineages independently caught a
        # negative `--max-iterations` reaching the seat argv, and the CLASS — not the symptom — is
        # "any numeric bound flag accepts nonsense and the nonsense becomes policy". A negative
        # wall-clock bound is worse than a bad step count: `TurnDeadline` treats non-positive as
        # DISARMED, so `--max-seconds -1` would read as "bound it tightly" and deliver "not bounded
        # at all" (`guard_scoped_by_symptom_misses_the_class`).
        if max_seconds is not None and max_seconds < 0:
            print(
                f"levain run: --max-seconds must be >= 0, got {max_seconds:g}. "
                f"Use 0 to disable the wall-clock bound explicitly.",
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
        )

    from levain.run import run_entity

    return run_entity(
        path=args.path,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        with_tools=args.with_tools,
    )


def _cmd_wrap(args: argparse.Namespace) -> int:
    from levain.wrap import wrap_entity

    return wrap_entity(
        path=args.path,
        composer=args.composer,
        base_url=args.base_url,
        api_key=args.api_key,
        dry_run=args.dry_run,
        reset=args.reset,
        affect_tag=args.affect_tag,
        affect_intensity=args.affect_intensity,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    from levain.web_server import run_web_server

    return run_web_server(
        path=args.path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        write=args.write,
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

    # Read the entity's DECLARED gate posture and resolve it exactly as the runtime will for an
    # unattended drive, so the output below states what will actually happen (see the print block).
    # Fail-safe: an unreadable/…invalid config must not silently produce a "governed" claim.
    try:
        spec = daemon.build_seat_spec(
            entity_path=entity, task=args.task, interval=args.interval,
            label=args.label, model=args.model, max_iterations=max_iters,
            max_seconds=max_secs,
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
        # DERIVED from the spec, never hardcoded: build_seat_spec emits --unattended today, but a
        # hardcoded "unattended" would keep claiming the strict floor for any future seat shape
        # that does not (glm L3 LOW) — a claim outliving its enforcement.
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
        print(f"  unit:    {plan.unit_path}")
        print(f"  on disk: {on_disk}")
        print(f"  live:    {st.load_state} — {st.detail}")
        print(f"  action:  {plan.action}")
        print("\n  honesty floor: a unit file on disk is NOT proof the service is loaded.")
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
    # THE BOUND AND THE CADENCE INTERACT, AND THE OPERATOR CANNOT SEE IT FROM EITHER FLAG ALONE.
    # launchd coalesces per label, so a turn allowed to run longer than the interval necessarily
    # skips intervals — the cadence silently becomes the bound. This is NOT an error and is not
    # refused: "poll as often as possible, one at a time" is a legitimate pattern (short interval,
    # long bound). But it must be SAID, because the operator asked for one cadence and will get
    # another, and a schedule that quietly means something else is how the original defect hid.
    if max_secs is not None and max_secs >= args.interval:
        print(
            f"\n  ⓘ the time bound ({max_secs:g}s) is >= the cadence ({args.interval}s). launchd "
            f"coalesces per label,\n"
            f"    so a long turn will SKIP intervals — the effective cadence becomes the turn's "
            f"own duration.\n"
            f"    Fine if that is what you want; lower --max-seconds or raise --interval if not."
        )
    elif max_secs is None:
        # The one genuinely dangerous configuration, and it is opt-in, so say what it costs.
        print(
            "\n  ⚠ NO WALL-CLOCK BOUND (--max-seconds 0). A turn that hangs inside one step will "
            "never exit,\n"
            "    and because launchd coalesces per label this seat would then STOP RUNNING "
            "PERMANENTLY behind a\n"
            "    unit still reporting installed + loaded. Nothing will tell you. Set "
            "--max-seconds unless something\n"
            "    else bounds this process."
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
    print()
    if cred_floor is True:
        why = ("pinned by deny_standard_creds: true" if cred_declared is True
               else "the default for an unattended seat")
        print(f"  cred floor: ~/.config/gh · ~/.aws/credentials · ~/.netrc are DENIED ({why}).")
    elif cred_floor is False:
        print(
            "  ⚠ cred floor: ~/.config/gh · ~/.aws/credentials · ~/.netrc are READABLE by this\n"
            "    seat — deny_standard_creds is explicitly false, so the unattended default is\n"
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
    sys.exit(main())
