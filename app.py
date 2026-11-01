"""Run from this directory: python app.py run --request 'Recommend five RAG papers'."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import uuid
from network import urlopen

from common import Budget, Config, DATA_DIR, GuideError, ROOT, RUNS_DIR, SOURCE_NAMES, utc_now
from report import markdown, save_run
from source import AcademicSource
from workflow import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='ScholarGuide: bounded literature discovery, no training required.')
    sub = parser.add_subparsers(dest='command', required=True)
    task = sub.add_parser('run', help='Live search + LLM; at most two search rounds.')
    group = task.add_mutually_exclusive_group(required=True)
    group.add_argument('--request', help='Natural-language request (parsed by the LLM).')
    group.add_argument('--request-file', type=Path, help='Structured research request JSON.')
    task.add_argument('--method', choices=['S', 'A'], default='A')
    task.add_argument('--source', choices=list(SOURCE_NAMES) + ['multi'])
    task.add_argument('--research-mode', action=argparse.BooleanOptionalAction, default=None,
                      help='Enable evidence-grounded research discovery and synthesis; default follows SG_RESEARCH_MODE (off).')
    task.add_argument('--no-cache', action='store_true', help='Disable retrieval cache for online timing.')
    task.add_argument('--timeout', type=float, help='Total task time budget in seconds.')
    task.add_argument('--out', type=Path, help='New output directory; existing results are never silently overwritten.')
    demo = sub.add_parser('demo', help='Offline, explicitly labelled fixture demo; no network or model.')
    demo.add_argument('--scenario', choices=['followup', 'satisfied', 'insufficient'], default='followup')
    demo.add_argument('--method', choices=['S', 'A'], default='A')
    demo.add_argument('--out', type=Path)
    lookup = sub.add_parser('lookup', help='Exact arXiv ID lookup; never substitutes fuzzy title matches.')
    lookup.add_argument('identifier')
    sub.add_parser('doctor', help='Check configuration; also check installed models when using Ollama.')
    web = sub.add_parser('web', help='Start the local browser frontend.')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--open', action='store_true', help='Open the browser automatically.')
    web.add_argument('--background', action='store_true', help='Run without a console window; save server output in the runs directory.')
    web.add_argument('--runs', type=Path, help='Run/session storage directory (default: ./runs).')
    chat = sub.add_parser('chat', help='Stateful multi-turn session (v2); feedback, ASK and resume.')
    chat.add_argument('--request', help='Initial natural-language request.')
    chat.add_argument('--request-file', type=Path, help='Initial structured request JSON.')
    chat.add_argument('--message', help='One message: the answer to an ASK, or feedback for a resumed session.')
    chat.add_argument('--script', type=Path, help='Non-interactive messages, one per line (blank lines ignored).')
    chat.add_argument('--session', help='Resume this session id (requires --state).')
    chat.add_argument('--state', type=Path, help='SQLite file for the session (reuse it to resume).')
    chat.add_argument('--out', type=Path, help='Output directory for per-turn result.json / report.md.')
    chat.add_argument('--demo', choices=['followup', 'ask'], help='Offline fixtures, no network or model.')
    chat.add_argument('--source', choices=list(SOURCE_NAMES) + ['multi'])
    chat.add_argument('--research-mode', action=argparse.BooleanOptionalAction, default=None,
                      help='Enable research discovery and synthesis; --no-research-mode overrides the environment.')
    chat.add_argument('--no-cache', action='store_true')
    chat.add_argument('--timeout', type=float)
    update = sub.add_parser('update', help='Check for or install a newer release; user data is preserved.')
    update.add_argument('--check', action='store_true', help='only report; change nothing')
    update.add_argument('--yes', action='store_true', help='install without asking')
    update.add_argument('--version', default='', help='install this tag instead of the latest')
    return parser


def _chat_messages(args) -> list[str]:
    if args.message:
        return [args.message]
    if args.script:
        text = args.script.read_text(encoding='utf-8-sig')
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]
    return []


def _chat_summary(result: dict) -> str:
    lines = [f"revision {result['revision']} · status={result['status']} · stop={result['stop_reason']}"]
    if result.get('question'):
        lines.append('QUESTION: ' + result['question'])
    for paper in result['papers']:
        lines.append(f"- {paper['title']} | {paper.get('category_status', 'unknown')} | "
                     f"verified={paper.get('evidence_verified', False)}")
    cost = result['metrics']['cost_units']['settled']
    lines.append(f"cost_units={cost} llm_calls={result['metrics']['llm_calls']} "
                 f"search_calls={result['metrics']['search_http_calls']} papers={len(result['papers'])}")
    reuse = result['metrics'].get('reuse')
    if reuse:
        reasons = {}
        for item in reuse.get('stale', []):
            reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
        reused = len(reuse.get('reused', []))
        stale = sum(reasons.values())
        if not reused and not stale:
            lines.append('reuse: n/a (no candidates yet)')
        else:
            tail = ' '.join(f'{k}:{v}' for k, v in sorted(reasons.items())) or 'none'
            lines.append(f"reuse: reused={reused} stale={stale} "
                         f"rate={reuse.get('reuse_rate')} reasons={tail}")
    return chr(10).join(lines)


def _chat_command(args) -> int:
    from state import StateStore

    config = Config() if args.demo else Config.from_env()
    config = replace(config, source=args.source or config.source, cache=not args.no_cache,
                     research_mode=config.research_mode if args.research_mode is None else args.research_mode,
                     task_timeout=args.timeout if args.timeout is not None else config.task_timeout)
    config.validate()

    if args.session and not args.state:
        raise GuideError('--session requires --state (the SQLite file that holds it).')
    state_path = args.state or RUNS_DIR / 'chat' / (uuid.uuid4().hex[:12] + '.sqlite3')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_path)
    try:
        return _chat_with_store(args, config, state_path, store)
    finally:
        store.close()


def _chat_with_store(args, config, state_path, store) -> int:
    from session import ChatSession

    source = None
    llm = None
    if args.demo:
        from demo import ChatDemoLLM, DemoSource
        source = DemoSource('followup')
        llm = ChatDemoLLM(ask_first=args.demo == 'ask')
    chat = ChatSession(config, store, args.session, source=source, llm=llm,
                       reuse=True)
    out = args.out or RUNS_DIR / 'chat' / chat.session_id
    out.mkdir(parents=True, exist_ok=True)
    (out / 'meta.json').write_text(json.dumps({
        'session_id': chat.session_id, 'state_path': str(state_path), 'mode': chat.mode,
        'created_at': utc_now(), 'demo': args.demo, 'reuse': True,
    }, ensure_ascii=False, indent=2) + chr(10), encoding='utf-8')

    messages = _chat_messages(args)
    turn = 0

    def save(result):
        nonlocal turn
        turn += 1
        save_run(result, out / f'turn-{turn}')
        print(_chat_summary(result))
        print(f'--- turn {turn} saved to {(out / f"turn-{turn}").resolve()}', file=sys.stderr)

    if chat.task():  # resumed session
        if not messages:
            raise GuideError('resuming a session needs --message or --script.')
        result = chat.send(messages.pop(0))
        save(result)
    elif args.request_file:
        request_data = json.loads(args.request_file.read_text(encoding='utf-8-sig'))
        result = chat.start(request_data=request_data)
        save(result)
    else:
        first = messages.pop(0) if messages else args.request
        if not first:
            raise GuideError('chat needs --request, --request-file, --script or interactive input.')
        result = chat.start(message=first)
        save(result)

    for message in messages:
        result = chat.send(message)
        save(result)

    interactive = not (args.request or args.request_file or args.message or args.script)
    if interactive:
        prefix = 'answer> ' if result['status'] == 'awaiting_user' else 'feedback> '
        while True:
            try:
                message = input(prefix).strip()
            except EOFError:
                break
            if not message or message.lower() in ('exit', 'quit'):
                break
            result = chat.send(message)
            save(result)
            prefix = 'answer> ' if result['status'] == 'awaiting_user' else 'feedback> '

    print(f'session_id={chat.session_id}')
    print(f'state={state_path.resolve()}')
    print(f'out={out.resolve()}')
    return 0 if result['status'] in ('complete', 'awaiting_user') else 2


def _background_web(args) -> int:
    """Detach the local service and retain logs for configuration/startup errors."""
    import os
    import subprocess
    from recovery import atomic_json

    if not 1 <= args.port <= 65535:
        raise GuideError('端口必须在 1–65535 之间。')
    directory = (args.runs or RUNS_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable)
    options = {'stdin': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        pythonw = executable.with_name('pythonw.exe')
        if pythonw.is_file():
            executable = pythonw
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        options['start_new_session'] = True
    command = [str(executable), str(ROOT / 'app.py'), 'web', '--port', str(args.port),
               '--runs', str(directory)]
    if args.open:
        command.append('--open')
    log_path = directory / f'service-{args.port}.log'
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    with log_path.open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                                   env=environment, **options)
    atomic_json(directory / f'service-{args.port}.json',
                {'pid': process.pid, 'port': args.port, 'log': log_path.name,
                 'started_at': utc_now(), 'background': True})
    print(f'ScholarGuide 后台进程已启动（PID {process.pid}）：http://127.0.0.1:{args.port}')
    print(f'启动和运行日志：{log_path}')
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    args = build_parser().parse_args(argv)
    if args.command == 'update':
        from update_cli import main as update_main
        forwarded = []
        if args.check:
            forwarded.append('--check')
        if args.yes:
            forwarded.append('--yes')
        if args.version:
            forwarded.extend(['--version', args.version])
        return update_main(forwarded)
    if args.command == 'web':
        try:
            if args.background:
                return _background_web(args)
            from webapp import serve
            serve(args.port, args.open, args.runs)
            return 0
        except GuideError as exc:
            print('Error: ' + str(exc), file=sys.stderr)
            return 1
    try:
        if args.command == 'chat':
            return _chat_command(args)
        config = Config() if args.command == 'demo' else Config.from_env()
        config.validate()
        if args.command == 'doctor':
            print(json.dumps({'python': sys.version.split()[0], 'config': config.public()}, ensure_ascii=False, indent=2))
            if config.provider == 'ollama':
                with urlopen(config.base_url + '/api/tags', timeout=5) as response:
                    available = [x['name'] for x in json.load(response).get('models', [])]
                print('Installed Ollama models: ' + ', '.join(available))
                if config.model not in available and config.model + ':latest' not in available:
                    raise GuideError('Configured model is not installed. Run: ollama pull ' + config.model)
                print('Model service reachable. Run a live task to verify inference and academic APIs.')
            else:
                print('OpenAI-compatible settings parsed. Connectivity/authentication are verified by a live task.')
            return 0
        if args.command == 'lookup':
            source = AcademicSource(config, Budget(config.task_timeout))
            papers = source.lookup(args.identifier)
            print(json.dumps([p.to_dict() for p in papers], ensure_ascii=False, indent=2))
            return 0 if papers else 2
        if args.out and args.out.exists() and any(args.out.iterdir()):
            raise GuideError('Output directory is not empty; choose a new path.')

        def progress(event):
            print(f"[{event['step']}] {event['action']}", file=sys.stderr, flush=True)

        if args.command == 'demo':
            from demo import demo_run
            result = demo_run(args.scenario, args.method, progress=progress)
        else:
            config = replace(config, source=args.source or config.source, cache=not args.no_cache,
                             research_mode=config.research_mode if args.research_mode is None else args.research_mode,
                             task_timeout=args.timeout if args.timeout is not None else config.task_timeout)
            request_data = None
            user_input = args.request
            if args.request_file:
                request_data = json.loads(args.request_file.read_text(encoding='utf-8-sig'))
                if not isinstance(request_data, dict):
                    raise GuideError('Request file must contain an object.')
                user_input = request_data.get('user_input', request_data.get('topic', ''))
            result = run(user_input, config, args.method, request_data=request_data, progress=progress)
        out = args.out or RUNS_DIR / result['run_id']
        save_run(result, out)
        print(markdown(result))
        print(f'Saved: {out.resolve()}', file=sys.stderr)
        return 0 if result['status'] == 'complete' else 2
    except (GuideError, ValueError, OSError) as exc:
        # OSError strings from network clients can include URLs; keep generic here.
        message = str(exc) if isinstance(exc, GuideError) else f'Unable to complete command ({type(exc).__name__}).'
        print('Error: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
