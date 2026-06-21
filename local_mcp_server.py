#!/usr/bin/env python3
"""Entry point for the local MCP server subprocess (stdio transport)."""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp_integration.local_server import main

if __name__ == "__main__":
    main()
