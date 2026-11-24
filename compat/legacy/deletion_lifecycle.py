"""Compatibility alias for the historical top-level module."""
import sys
from scholarguide.experimental import deletion_lifecycle as _implementation
sys.modules[__name__] = _implementation
