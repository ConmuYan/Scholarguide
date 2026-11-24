"""Compatibility import for the documented source-provider registry."""
import sys
from scholarguide import source as _implementation

sys.modules[__name__] = _implementation
