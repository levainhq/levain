"""CH-D2 S3b: the upload-pack read shape, the advertisement predicate and the per-launch advertisement set
(design §3.8.5(o) item 6, rev 15.11). Units: `advert` alone, and composed with S3a's `classify.decide` through its
`read_shaped=` parameter (B11's order is classify's; R-8's predicate is this module's).

Rows: R9-A2, R15-10-A12, R15-12-B11, R15-15-Q1, R15-17-1, R15-18-Q1, R15-19-3, R15-21-5, R15-23-1, R15-24-3, R15-26-1.
The real-clone reds (protocol v0 and v2, the 30,000-branch repo, github.com depth 1) are `test_advert_clone.py`.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from functools import partial

import pytest

from levain.chat_egress import advert, parse
from levain.chat_egress.advert import AdvertisementSet
from levain.chat_egress.classify import GATED, OPEN_NETWORK, decide

AUDIT = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"
ADV_CT = "application/x-git-upload-pack-advertisement"
REQ_CT = "application/x-git-upload-pack-request"
RES_CT = "application/x-git-upload-pack-result"
GIT_HEADERS = {"content-type": REQ_CT, "accept": RES_CT, "git-protocol": "version=2", "content-length": "182"}
HOST = "git.example"


@dataclass(frozen=True)
class FakeRoute:
    """The plan's fixed `RouteSnapshot` shape (S6 owns the definition)."""
    record_id: str
    canonical_host: str
    credential_source: str
    declared_repos: frozenset
    attach: bool


def route(host=HOST, declared=("/o/declared.git",)):
    return FakeRoute(record_id="rec-1", canonical_host=host, credential_source="keychain:x",
                     declared_repos=frozenset(declared), attach=True)


_ID = iter(range(1, 10**9))


def frame(method="GET", host=HOST, port=443, path="/", query=None, headers=None, sni="default"):
    hdrs = {k: list(v) if isinstance(v, (list, tuple)) else [v] for k, v in (headers or {}).items()}
    hdrs.setdefault("x-levain-audit", [AUDIT])
    f = {"t": "req", "id": next(_ID), "method": method, "host": host, "port": port, "path": path, "headers": hdrs}
    if sni == "default":
        sni = host if port == 443 else None
    if sni:
        f["sni"] = sni
    if query is not None:
        f["query"] = query
    return f


def discovery(prefix="/o/r.git", **kw):
    kw.setdefault("query", "service=git-upload-pack")
    return parse.parse(frame("GET", path=prefix + "/info/refs", **kw))


def post(prefix="/o/r.git", headers=None, drop=(), **kw):
    h = dict(GIT_HEADERS)
    h.update(headers or {})
    for d in drop:
        h.pop(d, None)
    return parse.parse(frame("POST", path=prefix + "/git-upload-pack", headers=h, **kw))


def advertised(adverts, prefix="/o/r.git", status=200, ct=ADV_CT, **kw):
    return adverts.observe(discovery(prefix, **kw), status, ct)


def verdict(p, adverts, *, keyed=False, mode=GATED, body_cut=False, rt=None):
    r = (rt or route(p.canonical_host)) if keyed else None
    rs = partial(advert.read_shaped, adverts=adverts, body_cut=body_cut)
    v = decide(p, r, mode, read_shaped=rs)
    return v.kind, v.why


SILENT = ("silent", None)
CARD = ("card", "body")   # an upload-pack POST carries a body: R-7's first clause on its card


@pytest.fixture
def adverts():
    return AdvertisementSet()


# --- the constants (R15-19-3, R15-10-A12, item 6's table) ---------------------------------------------------------

def test_r15_19_3_the_cap_is_two_mib_decoded():
    assert advert.UPLOAD_PACK_BODY_CAP == 2_097_152


