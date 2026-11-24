"""Command-line entry point for ScholarGuide updates."""
import sys
from scholarguide import update_cli as _implementation

if __name__ == '__main__':
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
