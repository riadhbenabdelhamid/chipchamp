"""Enable ``python -m chipchamp …`` (works without the console-script entry point,
e.g. before ``pip install`` or when only the venv's python is on PATH)."""
from .cli import main

if __name__ == "__main__":
    main()