def test_item6_the_read_shaped_list_is_one_row():
    assert advert.READ_SHAPED_VERSION == 1
    (row,) = advert.READ_SHAPED
    assert (row.method, row.path_suffix, row.content_type, row.accept, row.advertised, row.body_cap) == (
        "POST", "/git-upload-pack", REQ_CT, RES_CT, ADV_CT, 2_097_152)
    assert advert.ADVERTISEMENT_SET_CAP == 256


def test_r15_15_q1_the_predicate_has_no_body_bytes_to_parse():
    """The body is never parsed: the predicate's only body input is whether the spool ran past the cap."""
    params = set(inspect.signature(advert.read_shaped).parameters)
    assert params == {"parsed", "route", "adverts", "body_cut"}


# --- R15-17-1 / R15-18-Q1: no advertisement ---------------------------------------------------------------------

@pytest.mark.parametrize("keyed", [False, True])
def test_r15_17_1_post_with_no_advertisement_is_a_card_keyed_or_not(adverts, keyed):
    p = post("/anything")
    assert verdict(p, adverts, keyed=keyed) == CARD
    assert advert.upload_pack_disposition(p, adverts=adverts, body_cut=False) == "no_advertisement"


def test_r15_18_q1_open_network_unkeyed_unadvertised_post_is_forwarded(adverts):
    assert verdict(post("/anything"), adverts, mode=OPEN_NETWORK) == SILENT


def test_r15_18_q1_open_network_keyed_unadvertised_post_is_a_card(adverts):
    assert verdict(post("/anything"), adverts, keyed=True, mode=OPEN_NETWORK) == CARD


def test_r15_18_q1_open_network_keyed_credential_query_is_a_wall(adverts):
    advertised(adverts, "/anything")
    p = post("/anything", query="access_token=x")
    assert verdict(p, adverts, keyed=True, mode=OPEN_NETWORK) == ("wall", "credential_in_query")


def test_r15_18_q1_plain_http_unadvertised_post_is_a_card(adverts):
    assert verdict(post("/anything", port=80), adverts) == CARD


# --- R15-17-1: after an advertisement -------------------------------------------------------------------------------

def test_r15_17_1_advertised_post_on_a_declared_repo_is_silent_with_the_key(adverts):
    rt = route(declared=("/o/declared.git",))
    assert advertised(adverts, "/o/declared.git") == advert.CT_ADVERTISEMENT
    p = post("/o/declared.git")
    v = decide(p, rt, GATED, read_shaped=partial(advert.read_shaped, adverts=adverts, body_cut=False))
    assert (v.kind, v.why, v.reattached) == ("silent", None, HOST)
    assert advert.key_allowed(p, rt) and advert.key_allowed(discovery("/o/declared.git"), rt)


def test_r15_17_1_undeclared_repo_on_a_keyed_host_is_silent_and_keyless(adverts):
    rt = route(declared=("/o/declared.git",))
    advertised(adverts, "/o/undeclared.git")
    p = post("/o/undeclared.git")
    assert verdict(p, adverts, keyed=True, rt=rt) == SILENT
    assert not advert.key_allowed(p, rt)
    assert not advert.key_allowed(discovery("/o/undeclared.git"), rt), "its discovery GET carries no key either"


def test_r15_17_1_the_declared_repo_rule_narrows_git_exchanges_only():
    rt = route(declared=("/o/declared.git",))
    api = parse.parse(frame("GET", path="/api/v4/projects"))
    assert advert.key_allowed(api, rt), "a non-git request on a keyed host keeps the snapshot's attach rule"
    assert not advert.key_allowed(parse.parse(frame("GET", path="/o/other.git/info/refs")), rt), \
        "a dumb-protocol info/refs of an undeclared repo is a git exchange too: keyless (fail closed)"
    assert not advert.key_allowed(discovery("/o/other.git", query="service=git-upload-pack&service=x"), rt)


