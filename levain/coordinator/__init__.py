"""levain.coordinator: the old import path of :mod:`levain.desk`, kept for the 1.x line.

``import levain.coordinator`` and ``from levain.coordinator.room_host import HostCommands`` keep working: this
package re-exports ``levain.desk``, and a meta-path finder resolves ``levain.coordinator.<anything>`` to the very same
module object as ``levain.desk.<anything>`` (so ``levain.coordinator.x.Y is levain.desk.x.Y``). One of the two files
allowed to hold the old word as code (the other is ``levain/desk/legacy.py``).
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

import levain.desk as _desk

_OLD = __name__ + "."
_NEW = "levain.desk."


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real: str, real_spec) -> None:
        self._real = real
        self._real_spec = real_spec      # the real module's OWN spec, taken before an alias import overwrites its __spec__

    def create_module(self, spec):
        return importlib.import_module(self._real)

    def exec_module(self, module) -> None:        # the real module is already executed
        return None

    def _real_loader(self):
        return self._real_spec.loader

    def get_code(self, fullname):                 # runpy (``python -m levain.coordinator.x``) runs the real module's code
        return self._real_loader().get_code(self._real)

    def get_source(self, fullname):
        return self._real_loader().get_source(self._real)

    def is_package(self, fullname):
        return self._real_spec.submodule_search_locations is not None


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD):
            return None
        real = _NEW + fullname[len(_OLD):]
        try:
            real_spec = importlib.util.find_spec(real)
        except (ImportError, ValueError):
            return None
        if real_spec is None:
            return None
        if isinstance(real_spec.loader, _AliasLoader):      # the real module was already imported through its alias
            real_spec = real_spec.loader._real_spec
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real, real_spec))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name: str):
    return getattr(_desk, name)
