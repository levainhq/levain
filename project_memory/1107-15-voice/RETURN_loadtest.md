# RETURN: levain 50 load test (band 256, np-6916adcd)

Source: levain 50's commit 962f534f (test_cockpit_read_gate_load.py) was on main but only ran 240 rows with a 3 s ceiling.
New test tests/test_cockpit_read_load_1500.py: 1500-item QuestionStore (60 open, 1440 answered), 28 concurrent GETs x 5 rounds
(decisions panel, now panel, manifest), the real cockpit server on a loopback port, pytest tmp_path store.

RESULT (verbatim): LOADTEST items=1500 reads=140 status={200: 140} p50=5ms p95=277ms max=287ms
Bar: 0 503s, p95 < 1 s -> PASS. No fix needed.

Not covered: flow's host question type (per-answered-row signature subprocess) is not in this rig; the in-process flow
measure is flow 18's (recent(50) 0.15 s). The desk's merge+restart of the live serve and Phill's live Mobile answer are not this test.
