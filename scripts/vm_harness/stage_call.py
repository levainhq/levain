"""S14 W1 stager (lane s14, 11-18): the guest command behind ``wave3_e2e.py --stage-call``.

It stages, in the guest, with the PRODUCT's own helpers (it reimplements no crypto, no digest, no grammar):
  * the dispatch configuration store ``L/dispatch/control.db`` (``levain dispatch config init|sign``, run as _levain_dispatch):
    a provider_policy item, the verifier item, the R0 template (k = 2, two distinct models);
  * root's route trust pin (``levain.dispatch.cli.ROUTES_TRUST_HELPER``, run as the operator, as ``config init`` itself does);
  * the route map (kind routemap, ``levain-dispatch-routemap/3``, one brokered OpenRouter route per model) signed with
    ``levain.cockpit.keys.ssh_signer`` in the config namespace and sent to CONTROL's ``ROUTE-SUBMIT``; the verifier item
    read back out of the store and sent to ``CONFIG-SUBMIT`` (both as the _levain_dispatch peer, the only peer CONTROL answers).
It prints ``staged gen=<n>`` (n = the route map's generation) as its LAST line and exits 0, or prints ``stage-failed <step>: <why>``
and exits 1.

IDEMPOTENCE IS BY CONTENT. A config item is kept only when the store's active body of that kind is byte-for-byte (canonical) the
body this run built; otherwise a new generation is signed. The route map is skipped only when CONTROL's active map is the one
this stage last submitted (its record ``<dispatch-dir>/w3-staged.json``: generation, the map body's digest, the verifier item's
digest) AND CONTROL's own answer (each route's policy digest and backends) equals this body's; anything else is submitted again,
at a generation above both the store's top and CONTROL's active one. A changed input is never skipped; a same-input re-run
submits nothing.

MODES (all run as the guest operator ``admin``, which has passwordless sudo; none runs as root itself):
  prep      the signing key + the cockpit authority log the stage needs (see PREREQUISITES). Run once, before W1.
  repo      the target repo for wave3 --repo (one commit on main, world-readable, owned by the dispatch account). Run once.
  policy    sign a NEW provider_policy generation from the arguments (stage keeps an identical active one).
  stage     what W1 runs (``--stage-call "..."``).
  pool      read CONTROL's POOL as _levain_dispatch: ``egress_free=<n> none_free=<n>`` (the k = 2 slot check, before W3).
  selfcheck HOST ONLY, no VM, no sudo: builds every body and runs it through the same checkers CONTROL and dispatch run.

PREREQUISITES the stage cannot make for itself: the install is set up (CONTROL and keys up, the route trust directory
present); ``L/dispatch`` exists and is owned by _levain_dispatch 0700; ``prep`` has run (the signing key is enrolled as
laptop-presence in the authority log at --data-root, which must sit under the guest scratch tree /private/tmp/w3, and a copy of
its private file is owned by _levain_dispatch 0600 at --signing-key); the OpenRouter key is already installed as the keys
credential --cred (wave3's own W1 does that FIRST). Python 3.13 in the guest; the levain source at --levain-root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PY = "/usr/local/bin/python3.13"          # scripts.vm_harness.host_profile.DARWIN.python
DISPATCH_USER = "_levain_dispatch"       # DARWIN.dispatch_user
STORE = "/private/var/levain"            # DARWIN.store
ROOT = "/tmp/lvclean"                    # wave3_e2e --levain-root default
DEFAULT_MODELS = "z-ai/glm-5.2,moonshotai/kimi-k3"   # the c6/run_jobs test pair; NOT checked against OpenRouter's catalogue
NEVER = ["**/.env*", "**/*.pem", "**/*.key"]
#: The ONLY tree prep widens permissions in: the guest scratch directory the --data-root default (/private/tmp/w3/cockpit) sits
#: in. A data root anywhere else (a home, $TMPDIR, a relative path) is refused before anything is made or changed.
SCRATCH = Path("/private/tmp/w3")
#: explicit modes prep applies (never ``a+r``/``a+rx``, which keep a write bit already set): directories searchable by every
#: account and writable by no other; files exactly 0644
DIR_MODE = "a+x,go-w"
FILE_MODE = "0644"
#: the record of the route map this stage last submitted, in the dispatch account's own 0700 directory (see IDEMPOTENCE)
STAGED_RECORD = "w3-staged.json"
#: characters a --write-path may not hold: each is glob syntax in the provider policy's read pattern (or a home/escape form),
#: and the read pattern is built from the write path, so one would widen what the provider may read
WRITE_PATH_REFUSED = set("*?[]{}\\!~\x00")


def die(step: str, why: str, code: int = 1) -> "None":
    print(f"stage-failed {step}: {why}")
    sys.exit(code)


def run(argv, *, stdin=None, env=None, timeout=300):
    # cwd="/" : a command run as _levain_dispatch inherits admin's home as its cwd, which that account cannot read, and
    # pathlib's cwd() then raises PermissionError (measured in the 11-18 guest sitting: stage-failed config-init).
    p = subprocess.run(list(argv), input=None if stdin is None else stdin.encode(), capture_output=True, env=env, timeout=timeout, cwd="/")
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def json_in(text: str):
    """The last line-start ``{`` or ``[`` that decodes (wave3_e2e._json_in's rule)."""
    for m in reversed(list(re.finditer(r"(?m)^[\[{]", text))):
        try:
            return json.JSONDecoder().raw_decode(text[m.start():])[0]
        except ValueError:
            continue
    return None


def as_dispatch(a, argv):
    """Prefix for a command that must run as the dispatch account (wave3_e2e.dispatch_argv's form). --as-user '' = host test."""
    if a.as_user:
        return ["sudo", "-n", "-u", a.as_user, "/usr/bin/env", "PATH=/usr/bin:/bin", f"HOME={a.dispatch_dir}", f"PYTHONPATH={a.levain_root}", *argv]
    return ["/usr/bin/env", f"PYTHONPATH={a.levain_root}", *argv]


def as_dispatch_script(a, script_args):
    """``python -I -`` as the dispatch account with SCRIPT on stdin (the READ_TICKETS pattern)."""
    if a.as_user:
        return ["sudo", "-n", "-u", a.as_user, "/usr/bin/env", "PATH=/usr/bin:/bin", f"HOME={a.dispatch_dir}", a.python, "-I", "-", *script_args]
    return [a.python, "-I", "-", *script_args]


# --------------------------------------------------------------------------------------------------------------------
# the bodies (product helpers only)
# --------------------------------------------------------------------------------------------------------------------

def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower())[:40].strip("-")


def write_path_problem(write_path: str) -> "str | None":
    """Why --write-path cannot be the contract's write path and the root of the provider's read pattern, or None. Refused:
    empty, absolute, an empty / '.' / '..' component, and any glob or escape character (WRITE_PATH_REFUSED)."""
    if not write_path:
        return "--write-path is empty"
    if write_path.startswith("/"):
        return f"--write-path {write_path!r} is absolute; it is repo-relative"
    if any(p in ("", ".", "..") for p in write_path.split("/")):
        return f"--write-path {write_path!r} has an empty, '.' or '..' component"
    unprintable = sorted(c for c in set(write_path) if not c.isprintable())
    if unprintable:
        return f"--write-path {write_path!r} holds a character that is not printable ({unprintable[0]!r})"
    bad = sorted(WRITE_PATH_REFUSED & set(write_path))
    if bad:
        return f"--write-path {write_path!r} holds {''.join(bad)!r}: glob syntax would widen the provider's read allowlist"
    return None


def read_glob(write_path: str) -> str:
    """The provider-policy read pattern that covers the contract's own paths: the write path's top directory, recursive
    (``wave3/hello.txt`` -> ``wave3/**``; a one-component path is itself). A policy holds a write path to the read allowlist
    too (finding J). ``write_path`` has passed :func:`write_path_problem`."""
    top = write_path.split("/", 1)
    return top[0] + "/**" if len(top) > 1 else write_path


def route_ids(models) -> list:
    """``or-<slug>`` per model, refused by name when a model has no slug or two DISTINCT models collapse to one id (the slug
    folds case and punctuation and keeps 40 characters, so ``a/b.c`` and ``a/b-c`` would both be ``or-a-b-c``)."""
    ids = ["or-" + slug(m) for m in models]
    for m, rid in zip(models, ids):
        if rid == "or-":
            die("args", f"model {m!r} has no letter or digit to name its route by")
    seen: dict = {}
    for m, rid in zip(models, ids):
        if rid in seen:
            die("args", f"models {seen[rid]!r} and {m!r} both make the route id {rid}: name models that differ in their "
                        "first 40 letters and digits")
        seen[rid] = m
    return ids


def build_bodies(a) -> dict:
    """{provider_policy, verifier, templates, routemap}: each the BODY of one config item. Needs levain on sys.path."""
    from levain.contracts.canon import canonical_bytes
    from levain.dispatch import registry
    from levain.slot import vendor_routes

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    backends = [b.strip() for b in a.backends.split(",")] if a.backends else list(models)
    if len(models) != 2 or len(set(models)) != 2 or len(backends) != 2 or len(set(backends)) != 2:
        die("args", "--models (and --backends) name exactly two DISTINCT models: k = 2")
    ids = route_ids(models)
    problem = write_path_problem(a.write_path)
    if problem is not None:
        die("args", problem)
    sha =lambda v: "sha256:" + hashlib.sha256(canonical_bytes(v)).hexdigest()    # the registry's own digest form (registry._sha256)
    # verifier: the shipped output_compare driver, one step; it never runs in W3-W5 (W6/W7 are out of the signed scope)
    steps = [{"argv": a.verifier_argv.split(), "wall_s": 300}]
    driver_input = {"steps": [{"exit": 0}]}
    entry = {"verifier_id": a.verifier_id, "declared_kind": "black-box", "driver_template": "output_compare",
             "driver_template_digest": registry.driver_template_digest("output_compare"),
             "steps": steps, "steps_digest": sha(steps), "driver_input": driver_input, "driver_input_digest": sha(driver_input)}
    entry["verifier_artifact_digest"] = registry.verifier_artifact_digest(entry)
    verifier = {"format": registry.VERIFIER_FORMAT, "verifiers": [entry]}
    template = {"id": a.template, "tier": "R0", "executor": "claude", "models": models, "k": 2, "verifier_id": a.verifier_id,
                "verifier_hash": entry["verifier_artifact_digest"], "verifier_kind": "black-box", "write_paths": [a.write_path],
                "provider": "openrouter", "route": ids[0], "tool_versions": []}
    template["content_hash"] = registry.template_content_hash(template)
    # read must cover the contract's paths: with [] the filing is refused whole ("wave3/hello.txt is outside what provider
    # openrouter may read", measured in the 11-18 guest sitting, W3).
    policy = {"never": NEVER, "providers": {"openrouter": {"read": [read_glob(a.write_path)]}}}
    # route map /3: the anthropic route's own header grammar (vendor_routes.anthropic_route), pointed at OpenRouter's
    # Anthropic-compatible endpoint, one route per model, the credential the keys credential wave3 installs
    base = vendor_routes.anthropic_route()
    price = {"in": a.price_in, "out": a.price_out, "cache_write": a.price_in, "cache_read": a.price_in // 10, "default_output": 4096,
             "overhead_tokens": 64, "image_tokens": 1600, "source": a.price_source}
    routes = []
    for m, b, rid in zip(models, backends, ids):
        pol = {"route": rid, "type": "brokered", "upstream": {"scheme": "https", "host": "openrouter.ai", "base_path": "/api"},
               "requests": base["requests"], "credential": a.cred}
        routes.append({"policy": pol, "provider": "openrouter", "models": [{"requested": m, "backend": b, "price": price}],
                       "body_policy": {}, "retention_claim": "recorded, never a guarantee"})
    return {"provider_policy": policy, "verifier": verifier, "templates": [template],
            "routemap": {"format": "levain-dispatch-routemap/3", "routes": routes}}


# --------------------------------------------------------------------------------------------------------------------
# the dispatch-side script: reads the store, builds + signs the route map, submits (or, host test, checks)
# argv: levain-root dispatch-dir signing-key socket routemap-body-json dry(0|1) trust-pub-json
# --------------------------------------------------------------------------------------------------------------------

DISPATCH_SIDE = r'''import hashlib, json, os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
root, d, key, sock, body_json, dry, trust_json = sys.argv[1:8]
sys.path.insert(0, root)
RECORD = Path(d) / "w3-staged.json"
def fail(step, e):
    print("stage-failed %s: %s: %s" % (step, type(e).__name__, str(e)[:300])); sys.exit(1)
try:
    from levain.contracts import canon
    from levain.dispatch import control as dctl
    from levain.cockpit.keys import ssh_signer
    from levain.slot.routemaps import policy_digest
    con = sqlite3.connect((Path(d) / "control.db").as_uri() + "?mode=ro", uri=True)
    meta = dict(con.execute("SELECT key, value FROM meta"))
    rows = con.execute("SELECT generation, kind, item, signature FROM items ORDER BY generation").fetchall()
    vrows = [r for r in rows if r[1] == "verifier"]
    if not rows or not vrows:
        raise RuntimeError("the control store holds no verifier item (config sign has not run)")
    top, head = rows[-1][0], dctl.parse_item(rows[-1][2]).digest
    body = json.loads(body_json)
    vtext, vsig = vrows[-1][2], vrows[-1][3]
    # what this run would put in force, by content: the whole map body, each route's policy digest (CONTROL's own function)
    # and backends (the two ROUTEMAP-ACTIVE answers), and the verifier item's digest
    body_sha = "sha256:" + hashlib.sha256(canon.canonical_bytes(body)).hexdigest()
    want_digests = {r["policy"]["route"]: policy_digest(r["policy"]) for r in body["routes"]}
    want_backends = {r["policy"]["route"]: {m["requested"]: m["backend"] for m in r["models"]} for r in body["routes"]}
    vsha = dctl.parse_item(vtext).digest
    fp, sign = ssh_signer(Path(key), namespace=dctl.CONFIG_NAMESPACE)
    if fp != meta["trust_root"]:
        raise RuntimeError("--signing-key is %s, not the store's trust root %s" % (fp, meta["trust_root"]))
except Exception as e:
    fail("sign", e)
def signed(gen):
    raw = dctl.item_bytes("routemap", gen, body, datetime.now(timezone.utc), prev=head, store_id=meta["store_id"])
    return raw, sign(raw)
if dry == "1":
    try:
        from levain.slot import routemaps as RM
        raw, sig = signed(top + 1)
        t = json.loads(trust_json)
        trust = RM.Trust(t["public_key"], fp, meta["store_id"])
        c = RM.check_item(raw, sig, trust, frozenset())
        v = RM.check_verifier_item(vtext.encode("utf-8"), vsig, trust)
        print("dry-checked routemap gen=%d routes=%s verifier gen=%d ids=%s" % (c.generation, ",".join(sorted(c.routes)), v.generation, ",".join(sorted(v.entries))))
        print("staged gen=%d" % c.generation)
    except Exception as e:
        fail("dry-check", e)
    sys.exit(0)
try:
    from levain.slot.control_client import ControlClient
    with ControlClient(sock) as c:
        try:
            act = c.call("ROUTEMAP-ACTIVE")
        except Exception as e:
            if getattr(e, "name", None) != "no-routemap":
                raise
            act = None
        try:
            rec = json.loads(RECORD.read_text())
        except (OSError, ValueError):
            rec = None                     # no record (or an unreadable one) proves nothing: submit
        active_gen = act.get("generation") if isinstance(act, dict) else 0
        if not isinstance(active_gen, int) or isinstance(active_gen, bool):
            raise RuntimeError("ROUTEMAP-ACTIVE answered generation %r" % (active_gen,))
        # SKIP ONLY ON PROOF: CONTROL's active map is the one this stage last submitted (the record), no store item is newer,
        # and this run's map body, verifier item, route policy digests and backends are exactly what that map put in force
        same = (isinstance(act, dict) and isinstance(rec, dict) and rec.get("store_id") == meta["store_id"]
                and rec.get("generation") == active_gen and active_gen > top
                and rec.get("body_sha256") == body_sha and rec.get("verifier_sha256") == vsha
                and act.get("digests") == want_digests
                and {k: (v or {}).get("backends") for k, v in (act.get("routes") or {}).items()} == want_backends)
        if same:
            print("route-submit skipped: generation %s is active with this map and this verifier (by content)" % active_gen)
            print("staged gen=%s" % active_gen)
            sys.exit(0)
        # a changed (or unproven) map goes in force at a generation above the store's top AND CONTROL's active one
        # (ROUTE-SUBMIT refuses an equal generation with another digest; it allows gaps)
        gen = max(top, active_gen) + 1
        raw, sig = signed(gen)
        # the verifier item is the store's exact signed bytes: CONFIG-SUBMIT is a convergent repair when CONTROL holds them
        a1 = c.call("CONFIG-SUBMIT", item=canon.parse(vtext.encode("utf-8")), sig=vsig)
        print("config-submit verifier generation=%s" % a1.get("generation"))
        a2 = c.call("ROUTE-SUBMIT", map=canon.parse(raw), sig=sig)
        got = a2.get("generation") if isinstance(a2, dict) else None
        print("route-submit routemap generation=%s" % got)
        if got != gen:
            raise RuntimeError("ROUTE-SUBMIT answered generation %r for the map signed at %d" % (got, gen))
    try:
        tmp = RECORD.with_name(RECORD.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"store_id": meta["store_id"], "generation": gen, "body_sha256": body_sha, "verifier_sha256": vsha}, f, sort_keys=True)
        os.replace(tmp, RECORD)
    except OSError as e:
        print("record-failed %s: %s (the next stage submits again)" % (type(e).__name__, e))
    print("staged gen=%s" % gen)
except Exception as e:
    fail("submit", e)
'''

#: the store's ACTIVE body of each kind (its latest item), as ``{kind: {generation, sha256}}``: the sha256 of the body's canonical
#: bytes, the same form :func:`body_sha` gives a built body. Run as the dispatch account (the store is its 0700 directory).
STORE_SCRIPT = r'''import hashlib, json, sqlite3, sys
from pathlib import Path
root, d = sys.argv[1:3]
sys.path.insert(0, root)
try:
    from levain.contracts import canon
    from levain.dispatch import control as dctl
    con = sqlite3.connect((Path(d) / "control.db").as_uri() + "?mode=ro", uri=True)
    latest = {}
    for gen, kind, item in con.execute("SELECT generation, kind, item FROM items ORDER BY generation"):
        if kind in ("provider_policy", "verifier", "templates"):      # a legacy 'verifiers' item (0.11) is no longer read here
            latest[kind] = (gen, item)
    out = {k: {"generation": g, "sha256": "sha256:" + hashlib.sha256(canon.canonical_bytes(dctl.parse_item(t).body)).hexdigest()}
           for k, (g, t) in latest.items()}
    print(json.dumps(out, sort_keys=True))
except Exception as e:
    print("store-read-failed %s: %s" % (type(e).__name__, str(e)[:300])); sys.exit(1)
'''

POOL_SCRIPT = r'''import sys
root, sock = sys.argv[1:3]
sys.path.insert(0, root)
from levain.slot.control_client import ControlClient
try:
    with ControlClient(sock) as c:
        a = c.call("POOL")
    print("egress_free=%s none_free=%s" % (a.get("egress_free"), a.get("none_free")))
except Exception as e:
    print("pool-failed %s: %s" % (type(e).__name__, str(e)[:200])); sys.exit(1)
'''


# --------------------------------------------------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------------------------------------------------

def mode_stage(a) -> int:
    sys.path.insert(0, a.levain_root)
    from levain.cockpit.keys import fingerprint_of, signing_public_key
    try:
        pub = signing_public_key(Path(a.key_pub))
        fp = fingerprint_of(pub)
    except Exception as e:                                                   # noqa: BLE001
        die("key", f"{type(e).__name__}: {e}")
    bodies = build_bodies(a)
    if a.dry_submit:
        # HOST TEST ONLY: `levain dispatch config` refuses any process with a claude/codex ancestor (cli._refuse_an_agent), so the
        # store steps run through the same product API the CLI calls (ControlStore.init / .accept, item_bytes, ssh_signer)
        inproc_store(a, fp, bodies)
        return finish_stage(a, bodies, pub)
    cli = lambda *v: as_dispatch(a, [a.python, "-m", "levain", "dispatch", *v, "--dispatch-dir", a.dispatch_dir, "--data-root", a.data_root])
    # 1. the store, pinned to the key; root's route trust pinned by the operator when init could not (it ran as the dispatch account)
    rc, out, err = run(cli("config", "init", "--trust-root", fp, "--json"))
    init = json_in(out)
    if not isinstance(init, dict) or "store_id" not in init:
        die("config-init", f"rc={rc} {(out + err).strip()[-1600:]}")
    print(f"config-init store_id={init['store_id']} route_trust_pinned={init['route_trust'].get('pinned')}")
    if not init["route_trust"].get("pinned") and not a.no_pin:
        from levain.contracts.canon import canonical_bytes
        from levain.dispatch import cli as dcli
        req = canonical_bytes({"format": dcli.ROUTES_TRUST_REQUEST, "store_id": init["store_id"], "fingerprint": fp, "public_key": pub})
        prc, pout, perr = dcli.ROUTES_TRUST_HELPER(req)
        ans = json_in(pout)
        if not (isinstance(ans, dict) and ans.get("ok") is True):
            die("routes-trust", f"rc={prc} {(pout + perr).strip()[-300:]}")
        print("routes-trust pinned")
    # 2. what is already active, BY CONTENT: `config show` (the verified chain; a failed read is fatal, never "nothing active",
    # which would sign every kind again) names the active generation of each kind, and the store read gives each body's digest
    rc, out, err = run(cli("config", "show", "--json"))
    shown = json_in(out)
    if rc != 0 or not isinstance(shown, dict) or shown.get("configured") is not True or not isinstance(shown.get("kinds"), dict):
        die("config-show", f"rc={rc} {(out + err).strip()[-800:]}")
    stored = store_bodies(a)
    # 3. provider policy, the VERIFIER before templates (registry order), then templates; stdin carries each body. A kind is kept
    # only when its active body IS this body; any difference (another write path, models, verifier, policy) signs a new generation
    for kind in ("provider_policy", "verifier", "templates"):
        have, listed = stored.get(kind), shown["kinds"].get(kind)
        if (have is None) != (listed is None) or (have is not None and have.get("generation") != (listed or {}).get("generation")):
            die("store-read", f"{kind}: the store read {have} and config show {listed} disagree (a concurrent config sign?)")
        if have is not None and have.get("sha256") == body_sha(bodies[kind]):
            print(f"config-sign {kind}: generation={have['generation']} is active with this body (kept)")
            continue
        rc, out, err = run(cli("config", "sign", kind, "-", "--signing-key", a.signing_key, "--yes", "--json"),
                           stdin=json.dumps(bodies[kind]))
        got = json_in(out)
        if rc != 0 or not isinstance(got, dict) or "generation" not in got:
            die(f"config-sign-{kind}", f"rc={rc} {(out + err).strip()[-1600:]}")
        print(f"config-sign {kind}: generation={got['generation']}")
    return finish_stage(a, bodies, pub)


def body_sha(body) -> str:
    """``sha256:`` + the hex SHA-256 of a body's canonical bytes (what STORE_SCRIPT reports for a stored body)."""
    from levain.contracts.canon import canonical_bytes
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def store_bodies(a) -> dict:
    """``{kind: {generation, sha256}}`` of the store's active items, read as the dispatch account; a failed read is fatal."""
    rc, out, err = run(as_dispatch_script(a, [a.levain_root, a.dispatch_dir]), stdin=STORE_SCRIPT)
    got = json_in(out)
    if rc != 0 or not isinstance(got, dict) or not all(isinstance(v, dict) for v in got.values()):
        die("store-read", f"rc={rc} {(out + err).strip()[-800:]}")
    return got


def inproc_store(a, fp, bodies) -> None:
    from datetime import datetime, timezone
    from levain.authority import anchor_path
    from levain.cockpit.keys import ssh_signer
    from levain.consent.queue import authority_source
    from levain.dispatch import control as dctl
    root = Path(a.data_root)
    source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    dctl.ControlStore.init(a.dispatch_dir, fp, authority=source)
    store = dctl.ControlStore(a.dispatch_dir, authority=source)
    _fp, sign = ssh_signer(Path(a.signing_key), namespace=dctl.CONFIG_NAMESPACE)
    for kind in ("provider_policy", "verifier", "templates"):
        _items, top, head = store._read()
        raw = dctl.item_bytes(kind, top + 1, bodies[kind], datetime.now(timezone.utc), prev=head, store_id=store.store_id())
        got = store.accept(raw, sign(raw))
        print(f"inproc config-sign {kind}: generation={got.generation}")


def finish_stage(a, bodies, pub) -> int:
    # 4. the route map, signed here and never through `config sign` (dispatch's routemap kind, registry._routemap, takes another
    # body than CONTROL's /3, broker.policy.parse_routemap; no body satisfies both, so the store never holds a routemap item), sent
    # with the verifier to CONTROL; skipped only on proof by content (DISPATCH_SIDE)
    sock =f"{a.run_dir}/control/control.sock"
    rc, out, err = run(as_dispatch_script(a, [a.levain_root, a.dispatch_dir, a.signing_key, sock, json.dumps(bodies["routemap"]),
                                              "1" if a.dry_submit else "0", json.dumps({"public_key": pub})]), stdin=DISPATCH_SIDE)
    sys.stdout.write(out)
    if rc != 0 or not re.search(r"(?m)^staged gen=[1-9][0-9]*$", out):
        die("dispatch-side", f"rc={rc} {err.strip()[-300:]}")
    return 0


def mode_policy(a) -> int:
    """Sign a NEW provider_policy generation from build_bodies, unconditionally (stage signs one only when the active body
    differs). Run as admin."""
    sys.path.insert(0, a.levain_root)
    bodies = build_bodies(a)
    cli = lambda *v: as_dispatch(a, [a.python, "-m", "levain", "dispatch", *v, "--dispatch-dir", a.dispatch_dir, "--data-root", a.data_root])
    rc, out, err = run(cli("config", "sign", "provider_policy", "-", "--signing-key", a.signing_key, "--yes", "--json"),
                       stdin=json.dumps(bodies["provider_policy"]))
    got = json_in(out)
    if rc != 0 or not isinstance(got, dict) or "generation" not in got:
        die("policy", f"rc={rc} {(out + err).strip()[-1600:]}")
    print(f"policy generation={got['generation']}")
    return 0


def mode_pool(a) -> int:
    rc, out, err = run(as_dispatch_script(a, [a.levain_root, f"{a.run_dir}/control/control.sock"]), stdin=POOL_SCRIPT)
    sys.stdout.write(out)
    if rc:
        print(err.strip()[-300:])
    return rc


def init_cockpit(data_root: Path) -> None:
    """What ``levain serve --write --install-class reported`` does to a fresh data root, by the product's own class: the journal
    ``cockpit ledger init`` refuses to run without (levain.cockpit.keys._journal never creates one)."""
    from levain.cockpit.firejournal import CockpitDB
    CockpitDB(data_root).set_install_class("reported")


def _within(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def prep_paths(a) -> "tuple[Path, Path]":
    """(prep dir, data root), refused before anything is made: the data root is absolute, normal (no '.', '..', '//'), free of
    links, and inside SCRATCH (the only tree prep widens); the prep dir (the 0600 private key) is absolute and neither inside the
    data root nor containing it (the data root is opened to every account)."""
    raw_root, raw_home = a.data_root, a.prep_dir
    for flag, raw in (("--data-root", raw_root), ("--prep-dir", raw_home)):
        if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
            die("prep", f"{flag} {raw!r} is not an absolute, normal path")
    data_root, home = Path(raw_root), Path(raw_home)
    if not _within(data_root, SCRATCH):
        die("prep", f"--data-root {data_root} is outside {SCRATCH}: prep opens the data root to every account, and only inside "
                    "the guest scratch tree")
    if os.path.realpath(data_root) != str(data_root):
        die("prep", f"--data-root {data_root} passes through a symbolic link ({os.path.realpath(data_root)})")
    if _within(home, data_root) or _within(data_root, home):
        die("prep", f"--prep-dir {home} and --data-root {data_root} nest: the private key would sit in the opened tree")
    return home, data_root


def open_data_root(data_root: Path, made: list) -> None:
    """Let the dispatch account replay the authority log (config init/sign --data-root), and no more. sqlite needs a writable
    DIRECTORY even to read a WAL database (it makes ``-shm``; "attempt to write a readonly database", finding D), so the data root
    itself is 1777: every account may add a file, and the sticky bit keeps any of them from replacing or removing one it does not
    own (the authority log among them). Its subdirectories are made searchable (a+x, go-w); the authority log and the anchor are
    set to exactly 0644 (readable, NEVER writable by another account); no other file is touched (no private file is widened). Of the ancestors, only those prep
    itself made (``made``, all inside SCRATCH) are made searchable; a pre-existing ancestor that is not searchable is refused,
    never widened. Every chmod's result is checked: a failed one fails prep before the key is installed."""
    from levain.authority import anchor_path
    argvs = []
    for parent in sorted(made, key=lambda p: len(p.parts)):
        if parent != data_root:
            argvs.append(["/bin/chmod", DIR_MODE, str(parent)])
    for parent in data_root.parents:
        if not _within(parent, SCRATCH):
            break
        if parent not in made and (parent.stat().st_mode & 0o005) != 0o005:
            die("prep", f"{parent} is not searchable by other accounts and prep did not make it: prep widens no directory it "
                        "did not make (fix it by hand, or pick a fresh --data-root)")
    argvs.append(["/bin/chmod", "1777", str(data_root)])
    for dirpath, dirnames, filenames in os.walk(data_root):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                die("prep", f"{os.path.join(dirpath, name)} is a symbolic link: prep changes no mode through a link")
        if dirpath != str(data_root):
            argvs.append(["/bin/chmod", DIR_MODE, dirpath])
    for f in (data_root / "cockpit.db", anchor_path(data_root)):
        if f.is_file():
            argvs.append(["/bin/chmod", FILE_MODE, str(f)])
    for argv in argvs:
        rc, out, err = run(argv)
        if rc:
            die("prep-chmod", f"{' '.join(argv)}: rc={rc} {(out + err).strip()[-300:]}")
    # verify the RESULT, not the command: a world-write bit an earlier mode left must be gone, and nothing is left unreadable
    for argv in argvs:
        mode = os.stat(argv[2]).st_mode & 0o7777
        if argv[1] == "1777":
            ok = mode == 0o1777
        elif argv[1] == DIR_MODE:
            ok = (mode & 0o001) == 0o001 and (mode & 0o022) == 0
        else:
            ok = (mode & 0o044) == 0o044 and (mode & 0o022) == 0
        if not ok:
            die("prep-chmod", f"{argv[2]} is mode {mode:04o} after {' '.join(argv)}")


def mode_prep(a) -> int:
    """As the operator: a plain ecdsa-nistp256 key (NO passphrase: the harness signs non-interactively), the authority log that
    enrols it in --data-root (opened to the dispatch account by :func:`open_data_root`, and only that), and a copy of the private
    file (0600) and its public half (0644) owned by the dispatch account in --dispatch-dir. The custody word (default ``tpm``) is
    a DECLARATION levain records and never checks ('never a proof'): this guest has no TPM, and the guest authority log is not
    hardware-backed."""
    home, data_root = prep_paths(a)
    home.mkdir(parents=True, exist_ok=True)
    key = home / "key"
    if key.exists():
        die("prep", f"{key} exists: prep is run once per clone")
    rc, out, err = run(["/usr/bin/ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-C", "wave3-harness", "-f", str(key)])
    if rc:
        die("ssh-keygen", err.strip())
    sys.path.insert(0, a.levain_root)
    from levain.cockpit.keys import fingerprint_of, signing_public_key
    fp = fingerprint_of(signing_public_key(key))
    made = [p for p in (data_root, *data_root.parents) if _within(p, SCRATCH) and not p.exists()]
    data_root.mkdir(parents=True, exist_ok=True)
    if os.path.realpath(data_root) != str(data_root):
        die("prep", f"--data-root {data_root} became a symbolic link ({os.path.realpath(data_root)})")
    init_cockpit(data_root)
    env = dict(os.environ, PYTHONPATH=a.levain_root)
    rc, out, err = run([a.python, "-m", "levain", "cockpit", "ledger", "init", "--path", str(home), "--data-root", str(data_root),
                        "--signing-key", str(key), "--custody", a.custody, "--confirm", fp], env=env)
    print((out + err).strip()[-800:])
    if rc or not (data_root / "cockpit.db").is_file():
        die("ledger-init", f"rc={rc}")
    open_data_root(data_root, made)
    dest = f"{a.dispatch_dir}/w3-key"
    rc, out, err = run(["sudo", "-n", "/usr/bin/install", "-o", a.as_user, "-m", "0600", str(key), dest])
    if rc:
        die("install-key", err.strip())
    # `config sign` reads the private key's public half beside it (finding F: "cannot read the signing key's public half")
    rc, out, err = run(["sudo", "-n", "/usr/bin/install", "-o", a.as_user, "-m", "0644", str(home / "key.pub"), dest + ".pub"])
    if rc:
        die("install-key-pub", err.strip())
    os.chmod(home / "key.pub", 0o644)
    print(f"prepped fingerprint={fp} data_root={data_root} signing_key={dest} key_pub={home / 'key.pub'}")
    return 0


def mode_repo(a) -> int:
    """The repo the contract is filed against (wave3 --repo): one commit on ``main``, readable by every account, then chowned to
    the dispatch account (its git refuses a repo another account owns; no git config is written). ``main`` is read before the
    chown, while the operator still owns the repo."""
    repo = Path(a.repo)
    if repo.exists():
        die("repo", f"{repo} exists: repo is run once per clone")
    git = a.git
    ident = ["-c", "user.name=w3", "-c", "user.email=w3@invalid.example"]
    for argv in ([git, "init", "-q", "-b", "main", str(repo)],):
        rc, out, err = run(argv)
        if rc:
            die("git-init", err.strip())
    (repo / "README.md").write_text("wave3 target repo\n")
    for argv in ([git, "-C", str(repo), "add", "README.md"], [git, "-C", str(repo), *ident, "commit", "-q", "-m", "base"]):
        rc, out, err = run(argv)
        if rc:
            die("git-commit", err.strip())
    rc, out, err = run([git, "-C", str(repo), "rev-parse", "main"])
    main_sha = out.strip()
    if rc or not re.fullmatch(r"[0-9a-f]{40,64}", main_sha):
        die("git-rev-parse", f"rc={rc} {(out + err).strip()[-300:]}")
    rc, out, err = run(["/bin/chmod", "-R", "a+rX", str(repo)])
    if rc:
        die("chmod-repo", f"rc={rc} {(out + err).strip()[-300:]}")
    # the dispatch account must OWN the repo (finding L): the product's git ignores a system-wide safe.directory '*', so the
    # old one-line config never made the repo readable to it.
    rc, out, err = run(["sudo", "-n", "/usr/sbin/chown", "-R", a.as_user, str(repo)])
    if rc:
        die("chown-repo", err.strip())
    print(f"repo ready {repo} main={main_sha}")
    return 0


def mode_selfcheck(a) -> int:
    """HOST ONLY. A scratch levain home, a throwaway key, a scratch authority log, the REAL `levain dispatch config init|sign` CLI and the
    dispatch-side script in dry mode (the same Trust/check_item/check_verifier_item CONTROL runs). No sudo, no VM, no network."""
    work = Path(a.work)
    if work.exists():
        die("selfcheck", f"{work} exists: give a fresh directory")
    work.mkdir(parents=True)
    key_dir, data_root, ddir = work / "w3", work / "cockpit", work / "dispatch"
    ddir.mkdir(mode=0o700)
    a.prep_dir, a.data_root, a.dispatch_dir, a.as_user, a.dry_submit, a.no_pin = str(key_dir), str(data_root), str(ddir), "", True, True
    key_dir.mkdir(parents=True)
    rc, out, err = run(["/usr/bin/ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-C", "wave3-harness", "-f", str(key_dir / "key")])
    if rc:
        die("ssh-keygen", err.strip())
    sys.path.insert(0, a.levain_root)
    os.environ["LEVAIN_HOME"] = str(work / "home")
    os.environ["HOME"] = str(work / "home")
    (work / "home").mkdir()
    from levain.cockpit.keys import fingerprint_of, signing_public_key
    fp = fingerprint_of(signing_public_key(key_dir / "key"))
    data_root.mkdir()
    init_cockpit(data_root)
    env = dict(os.environ, PYTHONPATH=a.levain_root)
    rc, out, err = run([a.python, "-m", "levain", "cockpit", "ledger", "init", "--path", str(key_dir), "--data-root", str(data_root),
                        "--signing-key", str(key_dir / "key"), "--custody", a.custody, "--confirm", fp], env=env)
    print("ledger-init rc=%d: %s" % (rc, (out + err).strip()[-400:]))
    if rc:
        return 1
    a.key_pub, a.signing_key = str(key_dir / "key.pub"), str(key_dir / "key")
    os.environ["PYTHONPATH"] = a.levain_root
    return mode_stage(a)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("prep", "repo", "stage", "pool", "selfcheck", "policy"))
    ap.add_argument("--repo", default="/private/tmp/w3repo", help="repo mode: where the contract's target repo is made")
    ap.add_argument("--git", default="/Library/Developer/CommandLineTools/usr/bin/git", help="DARWIN.git")
    ap.add_argument("--levain-root", default=ROOT)
    ap.add_argument("--python", default=PY)
    ap.add_argument("--as-user", default=DISPATCH_USER, help="the dispatch account ('' = run as yourself: host test only)")
    ap.add_argument("--dispatch-dir", default=f"{STORE}/dispatch")
    ap.add_argument("--run-dir", default="/private/var/run/levain")
    ap.add_argument("--data-root", default="/private/tmp/w3/cockpit", help="the operator's cockpit data root (holds cockpit.db); outside the operator's home, which the dispatch account cannot traverse")
    ap.add_argument("--signing-key", default=f"{STORE}/dispatch/w3-key", help="the private key file, readable by the dispatch account")
    ap.add_argument("--key-pub", default="/Users/admin/w3/key.pub", help="the SAME key's public file, readable by the operator")
    ap.add_argument("--prep-dir", default="/Users/admin/w3")
    ap.add_argument("--custody", default="tpm", help="the DECLARED custody for the enrol link (prep): a declaration, never a proof")
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--backends", default="", help="the backend identity per model (default: the model ids themselves)")
    ap.add_argument("--cred", default="or-key")
    ap.add_argument("--template", default="wave3-r0")
    ap.add_argument("--write-path", default="wave3/hello.txt")
    ap.add_argument("--verifier-id", default="wave3-bb")
    ap.add_argument("--verifier-argv", default="python3 -m pytest -q")
    ap.add_argument("--price-in", type=int, default=2_000_000, help="microdollars per million input tokens (PLACEHOLDER: replace from OpenRouter's page)")
    ap.add_argument("--price-out", type=int, default=8_000_000, help="microdollars per million output tokens (PLACEHOLDER)")
    ap.add_argument("--price-source", default="https://openrouter.ai/models")
    ap.add_argument("--no-pin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--dry-submit", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--work", default="", help="selfcheck: a fresh scratch directory")
    a = ap.parse_args(argv)
    return {"prep": mode_prep, "repo": mode_repo, "stage": mode_stage, "pool": mode_pool, "selfcheck": mode_selfcheck, "policy": mode_policy}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
