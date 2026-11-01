"""Temporary confirmed quota for isolated local MCP tests only."""
import tempfile
import time
from pathlib import Path

from lune_quota import LuneQuotaStore


def confirmed_quota(testcase):
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    store = LuneQuotaStore(Path(directory.name) / 'quota.sqlite3')
    testcase.addCleanup(store.close)
    store.confirm(300, time.time() + 600)
    return store
