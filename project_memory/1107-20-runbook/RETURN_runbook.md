# RETURN: runbook for C9 (one real coordinator day) and C7 (closed-lid test)

Page: /Users/phillipclapham/Briefcase/.levain-1107-20-runbook/project_memory/1107-20-runbook/RUNBOOK_C9_C7.html (source RUNBOOK_C9_C7.md beside it). Nothing was run.

Findings that change the plan:
- Amphetamine is running and holds the Mac awake now (`pmset -g`): it must be quit for C7 or the lid test measures Amphetamine. Other `caffeinate` holders too.
- Levain's own code (`awake.py` LID_NOTE) already says an idle-sleep assertion does not stop lid sleep, so C7 is a premise check; the runbook defines PASS for both outcomes and files a doc fix if the lid stays awake.
- Lid-close sleeps flow's 24/7 host: C7 goes first, before seats, on a scratch `LEVAIN_HOME=/tmp/c7-home` throwaway thread.
- C9 PASS bar is the BUILD_PLAN DONE line (5 run-log items + CI/Sibling-grep). Today: serve daemon not installed, no coordinator agent set, no lease.
- Eight UNVERIFIED items collected at the foot (phone-to-serve reach, 0.11.28 on PyPI, LEVAIN_HOME end to end, fresh-mode clear, etc.), each with what settles it.
