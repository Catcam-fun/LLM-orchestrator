#!/usr/bin/env python3
"""vault.py — top-level CLI entry point.

Just imports and runs the cli module from scripts/vault_graph. Lives at vault
root so you can run `python vault.py <subcommand>` from any directory.

See `python vault.py --help` for usage.
"""
import sys
from pathlib import Path

# Add scripts/ to sys.path so we can import vault_graph as a package
_VAULT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_VAULT_ROOT / "scripts"))

from vault_graph.cli import main

if __name__ == "__main__":
    sys.exit(main())
