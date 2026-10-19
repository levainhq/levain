"""Generate frozen/namespaces.json (F11, the SSHSIG namespace registry). stdlib only.

The registry is written here by hand from S1_DESIGN F11 (shipped namespaces read from the levain code at 31dea1f;
reserved ones from the designs F11 names), never read from ``levain.contracts.namespaces``: the test compares the two.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "namespaces.json"
DOC = {
    "item": "F11",
    "generator": "namespaces_gen.py",
    "authority": {"kind": "spec", "source": "S1_DESIGN F11",
                  "clause": "one namespace, one meaning; reservations are exact strings, never prefixes"},
    "rule": "every string constant after \"-n\" in an ssh-keygen -Y argument list, every namespace= string argument "
            "and every module-level *NAMESPACE* string assignment in the levain package is an exact key below",
    "namespaces": [
        {"namespace": "git", "status": "shipped", "owner": "levain/team/signing.py"},
        {"namespace": "levain-authority", "status": "shipped", "owner": "levain/authority.py"},
        {"namespace": "levain-authority-reset", "status": "shipped", "owner": "levain/authority.py"},
        {"namespace": "levain-confirm", "status": "shipped", "owner": "levain/autonomic/confirm.py"},
        {"namespace": "levain-cockpit", "status": "shipped", "owner": "levain/autonomic/confirm.py"},
        {"namespace": "levain-team-entry", "status": "reserved", "owner": "S1 F9 / F12"},
        {"namespace": "levain-manifest", "status": "reserved", "owner": "fleet design §11o item 1"},
        {"namespace": "levain-consent", "status": "reserved", "owner": "fleet design §11o item 1"},
        {"namespace": "levain-consent-answer", "status": "shipped", "owner": "levain/consent/record.py"},
        {"namespace": "levain-consent-ruling", "status": "shipped", "owner": "levain/consent/record.py"},
        {"namespace": "levain-consent-away", "status": "shipped", "owner": "levain/consent/record.py"},
        {"namespace": "levain-dispatch-contract", "status": "reserved", "owner": "levain/dispatch/sign.py"},
        {"namespace": "levain-allowed-signers", "status": "reserved", "owner": "levain/qbroker/signers.py"},
    ],
    "families": [{"prefix": "levain-team-", "taken": ["entry"],
                  "rule": "P1's envelope kinds; no envelope kind is named 'entry'; a member is registered as its "
                          "own exact string, never by the prefix"}],
    "not_registered_controls": ["levain-team-foo", "levain-authority-x", "levain", "levain-team-", "Git", "git "],
}
OUT.write_text(json.dumps(DOC, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"{OUT}: {len(DOC['namespaces'])} namespaces")
