"""Repeatable local delivery checks. Does not call a paid model or deploy.

python scripts/verify_delivery.py --require-browser --screenshots
Each invocation writes a new evidence directory and exits nonzero on failure.
"""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def source_manifest():
    files = list(ROOT.glob('*.py')) + list(ROOT.glob('requirements*.txt'))
    files.extend([ROOT / 'state_schema.sql', ROOT / 'README.md', ROOT / '.env.example',
                  ROOT / 'scripts' / 'offline_test_site' / 'sitecustomize.py',
                  ROOT / 'tests' / 'fixtures' / 'offline_mcp_servers.json'])
    for directory, pattern in (('web', '*'), ('tests', '*.py'),
                               ('scripts', '*.py'), ('docs', '*.md'), ('vendor', '*.py'),
                               ('evaluation/engineering_agent', '*.py'),
                               ('evaluation/engineering_agent', '*.json'),
                               ('evaluation/engineering_agent', '*.md'),
                               ('.pi/agents', '*.md')):
        files.extend((ROOT / directory).glob(pattern))
    files.extend([ROOT / 'examples' / 'papers.json',
                  ROOT / 'evaluation' / 'engineering_agent' / 'fixtures' / 'papers-baseline.json'])
    return {file.relative_to(ROOT).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted(files) if file.is_file()}


def manifest_drift(before, after):
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def _installed_chromedriver(env):
    """Find an already-installed driver without invoking or modifying its manager."""
    candidate = shutil.which('chromedriver', path=env.get('PATH', ''))
    if candidate:
        return candidate
    executable = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
    cache = Path.home() / '.cache' / 'selenium' / 'chromedriver'
    try:
        candidates = [item for item in cache.rglob(executable) if item.is_file()]
    except OSError:
        return None
    def version_key(path):
        return tuple(int(part) for part in re.findall(r'\d+', path.parent.name))
    return str(max(candidates, key=version_key)) if candidates else None


def apply_browser_isolation(options, profile: Path):
    """Use a fresh Chrome profile and reject ordinary non-loopback traffic.

    This is defense in depth, not an operating-system network sandbox.
    """
    for flag in ('--user-data-dir=' + str(profile), '--disable-background-networking',
                 '--disable-sync', '--disable-quic', '--no-first-run',
                 '--proxy-server=http://127.0.0.1:1',
                 '--proxy-bypass-list=127.0.0.1;localhost'):
        options.add_argument(flag)


def isolated_environment(out: Path, inherited=None):
    """Keep offline verification away from user credentials and live ledgers."""
    env = dict(os.environ if inherited is None else inherited)
    # Selenium Manager can read user configuration, download binaries, prune its
    # cache, or send telemetry.  Never inherit its settings or use its real cache.
    for key in list(env):
        upper = key.upper()
        if upper.startswith('SE_'):
            del env[key]
        elif (upper.endswith(('_TOKEN', '_KEY')) or
              any(marker in upper for marker in ('API_KEY', 'ACCESS_KEY', 'SECRET',
                                                  'PASSWORD', 'CREDENTIAL'))):
            env[key] = ''
    for key in ('LUNE_API_KEY', 'LUNE_TOKEN', 'SG_API_KEY', 'OPENAI_API_KEY',
                'MINIMAX_API_KEY', 'ANTHROPIC_API_KEY', 'OPENALEX_API_KEY',
                'S2_API_KEY', 'SEMANTIC_SCHOLAR_API_KEY', 'DEEPSEEK_SEARCH_KEY',
                'DEEPSEEK_SEARCH_API_KEY', 'SG_SOURCE_PROXY'):
        env[key] = ''
    env.update({
        'SG_SOURCE_STORE': str(out / 'offline-sources.sqlite3'),
        'SG_LUNE_QUOTA_DB': str(out / 'offline-lune-quota.sqlite3'),
        'SG_LUNE_LEDGER': str(out / 'offline-lune-ledger.json'),
        'SG_MCP_TOKEN_FILE': str(out / 'offline-missing-token.json'),
        'SG_PROVIDER_STORE': str(out / 'offline-provider-store.json'),
        'SG_MEMORY_STORE': str(out / 'offline-memory.sqlite3'),
        'PI_CODING_AGENT_DIR': str(out / 'offline-pi-agent'),
        'SG_MCP_CONFIG': str(ROOT / 'tests' / 'fixtures' / 'offline_mcp_servers.json'),
        'SG_BASE_URL': 'http://127.0.0.1:1',
        'SG_SKIP_PROJECT_ENV': '1',
        'SG_OFFLINE_NETWORK_GUARD': '1',
        'PYTHONPATH': os.pathsep.join((str(ROOT / 'scripts' / 'offline_test_site'),
                                      str(ROOT), str(ROOT / 'tests'))),
        'SE_OFFLINE': 'true',
        'SE_AVOID_STATS': 'true',
        'SE_CACHE_PATH': str(out / 'offline-selenium-cache'),
        'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
        'http_proxy': '', 'https_proxy': '', 'all_proxy': '',
        'NO_PROXY': '127.0.0.1,localhost,::1',
        'no_proxy': '127.0.0.1,localhost,::1',
    })
    driver = _installed_chromedriver(env)
    if driver:
        # An explicit path avoids Selenium Manager and does not alter its cache.
        env['SE_CHROMEDRIVER'] = driver
    return env


