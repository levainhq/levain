"""Rule (g)'s inventory: every networking call site allowed in `levain/broker/**`, as exact structural signatures
`(file, enclosing definition path, API, called expression, count)`. The definition path is the full path from the
module (`Front.serve_unix`, `open_by_address`); the called expression is the call's function as source
(`socket.socket`, `asyncio.get_running_loop().sock_connect`). The test requires each signature to occur exactly
`count` times, so a second call at a declared site fails, and a declared signature with no remaining site fails as
stale. A bare constant, imported by the committed AST test. The session that adds a networking site adds its tuple
here in the same commit, for its own lines only."""

NETWORK_SITES = (
    # The seven call sites on origin/main (61f16688) before any 3b code, from `git grep` of levain/broker/front.py and
    # levain/broker/stub.py there.
    ("front.py", "open_by_address", "socket", "socket.socket", 1),
    ("front.py", "open_by_address", "sock_connect", "asyncio.get_running_loop().sock_connect", 1),
    ("front.py", "open_by_address", "open_connection", "asyncio.open_connection", 1),
    ("front.py", "Front.serve_unix", "socket", "socket.socket", 1),
    ("stub.py", "clear_stale_socket", "socket", "socket.socket", 1),
    ("stub.py", "clear_stale_socket", "connect_ex", "probe.connect_ex", 1),
    ("stub.py", "Stub.serve_unix", "socket", "socket.socket", 1),
    # The front's keys-side client: the STATUS connection and one connection per brokered exchange.
    ("front.py", "open_keys", "create_unix_connection", "loop.create_unix_connection", 1),
    # keys' listeners, built (and a stale socket probed) in the one function that creates them: the probe socket
    # and the listener socket.
    ("keys.py", "bind_listeners", "socket", "socket.socket", 2),
    ("keys.py", "bind_listeners", "connect_ex", "probe.connect_ex", 1),
    # The listeners the server-side API names cover (`start_server`, `start_unix_server`), present before the API set
    # named them: the stub's and the front's unix and TCP servers, and keys' servers on its bound sockets.
    ("front.py", "Front.serve_unix", "start_unix_server", "asyncio.start_unix_server", 1),
    ("front.py", "Front.serve_tcp", "start_server", "asyncio.start_server", 1),
    ("stub.py", "Stub.serve_unix", "start_unix_server", "asyncio.start_unix_server", 1),
    ("stub.py", "Stub.serve_tcp", "start_server", "asyncio.start_server", 1),
    ("keys.py", "Keys.start", "start_unix_server", "asyncio.start_unix_server", 1),
    # keys' TOKEN-RESOLVE client: one connection to CONTROL's socket per resolve.
    ("keys.py", "Keys._token_resolve", "open_unix_connection", "asyncio.open_unix_connection", 1),
    # keys' attach client (S1 3c-3): its one long-lived KEYS-ATTACH connection to CONTROL's socket.
    ("keys.py", "Keys._attach_open", "open_unix_connection", "asyncio.open_unix_connection", 1),
    # keys' upstream client: plain TCP to the validated address (the one connect seam), and TLS on that connection.
    ("upstream.py", "_connect", "open_connection", "asyncio.open_connection", 1),
    ("upstream.py", "_handshake", "start_tls", "loop.start_tls", 1),
    # NOT a network call: h11's serializer (`Connection.send` returns bytes and does no I/O) matches the API name
    # `send`; keys writes what it returns through `Upstream.write`. Every serialization goes through this one helper.
    ("upstream.py", "h11_bytes", "send", "conn.send", 1),
)
