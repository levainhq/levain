"""The phone server's stdout is a log under the LaunchAgent, so the token must not be printed when --link-file holds it."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from levain import phone_server


def _run(tmp_path: Path, monkeypatch, link_file: Path | None) -> "tuple[str, str]":
    """Run ``run_phone_server`` on a thread until it is serving; return (token, link-file text read while it ran)."""
    made = []
    serving = threading.Event()
    real = phone_server.make_phone_server

    def spy(*a, **kw):
        httpd = real(*a, **kw)
        loop = httpd.serve_forever

        def serve_forever(*a2, **kw2):
            serving.set()
            return loop(*a2, **kw2)

        httpd.serve_forever = serve_forever
        made.append(httpd)
        return httpd

    monkeypatch.setattr(phone_server, "make_phone_server", spy)
    t = threading.Thread(target=phone_server.run_phone_server, args=(tmp_path / "q",),
                         kwargs=dict(name="localhost", tailscale=False, port=0, link_file=link_file), daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not (serving.is_set() or not t.is_alive()):
            time.sleep(0.02)
        assert serving.is_set(), "the server never started serving"
        text = link_file.read_text(encoding="utf-8") if link_file is not None else ""
        return made[0].phone_token, text
    finally:
        if serving.is_set():            # shutdown() waits forever on a server whose serve_forever never ran
            made[0].shutdown()
        t.join(10)
        assert not t.is_alive(), "the server thread did not stop"


def test_with_a_link_file_the_token_is_not_printed(tmp_path: Path, monkeypatch, capsys) -> None:
    link = tmp_path / "link.json"
    token, text = _run(tmp_path, monkeypatch, link)
    out = capsys.readouterr().out
    assert token not in out
    assert str(link) in out
    assert json.loads(text)["phone_token"] == token


def test_without_a_link_file_the_token_is_still_printed(tmp_path: Path, monkeypatch, capsys) -> None:
    token, _ = _run(tmp_path, monkeypatch, None)
    assert f"#phone_token={token}" in capsys.readouterr().out
