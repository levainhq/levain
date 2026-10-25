"""``levain.consent.adapters.rules``: the adapters' pure rules the queue broker reads too, kept a leaf with one home."""
import ast
from pathlib import Path

from levain.consent import away
from levain.consent.adapters import claude_code, codex, rules
from levain.dispatch import cli as dispatch_cli
from levain.dispatch import contract, grant
from levain.qbroker import broker, grants


def test_rules_imports_nothing_of_levain():
    tree = ast.parse(Path(rules.__file__).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n == "levain" or n.startswith("levain.")]


def test_every_reader_has_the_one_copy():
    for name in ("CLEANUP_RESERVE", "DEFAULT_MAX_HOLD", "CLAUDE_CONFIG_DIRS", "CONFIG_FILE_NAMES", "GIT_STATE_DIRS",
                 "GIT_STATE_FILES", "_is_config_path", "_is_git_config"):
        assert getattr(claude_code, name) is getattr(rules, name), name
    assert claude_code.AGENT == rules.CLAUDE_CODE_AGENT == "claude_code"
    for name in ("CODEX_DIR_NAME", "PATCH_TOOL", "_PATCH_HEADERS", "_PATCH_MARKERS", "_in_codex_config", "patch_targets"):
        assert getattr(codex, name) is getattr(rules, name), name
    assert grant.patch_targets is rules.patch_targets and grant.PATCH_TOOL is rules.PATCH_TOOL
    assert away.MAX_HOLD == broker.MAX_HOLD == rules.MAX_HOLD == rules.DEFAULT_MAX_HOLD - rules.CLEANUP_RESERVE
    assert grants._is_config_path is rules._is_config_path and grants._in_codex_config is rules._in_codex_config
    assert dispatch_cli._CONTRACT_MAX == contract.CONTRACT_MAX
    assert grants.PUBLISHED_MAX == contract.CONTRACT_MAX + 64 * 1024
