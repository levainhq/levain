"""F11: the SSHSIG namespace registry. One namespace, one meaning; reservations are exact strings, never prefixes.

``levain-authority-reset`` is not "under" ``levain-authority``: each entry is its own string. The family
``levain-team-<kind>`` is reserved for P1's envelope kinds; kind ``entry`` is taken by F9, so no envelope kind may
be named ``entry``. A member of that family is in use only once its exact string is in ``REGISTRY``.

``tests/test_contracts_namespaces.py`` walks the package's AST: every string constant after ``"-n"`` in an
``ssh-keygen -Y`` argument list, every ``namespace=`` string argument and every module-level ``*NAMESPACE*``
string assignment must be a key of ``REGISTRY``. A namespace computed at run time escapes that test (known edge);
this registry names the rule so review catches it.
"""
from __future__ import annotations

# F9's device line signature (F12 verifies it in-process).
TEAM_ENTRY_NAMESPACE = "levain-team-entry"
# The operator's passkey clear of a "yours to clear" coordinator review item (c4-mut head ruling R2).
REVIEW_CLEAR_NAMESPACE = "levain-review-clear"

# namespace -> (status, owner, meaning). status is "shipped" (a released levain uses it) or "reserved" (a design
# names it; nothing signs in it yet).
REGISTRY: dict[str, tuple[str, str, str]] = {
    "git": ("shipped", "levain/team/signing.py", "T's signed ledger commits (git's own SSH signing namespace)"),
    "levain-authority": ("shipped", "levain/authority.py", "levain-authority/1 links"),
    "levain-authority-reset": ("shipped", "levain/authority.py", "moving an authority anchor to a new head"),
    "levain-confirm": ("shipped", "levain/autonomic/confirm.py", "a confirm-gate challenge"),
    "levain-cockpit": ("shipped", "levain/autonomic/confirm.py", "a cockpit action signed by its operator"),
    "levain-cockpit-phone": ("shipped", "levain/autonomic/confirm.py",
                             "a cockpit pending signed by the operator's phone passkey (K8); a passkey's signer "
                             "line names only this namespace"),
    TEAM_ENTRY_NAMESPACE: ("reserved", "S1 F9 / F12",
                           "a device's signature over a team-log line (origin, file, chain hash)"),
    "levain-manifest": ("reserved", "fleet design §11o item 1", "a fleet manifest change"),
    "levain-consent": ("reserved", "fleet design §11o item 1", "a consent envelope"),
    "levain-consent-answer": ("shipped", "levain/consent/record.py",
                              "a consent-queue answer to an external agent's gate (Q1)"),
    "levain-consent-ruling": ("shipped", "levain/consent/record.py",
                              "a late ruling on a consent-queue request that went undecided (Q14)"),
    "levain-consent-away": ("shipped", "levain/consent/record.py",
                            "leaving an away mode set by `levain consent away on` (its away mark)"),
    "levain-dispatch-contract": ("reserved", "levain/dispatch/sign.py",
                                 "the operator's signature over a filed dispatch contract's digest (F2)"),
    "levain-dispatch-cancel": ("reserved", "levain/dispatch/sign.py",
                               "the operator's signed cancel of a dispatch job, before it is adopted (F2)"),
    "levain-dispatch-config": ("reserved", "levain/dispatch/control.py",
                               "a human-signed dispatch configuration item (templates, verifiers, provider policy, "
                               "route map, adoption rules) under a monotonic generation"),
    "levain-dispatch-authorise": ("reserved", "levain/dispatch/adopt.py",
                                  "the operator's signed authorisation to land one verified dispatch run on one "
                                  "branch (F11 (A))"),
    "levain-thread-envelope": ("reserved", "levain/coordinator/envelope.py",
                               "the operator's signature over a coordinator thread envelope's digest (C0 §4.2)"),
    "levain-allowed-signers": ("reserved", "levain/qbroker/signers.py",
                               "the dispatch trust root's signature over the answer-signer snapshot "
                               "(CONTRACT c6 §3.7)"),
    REVIEW_CLEAR_NAMESPACE: ("shipped", "levain/coordinator/review.py",
                             "the operator's passkey clear of a 'yours to clear' coordinator review item, over the "
                             "item's id, state and last event"),
}

# The levain namespaces levain's OWN sign requests are filed in (levain.consent.sign serves them on every Signer,
# whatever namespaces its caller gave it): namespace -> the one message grammar levain files there. A caller still
# never names one (``levain.consent.sign.namespace_problem`` refuses every name beginning ``levain``), and a request
# file in one whose message is not exactly this grammar is refused, so a phone signature in it can only ever say
# what levain itself asks.
LEVAIN_SIGN_REQUESTS: dict[str, str] = {
    REVIEW_CLEAR_NAMESPACE: r"levain review clear rv-[0-9a-f]{32} open \S+",
}

# Reserved families: prefix -> the rule. A family reserves the prefix against any other meaning; it registers
# no member. Each member enters REGISTRY as its own exact string when the session that freezes it lands.
FAMILIES: dict[str, str] = {
    "levain-team-": "P1's signed-envelope kinds, levain-team-<kind>; kind 'entry' is F9's, so no envelope kind "
                    "is named 'entry'",
}


def is_registered(namespace: object) -> bool:
    """True only for an exact registry string (never a prefix match)."""
    return isinstance(namespace, str) and namespace in REGISTRY
