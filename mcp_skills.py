"""Command-line entry point for bundled MCP skills."""
import sys
from scholarguide import mcp_skills as _implementation

if __name__ == '__main__':
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
