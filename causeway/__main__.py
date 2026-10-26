"""Entry point for `python -m causeway`."""

import sys

from causeway.cli import main

if __name__ == "__main__":
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    sys.exit(main())