def test_r15_17_1_declared_repo_spellings():
    """A repo is matched by its prefix exactly as git spells it: `o/r` and `o/r.git` are different repos; the entry is
    compared in R-2's canonical form, with or without its leading slash."""
    rt = route(declared=("o/r.git", "/o/%72aw"))
    assert advert.key_allowed(post("/o/r.git"), rt)
    assert not advert.key_allowed(post("/o/r"), rt)
    assert advert.key_allowed(post("/o/raw"), rt)
    assert not advert.key_allowed(post("/O/r.git"), rt)
    assert advert.key_allowed(post("/o/r.git"), None), "no route, no key to narrow"


def test_r15_17_1_dumb_server_content_type_leaves_the_post_a_card(adverts):
    assert advertised(adverts, ct="text/plain") == advert.CT_OTHER
    assert verdict(post(), adverts) == CARD


def test_r15_17_1_an_advertisement_is_keyed_by_prefix_port_and_scheme(adverts):
    advertised(adverts, "/o/declared.git")
    assert verdict(post("/o/undeclared.git"), adverts) == CARD
    assert verdict(post("/o/declared.git", port=8443, sni=HOST), adverts) == CARD
    assert verdict(post("/o/declared.git", port=80), adverts) == CARD
    assert verdict(post("/o/declared.git", host="other.example"), adverts) == CARD
    assert verdict(post("/o/declared.git"), adverts) == SILENT


def test_r15_17_1_an_undrained_observation_is_no_advertisement(adverts):
    """The POST is classified with the set as the decider has drained it: an observation not yet read is absent."""
    p = post()
    assert verdict(p, adverts) == CARD
    advertised(adverts)
    assert verdict(p, adverts) == SILENT


# --- R9-A2 / R15-10-A12 / R15-15-Q1 / R15-19-3: the read shape and the cap ----------------------------------------

def test_r15_15_q1_body_of_arbitrary_bytes_under_the_cap_is_silent(adverts):
    advertised(adverts)
    assert verdict(post(headers={"content-length": "2097152"}), adverts, body_cut=False) == SILENT


def test_r15_19_3_a_body_over_the_cap_is_a_card(adverts):
    advertised(adverts)
    p = post(headers={"content-length": "2097153"})
    assert advert.upload_pack_disposition(p, adverts=adverts, body_cut=True) == "over_cap"
    assert verdict(p, adverts, body_cut=True) == CARD


def test_r15_17_2_an_unspooled_body_is_never_silent(adverts):
    """The cap is enforced before the allow: until the spool ran, the POST is not read-shaped; `needs_body` tells the
    decider to ask srt for the body (max = the cap) before it decides."""
    advertised(adverts)
    p = post()
    assert advert.upload_pack_disposition(p, adverts=adverts, body_cut=None) == "unspooled"
    assert advert.needs_body(p, adverts)
    assert verdict(p, adverts, body_cut=None) == CARD
    assert not advert.needs_body(post("/never-advertised"), adverts)
    assert not advert.needs_body(discovery(), adverts)


def test_r15_10_a12_accept_is_required(adverts):
    advertised(adverts)
    assert verdict(post(drop=("accept",)), adverts) == CARD
    assert verdict(post(headers={"accept": "*/*"}), adverts) == CARD
    assert verdict(post(headers={"accept": "application/x-git-upload-pack-resultx"}), adverts) == CARD
    assert verdict(post(headers={"accept": "text/html, application/x-git-upload-pack-result;q=1"}), adverts) == SILENT
    assert verdict(post(headers={"accept": "Application/X-Git-Upload-Pack-Result"}), adverts) == SILENT


def test_r15_15_q1_another_content_type_is_a_non_read(adverts):
    advertised(adverts)
    for ct in ("application/x-www-form-urlencoded", "application/x-git-receive-pack-request", "",
               "application/x-git-upload-pack-request-x"):
        assert verdict(post(headers={"content-type": ct}), adverts) == CARD, ct
    assert verdict(post(drop=("content-type",)), adverts) == CARD
    assert verdict(post(headers={"content-type": [REQ_CT, REQ_CT]}), adverts) == CARD, \
        "two Content-Type values: which one the origin reads is not ours to guess"
    assert verdict(post(headers={"content-type": "Application/X-Git-Upload-Pack-Request; x=1"}), adverts) == SILENT


