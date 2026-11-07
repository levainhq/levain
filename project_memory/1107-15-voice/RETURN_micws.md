# RETURN micws (S2, wsclient only)
Branch seat/1107-15-micws. Owns levain/voice/wsclient.py + tests/test_voice_wsclient.py (57 tests pass).
API: connect(url, headers, *, connect_timeout, idle_timeout, handshake_deadline, max_message, allowed_hosts, insecure_for_test) -> WebSocketClient
 send_binary/send_text/ping, recv() -> ("text",str)|("binary",bytes)|("close",(code,reason)), close(code, wait). Errors: WsError(code) fixed words; WsTimeout (recoverable idle timeout at a frame boundary; mid-frame timeout closes).
Not done (out of owned paths): the Deepgram stream adapter class in providers.py (design S2 lists it, this lane did not own it).
Design questions: (1) pass allowed_hosts=providers.ALLOWED_HOSTS from the adapter; (2) no permessage-deflate (offered none); (3) max_message default 1 MiB; (4) real TLS path untested (loopback ws:// only).
