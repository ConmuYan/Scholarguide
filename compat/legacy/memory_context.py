"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import memory_context as _implementation
sys.modules[__name__] = _implementation
