"""The flags every headless-Chrome launcher in the tests starts with, kept in one JSON file the node drivers read too.

``--use-mock-keychain --password-store=basic`` keep a fresh profile from asking the macOS keychain for Chrome's storage
key (a dialog on the operator's screen). A launcher spreads ``CHROME_SAFE_FLAGS`` instead of typing the flags, and
``test_chrome_launch_sites.py`` fails any launch site that does not.
"""
from __future__ import annotations

import json
from pathlib import Path

CHROME_FLAGS_FILE = Path(__file__).with_name("chrome_flags.json")
CHROME_SAFE_FLAGS: list[str] = json.loads(CHROME_FLAGS_FILE.read_text(encoding="utf-8"))["flags"]
