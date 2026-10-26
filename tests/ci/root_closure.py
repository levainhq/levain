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

#: The rows of both tables are keyed by CONTENT and enclosing context, never by line number, so an edit above a cited
#: site changes nothing: ``"<path>::<qualname>::<stripped source line>"``, ``qualname`` the dotted class / function path
#: of the innermost ``def`` / ``class`` holding the line (``<module>`` at top level), with ``"::#<n>"`` appended to the
#: n-th (n >= 2) occurrence of that same (qualname, line) pair in one file, in source order. A row whose key no longer
#: exists fails the test as stale, and a site no row names fails it as unclassified: a changed site, or an unchanged
#: one moved into another function (an operator-only import moved where root can run it), must be re-classified.
#:
#: Every dynamic-execution site in ``levain/`` -> the module(s) it runs. A value is
#: a tuple of dotted names, or a token resolved from source: ``@entries`` (``root_tree.ENTRY_MODULES`` values),
#: ``@services`` (``levain.slot.services.<name>`` per ``units.SERVICES``), ``@front`` (``runtime_helper.FRONT_MODULE``).
#: An empty tuple is a site that runs whatever its caller passed (the executor itself); its callers are rows here.
DYNAMIC_SITES = {
    "levain/slot/root_tree.py::entry_text::" 'f"runpy.run_module({ENTRY_MODULES[entry]!r}, run_name=\\"__main__\\", alter_sys=True)\\n"': "@entries",
    "levain/slot/service_boot.py::main::" 'runpy.run_module(f"levain.slot.services.{name}", run_name="__main__", alter_sys=True)': "@services",
    "levain/slot/wall.py::module_argv::def module_argv(host: str, module: str, *args: str) -> Tuple[str, ...]:": (),
    "levain/slot/wall.py::module_argv::" '"runpy.run_module(m, run_name=\'__main__\', alter_sys=True)")': (),
    "levain/slot/wall.py::launch_spec::" 'argv = module_argv(host, "levain.slot.forwarder", "--port", str(FORWARDER_PORT), "--unix", str(sock),': ("levain.slot.forwarder",),
    "levain/slot/wall.py::launch_spec::argv = module_argv(host, front_module or \"\", *argv)": "@front",
    # V13: the probe's connector child (stdlib only), built by launch_spec and matched by the stage's spec check
    "levain/slot/wall.py::launch_spec::" 'argv = module_argv(host, PROBE_MODULE, "--result-fd", str(PROBE_RESULT_FD), *probe_addrs)': ("levain.slot.probe_child",),
    "levain/slot/wall.py::_check_probe_spec::" 'prefix = module_argv(h, PROBE_MODULE, "--result-fd", str(PROBE_RESULT_FD))': ("levain.slot.probe_child",),
    "levain/slot/wall.py::stage_argv::" 'return module_argv(host, "levain.slot.wall", "--spec-fd", str(spec_fd))': ("levain.slot.wall",),
    "levain/slot/wall.py::trampoline_argv::" 'return module_argv("darwin", "levain.slot.wall", "--exec-fd", str(exec_fd), "--", *argv)': ("levain.slot.wall",),
    # in-process ``importlib`` calls, none run by a root process (empty = no module seeded); each row's reason:
    # the two comments name the explicit leaf maps; the firing / presence calls import only the blessed leaves of
    # ``_LAZY_FIRING_MODULES`` / ``_LAZY_PRESENCE_MODULES`` for the operator's daemon (a variable, not a root target);
    # the cockpit one is the operator's panel, importing ``levain.consent.limits`` (not a root-run module)
    "levain/cockpit/providers.py::_limit_lines::" 'mod = importlib.import_module("levain.consent.limits")': (),
    "levain/firing/contract.py::<module>::" '# rather than ``import_module(f"{__name__}.{kind}")`` GOVERNS which leaves build_firing may': (),
    "levain/firing/contract.py::build_firing::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py::build_presence::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py::<module>::" '# recover. An EXPLICIT map (not ``import_module(f"…{kind}")``) governs which modules may be': (),
}
LAZY_SITES = {
    # operator: ``_resolve`` <- ``pick_address`` <- ``creds._post`` / ``keys`` (the broker's dial); the root credential verbs only ``commit`` / ``remove`` files
    'levain/broker/upstream.py::_resolve::from levain.broker.front import system_resolve': 'operator',
    # operator: ``pick_address`` <- ``creds._post`` / ``keys`` (the broker's dial); the root credential verbs only ``commit`` / ``remove`` files
    'levain/broker/upstream.py::pick_address::from levain.broker.front import refused_address': 'operator',
    # operator: ``canon.REGISTRY`` (module ``__getattr__``) has no reader in ``levain/``: ``git grep -n REGISTRY -- levain`` finds only ``contracts.leaves`` and unrelated names
    'levain/contracts/canon.py::_shipped_registry::from .leaves import REGISTRY': 'operator',
    'levain/firing/contract.py::CaptureRequest.__post_init__::from levain.firing.encoding import RECEIPT_KEY, scan_text': 'root',
    'levain/firing/hands.py::_operator_name_refused::from levain.slot.grammar import is_sudoers_alias   # levain.slot imports this module': 'root',
    # operator: ``_readable_and_sanitised`` <- ``plan_undo`` <- ``cmd_setup_isolation`` <- ``cli.py`` (``levain setup-isolation``); root uses only ``hands._abs``
    'levain/firing/hands.py::_readable_and_sanitised::from levain.firing.ws_git import CONFIG_ALLOWLIST': 'operator',
    # operator: ``_undo_lock`` <- ``cmd_setup_isolation`` (undo path) <- ``cli.py``
    'levain/firing/hands.py::_undo_lock::from levain.firing.ws_git import HANDS_LOCK': 'operator',
    # operator: ``_repair_linux`` <- ``cmd_setup_isolation`` <- ``cli.py``
    'levain/firing/hands.py::_repair_linux::from levain.firing.confinement import ConfinementError, load_confinement_config': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py::cmd_setup_isolation::from levain.firing.confinement import ConfinementError, load_confinement_config': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py::cmd_setup_isolation::from levain.firing.isolation import IsolationError, guard_entity': 'operator',
    # operator: ``cmd_setup_isolation`` <- ``cli.py`` (the one caller)
    'levain/firing/hands.py::cmd_setup_isolation::from levain.firing.ws_git import LIVENESS_TIMEOUT, WsGitError, entity_session_live': 'operator',
    # operator: the console script ``levain = levain.launch:main`` (pyproject.toml): the operator's interpreter, never a root helper
    'levain/launch.py::main::from levain.firing.confinement import sweep_floor_placeholders': 'operator',
    # operator: the console script ``levain = levain.launch:main`` (pyproject.toml): the operator's interpreter, never a root helper
    'levain/launch.py::main::from levain.cli import main as cli_main': 'operator',
    'levain/slot/accounts.py::_directory::from levain.slot.install_helper import read_directory': 'root',
    'levain/slot/accounts.py::_directory::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/accounts.py::_group_ours::from levain.slot.removal import ABSENT, NOT_OURS, OURS': 'root',
    'levain/slot/accounts.py::member_state::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/accounts.py::remove_member_steps::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/accounts.py::remove_group_steps::from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably': 'root',
    'levain/slot/accounts.py::remove_group_steps::from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, RemoveStep': 'root',
    'levain/slot/accounts.py::_account_state_in::from levain.slot.removal import ABSENT, NOT_OURS, OURS': 'root',
    'levain/slot/accounts.py::remove_account_steps::from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably': 'root',
    'levain/slot/accounts.py::remove_account_steps::from levain.slot.removal import ABSENT, NOT_OURS, ReadFailed, RemoveStep, remove_step': 'root',
    'levain/slot/bwrap.py::_profile_loaded::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/bwrap.py::apparmor_state::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/bwrap.py::remove_apparmor_profile_steps::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/bwrap.py::bwrap_copy_state::from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, root_file': 'root',
    'levain/slot/bwrap.py::remove_bwrap_copy_steps::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/control.py::KeysAdmin.__init__::from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular': 'root',
    'levain/slot/control.py::Control._drop_until_acked::from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular': 'root',
    # S1 3c-4: ``levain.slot.routemaps`` (CONTROL, keys) parses the dispatch route map with ``parse_routemap``
    'levain/broker/policy.py::parse_routemap::from levain.dispatch import routemap_rules as rules': 'root',
    'levain/slot/export_run.py::_store_delete::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/export_run.py::mount_path::from levain.slot import wall': 'root',
    'levain/slot/export_run.py::scope_path::from levain.slot import wall': 'root',
    'levain/slot/export_run.py::enable_controllers::from levain.slot import wall': 'root',
    'levain/slot/export_run.py::uid_pids::from levain.slot import proc as P': 'root',
    'levain/slot/export_run.py::export_domain_prover::from levain.slot import proc as P': 'root',
    'levain/slot/fsck_reader.py::_Store.__init__::from levain.slot import adopt_verify as AV': 'root',
    'levain/slot/fsck_reader.py::read_trees::from levain.contracts import canon': 'root',
    'levain/slot/generation.py::write_record_durably::from levain.slot.records import write_durably': 'root',
    'levain/slot/ids.py::retire_ids_durably::from levain.slot.records import write_durably': 'root',
    'levain/slot/ids.py::retire_ids_durably::from levain.slot.generation import _full_fsync as fsync': 'root',
    'levain/slot/install_helper.py::_levain_package::import levain': 'root',
    'levain/slot/install_helper.py::_repair_facts::from levain.slot.intent import GROUP_RE': 'root',
    'levain/slot/install_helper.py::load_creds::from levain.broker import creds as C': 'root',
    'levain/slot/install_helper.py::drop_to_keys::from levain.slot import wall': 'root',
    'levain/slot/install_helper.py::_credential_child::from levain.broker import creds as C': 'root',
    'levain/slot/install_helper.py::_credential_child::from levain.slot.stage import _close_fds_except': 'root',
    'levain/slot/layout.py::base_routes_steps::from levain.contracts import canon': 'root',
    'levain/slot/layout.py::roots_record_text::from levain.contracts import canon': 'root',
    'levain/slot/layout.py::pool_config_text::from levain.contracts import canon': 'root',
    'levain/slot/layout.py::parse_pool_config::from levain.contracts import canon': 'root',
    'levain/slot/layout.py::shared_group_rows::from levain.slot.ids import SHARED_GROUPS': 'root',
    'levain/slot/layout.py::entry_state::from levain.slot.removal import root_file': 'root',
    'levain/slot/layout.py::remove_entry_steps::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/limits_doc.py::slot_doc::from levain.contracts import ContractError': 'root',
    'levain/slot/limits_doc.py::slot_doc::from levain.contracts.canon import canonical_bytes, parse': 'root',
    'levain/slot/netns.py::netns_state::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/netns.py::remove_netns_steps::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/pf.py::alias_state::from levain.slot.removal import ABSENT, OURS': 'root',
    'levain/slot/pf.py::remove_alias_steps::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/pf.py::anchor_state::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/pf.py::remove_anchor_steps::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/pf.py::enable_state::from levain.slot.removal import ABSENT, NOT_OURS, root_file': 'root',
    'levain/slot/pf.py::remove_enable_steps::from levain.slot.removal import remove_step': 'root',
    'levain/slot/reaper.py::Reaper.pool_health::from levain.slot.install_helper import _operator_held': 'root',
    'levain/slot/reaper.py::ensure_netns::from levain.slot.netns import netns_steps': 'root',
    'levain/slot/removal.py::_fsync::from levain.slot.generation import _full_fsync': 'root',
    'levain/slot/render.py::_export_profile::from levain.slot.wall import check_profile_text, export_wall_facts': 'root',
    # operator: ``repo_loosening_refusals`` and ``refuse_repo_loosening`` <- ``supervisor.check_repo_layer`` <- ``supervisor`` / ``dispatch.readset``; neither has a caller in ``levain/`` outside the operator, none in the closure (host_guard needs 3.11 ``tomllib``)
    'levain/slot/render.py::repo_loosening_refusals::from levain.host_guard import _LOOSENING_NONEMPTY, _LOOSENING_TRUE': 'operator',
    'levain/slot/render.py::parse_driver_input::from levain.contracts.canon import _int, _no_constant, _no_float, _no_lone_surrogate, _pairs, text_depth': 'root',
    'levain/slot/root_tree.py::h11_floor::from levain.slot.generation import read_version_text': 'root',
    'levain/slot/root_tree.py::_staged_is::from levain.slot.generation import generation_id': 'root',
    'levain/slot/root_tree.py::_hash_staged::from levain.slot.generation import generation_id': 'root',
    'levain/slot/root_tree.py::_place::from levain.launch import child_env': 'root',
    'levain/slot/root_tree.py::generation_boot_code::from levain.slot.generation import GEN_RE': 'root',
    'levain/slot/root_tree.py::read_current::from levain.slot.generation import GEN_RE': 'root',
    'levain/slot/root_tree.py::_gc::from levain.launch import child_env': 'root',
    'levain/slot/root_tree.py::tree_steps::from levain.slot.generation import generation_id': 'root',
    'levain/slot/runtime_helper.py::_default_client::from levain.slot.control_client import ControlClient': 'root',
    'levain/slot/runtime_helper.py::_front_interlock::from levain.slot import start as SS': 'root',
    # root: render builds the front's policy file and reads it with the front's own parser (3b-8a)
    'levain/slot/runtime_helper.py::front_policy_bytes::from levain.broker import front as FR': 'root',
    'levain/slot/runtime_helper.py::read_operator_uid::from levain.slot.install_helper import parse_operator_record': 'root',
    'levain/slot/runtime_helper.py::_start::from levain.slot import start as SS': 'root',
    'levain/slot/runtime_helper.py::_teardown::from levain.slot import start as SS': 'root',
    'levain/slot/runtime_helper.py::_probe::from levain.slot import probe as PB': 'root',
    'levain/slot/probe.py::session_route::from levain.broker.policy import PolicyError, parse_base_map': 'root',
    'levain/slot/runtime_helper.py::adopt_walk_caps::from levain.slot import walker as WK': 'root',
    'levain/slot/runtime_helper.py::_manifest_depths::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py::adopt_step3_root_facts::from levain.slot import end as E': 'root',
    'levain/slot/runtime_helper.py::adopt_step3_root_facts::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py::adopt_step3_root_facts::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py::adopt_step5_precaps::from levain.slot import walkframe as WF': 'root',
    'levain/slot/runtime_helper.py::make_work::from levain.slot import importer as IM': 'root',
    'levain/slot/runtime_helper.py::adopt_sealed_run::from levain.slot import export_run as ER': 'root',
    'levain/slot/runtime_helper.py::adopt_sealed_run::from levain.slot import importer as IM': 'root',
    'levain/slot/runtime_helper.py::adopt_sealed_run::from levain.slot import walker as WK': 'root',
    'levain/slot/runtime_helper.py::verify_sealed_run::from levain.slot import adopt_verify as AV': 'root',
    'levain/slot/runtime_helper.py::verify_sealed_run::from levain.slot import export_run as ER': 'root',
    'levain/slot/runtime_helper.py::verify_sealed_run::from levain.slot import importer as IM': 'root',
    # the root discard / restore verbs (VERB_HANDLERS, operator-uid caller; land16)
    'levain/slot/runtime_helper.py::_store_verb::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py::_store_verb::from levain.slot.root_intent import PhaseFailed': 'root',
    'levain/slot/runtime_helper.py::discard::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/runtime_helper.py::restore::from levain.slot import store_verbs as SV': 'root',
    'levain/slot/service_boot.py::main::from levain.slot.units import services_for': 'root',
    'levain/slot/start.py::check_vendor_installed::from levain.slot.root_tree import unit_problem': 'root',
    'levain/slot/start.py::vendor_argv::from levain.slot.layout import STORE': 'root',
    'levain/slot/start.py::vendor_argv::from levain.slot.root_tree import VENDOR_PREFIX': 'root',
    'levain/slot/store_verbs.py::retained_whole::from levain.slot import end as E': 'root',
    'levain/slot/sudoers.py::dropin_state::from levain.slot.removal import root_file': 'root',
    'levain/slot/sudoers.py::config_check_step::from levain.slot.removal import RemoveStep': 'root',
    'levain/slot/sudoers.py::_written_by_us::from levain.slot.removal import OURS, ReadFailed': 'root',
    'levain/slot/sudoers.py::remove_dropin_steps::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/units.py::_launchd_state::from levain.slot.removal import ReadFailed': 'root',
    'levain/slot/units.py::keys_start_steps.stop::from levain.slot.removal import Live, ReadFailed': 'root',
    'levain/slot/units.py::unit_state::from levain.slot.removal import ABSENT, OURS, root_file': 'root',
    'levain/slot/units.py::stop_unit_by_name::from levain.slot.removal import chain, point, remove_step': 'root',
    'levain/slot/units.py::remove_unit_file_by_name::from levain.slot.removal import at, chain, point, remove_step': 'root',
    'levain/slot/wall.py::principal::from levain.slot.ids import RANGES, SINGLETONS, in_own_range': 'root',
    'levain/slot/wall.py::export_principal::from levain.slot.ids import RANGES': 'root',
    'levain/slot/wall.py::Spec.to_json::from levain.contracts import canon': 'root',
    'levain/slot/wall.py::parse_spec::from levain.contracts import ContractError, canon': 'root',
    'levain/slot/wall.py::_session_dir::from levain.slot.grammar import SESSION_RE': 'root',
    'levain/slot/wall.py::_session_dir::from levain.slot.layout import STORE': 'root',
    'levain/slot/wall.py::_check_probe_spec::from levain.slot.grammar import SESSION_RE': 'root',
    'levain/slot/wall.py::_check_probe_spec::from levain.slot.layout import STORE': 'root',
    'levain/slot/wall.py::_check_probe_spec::from levain.slot.probe_child import MAX_ADDRS, probe_address': 'root',
    'levain/slot/wall.py::module_argv::from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code': 'root',
    'levain/slot/wall.py::clone_argv::from levain.slot.render import READSET_KINDS, READSET_TEMPLATE': 'root',
    'levain/slot/wall.py::clone_argv::from levain.slot.runtime_grammar import KINDS': 'root',
    'levain/slot/wall.py::launch_spec::from levain.slot.layout import IMPORT_TREE, RUN': 'root',
    'levain/slot/wall.py::launch_spec::from levain.slot.root_tree import ROOT_PYTHON, VENDOR_PREFIX': 'root',
    'levain/slot/wall.py::export_wall_facts::from levain.slot.layout import HELPER_DIR, IMPORT_TREE, STORE': 'root',
    'levain/slot/wall.py::_export_dir::from levain.slot.layout import STORE': 'root',
    'levain/slot/wall.py::export_launch_spec::from levain.slot.layout import HELPER_DIR': 'root',
    'levain/slot/wall.py::read_netns_names::from levain.firing.hands import _abs': 'root',
    'levain/slot/wall.py::read_netns_names::from levain.launch import child_env': 'root',
    'levain/slot/wall.py::linux_view_args::from levain.slot.layout import IMPORT_TREE, RUN': 'root',
    'levain/slot/wall.py::linux_view_args::from levain.slot.root_tree import VENDOR_PREFIX': 'root',
    'levain/slot/wall.py::view_ancestor_modes::from levain.slot.layout import base_run_rows, base_store_rows': 'root',
    'levain/slot/wall.py::linux_launch_argv::from levain.slot.bwrap import LEVAIN_BWRAP': 'root',
    'levain/slot/wall.py::export_linux_launch_argv::from levain.slot.bwrap import LEVAIN_BWRAP': 'root',
    'levain/slot/wall.py::expected_slot_rules::from levain.slot.pf import anchor_rules': 'root',
    'levain/slot/wall.py::check_egress_ready::from levain.slot.ids import loopback_alias': 'root',
    'levain/slot/wall.py::_read_cmd::from levain.launch import child_env': 'root',
    'levain/slot/wall.py::macos_egress_ready::from levain.slot.pf import ANCHOR, IFCONFIG, PFCTL': 'root',
    'levain/slot/wall.py::stage_main::from levain.slot.keyring_join import join_session_keyring, real_keyctl': 'root',
    # --- what the queue broker (``levain.slot.services.qbroker``) brings into the closure ---
    # operator: ``git grep -n '\.principal\b' -- levain`` finds it read in ``levain/cli.py``, ``levain/phone_app.py`` and ``levain/cockpit/{broker,engine,keys,labels,sign_client}.py`` only (engine's in a ``Cockpit`` panel)
    'levain/authority.py::AuthorityKey.principal::from levain.cockpit.keys import principal_of': 'operator',
    # operator: the authority replay (``_step`` <- ``_replay_entry`` <- ``snapshot``, run by ``consent.queue.AuthoritySource`` and ``cockpit.authority.RequestAuthority.capture``); the queue broker builds its ``AuthoritySnapshot`` from published signers (``qbroker.grants.contract_authority``) and runs neither (``git grep -n -e 'snapshot(' -e 'authority_source' -e 'RequestAuthority(' -- levain/qbroker`` finds none)
    'levain/authority.py::_enrolled_key::from levain.cockpit.keys import KeyError_, fingerprint_of, normalise_public_key': 'operator',
    'levain/authority.py::_step::from levain.consent.passkey import authority_signature_problem': 'operator',
    # operator: <- ``reset_lock`` <- ``reset_anchor`` <- ``levain/cockpit/keys.py`` (the operator's anchor reset; ``git grep -n 'reset_anchor(' -- levain``)
    'levain/authority.py::_no_live_cockpit::from levain.cockpit.broker import LOCK_NAME': 'operator',
    # root: the target is a closure member already (an import-time import of another member), so this row adds nothing to the closure; not traced further
    'levain/authority.py::_graduation_guard_problem::from levain.autonomic.coverage import guard_problem': 'root',
    'levain/authority.py::_parse_stamp::from levain.cockpit.consent import parse_stamp': 'root',
    'levain/authority.py::_ranks::from levain.cockpit.firejournal import _CLASS_RANK': 'root',
    'levain/authority.py::_stamp::from levain.cockpit.consent import utc_stamp': 'root',
    'levain/autonomic/journal.py::RunJournal._barrier::from levain.autonomic.binding import registry_bars': 'root',
    'levain/cockpit/authority.py::RequestAuthority.capture::from levain import authority as auth': 'root',
    'levain/cockpit/authority.py::RequestAuthority.capture::from levain.cockpit.firejournal import UNINITIALISED, _meta, conn_init_hint': 'root',
    'levain/cockpit/authority.py::RequestAuthority.fence::from levain.cockpit.firejournal import authority_fence': 'root',
    'levain/cockpit/authority.py::RequestAuthority.policy::from levain.authority import genesis_policy': 'root',
    'levain/cockpit/authority.py::RequestAuthority.revision::from levain.cockpit.firejournal import authority_fence': 'root',
    'levain/consent/queue.py::AuthoritySource.head::from levain.authority import log_head': 'root',
    'levain/dispatch/contract.py::git_env::from levain.launch import child_env': 'root',
    'levain/dispatch/published.py::sync::from levain.qbroker.grants import PUBLISHED_MAX   # here: grants imports this module': 'root',
    'levain/dispatch/published.py::sync::from levain.qbroker.store import replace_file': 'root',
    # root: ``record_verification`` <- ``follow.EventFollower`` (the dispatch service's ``follow_run.main``)
    'levain/dispatch/jobs.py::JobStore.record_verification::from levain.dispatch import adopt': 'root',
    # root: ``_stored_record`` <- ``record_verification`` / ``run_records`` <- ``follow.EventFollower`` (the dispatch service)
    'levain/dispatch/jobs.py::_stored_record::from levain.dispatch.adopt import ArtifactRecord, artifact_digest': 'root',
    # root: ``_verified_at`` <- ``record_verification`` <- ``follow.EventFollower`` (the dispatch service)
    'levain/dispatch/jobs.py::_verified_at::from levain.slot import records as SR': 'root',
    # operator: ``recommend`` <- ``dispatch/cli.py`` and ``dispatch/mcp.py``; the follower never calls it
    'levain/dispatch/jobs.py::JobStore.recommend::from levain.dispatch import adopt': 'operator',
    # operator: ``JobStore.__init__`` calls it only for ``directory=None``; ``follow_run.main`` passes ``a.store_dir``, and ``dispatch/cli.py`` / ``control.py`` are the other callers
    'levain/dispatch/jobs.py::default_dispatch_dir::from levain.consent.queue import default_queue_dir': 'operator',
    # operator: ``land`` <- ``dispatch/cli.py`` (``levain dispatch land``), the only caller; the follower never lands
    'levain/dispatch/adopt.py::_human_refusal::from levain.consent.queue import AuthoritySource': 'operator',
    # operator: ``land`` <- ``dispatch/cli.py`` (``levain dispatch land``), the only caller; the follower never lands
    'levain/dispatch/adopt.py::_human_refusal::from levain.dispatch.jobs import _replay': 'operator',
    # operator: ``authorise_refusal`` <- ``land`` (``dispatch/cli.py``) and ``JobStore.authorise``, neither run by the follower
    'levain/dispatch/adopt.py::authorise_refusal::from levain.consent.passkey import answer_signature_problem': 'operator',
    # operator: ``floor`` <- ``land`` <- ``dispatch/cli.py`` (``levain dispatch land``)
    'levain/dispatch/adopt.py::floor::from levain.dispatch.control import ControlStateUnavailable': 'operator',
    # operator: ``sweep_staged`` <- ``dispatch/cli.py`` (after a land, and the sweep verb); ``record_verification`` calls ``_delete_staged``, not this
    'levain/dispatch/adopt.py::sweep_staged::from levain.dispatch.jobs import JobStoreError': 'operator',
    # operator: ``driver_template_digest`` <- ``doctor.py`` / ``dispatch/cli.py``; nothing the follower runs reads a driver template
    'levain/dispatch/registry.py::driver_template_path::from levain import verifier_drivers': 'operator',
    # operator: ``body_refusal`` <- ``dispatch/cli.py`` and ``dispatch/control.py``; the follower reads no registry
    'levain/dispatch/registry.py::_verifier_entry_problem::from levain.verifier_drivers import output_compare': 'operator',
    # operator: <- ``StandingPolicy.apply`` (``git grep -n 'policy.apply(' -- levain`` finds ``levain/cockpit/broker.py`` and ``verbs.py`` only) and ``classify`` / ``_validate_policy`` / ``_apply_policy_op`` (the replay, ``_step``)
    'levain/authority.py::_tiers::from levain.cockpit.verbs import TIERS': 'operator',
    # operator: <- ``_validate_egress_key`` (``_validate_grant`` <- ``_validate_policy`` / ``_apply_policy_op``, the replay: the authority replay (``_step`` <- ``_replay_entry`` <- ``snapshot``, run by ``consent.queue.AuthoritySource`` and ``cockpit.authority.RequestAuthority.capture``); the queue broker builds its ``AuthoritySnapshot`` from published signers (``qbroker.grants.contract_authority``) and runs neither (``git grep -n -e 'snapshot(' -e 'authority_source' -e 'RequestAuthority(' -- levain/qbroker`` finds none)) and ``levain/chat_egress/advert.py``
    'levain/authority.py::egress_key_host_problem::from levain.chat_egress.parse import canonical_host': 'operator',
    # operator: <- ``_validate_egress_key`` (the replay's grant validation, as ``egress_key_host_problem``) and ``levain/chat_egress/advert.py``
    'levain/authority.py::egress_key_repo_problem::from levain.chat_egress.advert import KEYLESS_ESCAPES': 'operator',
    'levain/authority.py::egress_key_repo_problem::from levain.chat_egress.parse import canonical_path': 'operator',
    # operator: a ``Cockpit`` method; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py`` (``git grep -n 'Cockpit(' -- levain``), not in the closure; engine is here only as ``levain/cockpit/__init__.py``'s import
    'levain/cockpit/engine.py::Cockpit._note_decision_one::from levain.consent.notes import NoteStoreError': 'operator',
    # operator: a ``Cockpit`` method; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``, not in the closure
    'levain/cockpit/engine.py::Cockpit._question_decision_one::from levain.consent.questions import QuestionStoreError': 'operator',
    # operator: <- ``Cockpit._compose_decisions`` / ``Cockpit._consent_decision_one`` only; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``
    'levain/cockpit/engine.py::_consent_rows_in::from levain.consent.listing import egress_state_of': 'operator',
    'levain/cockpit/engine.py::_egress_lookup_of::from levain.consent.listing import egress_reading': 'operator',
    # operator: <- ``Cockpit._compose_decisions`` only; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``
    'levain/cockpit/engine.py::_note_rows_in::from levain.consent.listing import PENDING_CAP': 'operator',
    'levain/cockpit/engine.py::_question_rows_in::from levain.consent.listing import PENDING_CAP': 'operator',
    # operator: <- ``CockpitDB._ensure_schema_txn`` <- ``_ensure_schema``; ``CockpitDB(`` is constructed in ``levain/cli.py``, ``cockpit/{broker,keys,sign_client}.py`` and ``phone_app.py`` only (``git grep -n 'CockpitDB(' -- levain``)
    'levain/cockpit/firejournal.py::_take_store_lock::from levain.cockpit.broker import LOCK_NAME': 'operator',
    # operator: a ``FACETS`` validator, run by ``validate_facets``, whose one caller is ``levain/cockpit/engine.py`` (the ``Cockpit``)
    'levain/cockpit/registry.py::_is_sign_facet::from levain.sign_request_id import request_id_ok': 'operator',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): its GATE runs ``consent.limits.count_call`` -> ``_count``
    'levain/consent/limits.py::_count::from levain.cockpit.session_context import spend_step': 'root',
    # operator: the hook's limits path: ``consent.limits.check`` (called by the agent hooks ``consent/adapters/{claude_code,codex}.py`` only) -> ``_hold`` -> ``_resolve`` -> ``_filed_for`` / ``verified_allow``, ``release_if_extended``, and ``consent/limits_cli.py``; the queue broker runs ``count_call`` only
    'levain/consent/limits.py::_filed_for::from levain.consent.listing import filed_input': 'operator',
    'levain/consent/limits.py::_resolve::from levain.cockpit.consent import parse_stamp': 'operator',
    'levain/consent/limits.py::_resolve::from levain.consent.queue import Decision, Ruling': 'operator',
    'levain/consent/limits.py::verified_allow::from levain.consent.queue import Decision, Ruling': 'operator',
    # operator: no caller in ``levain/`` (``git grep -n _verified_deny -- levain`` finds only its def)
    'levain/consent/limits.py::_verified_deny::from levain.consent.queue import Decision': 'operator',
    # operator: callers are the agent hooks (``levain/consent/adapters/claude_code.py``, ``codex.py``) and ``levain/consent/limits_cli.py``, none in the closure
    'levain/consent/limits.py::ruling_text::from levain.consent.listing import filed_input': 'operator',
    # operator: ``ConsentQueue.file``'s callers (``git grep -n '\.file(' -- levain``) are the agent hooks, the gates, ``chat_egress.cards`` and ``consent.limits._hold`` / ``file_extend`` (the hook's limits path: ``consent.limits.check`` (called by the agent hooks ``consent/adapters/{claude_code,codex}.py`` only) -> ``_hold`` -> ``_resolve`` -> ``_filed_for`` / ``verified_allow``, ``release_if_extended``, and ``consent/limits_cli.py``; the queue broker runs ``count_call`` only); the queue broker files on its own ``qbroker.store.QueueStore``
    'levain/consent/queue.py::ConsentQueue.file::from levain.chat_egress.cards import CardError, filed_display': 'operator',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): ``qbroker.seam`` runs ``consent.queue.answer_refusal`` -> ``signer_refusal`` -> ``_signature_refusal``
    'levain/consent/queue.py::_signature_refusal::from levain.consent.passkey import answer_signature_problem': 'root',
    # operator: ``receipt_log.verify``'s one caller is ``levain/cli.py``'s ``_cmd_consent_receipts_verify`` (``levain consent receipts verify``); the broker reads ``head`` / ``head_refusal`` only
    'levain/consent/receipt_log.py::verify::from levain.authority import AuthorityError, VerifierUnavailable, anchor_path': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.answering import answer_root': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.queue import (_FIELDS, AnswerRefused, ConsentQueue, QueueStoreError, StoreNotCurrent,': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.record import UNCHAINED_FORMATS': 'operator',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): ``qbroker.grants`` checks a contract's signature with ``dispatch.sign.refusal`` -> ``_passkey_problem``
    'levain/dispatch/sign.py::_passkey_problem::from levain.consent.passkey import answer_signature_problem': 'root',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): its own package
    'levain/qbroker/grants.py::grant_row::from levain.qbroker.store import GRANT_ROW_FORMAT': 'root',
}
_NEEDLES = ("run_module(", "module_argv(", "import_module(", "__import__(")


