"""PRE-W: the brokered launcher's doctor row (design 1105-49 §4): per-run share vs the root ceiling, and no brokered
route for a model. Green, amber (cannot tell), red (a standing mint refusal seen before the first fire). OFF, the
default, shows no row and never changes the exit code."""
from __future__ import annotations

import pytest

from levain import doctor as D

ON, OFF = ("brokered", None), ("passthrough", None)
LINK = ("al-" + "1" * 12, 2_000_000, "z-ai/glm-5.2")


def rows(setting=ON, links=(LINK,), ceiling=5_000_000, serving=lambda m: "glm"):
    return D.brokered_launcher_rows(setting, lambda: list(links),
                                    (lambda: ceiling) if not callable(ceiling) else ceiling, serving)


def state(r):
    return "red" if not r.ok else "amber" if r.warn else "neutral" if r.neutral else "green"


def test_off_is_no_row():
    assert rows(setting=OFF) == []


def test_a_refused_setting_is_amber_never_on():
    (r,) = rows(setting=("passthrough", "the setting is not root's alone"))
    assert state(r) == "amber" and "off: the setting is not root's alone" in r.detail


def test_green_share_within_the_ceiling_and_a_route_serves_the_model():
    (r,) = rows()
    assert state(r) == "green"
    assert "per-run $2.00 within the ceiling $5.00" in r.detail and "route glm serves z-ai/glm-5.2" in r.detail


def test_red_share_over_the_ceiling():
    (r,) = rows(ceiling=1_000_000)
    assert state(r) == "red" and "over the ceiling $1.00" in r.detail and r.hint


def test_red_no_brokered_route_for_the_model():
    (r,) = rows(serving=lambda m: None)
    assert state(r) == "red" and "no brokered route serves model z-ai/glm-5.2" in r.detail


def test_equal_to_the_ceiling_is_green():
    assert state(rows(ceiling=2_000_000)[0]) == "green"


@pytest.mark.parametrize("kw, words", [
    ({"ceiling": lambda: (_ for _ in ()).throw(OSError("denied"))}, "dispatch ceiling cannot be read"),
    ({"ceiling": None}, "dispatch ceiling cannot be read"),
    ({"links": None}, "links cannot be read"),
    ({"serving": lambda m: (_ for _ in ()).throw(PermissionError())}, "cannot be told"),
    ({"links": (("al-" + "2" * 12, 2_000_000, None),)}, "judged at the first fire"),
])
def test_amber_when_a_source_cannot_be_read(kw, words):
    if kw.get("links", ()) is None:
        out = D.brokered_launcher_rows(ON, lambda: (_ for _ in ()).throw(RuntimeError("x")), lambda: 1, None)
    else:
        if "ceiling" in kw and kw["ceiling"] is None:
            kw = {**kw, "ceiling": lambda: None}
        out = rows(**kw)
    (r,) = out
    assert state(r) == "amber" and words in r.detail


def test_on_with_no_api_key_link_is_one_green_line():
    (r,) = rows(links=())
    assert state(r) == "green" and "no standing api_key agent link" in r.detail


def test_each_link_gets_its_own_row():
    two = (LINK, ("al-" + "3" * 12, 9_000_000, "z-ai/glm-5.2"))
    out = rows(links=two)
    assert [state(r) for r in out] == ["green", "red"] and LINK[0] in out[0].name


def test_the_row_is_wired_into_the_doctor_run():
    import inspect
    assert "_check_brokered_launcher(install)" in inspect.getsource(D._run_doctor)
