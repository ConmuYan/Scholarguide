from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.verify_delivery import isolated_environment


class MemoryOpenRaceTests(unittest.TestCase):
    def test_killed_before_publish_leaves_no_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'memory.sqlite3'
            code = '''\
import sys, time
from unittest.mock import patch
from memory_store import MemoryStore
def pause(*args, **kwargs):
    print('before publish', flush=True)
    time.sleep(60)
with patch('memory_store.os.link', side_effect=pause):
    MemoryStore(sys.argv[1])
'''
            child = subprocess.Popen([sys.executable, '-c', code, str(path)],
                                     env=isolated_environment(root), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'before publish')
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
            self.assertFalse(path.exists())
            from memory_store import MemoryStore
            with MemoryStore(path) as store:
                self.assertEqual(store.list(), {})
