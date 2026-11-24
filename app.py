"""Source and installed command-line entry point for ScholarGuide."""
import sys
from scholarguide import app as _app

if __name__ == '__main__':
    raise SystemExit(_app.main())

# Preserve the historical ``import app`` API and its module identity.
sys.modules[__name__] = _app
