"""Q2 (b), signed 11-11: rollback stays TEXT RESTORE ONLY. Both cards a person reads must say so: that day's episodes stay
counted as consolidated and the next dream does not offer them again. No behaviour is tested here that is not already
built; these two sentences are the contract the signed answer made, pinned so a rewording cannot drop it quietly.
"""
from __future__ import annotations

from levain.desk import review
from levain.desk.store import CoordStore
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import snapshot as sn
from tests.test_dream_agent import ZONE
from tests.test_dream_ready import Entity, ent  # noqa: F401
from tests.test_dream_snapshot import two_dreams

#: the signing card's sentence (what he signs under), whitespace-normalised
SIGNING = "that day's episodes stay counted as consolidated and are not offered again"
#: the rolled-back card's sentence (what he reads after an undo): the same fact, the card's own words
ROLLED_BACK = "The day's episodes are NOT re-offered: anneal still counts them as consolidated"


def test_the_signing_card_says_the_undone_days_episodes_stay_counted_and_are_not_offered_again(ent):  # noqa: F811
    text = " ".join(da.signing_card_text(ZONE, per_run_usd=1.0, night_usd=2.0, expires="2026-12-01",
                                         store_dir=ent.store_dir).split())
    assert SIGNING in text


def test_the_rolled_back_card_says_the_undone_days_episodes_are_not_re_offered(ent):  # noqa: F811
    two_dreams(ent.db)
    occ_dir = ent.db.parent / "dream" / "o2"
    res = sn.rollback(ent.db, occ_dir)
    assert res.ok and res.card, res
    store = CoordStore(ent.coord_dir)
    cd.file_card(store, cd.from_spec(res.card, occ_dir=occ_dir, store_dir=ent.store_dir), feed="agent")
    cards = [i for i in review.list_items(store, feed="agent") if i.body.get("kind") == "rolled-back"]
    assert len(cards) == 1 and ROLLED_BACK in cards[0].body["reason"]
