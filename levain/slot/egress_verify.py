"""Root's host-grant VERIFIER (board levain#66 slice S2, plan hgs2_plan_v2 section 3): what a signed ``levain-egress-host/1``
message must satisfy before root publishes it. No filesystem writes.

  - :func:`host_class`: a host a route already names (any route type in the base map, or a vendor's own host) is never
    granted: that would let a tap override a brokered or passthrough route, or re-grant a listed host
    (``egress-grant-host-routed``).
  - :func:`check_signature`: the signer line is found BY FINGERPRINT in root's own ``L/policy/authority_signers``
    (the one reader, ``install_helper._read_signers``), must name ``levain-egress-host`` in its ``namespaces=`` (a line
    with no options, or any other option, is not enrolment for this: fail closed), and only then does
    :func:`levain.authority_sig.verify_link_sig` verify the signed bytes in that namespace. The enrolled line's
    principals field is returned (with its options and key by :func:`enrolled_key`, alone by :func:`check_signature`)
    and scopes nothing: any key enrolled for the namespace may sign for any session on the install. An unavailable verifier is
    ``egress-grant-verifier``, never a verdict.

Root's ``/usr/bin/python3`` (3.9) imports this module: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

from typing import Tuple

from levain.authority_sig import VerifierUnavailable, verify_link_sig
from levain.slot import Refused
from levain.slot import egress_grants as eg
from levain.slot import vendor_hosts
from levain.slot.root_tree import AUTHORITY_SIGNERS

ERR_ROUTED = "egress-grant-host-routed"
ERR_UNENROLLED = "egress-grant-unenrolled"
ERR_SIG = "egress-grant-sig"
ERR_VERIFIER = "egress-grant-verifier"


def host_class(ctx: object, host: str) -> None:
    """:class:`Refused` ``egress-grant-host-routed`` when ``host`` is in any base-map route's host list (brokered,
    passthrough or an operator's egress route) or is one of a vendor's own hosts (:data:`vendor_hosts.VENDOR_HOSTS`:
    the static backstop for a route that carries no ``hosts`` list)."""
    from levain.slot import runtime_helper as H

    _types, hosts = H.base_routes(ctx.store)  # type: ignore[attr-defined]
    if any(host in hs for hs in hosts.values()) or any(host in hs for hs in vendor_hosts.VENDOR_HOSTS.values()):
        raise Refused(ERR_ROUTED, "that host already has a route")


def enrolled_key(ctx: object, fingerprint: str) -> Tuple[str, str, str]:
    """``(principals, options, key)`` of the line of root's signers file whose fingerprint is ``fingerprint`` and whose
    options are exactly ``namespaces="..."`` naming :data:`levain.slot.egress_grants.NAMESPACE`; ``principals`` is the
    line's principals field verbatim and ``key`` is ``<type> <base64>``. Else
    ``egress-grant-unenrolled`` (an absent file included)."""
    from levain.slot import install_helper as IH

    raw = IH._read_signers(ctx.seams.L / "policy" / AUTHORITY_SIGNERS)  # type: ignore[attr-defined]
    for line in IH._signer_lines(raw):
        fields = IH._signer_fields(line)
        if fields is None:
            continue
        principals, options, key = fields
        namespaces = IH._line_namespaces(options)
        if namespaces is not None and eg.NAMESPACE in namespaces and IH.blob_fingerprint(key) == fingerprint:
            return principals, options, key
    raise Refused(ERR_UNENROLLED, "no enrolled key for that fingerprint in this namespace")


def check_signature(ctx: object, signed: bytes, signature: str, fingerprint: str) -> str:
    """Verify ``signature`` over EXACTLY ``signed`` (the bytes the caller parsed) in ``levain-egress-host`` by the key
    enrolled for ``fingerprint``; returns that enrolled line's principals field. The caller holds the install-wide
    verify lock."""
    principals, _options, key = enrolled_key(ctx, fingerprint)
    try:
        good = verify_link_sig(signed, signature, public_key=key, fingerprint=fingerprint, namespace=eg.NAMESPACE)
    except VerifierUnavailable:
        raise Refused(ERR_VERIFIER, "ssh-keygen could not run") from None
    if good is not True:
        raise Refused(ERR_SIG, "the signature does not verify")
    return principals


def refusal_of(e: eg.GrantRefused) -> Refused:
    """An S1 :class:`GrantRefused` as the helper's refusal: its fixed name (never a caller's bytes), its fixed text as
    the detail."""
    return Refused(e.name, str(e))


__all__ = ["ERR_ROUTED", "ERR_SIG", "ERR_UNENROLLED", "ERR_VERIFIER", "check_signature", "enrolled_key", "host_class",
           "refusal_of"]
