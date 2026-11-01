"""Real Chrome uses a disposable profile and denies a synthetic external host."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts.verify_delivery import apply_browser_isolation, isolated_environment

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
except ImportError:
    webdriver = None


class IsolatedBrowserNetworkTests(unittest.TestCase):
    def test_local_fixture_allowed_and_synthetic_external_host_blocked(self):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                self.fail('Required Selenium is unavailable')
            self.skipTest('Selenium is unavailable')
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'local fixture')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        driver = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = isolated_environment(root)
                with patch.dict(os.environ, env, clear=True):
                    options = Options()
                    for flag in ('--headless=new', '--no-sandbox', '--disable-gpu',
                                 '--host-resolver-rules=MAP fixture.invalid 127.0.0.1'):
                        options.add_argument(flag)
                    apply_browser_isolation(options, root / 'profile')
                    try:
                        driver = webdriver.Chrome(options=options)
                    except WebDriverException as exc:
                        if required:
                            raise AssertionError('Required isolated Chrome unavailable') from exc
                        self.skipTest('Isolated Chrome unavailable')
                    try:
                        driver.get(f'http://fixture.invalid:{server.server_port}/external-probe')
                    except WebDriverException:
                        pass  # Expected: the configured local discard proxy is unavailable.
                    self.assertNotIn('/external-probe', seen)
                    driver.get(f'http://127.0.0.1:{server.server_port}/allowed')
                    self.assertIn('/allowed', seen)
                    self.assertTrue((root / 'profile').is_dir())
                    driver.quit()
                    driver = None
        finally:
            if driver is not None:
                driver.quit()
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    unittest.main()
