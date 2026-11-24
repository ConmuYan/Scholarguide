"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide import api_schema as _implementation
sys.modules[__name__] = _implementation
