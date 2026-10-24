"""levain.sign_request_id — what a sign request id is (the name a request file is filed under, and the id a cockpit
row's ``sign`` facet carries). Its own module, importing nothing that needs ``fcntl``, so the cockpit registry's facet
validator answers a bad id with ``FacetValueError`` wherever it runs; :mod:`levain.consent.sign` re-exports both."""
from __future__ import annotations

import re

ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def request_id_ok(request_id: object) -> bool:
    """True iff ``request_id`` is a sign request id (what a request file is named by, and a cockpit row's `sign` facet
    carries)."""
    return isinstance(request_id, str) and ID.fullmatch(request_id) is not None
