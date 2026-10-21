"""levain.verifier_drivers — the verifier driver templates Levain ships, one stdlib-only file each.

A template is run as a SCRIPT, never imported by a verifier session:
``<python> -I -S <template> <input.json> <run_dir>``
(CONTRACT §3.5, the fixed driver argv). A signed ``verifier`` configuration item names a template by its file stem and
pins the SHA-256 of its bytes (:func:`levain.dispatch.registry.driver_template_digest`).
"""
