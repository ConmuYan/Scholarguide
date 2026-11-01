"""Windows background service and native HTTP workers must own no console."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import network

ROOT = Path(__file__).resolve().parents[1]


def _inspect_background_http_worker(connection, _payload, _deadline):
    """Exercise the actual urlopen spawn boundary without external networking."""
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetConsoleWindow.restype = ctypes.c_void_p
    body = json.dumps({'executable': sys.executable,
                       'console_window': kernel.GetConsoleWindow() or 0}).encode()
    try:
        connection.send({'kind': 'response', 'status': 200, 'headers': [], 'body': body})
    finally:
        connection.close()


def _stop_owned_service(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x0001 | 0x00100000, False, pid)
    if handle:
        try:
            kernel.TerminateProcess(handle, 0)
            kernel.WaitForSingleObject(handle, 5000)
        finally:
            kernel.CloseHandle(handle)


@unittest.skipUnless(sys.platform == 'win32', 'Windows console behavior only')
class WindowsBackgroundTests(unittest.TestCase):
    def test_http_spawn_uses_matching_pythonw_without_a_console(self):
        self.assertTrue(Path(sys.executable).with_name('pythonw.exe').is_file(),
                        'The Windows test requires the matching pythonw interpreter.')
        with patch.object(network, '_http_worker', _inspect_background_http_worker):
            with network.urlopen('http://unused.test/background-worker', timeout=5) as response:
                worker = json.loads(response.read())
        self.assertEqual(Path(worker['executable']).name.lower(), 'pythonw.exe')
        self.assertEqual(worker['console_window'], 0)
        self.assertFalse([child for child in network.multiprocessing.active_children()
                          if child.name == 'ScholarGuide HTTP'])

    def test_background_cli_returns_and_leaves_a_usable_service_with_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            pid = None
            marker = runs / f'service-{port}.json'
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'app.py'), 'web', '--background',
                     '--port', str(port), '--runs', str(runs)],
                    cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                    env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                if marker.exists():
                    pid = json.loads(marker.read_text(encoding='utf-8'))['pid']
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(pid)
                self.assertNotEqual(pid, os.getpid())
                self.assertTrue(json.loads(marker.read_text(encoding='utf-8'))['background'])
                opener = build_opener(ProxyHandler({}))
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/openapi.json', timeout=1) as response:
                            schema = json.load(response)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('Background service did not start: ' +
                                      (runs / f'service-{port}.log').read_text(encoding='utf-8'))
                        time.sleep(.1)
                self.assertEqual(schema['openapi'], '3.1.0')
                self.assertIn('/api/v1/search', schema['paths'])
                self.assertTrue((runs / f'service-{port}.log').is_file())
            finally:
                if pid is None and marker.exists():
                    pid = json.loads(marker.read_text(encoding='utf-8'))['pid']
                if pid is not None:
                    _stop_owned_service(pid)


if __name__ == '__main__':
    unittest.main()
