"""Compatibility launcher. Normal app.py now uses the same source registry."""
from __future__ import annotations

import sys
import app as app_module


def activate_sources() -> dict:
    """Install optional MCP adapters idempotently without patching module constants."""
    from common import SOURCE_NAMES
    from mcp_lune import install
    provider = install()
    return {'provider': provider.usage(), 'source_names': list(SOURCE_NAMES),
            'source_names_extended_in': []}


def main(argv: list[str] | None = None) -> int:
    activate_sources()
    return app_module.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
