"""The keys of Claude Code's ``sandbox`` settings block that relax the profile, in one place both sides read.

``levain.host_guard`` refuses a managed or user settings layer that sets them, and ``levain.slot.render`` refuses a repo's
settings layer that does (:func:`levain.slot.render.repo_loosening_refusals`). That second reader runs wherever a repo's
snapshot is judged, the dispatch run server included, a service under the host's ``/usr/bin/python3`` (3.9 on macOS), and
``host_guard`` is a large operator-side module, so the lists live here, stdlib-free and importable on 3.9, and both import
them. Each entry is a key path inside ``sandbox``.

``network.allowMachLookup`` naming ``com.apple.trustd.agent`` opens what ``enableWeakerNetworkIsolation`` opens; a run
(levain-memory 1026-8/p1) made trustd fetch a URL a sandboxed command chose, outside the proxy.
"""
from __future__ import annotations

__all__ = ["LOOSENING_NONEMPTY", "LOOSENING_TRUE"]

#: Keys that must not be ``true``.
LOOSENING_TRUE = (("allowUnsandboxedCommands",), ("enableWeakerNestedSandbox",),
                  ("enableWeakerNetworkIsolation",), ("allowAppleEvents",), ("allowNestedUserNamespaces",),
                  ("network", "allowAllUnixSockets"), ("network", "allowLocalBinding"),
                  ("filesystem", "disabled"))
#: Keys that must be empty lists.
LOOSENING_NONEMPTY = (("excludedCommands",), ("filesystem", "allowWrite"), ("filesystem", "allowRead"),
                      ("network", "allowUnixSockets"), ("network", "allowedDomains"),
                      ("network", "allowMachLookup"))
