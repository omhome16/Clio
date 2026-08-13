# src/clio/__main__.py
"""Entry point for ``python -m clio``."""
import sys

from clio.cli import main

if __name__ == "__main__":
    sys.exit(main())
