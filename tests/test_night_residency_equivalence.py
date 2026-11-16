"""ND-5 (board levain#15, DONE clause 2): a resident desk and a fresh desk given one event corpus produce the same
journaled effects. The desk is stubbed (a "desk" here is the :class:`~levain.desk.effects.Effects` it calls); what is
compared is everything the design says residency must not change (night desk design 0.2, 2.4): the effect journal's
rows, the refusals' rows, what ran and what was sent, and each call's answer.

RESIDENT = one ``Effects`` and one ``Journal`` object for the whole night. FRESH = a new ``Effects`` over a new
``Journal`` object on the SAME file for every call (a new process: nothing remembered but the file). Not covered
here, and not claimable from here: that a real ``--bg`` desk can be handed the turn at all (levain#15's live
measurement) and a model's choices (levain#18)."""
from __future__ import annotations

import shutil

import pytest

from levain.desk import effects as fx
from levain.desk.effect_attempts import Journal

pytest.importorskip("cryptography")
from tests.test_effect_verbs import ACK_LINE, EVR, EVT, Rig, D, signed, world  # noqa: E402,F401
from tests.test_standing_order_signing import LINE  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

# one night's corpus, in arrival order: (verb, event, arguments, order digest override)
CORPUS = [
    ("restart_service", EVR, {"host": "taoserv", "service": "apache2"}, None),
    ("restart_service", EVR, {"host": "taoserv", "service": "apache2"}, None),       # a redelivery of the same event
    ("restart_service", EVR, {"host": "taoserv", "service": "php8.3-fpm"}, None),    # a second effect in one incident
    ("restart_service", EVR, {"host": "taoserv", "service": "mysql"}, None),         # outside the signed enum
    ("restart_service", EVR, {"host": "taoserv", "service": "apache2"}, "0" * 64),   # a digest of another version
    ("send_ack", EVT, {}, None),
    ("send_ack", EVT, {}, None),                                                     # a redelivered mail event
]


def play(g: dict, rig: Rig, *, fresh: bool) -> list[tuple]:
    answers = []
    eff = rig.effects()
    for verb, event, args, sha in CORPUS:
        if fresh:
            eff = rig.effects(journal=Journal(rig.journal.path))
        digest = sha or D(g)
        try:
            if verb == "restart_service":
                got = eff.restart_service(g["id"], args["host"], args["service"], event_id=event, expect_sha256=digest)
            else:
                got = eff.send_ack(g["id"], event, expect_sha256=digest)
            answers.append(("answered", got.ok, got.text))
        except fx.EffectRefused as exc:
            answers.append(("refused", fx.refusal_text(exc)))
    return answers


def effects_of(rig: Rig) -> dict:
    """What is journaled and what happened, with the row ids and clock stamps left out (they are not effects)."""
    return {"attempts": [(a.verb, a.event_id, a.status, a.recipient, a.order_id) for a in rig.journal.rows()],
            "refusals": rig.journal.refusals(), "ran": rig.runs, "sent": list(rig.sent)}


def test_resident_and_fresh_desks_given_one_corpus_journal_the_same_effects(world):
    g = signed(world, LINE, ACK_LINE)
    resident, fresh = Rig(world), Rig(world)
    a = play(g, resident, fresh=False)
    b = play(g, fresh, fresh=True)
    assert a == b
    assert effects_of(resident) == effects_of(fresh)
    # and the corpus did something worth comparing: each effect ran once, a redelivery ran nothing, refusals were counted
    got = effects_of(resident)
    assert [r[0] for r in got["attempts"]].count("restart_service") >= 1 and got["ran"] and got["sent"]
    assert [r[-1] for r in got["ran"]] == ["apache2", "php8.3-fpm"] and len(got["sent"]) == 1, got   # the redelivery and the refused calls ran nothing
    assert got["refusals"], "the out-of-enum and wrong-version calls left refusal rows"
    assert [x[0] for x in a].count("refused") >= 2


def test_a_desk_that_dies_between_calls_and_a_fresh_one_agree_on_the_replayed_corpus(world):
    """The resurrection path: the corpus played twice in full, the second time by fresh desks over the first's file,
    adds nothing and runs nothing (every call is answered from the journal)."""
    g = signed(world, LINE, ACK_LINE)
    rig = Rig(world)
    first = play(g, rig, fresh=False)
    ran, sent, rows = list(rig.runs), list(rig.sent), effects_of(rig)["attempts"]
    again = play(g, rig, fresh=True)
    assert rig.runs == ran and rig.sent == sent and effects_of(rig)["attempts"] == rows
    assert [x for x in again if x[0] == "answered"] == [x for x in first if x[0] == "answered"]
