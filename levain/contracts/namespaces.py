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
# The operator's passkey clear of a "yours to clear" desk review item (c4-mut head ruling R2).
REVIEW_CLEAR_NAMESPACE = "levain-review-clear"
# The operator's phone passkey countersigning a band answer typed at a REMOTE terminal (board row 64, B2): evidence
# beside an answer already recorded unsigned, never a gate on it.
ANSWER_COUNTERSIGN_NAMESPACE = "levain-answer-countersign"
# The operator's phone passkey APPROVING a dispatch configuration item (board row 113, option F2): a gate before the
# laptop key signs, never the configuration signature itself (that stays in ``levain-dispatch-config``).
CONFIG_APPROVE_NAMESPACE = "levain-dispatch-config-approve"
# The operator's passkey allowing ONE unlisted host for ONE egress-slot session (board levain#66, design
# ASK2_HOST_GRANTS_DESIGN.md): a levain-owned choice request (this session / 24 hours within the session) that root
# verifies against its own allowed-signers file. Never an authority-log link.
EGRESS_HOST_NAMESPACE = "levain-egress-host"

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
    "levain-thread-envelope": ("reserved", "levain/desk/envelope.py",
                               "the operator's signature over a desk thread envelope's digest (C0 §4.2)"),
    "levain-allowed-signers": ("reserved", "levain/qbroker/signers.py",
                               "the dispatch trust root's signature over the answer-signer snapshot "
                               "(CONTRACT c6 §3.7)"),
    REVIEW_CLEAR_NAMESPACE: ("shipped", "levain/desk/review.py",
                             "the operator's passkey clear of a 'yours to clear' desk review item, over the "
                             "item's id, state and last event"),
    ANSWER_COUNTERSIGN_NAMESPACE: ("shipped", "levain/consent/answer_countersign.py",
                                   "the operator's phone passkey countersigning a band answer typed at a remote "
                                   "terminal (message format levain-terminal-answer/1 with a countersign-of line)"),
    CONFIG_APPROVE_NAMESPACE: ("shipped", "levain/dispatch/phone_config.py",
                               "the operator's phone passkey approving one dispatch configuration item (by the "
                               "SHA-256 of its exact bytes) before the laptop trust-root key signs it; never a "
                               "configuration signature"),
    EGRESS_HOST_NAMESPACE: ("shipped", "levain/slot/egress_grants.py",
                            "the operator's passkey allowing one unlisted host (port 443) to one egress-slot session "
                            "for this session or 24 hours within it (message format levain-egress-host/1; a choice "
                            "request, key s or d)"),
    "levain-envelope-revoke": ("shipped", "levain/desk/envelope_revoke.py",
                               "the operator's phone passkey ending one desk envelope, over a message naming the "
                               "store, the envelope, the time and a nonce (levain-envelope-revoke/1); only narrows"),
    "levain-terminal-answer": ("shipped", "levain/consent/terminal_sign.py",
                               "the laptop Touch ID key's signature over a band answer typed at the terminal "
                               "(TERMSIGN; message format levain-terminal-answer/1)"),
    "levain-desk-answer": ("shipped", "levain/consent/terminal_sign.py",
                           "the laptop Touch ID key's signature over a click in the cockpit served at the laptop "
                           "(TERMSIGN L5; message format levain-desk-answer/1)"),
}

