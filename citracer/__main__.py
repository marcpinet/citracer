"""Package entry point: ``python -m citracer ...``

Having this file means users can run citracer from the repo root with
``python -m citracer --pdf paper.pdf --keyword ...`` without needing to
install the package or rely on a separate entry script at the repo
root (which would collide awkwardly with the package directory).
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