def test_ascii_case_folding_only(adverts):
    """ASCII case-insensitive means ASCII: U+212A KELVIN SIGN lower-cases to `k` under str.lower()."""
    kelvin = "application/x-git-upload-pacK-advertisement"
    assert advert.content_type_class(kelvin) == advert.CT_OTHER
    assert advertised(adverts, ct=kelvin) == advert.CT_OTHER
    assert verdict(post(), adverts) == CARD
    advertised(adverts)
    assert verdict(post(headers={"content-type": "application/x-git-upload-pacK-request"}), adverts)[0] == "card"


def test_r9_a2_the_suffix_is_checked_after_r2_normalisation(adverts):
    advertised(adverts)
    assert verdict(parse.parse(frame("POST", path="/o/r.git/git-upload-%70ack", headers=GIT_HEADERS)), adverts) == SILENT
    for path in ("/o/r.git/git-upload-pack/", "/o/r.git/GIT-UPLOAD-PACK", "/o/r.git/git-upload-pack;x",
                 "/o/r.git/xgit-upload-pack"):
        assert verdict(parse.parse(frame("POST", path=path, headers=GIT_HEADERS)), adverts)[0] == "card", path


def test_override_on_an_upload_pack_post_is_never_read_shaped(adverts):
    """R-8 lifts R-7's clauses; an override says another method, so the predicate refuses it (a card, never silent)."""
    advertised(adverts)
    for name in parse.OVERRIDE_HEADERS:
        assert verdict(post(headers={name: "DELETE"}), adverts)[0] == "card", name
    assert verdict(post(query="_method=DELETE"), adverts)[0] == "card"


def test_only_post_is_read_shaped(adverts):
    advertised(adverts)
    for method in ("PUT", "PATCH", "DELETE"):
        p = parse.parse(frame(method, path="/o/r.git/git-upload-pack", headers=GIT_HEADERS))
        assert verdict(p, adverts)[0] == "card", method


@pytest.mark.parametrize("mode", [GATED, OPEN_NETWORK])
def test_r15_15_q1_receive_pack_walls_in_both_modes(adverts, mode):
    advertised(adverts)
    rp = parse.parse(frame("POST", path="/o/r.git/git-receive-pack",
                           headers={"content-type": "application/x-git-receive-pack-request", "accept": RES_CT,
                                    "content-length": "4"}))
    assert verdict(rp, adverts, mode=mode) == ("wall", "use_w2_push")
    assert verdict(discovery(query="service=git-receive-pack"), adverts, mode=mode) == ("wall", "use_w2_push")


# --- R15-12-B11: the classification order (classify's), with this predicate -------------------------------------

@pytest.mark.parametrize("keyed", [False, True])
def test_r15_12_b11_access_token_query_is_a_card_never_silent(adverts, keyed):
    advertised(adverts)
    assert verdict(post(query="access_token="), adverts, keyed=keyed) == ("card", "credential_in_query")


def test_r15_12_b11_keyed_exempt_only_query_is_a_card_gated_a_wall_open(adverts):
    advertised(adverts)
    p = post(query="X-Amz-Signature=x&X-Amz-Date=y")
    assert verdict(p, adverts, keyed=True) == ("card", "credential_in_query")
    assert verdict(p, adverts, keyed=True, mode=OPEN_NETWORK) == ("wall", "credential_in_query")


