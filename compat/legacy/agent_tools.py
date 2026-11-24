"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import agent_tools as _implementation
sys.modules[__name__] = _implementation
