"""The import closure of the root-run entry modules (``levain.slot.root_tree.ENTRY_MODULES``), derived at run time.

Root runs these under ``ROOT_PYTHON`` (``/usr/bin/python3``, 3.9.6 on macOS), which is older than the CI matrix, so the
``root-py39`` CI job byte-compiles this closure and runs the test files that exercise it under 3.9.

    python tests/ci/root_closure.py modules       # the import-time closure: what the entries execute when imported
    python tests/ci/root_closure.py compile       # byte-compile the ``modules`` set (exit 1 on any failure)
    python tests/ci/root_closure.py import-check  # ``python -c "import m"`` per ``modules`` member, in a subprocess
    python tests/ci/root_closure.py tests         # the tests/test_*.py files that import only ``modules`` members
    python tests/ci/root_closure.py h11-spec      # the pip requirement for the h11 floor ``root_tree.H11_MIN`` states
    python tests/ci/root_closure.py sites         # the dynamic-execution sites found, with the module(s) each runs
    python tests/ci/root_closure.py lazy          # the function-local imports in the closure, with their class

Function-local imports are a tripwire too, classified rather than followed blind: following every one chains into the
operator CLI and most of the package (``levain/slot/render.py`` imports ``levain.host_guard``, which needs 3.11
``tomllib``, inside a function only the operator's process runs). ``LAZY_SITES`` lists every function-local ``levain``
import in the closure, each ``"root"`` (a root process can run that function; its target joins the closure, to a fixed
point) or ``"operator"`` (only the operator's interpreter does; the row's comment names the call path). When that
cannot be shown the row is ``"root"``. ``lazy`` prints the sites found.

Dynamic roots are a tripwire, not an inference: an import graph cannot see ``runpy.run_module`` / ``wall.module_argv``
targets, so ``DYNAMIC_SITES`` classifies every such call site in ``levain/`` (found by a text scan of the same two
needles ``git grep -n -e 'run_module(' -e 'module_argv(' -- levain`` uses) and seeds the closure from it. A test
(``tests/test_ci_root_closure.py``) asserts the scan finds exactly the table's keys, so a new site fails until classified.

Stdlib only and 3.9-valid: it must run under the interpreter it checks. The walk is an AST walk over the imports a module
runs when it is imported (function bodies are skipped), and importing a submodule runs its parent packages'
``__init__`` too, so those are members as well.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = "levain"
_WARNED: set = set()

#: Every dynamic-execution site in ``levain/``: ``"<path>:<line number>::<stripped source line>"`` -> the module(s) it runs. A value is
#: a tuple of dotted names, or a token resolved from source: ``@entries`` (``root_tree.ENTRY_MODULES`` values),
#: ``@services`` (``levain.slot.services.<name>`` per ``units.SERVICES``), ``@front`` (``runtime_helper.FRONT_MODULE``).
#: An empty tuple is a site that runs whatever its caller passed (the executor itself); its callers are rows here.
DYNAMIC_SITES = {
    "levain/slot/root_tree.py:78::" 'f"runpy.run_module({ENTRY_MODULES[entry]!r}, run_name=\\"__main__\\", alter_sys=True)\\n"': "@entries",
    "levain/slot/service_boot.py:87::" 'runpy.run_module(f"levain.slot.services.{name}", run_name="__main__", alter_sys=True)': "@services",
    "levain/slot/wall.py:321::def module_argv(host: str, module: str, *args: str) -> Tuple[str, ...]:": (),
    "levain/slot/wall.py:330::" '"runpy.run_module(m, run_name=\'__main__\', alter_sys=True)")': (),
    "levain/slot/wall.py:460::" 'argv = module_argv(host, "levain.slot.forwarder", "--port", str(FORWARDER_PORT), "--unix", str(sock),': ("levain.slot.forwarder",),
    "levain/slot/wall.py:468::argv = module_argv(host, front_module or \"\", *argv)": "@front",
    "levain/slot/wall.py:625::" 'return module_argv(host, "levain.slot.wall", "--spec-fd", str(spec_fd))': ("levain.slot.wall",),
    "levain/slot/wall.py:1449::" 'return module_argv("darwin", "levain.slot.wall", "--exec-fd", str(exec_fd), "--", *argv)': ("levain.slot.wall",),
    # in-process ``importlib`` calls, none run by a root process (empty = no module seeded); each row's reason:
    # the two comments name the explicit leaf maps; the firing / presence calls import only the blessed leaves of
    # ``_LAZY_FIRING_MODULES`` / ``_LAZY_PRESENCE_MODULES`` for the operator's daemon (a variable, not a root target);
    # the cockpit one is the operator's panel, importing ``levain.consent.limits`` (not a root-run module)
    "levain/cockpit/providers.py:1174::" 'mod = importlib.import_module("levain.consent.limits")': (),
    "levain/firing/contract.py:193::" '# rather than ``import_module(f"{__name__}.{kind}")`` GOVERNS which leaves build_firing may': (),
    "levain/firing/contract.py:229::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py:136::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py:92::" '# recover. An EXPLICIT map (not ``import_module(f"…{kind}")``) governs which modules may be': (),
}
LAZY_SITES = {
    # operator: ``_resolve`` <- ``pick_address`` <- ``creds._post`` / ``keys`` (the broker's dial); the root credential verbs only ``commit`` / ``remove`` files
    'levain/broker/upstream.py:183::from levain.broker.front import system_resolve': 'operator',
    # operator: ``pick_address`` <- ``creds._post`` / ``keys`` (the broker's dial); the root credential verbs only ``commit`` / ``remove`` files
    'levain/broker/upstream.py:190::from levain.broker.front import refused_address': 'operator',
    # operator: ``canon.REGISTRY`` (module ``__getattr__``) has no reader in ``levain/``: ``git grep -n REGISTRY -- levain`` finds only ``contracts.leaves`` and unrelated names
    'levain/contracts/canon.py:47::from .leaves import REGISTRY': 'operator',
    'levain/firing/contract.py:105::from levain.firing.encoding import RECEIPT_KEY, scan_text': 'root',
    'levain/firing/hands.py:604::from levain.slot.grammar import is_sudoers_alias   # levain.slot imports this module': 'root',
    # operator: ``_readable_and_sanitised`` <- ``plan_undo`` <- ``cmd_setup_isolation`` <- ``cli.py`` (``levain setup-isolation``); root uses only ``hands._abs``
    'levain/firing/hands.py:1047::from levain.firing.ws_git import CONFIG_ALLOWLIST': 'operator',
    # operator: ``_undo_lock`` <- ``cmd_setup_isolation`` (undo path) <- ``cli.py``
    'levain/firing/hands.py:1475::from levain.firing.ws_git import HANDS_LOCK': 'operator',
    # operator: ``_repair_linux`` <- ``cmd_setup_isolation`` <- ``cli.py``
    'levain/firing/hands.py:1574::from levain.firing.confinement import ConfinementError, load_confinement_config': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py:1615::from levain.firing.confinement import ConfinementError, load_confinement_config': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py:1616::from levain.firing.isolation import IsolationError, guard_entity': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py:1684::from levain.firing.ws_git import LIVENESS_TIMEOUT, WsGitError, entity_session_live': 'operator',
    # operator: the console script ``levain = levain.launch:main`` (pyproject.toml): the operator's interpreter, never a root helper
    'levain/launch.py:324::from levain.firing.confinement import sweep_floor_placeholders': 'operator',
    # operator: the console script ``levain = levain.launch:main`` (pyproject.toml): the operator's interpreter, never a root helper
    'levain/launch.py:329::from levain.cli import main as cli_main': 'operator',
    'levain/slot/accounts.py:245::from levain.slot.install_helper import read_directory': 'root',
    'levain/slot/accounts.py:246::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/accounts.py:265::from levain.slot.removal import ABSENT, NOT_OURS, OURS': 'root',
    'levain/slot/accounts.py:277::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/accounts.py:288::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/accounts.py:308::from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably': 'root',
    'levain/slot/accounts.py:309::from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, RemoveStep': 'root',
    'levain/slot/accounts.py:346::from levain.slot.removal import ABSENT, NOT_OURS, OURS': 'root',
    'levain/slot/accounts.py:379::from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably': 'root',
    'levain/slot/accounts.py:380::from levain.slot.removal import ABSENT, NOT_OURS, ReadFailed, RemoveStep, remove_step': 'root',
    'levain/slot/bwrap.py:141::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/bwrap.py:155::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/bwrap.py:167::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/bwrap.py:216::from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, root_file': 'root',
    'levain/slot/bwrap.py:243::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/control.py:503::from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular': 'root',
    'levain/slot/control.py:970::from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular': 'root',
    # S1 3c-4: ``levain.slot.routemaps`` (CONTROL, keys) parses the dispatch route map with ``parse_routemap``
    'levain/broker/policy.py:380::from levain.dispatch import routemap_rules as rules': 'root',
    'levain/slot/export_run.py:349::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/export_run.py:786::from levain.slot import wall': 'root',
    'levain/slot/export_run.py:997::from levain.slot import wall': 'root',
    'levain/slot/export_run.py:1040::from levain.slot import wall': 'root',
    'levain/slot/export_run.py:1133::from levain.slot import proc as P': 'root',
    'levain/slot/export_run.py:1217::from levain.slot import proc as P': 'root',
    'levain/slot/fsck_reader.py:125::from levain.slot import adopt_verify as AV': 'root',
    'levain/slot/fsck_reader.py:281::from levain.contracts import canon': 'root',
    'levain/slot/generation.py:293::from levain.slot.records import write_durably': 'root',
    'levain/slot/ids.py:247::from levain.slot.records import write_durably': 'root',
    'levain/slot/ids.py:250::from levain.slot.generation import _full_fsync as fsync': 'root',
    'levain/slot/install_helper.py:564::import levain': 'root',
    'levain/slot/install_helper.py:2089::from levain.slot.intent import GROUP_RE': 'root',
    'levain/slot/install_helper.py:2236::from levain.broker import creds as C': 'root',
    'levain/slot/install_helper.py:2277::from levain.slot import wall': 'root',
    'levain/slot/install_helper.py:2295::from levain.broker import creds as C': 'root',
    'levain/slot/install_helper.py:2296::from levain.slot.stage import _close_fds_except': 'root',
    'levain/slot/layout.py:200::from levain.contracts import canon': 'root',
    'levain/slot/layout.py:237::from levain.contracts import canon': 'root',
    'levain/slot/layout.py:263::from levain.contracts import canon': 'root',
    'levain/slot/layout.py:271::from levain.contracts import canon': 'root',
    'levain/slot/layout.py:376::from levain.slot.ids import SHARED_GROUPS': 'root',
    'levain/slot/layout.py:409::from levain.slot.removal import root_file': 'root',
    'levain/slot/layout.py:416::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/netns.py:63::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/netns.py:71::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/pf.py:298::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/pf.py:305::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/pf.py:316::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/pf.py:328::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/pf.py:346::from levain.slot.removal import ABSENT, NOT_OURS, root_file': 'root',
    'levain/slot/pf.py:356::from levain.slot.removal import remove_step': 'root',
    'levain/slot/reaper.py:610::from levain.slot.install_helper import _operator_held': 'root',
    'levain/slot/reaper.py:899::from levain.slot.netns import netns_steps': 'root',
    'levain/slot/removal.py:63::from levain.slot.generation import _full_fsync': 'root',
    'levain/slot/render.py:615::from levain.slot.wall import check_profile_text, export_wall_facts': 'root',
    # operator: ``repo_loosening_refusals`` and ``refuse_repo_loosening`` <- ``supervisor.check_repo_layer`` <- ``supervisor`` / ``dispatch.readset``; neither has a caller in ``levain/`` outside the operator, none in the closure (host_guard needs 3.11 ``tomllib``)
    'levain/slot/render.py:748::from levain.host_guard import _LOOSENING_NONEMPTY, _LOOSENING_TRUE': 'operator',
    'levain/slot/render.py:848::from levain.contracts.canon import _int, _no_constant, _no_float, _no_lone_surrogate, _pairs, text_depth': 'root',
    'levain/slot/root_tree.py:240::from levain.slot.generation import read_version_text': 'root',
    'levain/slot/root_tree.py:285::from levain.slot.generation import generation_id': 'root',
    'levain/slot/root_tree.py:369::from levain.slot.generation import generation_id': 'root',
    'levain/slot/root_tree.py:394::from levain.launch import child_env': 'root',
    'levain/slot/root_tree.py:432::from levain.slot.generation import GEN_RE': 'root',
    'levain/slot/root_tree.py:479::from levain.slot.generation import GEN_RE': 'root',
    'levain/slot/root_tree.py:517::from levain.launch import child_env': 'root',
    'levain/slot/root_tree.py:621::from levain.slot.generation import generation_id': 'root',
    'levain/slot/runtime_helper.py:198::from levain.slot.control_client import ControlClient': 'root',
    'levain/slot/runtime_helper.py:497::from levain.slot import start as SS': 'root',
    'levain/slot/runtime_helper.py:826::from levain.slot.install_helper import parse_operator_record': 'root',
    'levain/slot/runtime_helper.py:1752::from levain.slot import start as SS': 'root',
    'levain/slot/runtime_helper.py:1758::from levain.slot import start as SS': 'root',
    'levain/slot/runtime_helper.py:1837::from levain.slot import walker as WK': 'root',
    'levain/slot/runtime_helper.py:1912::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py:1938::from levain.slot import end as E': 'root',
    'levain/slot/runtime_helper.py:1939::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py:1940::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py:2040::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py:2274::from levain.slot import importer as IM': 'root',
    'levain/slot/runtime_helper.py:2361::from levain.slot import export_run as ER': 'root',
    'levain/slot/runtime_helper.py:2362::from levain.slot import importer as IM': 'root',
    'levain/slot/runtime_helper.py:2363::from levain.slot import walker as WK': 'root',
    'levain/slot/runtime_helper.py:2694::from levain.slot import adopt_verify as AV': 'root',
    'levain/slot/runtime_helper.py:2695::from levain.slot import export_run as ER': 'root',
    'levain/slot/runtime_helper.py:2696::from levain.slot import importer as IM': 'root',
    # the root discard / restore verbs (VERB_HANDLERS, operator-uid caller; land16)
    'levain/slot/runtime_helper.py:2901::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py:2902::from levain.slot.root_intent import PhaseFailed': 'root',
    'levain/slot/runtime_helper.py:2923::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py:2929::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/service_boot.py:74::from levain.slot.units import services_for': 'root',
    'levain/slot/start.py:142::from levain.slot.layout import STORE': 'root',
    'levain/slot/start.py:143::from levain.slot.root_tree import VENDOR_PREFIX': 'root',
    'levain/slot/store_verbs.py:297::from levain.slot import end as E': 'root',
    'levain/slot/sudoers.py:88::from levain.slot.removal import root_file': 'root',
    'levain/slot/sudoers.py:98::from levain.slot.removal import RemoveStep': 'root',
    'levain/slot/sudoers.py:114::from levain.slot.removal import OURS, ReadFailed': 'root',
    'levain/slot/sudoers.py:141::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/units.py:314::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/units.py:347::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/units.py:396::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/units.py:408::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/wall.py:128::from levain.slot.ids import RANGES, SINGLETONS, in_own_range': 'root',
    'levain/slot/wall.py:160::from levain.slot.ids import RANGES': 'root',
    'levain/slot/wall.py:226::from levain.contracts import canon': 'root',
    'levain/slot/wall.py:286::from levain.contracts import ContractError, canon': 'root',
    'levain/slot/wall.py:309::from levain.slot.grammar import SESSION_RE': 'root',
    'levain/slot/wall.py:310::from levain.slot.layout import STORE': 'root',
    'levain/slot/wall.py:324::from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code': 'root',
    'levain/slot/wall.py:349::from levain.slot.render import READSET_KINDS, READSET_TEMPLATE': 'root',
    'levain/slot/wall.py:350::from levain.slot.runtime_grammar import KINDS': 'root',
    'levain/slot/wall.py:419::from levain.slot.layout import IMPORT_TREE, RUN': 'root',
    'levain/slot/wall.py:420::from levain.slot.root_tree import ROOT_PYTHON, VENDOR_PREFIX': 'root',
    'levain/slot/wall.py:560::from levain.slot.layout import HELPER_DIR, IMPORT_TREE, STORE': 'root',
    'levain/slot/wall.py:581::from levain.slot.layout import STORE': 'root',
    'levain/slot/wall.py:599::from levain.slot.layout import HELPER_DIR': 'root',
    'levain/slot/wall.py:676::from levain.firing.hands import _abs': 'root',
    'levain/slot/wall.py:677::from levain.launch import child_env': 'root',
    'levain/slot/wall.py:713::from levain.slot.layout import IMPORT_TREE, RUN': 'root',
    'levain/slot/wall.py:714::from levain.slot.root_tree import VENDOR_PREFIX': 'root',
    'levain/slot/wall.py:801::from levain.slot.layout import base_run_rows, base_store_rows': 'root',
    'levain/slot/wall.py:848::from levain.slot.bwrap import LEVAIN_BWRAP': 'root',
    'levain/slot/wall.py:873::from levain.slot.bwrap import LEVAIN_BWRAP': 'root',
    'levain/slot/wall.py:986::from levain.slot.pf import anchor_rules': 'root',
    'levain/slot/wall.py:994::from levain.slot.ids import loopback_alias': 'root',
    'levain/slot/wall.py:1019::from levain.launch import child_env': 'root',
    'levain/slot/wall.py:1031::from levain.slot.pf import ANCHOR, IFCONFIG, PFCTL': 'root',
    'levain/slot/wall.py:1467::from levain.slot.keyring_join import join_session_keyring, real_keyctl': 'root',
}
_NEEDLES = ("run_module(", "module_argv(", "import_module(", "__import__(")


def dynamic_sites() -> list:
    """The sites a text scan of ``levain/*.py`` finds: ``"<path>:<line number>::<stripped line>"``, sorted."""
    found = []
    for path in sorted((ROOT / PKG).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(n in line for n in _NEEDLES):
                found.append("%s:%d::%s" % (rel, no, line.strip()))
    return sorted(found)


def _table(rel: str, name: str):
    """A module-level literal assigned to ``name``, read from source: importing the module would need the
    3.9-incompatible code this job exists to find."""
    src = (ROOT / PKG / rel).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == name:
            return node.value
    raise SystemExit("root_closure: %s not found in levain/%s" % (name, rel))


def _service_names() -> list:
    """The ``name`` of every ``Service(...)`` in ``levain.slot.units.SERVICES`` (first positional or ``name=``)."""
    names = []
    for call in _table("slot/units.py", "SERVICES").elts:
        kw = {k.arg: k.value for k in call.keywords}
        arg = call.args[0] if call.args else kw.get("name")
        names.append(ast.literal_eval(arg))
    return names


def _site_modules(spec) -> list:
    """The modules a ``DYNAMIC_SITES`` value names: a tuple as written, or a token resolved from source."""
    if spec == "@entries":
        return list(ast.literal_eval(_table("slot/root_tree.py", "ENTRY_MODULES")).values())
    if spec == "@services":
        return ["levain.slot.services." + n for n in _service_names()]
    if spec == "@front":
        return [ast.literal_eval(_table("slot/runtime_helper.py", "FRONT_MODULE"))]
    return list(spec)


def _entry_modules() -> list:
    """The roots: every module a dynamic-execution site runs (``DYNAMIC_SITES``: the entry scripts, ``service_boot``'s
    services, the wall's forwarder / stage / trampoline, the front). A module with no file is printed, not dropped."""
    roots = set()
    for spec in DYNAMIC_SITES.values():
        for mod in _site_modules(spec):
            if _path(mod) is None:
                if mod not in _WARNED:
                    _WARNED.add(mod)
                    print("root_closure: no module for %s: handled elsewhere or missing" % mod, file=sys.stderr)
            else:
                roots.add(mod)
    return sorted(roots)


def _path(mod: str):
    base = ROOT.joinpath(*mod.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _nodes(tree):
    """Every node that runs at import time (not inside a function body)."""
    stack = [tree]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(c)


def _import_names(node, pkg: str) -> set:
    """The ``levain`` names an import statement names: the module, and ``module.name`` for each ``from`` name (which
    is a module only when a file of that name exists; ``_eager`` skips the rest). ``pkg`` resolves relative imports."""
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            parts = pkg.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1])
            base = base + "." + node.module if node.module else base
        else:
            base = node.module or ""
        names = [base] + [base + "." + a.name for a in node.names]
    else:
        return set()
    return {n for n in names if n == PKG or n.startswith(PKG + ".")}


def _imports(mod: str, path: Path) -> set:
    is_pkg = path.name == "__init__.py"
    pkg = mod if is_pkg else mod.rpartition(".")[0]
    found = set()
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # A file this interpreter cannot parse is exactly what the job exists to report (``compile`` fails on it), but
        # its imports still belong to the closure: fall back to a line scan so the walk goes on past it.
        for m in re.finditer(r"^\s*(?:from\s+(levain[\w.]*)\s+import\s+\(?([^)\n]*)|import\s+(levain[\w.]*))", text, re.M):
            if m.group(3):
                found.add(m.group(3))
            else:
                found.add(m.group(1))
                found.update(m.group(1) + "." + n.strip().split(" as ")[0] for n in m.group(2).split(",") if n.strip())
        return found
    for node in _nodes(tree):
        found.update(_import_names(node, pkg))
    return found


def _eager(roots) -> set:
    seen, todo = set(), list(roots)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        path = _path(mod)
        if path is None:  # a name imported from a module (``from m import func``), not a module
            continue
        seen.add(mod)
        parts = mod.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))  # parent packages run their __init__
        todo.extend(_imports(mod, path))
    return seen


def lazy_sites(mods) -> dict:
    """Every function-local ``levain`` import in ``mods``: ``"<path>:<line number>::<stripped line>"`` -> the module
    names it names. A module this interpreter cannot parse is reported on stderr and has no sites here."""
    found = {}
    for mod in mods:
        path = _path(mod)
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            print("root_closure: lazy sites not scanned (3.10+ syntax): " + mod, file=sys.stderr)
            continue
        is_pkg = path.name == "__init__.py"
        pkg = mod if is_pkg else mod.rpartition(".")[0]
        eager = {id(n) for n in _nodes(tree)}
        lines = text.splitlines()
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in eager:
                names = _import_names(node, pkg)
                if names:
                    found.setdefault("%s:%d::%s" % (rel, node.lineno, lines[node.lineno - 1].strip()), set()).update(names)
    return found


def closure() -> list:
    """The modules the entries import at import time, plus the targets of every ``"root"`` row of ``LAZY_SITES``
    (and what those import at import time), to a fixed point: a lazy import a root process can run is as much root
    code as a top-level one. A site not in the table adds nothing here; the test fails it."""
    roots = set(_entry_modules())
    while True:
        mods = _eager(roots)
        grown = {n for key, names in lazy_sites(mods).items() if LAZY_SITES.get(key) == "root" for n in names} - roots
        if not grown:
            return sorted(mods)
        roots |= grown


def _levain_imports(tree) -> set:
    """The ``levain`` modules (files that exist) a parsed test file imports."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(node.module + "." + a.name for a in node.names)
    # ``from levain.slot import x`` names a module when levain/slot/x.py exists, else a function: keep modules only
    return {n for n in names if (n == PKG or n.startswith(PKG + ".")) and _path(n) is not None}


def _test_deps(path: Path, seen: set):
    """The ``levain`` modules a test file imports, plus those of every ``tests.*`` helper it imports, recursively;
    None when a file in that chain cannot be parsed by this interpreter (a 3.12-only test is not a root-run test)."""
    if path in seen:
        return set()
    seen.add(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None
    out = _levain_imports(tree)
    helpers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            helpers.update(a.name for a in node.names if a.name.startswith("tests."))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module and (node.module == "tests" or node.module.startswith("tests.")):
            helpers.add(node.module)
            helpers.update(node.module + "." + a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            helpers.add("tests" + ("." + node.module if node.module else ""))
            helpers.update("tests." + (node.module + "." if node.module else "") + a.name for a in node.names)
    for h in sorted(helpers):
        hp = ROOT.joinpath(*h.split("."))
        hp = hp.with_suffix(".py") if hp.with_suffix(".py").is_file() else hp / "__init__.py"
        if not hp.is_file():
            continue  # ``from tests.x import name`` names a function, not a module
        sub = _test_deps(hp, seen)
        if sub is None:
            return None
        out |= sub
    return out


def _test_files(mods: list) -> list:
    """The test files that import a closure module and import NOTHING of ``levain`` outside the closure (a ``levain``
    package import aside), counting what the ``tests.*`` helpers they import (recursively) import too: the files
    wholly about root-run code. A file that also reaches operator-side code (the CLI, the cockpit, ``tomllib`` users),
    directly or through a helper, is 3.11+ by design and its 3.9 red says nothing about root, so it is left to the
    3.12/3.13 matrix. A file (or helper) this interpreter cannot parse is skipped for the same reason."""
    inside = set(mods)
    leaf = [m for m in mods if _path(m).name != "__init__.py"]  # a package import says nothing: every test has one
    out = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        names = _test_deps(f, set())
        if names is None:
            print("skipped (3.10+ syntax): " + f.relative_to(ROOT).as_posix(), file=sys.stderr)
        elif names & set(leaf) and names <= inside:
            out.append(f.relative_to(ROOT).as_posix())
    return out


def _compile(mods: list, label: str) -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, mod in enumerate(mods):
            try:
                py_compile.compile(str(_path(mod)), cfile=str(Path(tmp) / (str(i) + ".pyc")), doraise=True)
            except py_compile.PyCompileError as e:
                bad += 1
                print("FAIL " + mod + ": " + e.msg.strip().splitlines()[-1])
    print("compiled %d %s modules under %s, %d failed" % (len(mods), label, sys.version.split()[0], bad))
    return 1 if bad else 0


def _import_check(mods: list) -> int:
    """``python -c "import m"`` for each module, one subprocess each: ``py_compile`` is syntax only and misses what
    fails at import (an unguarded ``X | None`` annotation, a 3.10+ stdlib name)."""
    bad = 0
    for mod in mods:
        r = subprocess.run([sys.executable, "-c", "import " + mod], cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode:
            bad += 1
            print("FAIL " + mod + ": " + (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1])
    print("imported %d import-time modules under %s, %d failed" % (len(mods), sys.version.split()[0], bad))
    return 1 if bad else 0


def _h11_spec() -> str:
    return "h11>=" + ".".join(str(n) for n in ast.literal_eval(_table("slot/root_tree.py", "H11_MIN")))


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "modules"
    mods = closure()
    if cmd == "modules":
        print("\n".join(mods))
    elif cmd == "compile":
        return _compile(mods, "import-time")
    elif cmd == "import-check":
        return _import_check(mods)
    elif cmd == "tests":
        files = _test_files(mods)
        sys.stdout.write("".join(f + "\n" for f in files))  # nothing at all when empty: no blank line for mapfile
    elif cmd == "h11-spec":
        print(_h11_spec())
    elif cmd == "sites":
        for key, spec in sorted(DYNAMIC_SITES.items()):
            print("%s -> %s" % (key, ", ".join(_site_modules(spec)) or "(caller's module)"))
    elif cmd == "lazy":
        for key, names in sorted(lazy_sites(mods).items()):
            print("%s -> %s [%s]" % (key, ", ".join(sorted(names)), LAZY_SITES.get(key, "UNCLASSIFIED")))
    else:
        raise SystemExit("usage: root_closure.py modules|compile|import-check|tests|h11-spec|sites|lazy")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
