"""Independent interpreter CAS regression; all databases are temporary."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from memory_store import MemoryStore


WORKER = r'''
import sys
from memory_store import MemoryStore, RevisionConflict
store = MemoryStore(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
try:
    store.grant('prefer_recent', sys.argv[2] == 'true', consent=True,
                expires_at=float(sys.argv[3]), expected_revision=1)
    print('success', flush=True)
except RevisionConflict:
    print('conflict', flush=True)
finally:
    store.close()
'''


class ProcessRaceTests(unittest.TestCase):
    def test_processes_compete_for_same_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preferences.sqlite3'
            expiry = time.time() + 3600
            with MemoryStore(path) as store:
                store.grant('prefer_recent', False, consent=True, expires_at=expiry, expected_revision=0)
                self.assertEqual(store.active_snapshot(), {'prefer_recent': False})
            processes = []
            try:
                for value in ('true', 'false'):
                    processes.append(subprocess.Popen(
                        [sys.executable, '-B', '-c', WORKER, str(path), value, str(expiry)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                for proc in processes:
                    self.assertEqual(proc.stdout.readline().strip(), 'ready')
                for proc in processes:
                    proc.stdin.write('go\n')
                    proc.stdin.flush()
                outcomes = []
                for proc in processes:
                    out, err = proc.communicate(timeout=15)
                    self.assertEqual(proc.returncode, 0, 'Worker failed')
                    self.assertEqual(err, '')
                    outcomes.append(out.strip())
                self.assertCountEqual(outcomes, ['success', 'conflict'])
                with MemoryStore(path) as store:
                    row = store.list()['prefer_recent']
                    self.assertEqual(row['revision'], 2)
                    self.assertIn(row['value'], (True, False))
                    self.assertEqual(store.active_snapshot(), {'prefer_recent': row['value']})
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()


if __name__ == '__main__':
    unittest.main()
