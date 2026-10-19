"""The phone page keeps every element the consent view needs, and loads the app script before the consent script."""
from pathlib import Path

import levain

PAGE = Path(levain.__file__).parent / "templates" / "web" / "phone.html"


def test_phone_page_keeps_its_ids_and_loads_the_app_script_first():
    html = PAGE.read_text(encoding="utf-8")
    for ident in ("refresh", "status", "requests", "enrol", "enrol-note", "enrol-btn", "enrol-out",
                  "stateline", "tabs", "board", "notes", "pings", "manifest"):
        assert f'id="{ident}"' in html, ident
    assert html.index('src="/phone_app.js"') < html.index('src="/phone.js"')
