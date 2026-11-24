"""Command-line entry point for Lune quota scheduling."""
import sys
from scholarguide import lune_quota_schedule as _implementation

if __name__ == '__main__':
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