def _keys(rel: str, sites: list) -> list:
    """The table keys of one file's sites, ``sites`` their (qualname, stripped source line) pairs in source order."""
    seen, out = {}, []
    for site in sites:
        n = seen[site] = seen.get(site, 0) + 1
        out.append("%s::%s::%s" % ((rel,) + site) + ("::#%d" % n if n > 1 else ""))
    return out


def _qualnames(tree):
    """A function of a line number -> the dotted class / function path of the innermost ``def`` / ``class`` whose span
    (its ``def`` line to its last line; decorators sit outside it) holds that line, ``<module>`` when none does."""
    spans = []

    def walk(node, prefix):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                spans.append((c.lineno, c.end_lineno, prefix + c.name))
                walk(c, prefix + c.name + ".")
            else:
                walk(c, prefix)
    walk(tree, "")
    # a nested span starts after its parent's, so the latest start holding the line is the innermost
    return lambda no: max(((s, q) for s, e, q in spans if s <= no <= e), default=(0, "<module>"))[1]


def dynamic_sites() -> list:
    """The sites a text scan of ``levain/*.py`` finds, as table keys, sorted. A file this interpreter cannot parse has
    no enclosing context to key by: its sites get ``<unparsed>`` (stderr says which), which no row names, so the test
    fails there rather than classifying blind."""
    found = []
    for path in sorted((ROOT / PKG).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        hits = [(no, line.strip()) for no, line in enumerate(text.splitlines(), 1) if any(n in line for n in _NEEDLES)]
        if not hits:
            continue
        try:
            qual = _qualnames(ast.parse(text, filename=str(path)))
            sites = [(qual(no), line) for no, line in hits]
        except SyntaxError:
            print("root_closure: dynamic sites keyed <unparsed> (3.10+ syntax): " + rel, file=sys.stderr)
            sites = [("<unparsed>", line) for _no, line in hits]
        found.extend(_keys(rel, sites))
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


def _exact(rel: list):
    """``ROOT`` joined with ``rel`` when that is a file and every component matches a directory entry exactly, else None:
    macOS's filesystem is case-insensitive, so ``is_file`` alone finds ``binding.py`` for ``Binding`` there and not on
    Linux, and the local closure would differ from CI's."""
    d = ROOT
    for name in rel:
        try:
            if name not in os.listdir(str(d)):
                return None
        except OSError:
            return None
        d = d / name
    return d if d.is_file() else None


def _path(mod: str):
    """The file of module ``mod`` (``<name>.py`` or ``<name>/__init__.py``), its name matched case-sensitively, or None."""
    parts = mod.split(".")
    return _exact(parts[:-1] + [parts[-1] + ".py"]) or _exact(parts + ["__init__.py"])


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
    """Every function-local ``levain`` import in ``mods``: its table key (the statement's first line) -> the module
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
        by_line = {}  # two statements on one line are one site
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in eager:
                names = _import_names(node, pkg)
                if names:
                    by_line.setdefault(node.lineno, set()).update(names)
        nos = sorted(by_line)  # source order, which the ordinal of a repeated line counts in
        qual = _qualnames(tree)
        for no, key in zip(nos, _keys(rel, [(qual(no), lines[no - 1].strip()) for no in nos])):
            found[key] = by_line[no]
    return found


def closure(entries=None) -> list:
    """The modules the entries (``entries``, else every root of :func:`_entry_modules`) import at import time, plus
    the targets of every ``"root"`` row of ``LAZY_SITES`` (and what those import at import time), to a fixed point: a
    lazy import a root process can run is as much root code as a top-level one. A site not in the table adds nothing
    here; the test fails it."""
    roots = set(_entry_modules() if entries is None else entries)
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


_CONFTEST_EXECUTOR = ("tests/conftest.py", "import test_only_confined_executor")
_CONFTEST_SIGNERS = ("tests/conftest.py", "def _test_uid_may_own_the_test_signers_file(")
#: Test files the import rule selects (they import only closure modules) whose 3.9 red says nothing about root, each
#: -> (reason, a string the test file holds, (a file, a string it holds)): both strings are the reason's evidence and a
#: test checks them, and that every entry still exists and is still selected without this table, so none can rot. The
#: job runs with ``--noconftest``, so a file that needs a conftest fixture cannot pass there.
NOT_ROOT_TESTS = {
    "tests/test_autonomic_gate_handle.py": ("needs conftest's test_only_confined_executor fixture",
                                            "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_journal_wiring.py": ("needs conftest's test_only_confined_executor fixture",
                                               "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_chainpath.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_firepath.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_coverage.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_coverage_r1.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_coverage_r2.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_graduation.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_l3_fixes.py": ("its enrolled test key verifies only under conftest's autouse signers-owner "
                                         "fixture", "confirm_signers", _CONFTEST_SIGNERS),
    "tests/test_autonomic_l3_r2_fixes.py": ("its enrolled test key verifies only under conftest's autouse "
                                            "signers-owner fixture", "confirm_signers", _CONFTEST_SIGNERS),
    "tests/test_autonomic_transport.py": ("its enrolled test key verifies only under conftest's autouse signers-owner "
                                          "fixture", "confirm_signers", _CONFTEST_SIGNERS),
    "tests/test_cockpit_k2b_journal.py": ("the test itself calls sqlite3.Connection.setconfig, 3.12+",
                                          ".setconfig(", ("tests/test_cockpit_k2b_journal.py", ".setconfig(")),
    "tests/test_consent_queue.py": ("its fixture signs through authority.sign_and_append, whose replay reaches "
                                    "levain.writes, which imports anneal_memory (not installed for 3.9)",
                                    "sign_and_append", ("levain/writes.py", "from anneal_memory import")),
}


def _test_files(mods: list, excluded=None) -> list:
    """The test files that import a closure module and import NOTHING of ``levain`` outside the closure (a ``levain``
    package import aside), counting what the ``tests.*`` helpers they import (recursively) import too: the files
    wholly about root-run code. A file that also reaches operator-side code (the CLI, the cockpit, ``tomllib`` users),
    directly or through a helper, is 3.11+ by design and its 3.9 red says nothing about root, so it is left to the
    3.12/3.13 matrix. A file (or helper) this interpreter cannot parse is skipped for the same reason, and so is every
    file of ``excluded`` (default :data:`NOT_ROOT_TESTS`)."""
    excluded = NOT_ROOT_TESTS if excluded is None else excluded
    inside = set(mods)
    leaf = [m for m in mods if _path(m).name != "__init__.py"]  # a package import says nothing: every test has one
    out = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        names = _test_deps(f, set())
        if names is None:
            print("skipped (3.10+ syntax): " + f.relative_to(ROOT).as_posix(), file=sys.stderr)
        elif names & set(leaf) and names <= inside and f.relative_to(ROOT).as_posix() not in excluded:
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
