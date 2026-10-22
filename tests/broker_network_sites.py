"""Rule (g)'s inventory: every networking call site allowed in `levain/broker/**`, as exact
`(file, enclosing function, API)` tuples. A bare constant, imported by the committed AST test. The session that adds
a networking site adds its tuple here in the same commit, for its own lines only."""

NETWORK_SITES = (
    # The seven call sites on origin/main (61f16688) before any 3b code, from `git grep` of levain/broker/front.py and
    # levain/broker/stub.py there.
    ("front.py", "open_by_address", "socket"),
    ("front.py", "open_by_address", "sock_connect"),
    ("front.py", "open_by_address", "open_connection"),
    ("front.py", "serve_unix", "socket"),
    ("stub.py", "clear_stale_socket", "socket"),
    ("stub.py", "clear_stale_socket", "connect_ex"),
    ("stub.py", "serve_unix", "socket"),
    # The front's keys-side client: the STATUS connection and one connection per brokered exchange.
    ("front.py", "open_keys", "create_unix_connection"),
)
