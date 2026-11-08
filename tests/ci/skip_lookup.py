"""Decide whether a push to main can skip the full suite because the same sha already passed on another ref.

    python tests/ci/skip_lookup.py            (reads the GitHub Actions environment, writes to $GITHUB_OUTPUT)

A push to main lands a sha that the land waiter already tested on its `land/**` candidate (a fast-forward keeps the
sha), so re-running the whole suite on main buys nothing. The nightly run on main HEAD is the same-sha rerun that
catches nondeterminism, so the duplicate here is not the detector.

The decision is `decide()`, a pure function of the event and the run list; `main()` only fetches the list and writes
the outputs: `skip` ("true" or "false"), and, when it skips, `ref` and `run_id` of the verifying run.

FAILS CLOSED. Anything other than one clear answer runs the full suite: any API error, a malformed reply, a list
longer than the one page asked for (so a run might be unseen), a missing environment variable. A tag push, a manual
run, a nightly run and a pull request never skip: only a plain push to the main branch can.
"""
import json
import os
import sys
import urllib.request

MAIN = "main"
# Event names of runs that count as "this sha was tested": a push or a manual run on a ref other than main. A
# scheduled run is main by construction, and a pull request run is not a verdict on the ref that gets landed.
VERIFYING_EVENTS = ("push", "workflow_dispatch")
# A run's head_branch carries the bare ref name for a branch AND for a tag (a v1.0 tag run says "v1.0"), so a tag
# cannot be told from a branch by that field. Only a ref under these prefixes counts as the landing verdict (the land
# waiter's candidate, a seat's own branch); a tag's run, main's own run and any other branch's run never do.
LANDING_REFS = ("land/", "seat/")
PAGE = 100


def decide(event, ref_type, ref_name, run_id, runs_reply):
    """(skip, ref, verifying_run_id) from the push's own facts and the API's reply for this sha.

    `runs_reply` is the parsed JSON of the workflow-runs list filtered by head_sha and conclusion success. Any shape
    this function does not recognise returns (False, "", "")."""
    no = (False, "", "")
    if event != "push" or ref_type != "branch" or ref_name != MAIN:
        return no
    try:
        runs = runs_reply["workflow_runs"]
        total = runs_reply["total_count"]
        if not isinstance(runs, list) or isinstance(total, bool) or not isinstance(total, int) or total > len(runs):
            return no
        for r in runs:
            if (r["conclusion"] == "success" and r["status"] == "completed" and r["event"] in VERIFYING_EVENTS
                    and r["head_branch"].startswith(LANDING_REFS)
                    and str(r["id"]) != str(run_id)):
                return True, str(r["head_branch"]), str(r["id"])
    except (KeyError, TypeError, AttributeError):
        return no
    return no


def fetch(repo, sha, token, workflow="test.yml"):
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
           f"?head_sha={sha}&status=success&per_page={PAGE}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _write(lines, environ):
    target = environ.get("GITHUB_OUTPUT")
    text = "".join(line + "\n" for line in lines)
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text, end="")


def main(environ=None, fetcher=fetch):
    environ = os.environ if environ is None else environ
    skip, ref, vid = False, "", ""
    try:
        reply = fetcher(environ["GITHUB_REPOSITORY"], environ["GITHUB_SHA"], environ["GITHUB_TOKEN"])
        skip, ref, vid = decide(environ["GITHUB_EVENT_NAME"], environ["GITHUB_REF_TYPE"],
                                environ["GITHUB_REF_NAME"], environ["GITHUB_RUN_ID"], reply)
    except Exception as e:  # noqa: BLE001 - every failure means "run the suite"
        print(f"skip lookup failed ({type(e).__name__}: {e}); running the full suite", file=sys.stderr)
        skip, ref, vid = False, "", ""
    if skip:
        print(f"verified on {ref} run {vid}")
        lines = ["skip=true", f"ref={ref}", f"run_id={vid}"]
    else:
        print("no verifying run: the full suite runs")
        lines = ["skip=false", "ref=", "run_id="]
    try:
        _write(lines, environ)
    except Exception as e:  # noqa: BLE001 - an output nobody can read is no verdict
        # Exit non-zero: the gate job fails, and a failed gate runs every heavy job (the workflow's `if:`), so this fails closed.
        print(f"skip lookup could not write its outputs ({type(e).__name__}: {e}); the gate fails, so the full suite runs",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
