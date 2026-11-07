# RETURN doctorhint (row 81 residue)

Fixed the `levain doctor` reaper-log hint. `reaper_log_where` (levain/doctor.py) said the log is "not created until `sudo levain setup-isolation` is re-run"; measured on a real macOS VM, that does not create it. It now says: not created until `sudo <install-helper> repair --source <this install's levain folder>` is run, and `h11` must sit beside that folder. The command is built from `layout.repair_line(host)`, so no invented flags.

Tests: tests/test_doctor_6b_rows.py updated, plus a new test that parses the remedy out of the hint and feeds it to the real `grammar.parse_install_argv` (verb must be in `grammar.VERBS`, `--source` accepted). The quarantine-row test's "no repair in hint" assertion now excludes the log words. Ran: all tests/test_doctor_*.py, test_slot_reaper_log_bound, test_slot_service_dispatch, test_upgrade_cmd, test_install: 838 passed. CHANGELOG [Unreleased] Fixed line added. Not pushed.