def test_r15_12_b11_unkeyed_exempt_only_query_is_a_card_pinned():
    """B11 (rev15.2) reads "a READ_SHAPED-matching request with an exempt-only query is read-shaped as before on an
    UNKEYED host"; B2 (as amended) reads "a non-read carrying one [an exempt name] is a card". Classify applies the
    exemptions to a READ BY SHAPE only, so the upload-pack POST is marked at step 3 and R-8 never lifts it. PINNED
    as the fail-closed reading (friction, never a reach); the collision is in S3b's handoff for the head."""
    adverts = AdvertisementSet()
    advertised(adverts)
    assert verdict(post(query="X-Amz-Signature=x"), adverts) == ("card", "credential_in_query")


def test_r15_12_b11_the_predicate_is_not_asked_about_a_marked_request(adverts):
    asked = []

    def spy(parsed, route):
        asked.append(parsed)
        return True

    decide(post(query="token=x"), None, GATED, read_shaped=spy)
    assert asked == []


def test_plain_query_on_an_upload_pack_post_stays_read_shaped(adverts):
    advertised(adverts)
    assert verdict(post(query="x=1"), adverts) == SILENT


# --- R15-21-5 / R15-23-1: the advertisement predicate, clause by clause --------------------------------------------

@pytest.mark.parametrize("status", [201, 204, 206, 301, 304, 401, 403, 404, 500])
def test_r15_21_5_a_non_200_discovery_reply_is_no_advertisement(adverts, status):
    advertised(adverts, status=status)
    assert verdict(post(), adverts) == CARD


@pytest.mark.parametrize("ct", ["application/X-Git-Upload-Pack-Advertisement", ADV_CT.upper(),
                                ADV_CT + "; charset=utf-8", ADV_CT + ";x=y", ADV_CT + " ; q=1", ADV_CT + "\t"])
def test_r15_21_5_media_type_case_insensitive_and_parameters_ignored(adverts, ct):
    assert advertised(adverts, ct=ct) == advert.CT_ADVERTISEMENT
    assert verdict(post(), adverts) == SILENT


@pytest.mark.parametrize("ct", [None, "", ";" + ADV_CT, ADV_CT + "x", "x" + ADV_CT, "application/x-git-upload-pack",
                                "application/x-git-receive-pack-advertisement", ADV_CT + "," + ADV_CT,
                                "text/plain; " + ADV_CT])
def test_r15_21_5_any_other_content_type_is_no_advertisement(adverts, ct):
    advertised(adverts, ct=ct)
    assert verdict(post(), adverts) == CARD


def test_r15_21_3_the_stored_observation_is_an_enum():
    assert advert.content_type_class(None) == "absent"
    assert advert.content_type_class(ADV_CT + "; a=SECRET") == "git-upload-pack-advertisement"
    assert advert.content_type_class("text/html; SECRET") == "other"
    assert advert.content_type_class("") == "other"


@pytest.mark.parametrize("query", ["service=git-upload-pack&service=git-upload-pack", "service=GIT-UPLOAD-PACK",
                                   "service=git-upload-pack%20", "service=", "service", "", "svc=git-upload-pack",
                                   "service=git-upload-pack&service=x"])
def test_r15_21_5_the_service_value_is_exactly_git_upload_pack(adverts, query):
    advertised(adverts, query=query)
    assert verdict(post(), adverts) == CARD


@pytest.mark.parametrize("query", ["Service=git-upload-pack", "SERVICE=git-upload-pack", "%73ervice=git-upload-pack",
                                   "service=git%2Dupload-pack", "service=git-upload-pack&x=1"])
def test_r15_21_5_the_service_value_is_read_through_r1s_folded_parse(adverts, query):
    advertised(adverts, query=query)
    assert verdict(post(), adverts) == SILENT


