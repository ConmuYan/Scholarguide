"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import research_policy as _implementation
sys.modules[__name__] = _implementation