def capture_ui(out):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from unittest.mock import patch
    from activity import ActivityFeed
    from providers import ProviderStore
    from webapp import ChatService, Jobs, create_server

    fixture = out / 'ui-fixture'
    providers = ProviderStore(fixture / 'providers.json')
    providers.upsert({'id': 'delivery-fixture', 'name': 'Offline verification fixture',
                      'base_url': 'http://127.0.0.1:1', 'model': 'demo-no-api'}, activate=True)
    chat = ChatService(fixture / 'web', providers=providers)
    jobs = Jobs(fixture, providers=providers)
    with patch.dict(os.environ, {'SG_SOURCE_STORE': str(fixture / 'sources.json')}):
        server = create_server(0, jobs=jobs, chat=chat,
                               activity=ActivityFeed(runs=fixture), providers=providers)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = Options()
    apply_browser_isolation(options, fixture / 'chrome-profile')
    for argument in ('--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars'):
        options.add_argument(argument)
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    driver = None
    records = []

    def frame_film(milliseconds):
        WebDriverWait(driver, 10).until(lambda d: d.execute_script(
            "return document.querySelector('#research-film')?.dataset.enhanced === 'true'"))
        driver.execute_script('''
            document.getElementById('research-film').dispatchEvent(
              new CustomEvent('scholarguide:film-frame', {detail:{time:arguments[0]}}));
        ''', milliseconds)
        WebDriverWait(driver, 10).until(lambda d: d.execute_script('''
            const film = document.getElementById('research-film');
            return Number(film.dataset.time) === arguments[0] && film.dataset.playing === 'false';
        ''', milliseconds))

    try:
        driver = webdriver.Chrome(options=options)
        base = f'http://127.0.0.1:{server.server_port}'
        session = chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})['session_id']
        pages = (('landing', '/', '#landing-main'),
                 ('search', '/search?demo=followup', '#paper-list article'),
                 ('chat', '/chat?session=' + session, '#turns .turn'),
                 ('activity', '/activity?session=' + session, '#activity-timeline li'),
                 ('settings', '/settings', '#provider-list button'),
                 ('sources', '/sources', '#sources-list'))
        screenshots = out / 'screenshots'
        screenshots.mkdir()
        scenarios = [(name, route, selector, theme, width)
                     for theme in ('light', 'dark') for name, route, selector in pages
                     for width in (1440, 1024, 390)]
        scenarios.extend((name, route, selector, 'light', width)
                         for name, route, selector in (
                             ('search-new', '/search', '.search-examples'),
                             ('chat-new', '/chat', '.chat-examples'))
                         for width in (1440, 390))
        for name, route, selector, theme, width in scenarios:
            if name.endswith('-new'):
                driver.execute_script('localStorage.clear(); sessionStorage.clear();')
            if driver.current_url.startswith(base):
                driver.execute_script("localStorage.setItem('scholarguide-theme',arguments[0]);", theme)
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
            driver.get(base + route)
            if driver.execute_script('return document.documentElement.dataset.theme') != theme:
                driver.execute_script("localStorage.setItem('scholarguide-theme',arguments[0]);", theme)
                driver.refresh()
            if driver.execute_script('return document.documentElement.dataset.theme') != theme:
                raise AssertionError(f'{name}: theme did not resolve to {theme}')
            WebDriverWait(driver, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, selector))
            if name == 'landing':
                # The public export event pauses a deterministic evidence frame.
                frame_film(12000)
            dimensions = driver.execute_script('return {width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth}')
            if dimensions['scroll'] > dimensions['width']:
                raise AssertionError(f'{name} at {width}px overflows: {dimensions}')
            errors = [row for row in driver.get_log('browser') if row['level'] == 'SEVERE']
            if errors:
                raise AssertionError(f'{name} browser console errors: {errors}')
            suffix = '-dark' if theme == 'dark' else ''
            path = screenshots / f'{name}-{width}{suffix}.png'
            shot = driver.execute_cdp_cmd('Page.captureScreenshot', {'captureBeyondViewport': True})
            path.write_bytes(base64.b64decode(shot['data']))
            records.append({'page': name, 'width': width, 'dimensions': dimensions,
                            'theme': theme,
                            'screenshot': path.relative_to(out).as_posix(), 'console_errors': errors,
                            'browser_version': driver.capabilities.get('browserVersion')})
        film_frames = out / 'film-frames'
        film_frames.mkdir()
        for width, theme in ((1440, 'light'), (390, 'dark')):
            driver.execute_script("localStorage.setItem('scholarguide-theme',arguments[0]);", theme)
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
            driver.get(base + '/')
            for label, milliseconds in (('typing', 1600), ('request', 6500), ('evidence', 12000),
                                         ('synthesis', 20000), ('followup', 28500)):
                frame_film(milliseconds)
                element = driver.find_element(By.ID, 'research-film')
                path = film_frames / f'{label}-{width}-{theme}.png'
                clip = driver.execute_script('const r=arguments[0].getBoundingClientRect();return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1}', element)
                frame = driver.execute_cdp_cmd('Page.captureScreenshot', {'captureBeyondViewport': True, 'clip': clip})
                path.write_bytes(base64.b64decode(frame['data']))
                dimensions = driver.execute_script('return {width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth}')
                if dimensions['scroll'] > dimensions['width']:
                    raise AssertionError(f'Film {label} overflows at {width}px: {dimensions}')
                errors = [row for row in driver.get_log('browser') if row['level'] == 'SEVERE']
                if errors:
                    raise AssertionError(f'Film {label} console errors: {errors}')
                records.append({'page': 'film-' + label, 'width': width, 'theme': theme,
                    'time_ms': milliseconds, 'dimensions': dimensions,
                    'screenshot': path.relative_to(out).as_posix(), 'console_errors': errors,
                    'browser_version': driver.capabilities.get('browserVersion')})
    finally:
        if driver is not None:
            driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(3)
        chat.close()
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='New, empty evidence directory.')
    parser.add_argument('--require-browser', action='store_true', help='Fail if Selenium/Chrome tests cannot run.')
    parser.add_argument('--screenshots', action='store_true', help='Capture six local product pages, including Sources, in light/dark at 1440/1024/390px plus first-use screens, using offline data.')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = (args.out or ROOT / 'runs' / 'verification' / stamp).resolve()
    if out.exists() and any(out.iterdir()):
        parser.error('--out must be new or empty; evidence is never overwritten.')
    out.mkdir(parents=True, exist_ok=True)
    env = isolated_environment(out)
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    if args.require_browser:
        env['SG_REQUIRE_BROWSER'] = '1'
    # Legacy variable name belongs to a product UI fixture, not the archived suite.
    env['SG_RESEARCH_SCREENSHOTS_DIR'] = str(out / 'product-browser' / 'screenshots')
    env['SG_EXPERIENCE_SHOTS'] = str(out / 'experience-browser' / 'screenshots')
    manifest = source_manifest()
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'platform': platform.platform(),
              'require_browser': args.require_browser, 'packages': {}, 'checks': [],
              'source_sha256': manifest, 'mode': 'offline engineering checks/local HTTP fixtures; no paid calls'}
    for package in ('langgraph', 'langgraph-checkpoint-sqlite', 'selenium'):
        try:
            report['packages'][package] = version(package)
        except PackageNotFoundError:
            report['packages'][package] = None

    def checkpoint():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def command(name, cmd, expected=0, timeout=300):
        started = time.monotonic()
        log = out / f'{name}.log'
        print(f'Running {name}...', flush=True)
        try:
            with log.open('w', encoding='utf-8') as stream:
                result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=timeout)
            text = log.read_text(encoding='utf-8')
            check = {'name': name, 'passed': result.returncode == expected,
                     'command': cmd,
                     'exit_code': result.returncode, 'expected_exit': expected,
                     'seconds': round(time.monotonic() - started, 3), 'log': log.name}
            if name == 'python-tests':
                count = re.search(r'Ran (\d+) tests?', text)
                check['tests'] = int(count.group(1)) if count else None
                skips = re.search(r'skipped=(\d+)', text)
                check['skipped'] = int(skips.group(1)) if skips else 0
                browser_module_names = {
                    file.stem for file in (ROOT / 'tests').glob('test_*.py')
                    if any(marker in file.read_text(encoding='utf-8-sig')
                           for marker in ('selenium', 'browser_support', 'test_ui_browser import'))
                }
                test_modules = re.findall(r'^test_.*\((test_[a-z0-9_]+)\.', text, re.MULTILINE)
                browser_classes = [module for module in test_modules if module in browser_module_names]
                check['browser_tests'] = len(browser_classes)
                check['browser_modules'] = {module: browser_classes.count(module) for module in sorted(set(browser_classes))}
                if args.require_browser and (check['skipped'] or check['tests'] is None
                                             or not check['browser_tests']):
                    check['passed'] = False
                product_shots = sorted((out / 'product-browser' / 'screenshots').glob('*.png'))
                check['product_screenshots'] = [path.relative_to(out).as_posix() for path in product_shots]
                check['browser_evidence_mode'] = 'offline product fixtures in real Chrome'
        except (OSError, subprocess.TimeoutExpired) as exc:
            check = {'name': name, 'passed': False, 'error': str(exc), 'log': log.name}
        report['checks'].append(check)
        checkpoint()
        print(f"{name}: {'PASS' if check['passed'] else 'FAIL'}", flush=True)

    checkpoint()
    command('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], timeout=720)
    try:
        python_files = [ROOT / name for name in manifest if name.endswith('.py')]
        for file in python_files:
            ast.parse(file.read_text(encoding='utf-8-sig'), filename=str(file))
        report['checks'].append({'name': 'python-syntax', 'passed': True, 'files': len(python_files)})
    except (SyntaxError, OSError) as exc:
        report['checks'].append({'name': 'python-syntax', 'passed': False, 'error': str(exc)})
    node = shutil.which('node') or 'node'
    command('node-version', [node, '--version'], timeout=30)
    javascript = list((ROOT / 'web').glob('*.js'))
    javascript.extend((ROOT / 'scripts').glob('*.mjs'))
    for index, file in enumerate(sorted(javascript), start=1):
        command(f'js-{index:02}-{file.stem}', [node, '--check', str(file)], timeout=30)
    for scenario, expected in (('satisfied', 0), ('followup', 0), ('insufficient', 2)):
        command('cli-' + scenario, [sys.executable, 'app.py', 'demo', '--scenario', scenario,
                                   '--out', str(out / ('cli-' + scenario))], expected=expected, timeout=30)
    if args.screenshots:
        try:
            from unittest.mock import patch
            with patch.dict(os.environ, env, clear=True):
                report['screenshots'] = capture_ui(out)
            report['checks'].append({'name': 'responsive-screenshots', 'passed': True,
                                     'count': len(report['screenshots'])})
        except Exception as exc:
            report['checks'].append({'name': 'responsive-screenshots', 'passed': False,
                                     'error': f'{type(exc).__name__}: {exc}'})
    changed = manifest_drift(manifest, source_manifest())
    report['source_changed_during_verification'] = changed
    report['passed'] = all(check['passed'] for check in report['checks']) and not changed
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {out / 'summary.json'}", flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
