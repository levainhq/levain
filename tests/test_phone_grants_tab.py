"""Grant-fixture helpers shared by the grant tests (``_grant``, ``_answered``).

The phone page's Grants tab (phone.js) and its node drive were deleted with the page (ONESURFACE OS-6); the helpers stay
because ``test_cockpit_grant_revoke``, ``test_cockpit_grant_slot``, ``test_m17_fix2_refused_count`` and
``test_m17_fix_server`` import them. No test lives here."""
from __future__ import annotations

from datetime import timedelta

from levain.consent import policy
from levain.consent.grants import create_grant
from levain.consent.ledger import Ledger
from levain.consent.queue import ConsentQueue, authority_source
from levain import authority as auth
from tests._h4 import h4_home
from tests.test_grant_phone import _now


def _grant(world, gid: str, tool: str) -> None:
    create_grant(world.root, agent="claude_code", tools=[tool], workspace=world.ws, postures=["default"],
                 expires=timedelta(days=1), signer_fp=world.lfp, sign=world.laptop_sign, clock=_now, grant_id=gid)


def _answered(world, tool: str, name: str, outcome: str) -> str:
    """One call a standing grant answered at the gate, then ran (``accepted``) or was refused after the allow."""
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    queue = ConsentQueue(world.qdir, authority=source, home=h4_home())
    tool_input = {"file_path": str(world.ws / name), "content": "x"}
    r = queue.file(agent="claude_code", agent_session_id="s1", tool_name=tool, tool_input=tool_input, cwd=str(world.ws),
                   hold=timedelta(minutes=5))
    d = policy.guarded_policy(r, queue, posture="default", tool_input=tool_input)
    assert d is not None and d.via == "grant", "the grant did not answer"
    ledger = Ledger(world.qdir)
    common = dict(agent="claude_code", agent_session_id="s1", tool_name=tool, tool_input=tool_input, cwd=str(world.ws),
                  tool_use_id="tu-" + name)
    ledger.record_call(**common, phase="pre", stamp="grant", request_id=r.id)
    if outcome == "refused":
        ledger.record_call(**common, phase="post_failure", stamp="unknown", outcome="refused")
    else:
        ledger.record_call(**common, phase="post", stamp="grant")
    return d.grant_id
