"""Q6b: a cockpit served behind an https proxy (``make_server(remote_hosts=...)``) for passkey consent answers.

The routes' full allow/deny journey runs in a real browser (project memory Q6B_E2E); these pin the boundary: what a
proxied request may read and post, and what a remote-host server refuses to be.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.consent.passkey import is_host_name
from levain.web_server import make_server
from tests.test_web_server import _TOK, _TOK_HEADER, _get_h, _make_full_install, _post, _store_with_data

_NAME = "box.example.ts.net"


@contextmanager
def _serving_remote(source):
    httpd = make_server(source, host="127.0.0.1", port=0, write_token=_TOK, remote_hosts=(_NAME,))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_remote_host_server_needs_the_token_for_every_read_and_takes_only_passkey_posts(tmp_path: Path) -> None:
    with _serving_remote(_make_full_install(tmp_path)) as (base, _):
        assert _get_h(base + "/substrate.json")[0] == 403                                    # even from loopback
        assert _get_h(base + "/substrate.json", headers={_TOK_HEADER: _TOK})[0] == 200
        proxied = {"X-Forwarded-For": "100.64.0.1", "Host": _NAME}
        assert _get_h(base + "/substrate.json", headers={"X-Forwarded-For": "100.64.0.1"})[0] == 403
        status, body = _post(base + "/edit", {"kind": "section", "path": "x"}, headers=proxied)
        assert (status, body["error"]) == (403, "off_box_write")
        status, body = _post(base + "/edit", {"kind": "section", "path": "x"}, headers={"Host": _NAME})
        assert (status, body["error"]) == (403, "off_box_write")                            # a header-less proxy
        status, body = _post(base + "/cockpit/consent/req-1/passkey-challenge", {"decision": "allow"},
                             headers=proxied, token=None)
        assert (status, body["error"]) == (403, "credential_required")                      # the write gate still holds


def test_a_loopback_server_without_a_remote_host_keeps_token_free_reads_until_a_proxy_header(tmp_path: Path) -> None:
    from tests.test_web_server import _serving

    with _serving(_make_full_install(tmp_path)) as (base, _):
        assert _get_h(base + "/substrate.json")[0] == 200
        assert _get_h(base + "/substrate.json", headers={"X-Forwarded-For": "100.64.0.1"})[0] == 403


def test_a_remote_host_is_refused_for_a_read_only_source_and_a_bad_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="writable"):
        make_server(_store_with_data(tmp_path), port=0, remote_hosts=(_NAME,))
    with pytest.raises(ValueError, match="lowercase DNS name"):
        make_server(_make_full_install(tmp_path), port=0, write_token=_TOK, remote_hosts=("https://x.example/",))
    assert not is_host_name("127.0.0.1") and is_host_name(_NAME)
