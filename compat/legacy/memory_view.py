"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import memory_view as _implementation
sys.modules[__name__] = _implementation
