"""Run engineering tests with a durable completion marker and a finite deadline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    root = Path(__file__).resolve().parents[1]
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'runs/upgrade-20261028/engineering-1'
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', SG_REQUIRE_BROWSER='1')
    cmd = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
    try:
        with (out / 'tests.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=720)
        code = result.returncode
    except subprocess.TimeoutExpired:
        code = 124
    (out / 'exit.json').write_text(json.dumps({'exit_code': code, 'seconds': time.time() - started,
                                             'command': cmd}), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
