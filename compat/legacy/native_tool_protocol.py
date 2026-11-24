"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import native_tool_protocol as _implementation
sys.modules[__name__] = _implementation
