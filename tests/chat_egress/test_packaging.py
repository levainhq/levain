"""CH-D2 S8 (second slice): what the wheel ships for chat network (RULINGS_CORE item 3)."""

from levain.chat_egress import guard


def test_the_glue_and_its_srt_lock_ship_in_the_wheel():
    """What setuptools packs: each package-data glob of `levain`, expanded against the package dir, minus the
    exclude globs. The launch spawns `node/glue.mjs`, and `levain chat setup` installs srt from `node/package.json` +
    `node/package-lock.json`: all three must be in it (a wheel without them cannot start chat network)."""
    import tomllib
    from fnmatch import fnmatch
    from pathlib import Path
    root = Path(guard.__file__).resolve().parents[2]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]
    pkg = root / "levain"
    shipped = {p.relative_to(pkg).as_posix() for g in cfg["package-data"]["levain"] for p in pkg.glob(g) if p.is_file()}
    shipped = {s for s in shipped if not any(fnmatch(s, x) for x in cfg["exclude-package-data"]["levain"])}
    for need in ("chat_egress/node/glue.mjs", "chat_egress/node/package.json", "chat_egress/node/package-lock.json",
                 "chat_egress/denied_table.json"):
        assert need in shipped, need
    assert not any(s.startswith("chat_egress/node/node_modules") for s in shipped)
