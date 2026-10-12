"""Enable `python -m levain` invocation, through the same hardened entry as the console script."""

from levain.launch import main

if __name__ == "__main__":
    raise SystemExit(main())
