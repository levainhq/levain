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
    # G1b: the Claude identity call's child (stdlib only), built by launch_spec and matched by the stage's spec check
    "levain/slot/wall.py::launch_spec::argv = module_argv(host, IDENTITY_MODULE)": ("levain.slot.identity_child",),
    "levain/slot/wall.py::_check_identity_spec::" 'host = next((h for h in ("darwin", "linux") if s.argv == module_argv(h, IDENTITY_MODULE)': ("levain.slot.identity_child",),
    "levain/slot/wall.py::stage_argv::" 'return module_argv(host, "levain.slot.wall", "--spec-fd", str(spec_fd))': ("levain.slot.wall",),
    "levain/slot/wall.py::trampoline_argv::" 'return module_argv("darwin", "levain.slot.wall", "--exec-fd", str(exec_fd), "--", *argv)': ("levain.slot.wall",),
    # in-process ``importlib`` calls, none run by a root process (empty = no module seeded); each row's reason:
    # the two comments name the explicit leaf maps; the firing / presence calls import only the blessed leaves of
    # ``_LAZY_FIRING_MODULES`` / ``_LAZY_PRESENCE_MODULES`` for the operator's daemon (a variable, not a root target);
    # the cockpit one is the operator's panel, importing ``levain.consent.limits`` (not a root-run module)
    "levain/cockpit/providers.py::_limit_lines::" 'mod = importlib.import_module("levain.consent.limits")': (),
    # the 1.x old-name shim: its loader imports the real ``levain.desk.<x>`` a ``levain.coordinator.<x>`` import
    # names (the same module object); no levain module imports ``levain.coordinator``, so no root process reaches it
    "levain/coordinator/__init__.py::_AliasLoader.create_module::return importlib.import_module(self._real)": (),
    "levain/firing/contract.py::<module>::" '# rather than ``import_module(f"{__name__}.{kind}")`` GOVERNS which leaves build_firing may': (),
    "levain/firing/contract.py::build_firing::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py::build_presence::" 'importlib.import_module(module)  # blessed leaf — its own import errors propagate': (),
    "levain/firing/presence.py::<module>::" '# recover. An EXPLICIT map (not ``import_module(f"…{kind}")``) governs which modules may be': (),
}
LAZY_SITES = {
    # operator: ``_landed`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::_landed::from levain.agents import bars': 'operator',
    # operator: ``_lost`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::_lost::from levain.agents import bars': 'operator',
    # operator: ``open_bar_for`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::open_bar_for::from levain.agents import bars': 'operator',
    # operator: ``open_bar_for`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::open_bar_for::from levain.autonomic.db import DB_NAME as _STORE_DB, default_store_dir': 'operator',
    # operator: ``revoke_agent_begin`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::revoke_agent_begin::from levain.agents import bars': 'operator',
    # operator: ``revoke_agent_finish`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::revoke_agent_finish::from levain.agents import bars': 'operator',
    # operator: ``revoke_agent_sign`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::revoke_agent_sign::from levain.agents import bars': 'operator',
    # operator: ``revoke_agent_sign`` runs only from the operator's agent verbs (``levain agents renew`` / ``revoke`` via ``agents.signing`` / ``agents.cli``)
    'levain/agent_authority.py::revoke_agent_sign::from levain.cockpit.consent import utc_stamp': 'operator',
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
    'levain/slot/end.py::confirmed_relogin::from levain.slot import identity as ID': 'root',
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
    # root: ``render_facts`` (the runtime helper's render verb) builds the hook templates the blob names
    'levain/slot/runtime_helper.py::hook_templates_for::from levain.slot.hook_templates import hook_templates': 'root',
    # operator: ``hook_run.run`` is the SLOT HOOK's process (the slot uid, never root, like the queue broker's service): it runs the consent adapter, which imports the authority store and the hooks that the root closure keeps out (tests/test_ci_root_closure.py). Its 3.9 floor is held by tests/test_slot_hook_templates.py, not by this closure
    'levain/slot/hook_run.py::_load_adapter::from levain.consent.adapters import claude_code': 'operator',
    # operator: same slot hook process; ``in_slot`` of the broker client
    'levain/slot/hook_run.py::run::from levain.consent.broker_client import in_slot': 'operator',
    # operator: same slot hook process; the ConfigChange guard's ALARM to the broker
    'levain/slot/hook_run.py::config_guard::from levain.consent.broker_client import BrokerClient, in_slot': 'operator',
    'levain/slot/runtime_helper.py::_front_interlock::from levain.slot import start as SS': 'root',
    # root: render builds the front's policy file and reads it with the front's own parser (3b-8a)
    'levain/slot/runtime_helper.py::front_policy_bytes::from levain.broker import front as FR': 'root',
    'levain/slot/runtime_helper.py::read_operator::from levain.slot.install_helper import parse_operator_record': 'root',
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
    # the identity gate (1027+18 idg1): confirm-identity's handler and the baseline reader prepare runs
    'levain/slot/runtime_helper.py::_baseline_vendor::from levain.slot import identity as ID': 'root',
    'levain/slot/runtime_helper.py::_confirm_identity::from levain.slot import identity as ID': 'root',
    'levain/slot/runtime_helper.py::status::from levain.slot import identity as ID': 'root',
    # G1b: the Claude identity call spawns its child through start (identity is imported by start at module level)
    'levain/slot/identity.py::_claude_call::from levain.slot import start as SS': 'root',
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
    'levain/slot/wall.py::_check_identity_spec::from levain.slot.grammar import SESSION_RE': 'root',
    'levain/slot/wall.py::_check_identity_spec::from levain.slot.layout import STORE': 'root',
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
    # operator: ``git grep -n '\.principal\b' -- levain`` finds it read in ``levain/cli.py`` and ``levain/cockpit/{broker,engine,keys,labels,sign_client}.py`` only (engine's in a ``Cockpit`` panel)
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
    'levain/authority.py::_agent_text::from levain import unshowable': 'root',
    'levain/authority.py::_stamp::from levain.cockpit.consent import utc_stamp': 'root',
    'levain/autonomic/journal.py::RunJournal._barrier::from levain.autonomic.binding import agent_run_proof, registry_bars, registry_scan_bars': 'root',
    # root: lazy because ``schema3`` imports ``journal`` at module level (a hoist is a cycle); ``schema3`` is already a closure member through ``binding``'s import-time import, so this row adds nothing to the closure
    'levain/autonomic/journal.py::_insert_receipt::from levain.autonomic.schema3 import close_subject': 'root',
    # root: the same cycle and the same already-member target as ``_insert_receipt`` above
    'levain/autonomic/journal.py::close_effect_in::from levain.autonomic.schema3 import effect_subject': 'root',
    # root: the same cycle and the same already-member target as ``_insert_receipt`` above
    'levain/autonomic/journal.py::_closer_of::from levain.autonomic.schema3 import _effect_key': 'root',
    'levain/autonomic/journal.py::close_not_started_in::from levain.autonomic.schema3 import _closed, count_held, effect_subject, held_limit': 'root',
    'levain/autonomic/journal.py::intent_in::from levain.autonomic.schema3 import effect_subject': 'root',
    'levain/autonomic/journal.py::effect_seal_in::from levain.autonomic.schema3 import hold_decision': 'root',
    # root: the same cycle and the same already-member target as ``_insert_receipt`` above (R15-5 S1)
    'levain/autonomic/journal.py::RunJournal.__init__::from levain.autonomic.schema3 import AutonomicStore3': 'root',
    'levain/autonomic/journal.py::RunJournal._intent3::from levain.autonomic.schema3 import CLOSE_STATES, CloseUnbuildable, dry_run_close, effect_subject, utc_text': 'root',
    'levain/cockpit/authority.py::RequestAuthority.capture::from levain import authority as auth': 'root',
    'levain/cockpit/authority.py::RequestAuthority.capture::from levain.cockpit.firejournal import UNINITIALISED, _meta, conn_init_hint': 'root',
    'levain/cockpit/authority.py::RequestAuthority.fence::from levain.cockpit.firejournal import authority_fence': 'root',
    'levain/cockpit/authority.py::RequestAuthority.policy::from levain.authority import genesis_policy': 'root',
    'levain/cockpit/authority.py::RequestAuthority.revision::from levain.cockpit.firejournal import authority_fence': 'root',
    # operator: _agent_link_card has no caller in levain/ (git grep: only tests/test_cockpit_agent_card.py); it is the cockpit's agent-link signing card, run only when the operator signs a link, never by a root process
    'levain/cockpit/authority.py::_agent_link_card::from levain.authority import AGENT_LINK_KIND, _thaw': 'operator',
    # operator: _agent_link_card has no caller in levain/ (git grep: only tests/test_cockpit_agent_card.py); it is the cockpit's agent-link signing card, run only when the operator signs a link, never by a root process
    'levain/cockpit/authority.py::_agent_link_card::from levain.autonomic.coverage import guard_problem': 'operator',
    # operator: open_bar_problem has no caller in levain/ (git grep: only tests/test_cockpit_agent_card.py); it is the cockpit's agent-link signing card, run only when the operator signs a link, never by a root process
    'levain/cockpit/authority.py::open_bar_problem::from levain.agent_authority import OPEN_BAR_REFUSAL, AgentAuthorityError, open_bar_for': 'operator',
    # operator: sign_card has no caller in levain/ (git grep: only tests/test_cockpit_agent_card.py); it is the cockpit's agent-link signing card, run only when the operator signs a link, never by a root process
    'levain/cockpit/authority.py::sign_card::from levain.agents.signing import sign_agent_link': 'operator',
    'levain/consent/queue.py::AuthoritySource.head::from levain.authority import log_head': 'root',
    'levain/dispatch/contract.py::git_env::from levain.launch import child_env': 'root',
    # root: the read set's ingest (``_verify_in``) runs in the dispatch run server for a workspace upload (``levain.slot.workspace``, levain 34, W1); each of these is a step of it
    'levain/dispatch/readset.py::_layer_check::from levain.slot import Refused': 'root',
    'levain/dispatch/readset.py::_layer_check::from levain.slot.render import freeze_repo_settings': 'root',
    'levain/dispatch/readset.py::_layer_check::from levain.slot.supervisor import check_repo_layer, repo_layer_docs': 'root',
    'levain/dispatch/readset.py::_limits::from levain.slot.stage import BUNDLE_CAP, STAGE_DEADLINE_S': 'root',
    'levain/dispatch/readset.py::_write_attributes::from levain.slot import write_all': 'root',
    'levain/dispatch/readset.py::_write_attributes::from levain.slot.render import READSET_ATTRIBUTES': 'root',
    # operator: ``checkout_prefix`` <- ``build_readset`` (the operator's build); the ingest never calls it
    'levain/dispatch/readset.py::checkout_prefix::from levain.slot.grammar import SESSION_RE': 'operator',
    'levain/dispatch/readset.py::checkout_prefix::from levain.slot.layout import session_repo': 'operator',
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
    # operator: ``land`` is the operator's (``levain dispatch land``)
    'levain/dispatch/adopt.py::_human_refusal::from levain.consent.queue import AuthoritySource': 'operator',
    # operator: ``land`` is the operator's (``levain dispatch land``)
    'levain/dispatch/adopt.py::_human_refusal::from levain.dispatch.jobs import _replay': 'operator',
    # operator: ``agent_config_paths`` <- ``dispatch/cli.py`` (the authorise view) and ``dispatch/phone.py`` (the card); the follower never calls it
    'levain/dispatch/adopt.py::agent_config_paths::from levain.slot.render import REPO_DISCOVERY': 'operator',
    # operator: same callers as the row above
    'levain/dispatch/adopt.py::agent_config_paths::from levain.slot.render import fold_name as _fold': 'operator',
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
    # operator: <- ``_validate_egress_key`` (``_validate_grant`` <- ``_validate_policy`` / ``_apply_policy_op``, the replay: the authority replay (``_step`` <- ``_replay_entry`` <- ``snapshot``, run by ``consent.queue.AuthoritySource`` and ``cockpit.authority.RequestAuthority.capture``); the queue broker builds its ``AuthoritySnapshot`` from published signers (``qbroker.grants.contract_authority``) and runs neither (``git grep -n -e 'snapshot(' -e 'authority_source' -e 'RequestAuthority(' -- levain/qbroker`` finds none)) and ``levain/chat_egress/advert.py``
    'levain/authority.py::egress_key_host_problem::from levain.chat_egress.parse import canonical_host': 'operator',
    # operator: <- ``_validate_egress_key`` (the replay's grant validation, as ``egress_key_host_problem``) and ``levain/chat_egress/advert.py``
    'levain/authority.py::egress_key_repo_problem::from levain.chat_egress.advert import KEYLESS_ESCAPES': 'operator',
    'levain/authority.py::egress_key_repo_problem::from levain.chat_egress.parse import canonical_path': 'operator',
    # operator: <- ``_grant_append_checks`` / the renew branch of ``_append_only_checks`` <- ``prepare`` / ``append`` (the signer's process: ``cli``, ``cockpit.keys``, ``consent.grants``, ``agents``, ``chat_egress.key_setup``); never the replay (``_step``)
    'levain/authority.py::_hands_mcp_check::from levain import hands_mcp': 'operator',
    # operator: the seat-lease append / renew rule (``SEAT_LEASE_KIND``'s ``append_checks`` / ``renew_checks``), reached through ``_grant_append_checks`` and the renew branch, the signer's process; its docstring: an append rule, not a replay rule, so no root process runs it (``levain.host_guard`` needs 3.11 ``tomllib``)
    'levain/authority.py::_seat_lease_check::from levain import host_guard as hg': 'operator',
    # operator: the seat-egress append / renew rule (``SEAT_EGRESS_KIND``'s ``append_checks`` / ``renew_checks``, PATHZERO Z2a), reached through ``_grant_append_checks`` and the renew branch, the signer's process; an append rule, not a replay rule, so no root process runs it (``levain.host_guard`` needs 3.11 ``tomllib``; the canonical-remote spelling is ``levain.consent.push``'s)
    'levain/authority.py::_seat_egress_check::from levain import host_guard as hg': 'operator',
    'levain/authority.py::_seat_egress_check::from levain.consent import push': 'operator',
    # operator: the case-folded agent-config name test both rules above call (``_seat_lease_check``,
    # ``_seat_egress_check``; L3 zc r1 deepseek MED-2), so it runs only where they run: the signer's process
    'levain/authority.py::_names_agent_config::from levain import host_guard as hg': 'operator',
    'levain/authority.py::_names_agent_config::from levain import pathid': 'operator',
    # operator: a ``Cockpit`` method; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py`` (``git grep -n 'Cockpit(' -- levain``), not in the closure; engine is here only as ``levain/cockpit/__init__.py``'s import
    'levain/cockpit/engine.py::Cockpit._note_decision_one::from levain.consent.notes import NoteStoreError': 'operator',
    # operator: a ``Cockpit`` method; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``, not in the closure
    'levain/cockpit/engine.py::Cockpit._question_decision_one::from levain.consent.questions import QuestionStoreError': 'operator',
    'levain/cockpit/engine.py::Cockpit._question_decision_one::from levain.consent.questions import intention_of': 'operator',
    # operator: <- ``Cockpit._compose_decisions`` / ``Cockpit._consent_decision_one`` only; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``
    'levain/cockpit/engine.py::_consent_rows_in::from levain.consent.listing import egress_state_of': 'operator',
    'levain/cockpit/engine.py::_egress_lookup_of::from levain.consent.listing import egress_reading': 'operator',
    'levain/cockpit/engine.py::_board_visible::from levain.cockpit import board': 'operator',
    # operator: <- ``Cockpit._compose_decisions`` only; ``Cockpit(`` is constructed only in ``levain/cockpit/providers.py``
    'levain/cockpit/engine.py::_note_rows_in::from levain.consent.listing import PENDING_CAP': 'operator',
    # operator: the same function as the row below (``Cockpit._compose_decisions`` only)
    'levain/cockpit/engine.py::_question_rows_in::from levain.band_view import later_facets': 'operator',
    'levain/cockpit/engine.py::_question_rows_in::from levain.consent.questions import intention_of': 'operator',
    'levain/cockpit/engine.py::_intention_words_digest::from levain.consent.questions import intention_text_of': 'operator',
    'levain/cockpit/engine.py::_question_rows_in::from levain.consent.listing import PENDING_CAP': 'operator',
    # operator: <- ``CockpitDB._ensure_schema_txn`` <- ``_ensure_schema``; ``CockpitDB(`` is constructed in ``levain/cli.py`` and ``cockpit/{broker,keys,sign_client}.py`` only (``git grep -n 'CockpitDB(' -- levain``)
    'levain/cockpit/firejournal.py::_take_store_lock::from levain.cockpit.broker import LOCK_NAME': 'operator',
    # operator: a ``FACETS`` validator, run by ``validate_facets``, whose one caller is ``levain/cockpit/engine.py`` (the ``Cockpit``)
    'levain/cockpit/registry.py::_is_sign_facet::from levain.sign_request_id import request_id_ok': 'operator',
    'levain/cockpit/registry.py::_is_intention_id::from levain.consent.questions import valid_intention_id': 'operator',
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
    # operator: ``_terminal_ruling_refusal`` and ``answer_terminal_ruling`` (TERMSIGN L4) are run by ``_verified_ruling`` (the take / claim / decision readers the agent hooks call) and by ``band_view._terminal_signer`` (the seat's UserPromptSubmit hook); the queue broker runs ``answer_refusal`` -> ``signer_refusal`` only, never a ruling
    'levain/consent/queue.py::ConsentQueue._terminal_ruling_refusal::from levain.authority import LAPTOP, VerifierUnavailable': 'operator',
    'levain/consent/queue.py::ConsentQueue._terminal_ruling_refusal::from levain.authority_sig import _signers_line': 'operator',
    'levain/consent/queue.py::ConsentQueue._terminal_ruling_refusal::from levain.consent import terminal_sign as ts': 'operator',
    'levain/consent/queue.py::ConsentQueue.answer_terminal_ruling::from levain.consent import terminal_sign as ts': 'operator',
    # operator: ``_carrier`` is called only by ``_terminal_ruling_refusal`` and ``answer_terminal_ruling`` (the two rows above), so it runs in the same operator processes
    'levain/consent/queue.py::ConsentQueue._carrier::from levain.consent import terminal_sign as ts': 'operator',
    # operator: ``_announce_settled`` runs from ``ConsentQueue._write`` and ``sweep`` is a ``ConsentQueue`` method, both on an INSTANCE; the root queue broker (``levain/qbroker/``) uses only the module-level ``answer_refusal`` and ``ConsentQueue._face`` and builds no instance (``git grep -n "ConsentQueue(" -- levain/qbroker levain/slot/services`` finds none), so only the operator's processes (the hooks, the cockpit, the CLI) announce a settle or prune a mark
    'levain/consent/queue.py::ConsentQueue._announce_settled::from levain.consent.band_notice import announce_settled': 'operator',
    'levain/consent/queue.py::ConsentQueue.sweep::from levain.consent.band_notice import prune_marks': 'operator',
    # operator: ``receipt_log.verify``'s one caller is ``levain/cli.py``'s ``_cmd_consent_receipts_verify`` (``levain consent receipts verify``); the broker reads ``head`` / ``head_refusal`` only
    'levain/consent/receipt_log.py::verify::from levain.authority import AuthorityError, VerifierUnavailable, anchor_path': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.answering import answer_root': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.queue import (_FIELDS, AnswerRefused, ConsentQueue, QueueStoreError, StoreNotCurrent,': 'operator',
    'levain/consent/receipt_log.py::verify::from levain.consent.record import UNCHAINED_FORMATS': 'operator',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): ``qbroker.grants`` checks a contract's signature with ``dispatch.sign.refusal`` -> ``_passkey_problem``
    'levain/dispatch/sign.py::_passkey_problem::from levain.consent.passkey import answer_signature_problem': 'root',
    # the queue broker (``levain.slot.services.qbroker``, ROOT_PYTHON as ``_levain_qbroker``): its own package
    'levain/qbroker/grants.py::grant_row::from levain.qbroker.store import GRANT_ROW_FORMAT': 'root',
    # operator: ``_admit_run_in`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    "levain/agents/controller.py::_admit_run_in::from levain.autonomic.journal import RunJournal   # here: the journal is not the controller's import closure": 'operator',
    # operator: ``_default_input_check`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    # operator: ``_default_input_check`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/controller.py::_default_input_check::from levain.agents import spec as _spec': 'operator',
    'levain/agents/controller.py::_default_input_check::from levain.dream import agent as dream_agent': 'operator',
    # operator: ``_dream_body`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    "levain/agents/controller.py::_dream_body::from levain.dream import agent as dream_agent   # here: the dream is not the controller's import closure": 'operator',
    # operator: ``_pinned_executor`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/controller.py::_pinned_executor::from levain.agents import role as _role, spec as _spec': 'operator',
    # operator: ``_record_is_dead`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/controller.py::_record_is_dead::from levain.consent.delivery import process_running': 'operator',
    # operator: ``_session_body`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/controller.py::_session_body::from levain.agents import session, session_wire   # here: both import this module': 'operator',
    # operator: ``fire`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    "levain/agents/controller.py::fire::from levain.consent.delivery import process_identity   # here: the consent package is not the controller's closure": 'operator',
    # operator: ``fire_refusal`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    "levain/agents/controller.py::fire_refusal::from levain.autonomic.journal import RunJournal   # here: the journal is not the controller's import closure": 'operator',
    # operator: ``_stored_spec`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/gauges.py::_stored_spec::from levain.agents import spec as _spec': 'operator',
    # operator: ``_fire_state`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session.py::_fire_state::from levain.consent.delivery import process_identity': 'operator',
    # operator: ``_signed_by_spec`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session.py::_signed_by_spec::from levain.agents import spec as _spec': 'operator',
    # operator: ``run_session`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session.py::run_session::from levain.autonomic.journal import EffectStatus': 'operator',
    # root: ``RealLauncher._workspace_record`` runs in the dispatch run server (``spawn`` writes the run record's workspace facts); its target is already a closure member
    'levain/agents/session_launcher.py::RealLauncher._workspace_record::from levain.dispatch.snapshot import withhold_digest': 'root',
    # operator: ``AuthorityFeed.read`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::AuthorityFeed.read::from levain.agent_authority import AgentAuthority': 'operator',
    # operator: ``AuthorityFeed.rows`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::AuthorityFeed.rows::from levain.cockpit.firejournal import DB_NAME': 'operator',
    # operator: ``build_workspace`` builds the workspace snapshot from the operator's repo before the LAUNCH connection is opened (levain 34, W1); it runs only in the fire, the operator's uid, and the dispatch run server never calls it
    'levain/agents/session_client.py::DispatchSessionLauncher.build_workspace::from levain.dispatch import stager': 'operator',
    # operator: ``build_launcher`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::build_launcher::from levain.agents.session_wire import read_install': 'operator',
    # operator: ``build_workspace`` re-derives the signed role's withhold before the snapshot is cut (levain 1107, W2); it runs only in the fire, the operator's uid
    'levain/agents/session_client.py::DispatchSessionLauncher.build_workspace::from levain.agents.signed_role import SignedRoleRefused, signed_withhold': 'operator',
    # operator: ``signed_role_of`` reads the agent store for the spec and role texts a workspace LAUNCH carries; the fire only, the run server never calls it
    'levain/agents/session_client.py::signed_role_of::from levain.agents import role as _role, spec as _spec': 'operator',
    'levain/agents/session_client.py::signed_role_of::from levain.authority import _canonical': 'operator',
    # operator: ``_withhold`` validates a role file's ceiling.withhold at registration (the operator's CLI); the run server never parses a role file
    'levain/agents/role.py::_withhold::from levain.dispatch.policy import _pattern_problem': 'operator',
    # root: ``signed_withhold`` runs in the dispatch run server (``RunServer._signed_withhold`` re-derives the signed role's withhold), so its lazy target ``levain.agents.role`` joins the closure
    'levain/agents/signed_role.py::signed_withhold::from levain.agents.role import RoleError, _withhold     # role imports this module: resolved at the call': 'root',
    # operator: ``default_run_socket`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::default_run_socket::from levain.slot import layout': 'operator',
    # operator: ``_standing_link`` runs only under ``link_proof`` and ``signed_link_of``, in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::_standing_link::from levain import authority as _auth': 'operator',
    # operator: ``_standing_link`` runs only under ``link_proof`` and ``signed_link_of``, in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::_standing_link::from levain.agent_authority import AgentAuthority': 'operator',
    # operator: ``link_proof`` runs only in the operator's process (the controller, an agent session or the session client); the dispatch run server only imports the module
    'levain/agents/session_client.py::link_proof::from levain.cockpit.firejournal import DB_NAME': 'operator',
    # operator: ``register_specs`` (and ``_register_in`` under it) is called only by ``levain agents register`` (``levain/agents/cli.py``), the operator's process; the dispatch run server only imports the module
    "levain/agents/spec.py::_register_in::from levain.agents import operator as _operator     # lazy: the registry's side, never reached by a fire": 'operator',
    "levain/agents/spec.py::register_specs::from levain.agents import operator as _operator     # lazy: the registry's side, never reached by a fire": 'operator',
    # root: ``_default_verify_sig`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::_default_verify_sig::from levain.authority_sig import verify_link_sig': 'root',
    # root: ``bound_usage_sources`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::bound_usage_sources::from levain.qbroker import operator as _op': 'root',
    # root: ``bound_usage_sources`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::bound_usage_sources::from levain.slot import dispatch_usage as _du': 'root',
    # root: ``build_server`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::build_server::from levain.qbroker.peercred import peer_uid': 'root',
    # root: ``build_server`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::build_server::from levain.slot.dispatch_runtime import DispatchControlPort, HelperSlotRuntime': 'root',
    # root: ``build_server`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry (PRE-W: the brokered launcher's spend reader, setting ON)
    'levain/slot/services/dispatch_run.py::build_server::from levain.slot.dispatch_usage import lineage_spend_reader': 'root',
    # root: ``build_server`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry (PRE-W: the lineage sink's name)
    'levain/slot/services/dispatch_run.py::build_server::from levain.slot.lineage import SINK': 'root',
    # root: ``supervisor_facts`` runs in the dispatch follower's own process (``follow_run.main``, ``_levain_dispatch``), which is a service entry (PRE-W)
    'levain/dispatch/follow_run.py::supervisor_facts::from levain.slot.dispatch_runtime import DispatchControlPort': 'root',
    # root: ``_parser`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry (PRE-W: the setting's path)
    'levain/slot/services/dispatch_run.py::_parser::from levain.slot.layout import policy_dispatch_auth_path': 'root',
    # root: ``main`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::main::from levain.slot.layout import operator_record_path': 'root',
    # root: ``main`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::main::from levain.slot.services.control import _operator_uid': 'root',
    # root: ``service_row`` runs in the dispatch run server's own process (``_levain_dispatch``, never the operator's), which is a service entry
    'levain/slot/services/dispatch_run.py::service_row::from levain.slot.units import Service': 'root',
    # root: ``_default_read_total`` runs in the dispatch run server's process (its usage settle)
    'levain/slot/dispatch_usage.py::_default_read_total::from levain.slot.runtime_helper import read_session_total': 'root',
    # operator: ``Seams.__post_init__`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::Seams.__post_init__::from levain.slot.control_client import ControlClient': 'operator',
    # operator: ``Supervisor._confirm_identity`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::Supervisor._confirm_identity::from levain.slot.identity import BASELINE_WRITTEN, NOT_A_CHECK': 'operator',
    # operator: ``Supervisor.run`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::Supervisor.run::from levain.slot.render import freeze_repo_settings': 'operator',
    # operator: ``Supervisor._leased`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::Supervisor._leased::from levain.slot.stage import make_bundle': 'operator',
    # root: ``_git`` runs the repo-layer check's git, and the dispatch run server runs that check on a workspace snapshot (``readset._layer_check`` <- ``workspace``, levain 34, W1); its target is already a closure member
    'levain/slot/supervisor.py::_git::from levain.slot.stage import STAGE_DEADLINE_S, _git_env as env': 'root',
    # root: ``check_repo_layer`` is the interactive supervisor's check AND the dispatch run server's on a workspace snapshot (``readset._layer_check`` <- ``workspace``, levain 34, W1); its target is already a closure member
    'levain/slot/supervisor.py::check_repo_layer::from levain.slot.render import repo_loosening_refusals': 'root',
    # root: ``repo_layer_docs`` feeds ``check_repo_layer`` on the same two paths (``readset._layer_check`` runs it in the dispatch run server); its target is already a closure member
    'levain/slot/supervisor.py::repo_layer_docs::from levain.slot.render import fold_name as _fold   # the one fold, imported where it is used': 'root',
    # operator: ``worktree_layer_docs`` reads the operator's checkout as it is on disk and is called only by ``Supervisor.run``, the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::worktree_layer_docs::from levain.slot.render import fold_name as _fold   # the one fold, imported where it is used': 'operator',
    # operator: ``identity_answer`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::identity_answer::from levain.slot.records import _REASON_RE, OBSERVED_ID_RE': 'operator',
    # operator: ``prepare_answer`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::prepare_answer::from levain.slot.ids import SlotId': 'operator',
    # operator: ``prepare_answer`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::prepare_answer::from levain.slot.render import gwport_for': 'operator',
    # operator: ``prepare_answer`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::prepare_answer::from levain.slot.runtime_helper import PREPARE_RESULT_FORMAT': 'operator',
    # operator: ``probe_answer`` is the interactive supervisor's own code, the operator uid's process; the dispatch run server imports the module for its names
    'levain/slot/supervisor.py::probe_answer::from levain.slot.probe import MAX_PROBE_ADDRS, PROBE_HOSTS': 'operator',
    # root: ``default_store_dir`` has a caller a root process can run, and no call path shows it operator-only, so the table's default applies (levain 16, 11-12)
    'levain/autonomic/db.py::default_store_dir::from levain.home_guard import refuse_real_home_fallback': 'root',
    # root: ``queue_dir_for`` has a caller a root process can run, and no call path shows it operator-only, so the table's default applies (levain 16, 11-12)
    'levain/consent/queue.py::queue_dir_for::from levain.home_guard import refuse_real_home_fallback': 'root',
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
    "tests/test_autonomic_attempts_readers.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_v3_s2.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_v3_s2f.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_v3_s2fx.py": ("needs conftest's test_only_confined_executor fixture",
        "test_only_confined_executor", _CONFTEST_EXECUTOR),
    "tests/test_autonomic_gate_v3_s2fx2.py": ("needs conftest's test_only_confined_executor fixture",
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
