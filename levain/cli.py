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
            "(the sovereignty guard fail-closes before the first turn). This first "
            "slice is a conversational partner (no executor tools); the file "
            "workspace is confined to <entity>/workspace/. Needs the OpenHands "
            "runtime: pip install 'levain[openhands]'."
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
        default="minimax-m3:cloud",
        help=(
            "The model to run on (default: minimax-m3:cloud). A bare name is routed "
            "through Ollama (ollama/<name>); a provider-prefixed name (ollama/…, "
            "openai/…) is used as-is."
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
    run_p.set_defaults(func=_cmd_run)

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

    return run_init(path=args.path, adapter=args.adapter, force=args.force, packs=args.pack)


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
    from levain.run import run_entity

    return run_entity(
        path=args.path,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
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
