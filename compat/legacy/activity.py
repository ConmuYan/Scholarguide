"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import activity as _implementation
sys.modules[__name__] = _implementation