def test_r15_21_5_the_discovery_must_be_a_get_of_info_refs(adverts):
    for method in ("HEAD", "POST", "OPTIONS"):
        adverts.observe(parse.parse(frame(method, path="/o/r.git/info/refs", query="service=git-upload-pack")), 200,
                        ADV_CT)
    for path in ("/o/r.git/info/refs/", "/o/r.git/info/refsx", "/o/r.git/INFO/refs", "/o/r.git/info/refs;p"):
        adverts.observe(parse.parse(frame("GET", path=path, query="service=git-upload-pack")), 200, ADV_CT)
    assert len(adverts) == 0
    assert verdict(post(), adverts) == CARD


@pytest.mark.parametrize("adv,posted", [("/a", "/ab"), ("/a", "/a/b"), ("/ab", "/a"), ("/a/b", "/a"), ("", "/a"),
                                        ("/a", "")])
def test_r15_21_5_prefixes_compare_by_string_equality_never_startswith(adverts, adv, posted):
    advertised(adverts, adv)
    assert verdict(post(posted), adverts) == CARD
    assert verdict(post(adv), adverts) == SILENT


def test_r15_23_1_both_prefixes_compare_after_r2_normalisation(adverts):
    advertised(adverts, "/%61")                     # /a, spelled with an unreserved escape
    assert verdict(post("/a"), adverts) == SILENT
    assert verdict(post("/a%62"), adverts) == CARD, "the /a vs /ab red on normalised prefixes"
    advertised(adverts, "/o/r%2Fs.git")             # %2F stays an escape (a resource name), upper-cased
    assert verdict(post("/o/r%2fs.git"), adverts) == SILENT
    assert verdict(post("/o/r/s.git"), adverts) == CARD


def test_r15_23_1_a_walled_discovery_path_records_nothing(adverts):
    adverts.observe(parse.parse(frame("GET", path="/o/../r.git/info/refs", query="service=git-upload-pack")), 200,
                    ADV_CT)
    assert len(adverts) == 0


def test_r15_21_5_hosts_compare_by_canonical_host(adverts):
    advertised(adverts, host="git.example.")
    assert verdict(post(host="git.example"), adverts) == SILENT


# --- R15-24-3 / R15-26-1: the bounded set --------------------------------------------------------------------------

def test_r15_24_3_257_advertisements_evict_the_first(adverts):
    for i in range(257):
        advertised(adverts, f"/r{i}.git")
    assert len(adverts) == 256
    assert verdict(post("/r0.git"), adverts) == CARD
    assert verdict(post("/r256.git"), adverts) == SILENT
    assert verdict(post("/r1.git"), adverts) == SILENT


def test_r15_26_1_a_re_inserted_advertisement_refreshes_to_newest(adverts):
    for i in range(256):
        advertised(adverts, f"/r{i}.git")
    advertised(adverts, "/r0.git")
    advertised(adverts, "/new.git")
    assert verdict(post("/r0.git"), adverts) == SILENT
    assert verdict(post("/r1.git"), adverts) == CARD


def test_r15_26_1_a_failed_reply_does_not_refresh(adverts):
    for i in range(256):
        advertised(adverts, f"/r{i}.git")
    assert advertised(adverts, "/r0.git", ct="text/plain") == advert.CT_OTHER
    advertised(adverts, "/new.git")
    assert verdict(post("/r0.git"), adverts) == CARD
    assert verdict(post("/r1.git"), adverts) == SILENT


def test_r15_26_1_a_failed_reply_does_not_remove_a_live_entry(adverts):
    advertised(adverts)
    advertised(adverts, status=404)
    assert verdict(post(), adverts) == SILENT


def test_the_set_is_per_launch():
    a, b = AdvertisementSet(), AdvertisementSet()
    advertised(a)
    assert verdict(post(), b) == CARD


def test_the_set_bound_holds_under_concurrent_observers():
    s = AdvertisementSet()
    def work(k):
        for i in range(200):
            advertised(s, f"/t{k}-{i}.git")
    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(s) == 256


def test_the_set_capacity_is_not_a_launch_knob():
    with pytest.raises(ValueError):
        AdvertisementSet(capacity=0)