# The levain namespaces levain's OWN sign requests are filed in (levain.consent.sign serves them on every Signer,
# whatever namespaces its caller gave it): namespace -> the one message grammar levain files there. A caller still
# never names one (``levain.consent.sign.namespace_problem`` refuses every name beginning ``levain``), and a request
# file in one whose message is not exactly this grammar is refused, so a phone signature in it can only ever say
# what levain itself asks.
LEVAIN_SIGN_REQUESTS: dict[str, str] = {
    REVIEW_CLEAR_NAMESPACE: r"levain review clear rv-[0-9a-f]{32} open \S+",
    # multi-line (re.fullmatch's "." stops at a newline): terminal_sign.build_message's format line and store line,
    # its seven header lines (install .. at), then countersign-of, then the entry lines. A shape fence only; the
    # verifier re-parses.
    ANSWER_COUNTERSIGN_NAMESPACE: r"levain-terminal-answer/1\nstore: levain-questions:[^\n]+\n(?:.+\n){7}"
                                  r"countersign-of: .+\n(?:.+\n)+",
    # row 113: the format line and nine fixed lines; the item's bytes are bound by item-sha256, the approval's
    # deadline by expires (signed, so editing the request file cannot extend it) and the passkey site that may
    # approve by rp (the queue cockpit's host at filing; the signer's key must be of it); change and summary are
    # levain's words. A shape fence only: kind is the kept item's own (registry.KINDS is its one definition, compared
    # at use)
    CONFIG_APPROVE_NAMESPACE: r"levain-dispatch-config-approve/1\nstore: [^\n]+\nkind: [^\n]+\ngeneration: [0-9]+\n"
                              r"prev: [^\n]+\nitem-sha256: [0-9a-f]{64}\n"
                              r"expires: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\n"
                              r"rp: [a-z0-9.-]+\nchange: [^\n]+\nsummary: [^\n]+\n",
    # levain#66: one host grant, every field its own shape (no free text): the install store, a levain-minted
    # single-use grant id, the slot session and slot index, the A-label host and port 443, three stamps (issued and
    # the two expiries the choice picks between), the passkey site and the writable flag. A shape fence; the
    # parser (levain.slot.egress_grants) re-validates every field, the bounds and the host grammar.
    EGRESS_HOST_NAMESPACE: r"levain-egress-host/1\nstore: [A-Za-z0-9-]{8,64}\ngrant: eh-[0-9a-f]{24}\n"
                           r"session: s-[0-9a-f]{16}\nslot: (?:0|[1-9][0-9]?)\nhost: [a-z0-9][a-z0-9.-]{2,252}\n"
                           r"port: 443\n"
                           r"issued: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\n"
                           r"expires-session: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\n"
                           r"expires-day: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\n"
                           r"rp: [a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\nwritable: (?:yes|no)\n",
}

# The levain namespaces whose own request is a CHOICE request: namespace -> its exact options, (key, text) pairs. A
# request file in one is refused unless its options are exactly these (so a key signed for one text can never be
# read as the other) and it offers no write-in. The signed bytes are the message, a newline and the choice line, so a
# signature for key ``s`` covers a different byte string than one for ``d``.
#
# The host-grant words are the button the operator taps AND the line his passkey signs (board levain#66, plan
# hgs4_plan_v2 section 7). They carry the risk because root attests no card author and no reason: the signed host and
# the signed words are all it holds him to. ``s`` says "at most 8 hours" because the one-tap session grant ends at the
# earlier of the session and ``egress_grants.SESSION_TTL_MAX_S``. Root reads this same table from its own installed copy
# (``egress_grants.option_text``), so a changed word is a skew between a root not yet repaired and the operator's
# levain: it refuses the choice after the tap, and the sequence is: change the words before the chain ships, run the
# install helper's repair and restart ``levain serve`` after an upgrade, and after a release change them only with a
# message-format bump (``levain-egress-host/2``). Phill's ruling on these exact words is pending (band row np-386737ea,
# options a/b/c): the working value is option b, and the words are the ONLY thing that ruling may change.
LEVAIN_SIGN_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    EGRESS_HOST_NAMESPACE: (
        ("s", "Allow this host for this session (at most 8 hours). The agent can then send anything it can read to "
              "any account on this host; Levain cannot see or limit what is sent."),
        ("d", "Allow this host for 24 hours or until this session ends. The agent can then send anything it can read "
              "to any account on this host; Levain cannot see or limit what is sent."),
    ),
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
