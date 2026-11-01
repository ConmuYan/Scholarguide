"""Only synthetic temporary directories; no service or product data."""
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from deletion_lifecycle import DeletionLifecycle, Tombstoned, UnsupportedPlatform

ID = "a" * 32
OTHER = "b" * 32


def hold_lease(root, entered, release):
    with DeletionLifecycle(root).execution_lease("session", ID):
        entered.set()
        release.wait(15)


def retire(root, started, finished):
    started.set()
    DeletionLifecycle(root).tombstone("session", ID)
    finished.set()


def windows_try_lock(file):
    """One-byte experimental native lock, not a lifecycle implementation."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    lock = kernel.LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    overlapped = Overlapped()
    if not lock(msvcrt.get_osfhandle(file.fileno()), 3, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())
    # The synchronous lock remains in effect until the file handle is closed.


def windows_lock_holder(path, entered, release):
    with open(path, 'r+b') as file:
        windows_try_lock(file)
        entered.set()
        if not release.wait(15):
            os._exit(74)
        os._exit(73)  # abnormal exit, without explicit unlock/close


def crash_during_tombstone(root):
    real_fsync = os.fsync
    def crash(fd):
        if os.readlink('/proc/self/fd/' + str(fd)).endswith('.tombstone'):
            os._exit(73)
        return real_fsync(fd)
    with mock.patch('deletion_lifecycle.os.fsync', side_effect=crash):
        DeletionLifecycle(root).tombstone('session', ID)


class UnsupportedTests(unittest.TestCase):
    def test_windows_fails_before_any_path_access(self):
        with mock.patch("deletion_lifecycle.sys.platform", "win32"), \
                mock.patch("deletion_lifecycle.os.open") as opened:
            with self.assertRaises(UnsupportedPlatform):
                DeletionLifecycle(r"C:\anything\junction")
            opened.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows-specific checks")
class WindowsFailClosedTests(unittest.TestCase):
    def test_real_junction_and_child_process_fail_closed(self):
        # Both junction and target live exclusively inside the temporary tree.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'target'
            target.mkdir()
            sentinel = target / 'sentinel'
            sentinel.write_bytes(b'synthetic')
            junction = base / 'junction'
            result = subprocess.run(
                ['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(target)],
                capture_output=True, text=True, timeout=15)
            if result.returncode:
                self.skipTest('cannot create temporary junction: ' + result.stderr)
            try:
                attrs = os.lstat(junction).st_file_attributes
                self.assertTrue(attrs & 0x400, 'fixture is not a reparse point')
                with self.assertRaises(UnsupportedPlatform):
                    DeletionLifecycle(junction)
                # A fresh, real Windows process must also refuse the real junction.
                script = ("import sys; from deletion_lifecycle import "
                          "DeletionLifecycle, UnsupportedPlatform\n"
                          "try: DeletionLifecycle(sys.argv[1])\n"
                          "except UnsupportedPlatform: sys.exit(73)\n"
                          "sys.exit(0)")
                child = subprocess.run([sys.executable, '-c', script, str(junction)],
                                       cwd=Path(__file__).resolve().parents[1],
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, 73, child.stderr)
                self.assertEqual(sentinel.read_bytes(), b'synthetic')
                self.assertEqual(sorted(p.name for p in target.iterdir()), ['sentinel'])
            finally:
                # Remove the junction itself, never recurse through it.
                os.rmdir(junction)

    def test_native_lock_contention_and_abnormal_exit_probe(self):
        # Tests one native primitive, not stability of a lifecycle lock inode,
        # path security, or the durability of tombstone creation.
        ctx = mp.get_context('spawn')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic-lock'
            path.write_bytes(b'0')
            entered, release = ctx.Event(), ctx.Event()
            child = ctx.Process(target=windows_lock_holder, args=(str(path), entered, release))
            child.start()
            try:
                self.assertTrue(entered.wait(10), 'child did not acquire native lock')
                with open(path, 'r+b') as contender:
                    with self.assertRaises(OSError) as error:
                        windows_try_lock(contender)
                    self.assertEqual(error.exception.winerror, 33)  # ERROR_LOCK_VIOLATION
                    release.set()
                    child.join(10)
                    self.assertEqual(child.exitcode, 73)
                    windows_try_lock(contender)
            finally:
                release.set()
                if child.is_alive():
                    child.terminate()
                child.join(10)

    def test_windows_directory_flush_capability_probe(self):
        # Exploratory evidence only: success on one volume does not establish a
        # durable namespace protocol or justify enabling Windows support.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                           wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        flush = kernel.FlushFileBuffers
        flush.argtypes = [wintypes.HANDLE]
        flush.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as tmp:
            # GENERIC_READ|GENERIC_WRITE, share read/write/delete,
            # OPEN_EXISTING, BACKUP_SEMANTICS|OPEN_REPARSE_POINT.
            handle = create(tmp, 0xC0000000, 7, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                print('Windows directory CreateFileW failed:', ctypes.get_last_error(), flush=True)
                return
            try:
                ok = flush(handle)
                error = 0 if ok else ctypes.get_last_error()
                print('Windows directory FlushFileBuffers:', bool(ok), 'error:', error, flush=True)
            finally:
                self.assertTrue(close(handle))


@unittest.skipUnless(sys.platform == "linux", "Linux implementation only")
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir(mode=0o700)
        self.lifecycle = DeletionLifecycle(self.root)
        self.ctx = mp.get_context("spawn")
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for p in self.processes:
            if p.is_alive():
                p.terminate()
            p.join(10)

    def start(self, target, *args):
        p = self.ctx.Process(target=target, args=args)
        p.start()
        self.processes.append(p)
        return p

    def test_barrier_and_identity_isolation(self):
        entered, release, started, finished = (self.ctx.Event() for _ in range(4))
        worker = self.start(hold_lease, str(self.root), entered, release)
        self.assertTrue(entered.wait(10))
        deleter = self.start(retire, str(self.root), started, finished)
        self.assertTrue(started.wait(10))
        self.assertFalse(finished.wait(0.4))
        # Other IDs, kinds and roots do not share this barrier.
        with self.lifecycle.execution_lease("session", OTHER):
            pass
        with self.lifecycle.execution_lease("run", ID):
            pass
        root2 = Path(self.temp.name) / "root2"
        root2.mkdir(mode=0o700)
        with DeletionLifecycle(root2).execution_lease("session", ID):
            pass
        release.set()
        self.assertTrue(finished.wait(10))
        worker.join(10); deleter.join(10)
        self.assertEqual((worker.exitcode, deleter.exitcode), (0, 0))
        with self.assertRaises(Tombstoned):
            with self.lifecycle.execution_lease("session", ID):
                self.fail("retired ID admitted")
        with self.lifecycle.execution_lease("run", ID):
            pass
        with DeletionLifecycle(root2).execution_lease("session", ID):
            pass

    def test_crashed_lease_released_and_retirement_persists(self):
        entered, release = self.ctx.Event(), self.ctx.Event()
        worker = self.start(hold_lease, str(self.root), entered, release)
        self.assertTrue(entered.wait(10))
        worker.terminate(); worker.join(10)
        self.assertIsNotNone(worker.exitcode)
        self.lifecycle.tombstone("session", ID)
        DeletionLifecycle(self.root).tombstone("session", ID)
        with self.assertRaises(Tombstoned):
            with DeletionLifecycle(self.root).execution_lease("session", ID):
                pass

    def test_process_crash_during_tombstone_recovery(self):
        process = self.start(crash_during_tombstone, str(self.root))
        process.join(10)
        self.assertEqual(process.exitcode, 73)
        with self.assertRaises(Tombstoned):
            with self.lifecycle.execution_lease('session', ID):
                pass
        self.lifecycle.tombstone('session', ID)
        with self.assertRaises(Tombstoned):
            with DeletionLifecycle(self.root).execution_lease('session', ID):
                pass

    def test_interrupted_tombstone_recovery(self):
        real_fsync = os.fsync
        def fail_tombstone(fd):
            # Identify the fresh tombstone descriptor, not the lock or directory.
            if os.readlink('/proc/self/fd/' + str(fd)).endswith('.tombstone'):
                raise OSError('synthetic fsync failure')
            return real_fsync(fd)
        with mock.patch('deletion_lifecycle.os.fsync', side_effect=fail_tombstone):
            with self.assertRaises(OSError):
                self.lifecycle.tombstone('session', ID)
        # Existing but not-yet-confirmed marker is never treated as reusable.
        with self.assertRaises(Tombstoned):
            with self.lifecycle.execution_lease('session', ID):
                pass
        self.lifecycle.tombstone('session', ID)
        with self.assertRaises(Tombstoned):
            with self.lifecycle.execution_lease('session', ID):
                pass

    def test_paths_and_names_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir(mode=0o700)
        link = Path(self.temp.name) / 'link'
        link.symlink_to(outside, target_is_directory=True)
        for root in (str(link), str(self.root / '..' / 'outside'), 'relative'):
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                DeletionLifecycle(root)
        for kind, oid in [('../x', ID), ('session', '../' + ID),
                          ('session', 'A'*32), ('session', 'a'*31),
                          ('', ID)]:
            with self.subTest(kind=kind, oid=oid), self.assertRaises(ValueError):
                self.lifecycle.tombstone(kind, oid)
        (self.root / '.deletion-lifecycle').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.lifecycle.tombstone('session', ID)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_and_hardlink_markers_rejected(self):
        with self.lifecycle.execution_lease('session', ID):
            pass
        infra = self.root / '.deletion-lifecycle'
        outside = Path(self.temp.name) / 'outside-file'
        outside.write_text('untouched')
        outside.chmod(0o600)
        marker = infra / ('session-' + ID + '.tombstone')
        marker.symlink_to(outside)
        with self.assertRaises(OSError):
            self.lifecycle.tombstone('session', ID)
        marker.unlink()
        os.link(outside, marker)
        with self.assertRaises(PermissionError):
            self.lifecycle.tombstone('session', ID)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_permissions_and_io_errors_propagate(self):
        self.root.chmod(0o755)
        with self.assertRaises(PermissionError):
            DeletionLifecycle(self.root)
        self.root.chmod(0o700)
        with mock.patch('deletion_lifecycle.os.mkdir', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.lifecycle.tombstone('session', ID)
        with mock.patch('deletion_lifecycle.os.fsync', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.lifecycle.tombstone('session', ID)

    def test_never_deletes_object(self):
        obj = self.root / ID
        obj.mkdir()
        (obj / 'payload').write_text('synthetic')
        self.lifecycle.tombstone('session', ID)
        self.assertEqual((obj / 'payload').read_text(), 'synthetic')


if __name__ == '__main__':
    unittest.main()
