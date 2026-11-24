"""Compatibility entry point for the optional MCP source launcher."""
import sys
from scholarguide import app_mcp as _implementation

if __name__ == '__main__':
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
