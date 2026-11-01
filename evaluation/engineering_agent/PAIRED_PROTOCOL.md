# Offline paired engineering pilot

Status: opt-in, infrastructure only. No verified multi-agent arm exists here; the specialist components are not relabelled as agents. A comparison is unavailable and cannot pass. This is not a scientific quality scorer or a formal research dataset.

## Fixed protocol

`paired_cases.json` fixes two dev tasks and one visible holdout task. Each requests a count and category quota from the existing official `examples/papers.json` demo fixture. The tasks are synthetic and illustrative, but the bundled fixture contains metadata from real papers; it is **not** a synthetic paper corpus or scientific ground truth. Reports record `synthetic_tasks: true`, `synthetic: false`, and `paper_fixture_kind: bundled_real_metadata`. Each child reads the fixture bytes once, checks the pinned SHA-256, and uses the resulting in-memory snapshot both for its `DemoSource` and its independent evidence oracle. The single arm actually calls `workflow.run` with `DemoLLM` and method `S`. Input identity hashes the complete case, shared budget, allowlist and fixture hash. The runner pins both manifest bytes and canonical content. Changing fixed inputs requires a consciously versioned protocol and updated pinned hashes; no silent changes.

Each arm is assigned the same 30-second deadline, one source search, two fake-model calls and a configured 3000 maximum output tokens, with only `search` and `assess` allowed. Separate attempt counters increment before raising, and final checks independently reject excess or denied calls even when the workflow swallows an exception. A passing result also requires workflow status `complete` and stop reason `requirements_met`. The absent multi arm has no execution or measured budget compliance. A future real multi-agent adapter must enforce the same shared limits across all agents, tools, retries and subprocesses; it cannot be marked completed without observable real execution. Demo token use is not measured: `token_limit_status` is `unmeasured`, token usage and monetary cost are unknown, and no actual token-limit compliance is claimed. Elapsed wall-clock time is an observation, not an independently verified deadline or cost guarantee.

Every executed task gets a fresh temporary directory and subprocess, an `isolated_environment` constructed from a minimal environment, disabled project dotenv and a network guard; the worker also blocks socket operations. Config, caches and state are not inherited from real sessions. The report includes before/after per-file SHA-256 for root Python sources, the runner, isolation helper and sitecustomize, plus Python/platform and installed distribution versions. Observed source drift fails the single-arm checks. The hashes do not prove which bytes Python executed under malicious concurrent changes. The runner records checks, attempt counts, elapsed time and references to existing cancellation, interruption/recovery and session-restoration tests. Each reference has `executed: false`; these tests do not run in the pilot. Fixture summaries, paper text, raw model outputs, credentials and private transcripts are not written to evidence. Failure rows are retained; errors are class-only to avoid leaking content.

Evidence checks require unique selected and candidate IDs, all present in the pinned fixture with exactly matching abstracts, and each delivered `evidence_verified` quote at least 12 characters and a substring of that fixture abstract. This is attribution and lexical matching only, not semantic entailment, factual correctness, independent paper verification or quality. Zero delivered papers cannot pass evidence. Count, quota and supplied year/venue filters are evaluated separately.

## Execution

The parent Python process itself is not initialized in isolation. Launch the parent as a subprocess with `scripts.verify_delivery.isolated_environment` built from a minimal inherited environment, set HOME/USERPROFILE/TEMP/TMP to a fresh temporary directory, and retain the returned `PYTHONPATH` and isolated paths. Do not run this pilot with user credentials or network access; use an OS-enforced sandbox when those guarantees are required. The guard is Python-level defense in depth, not an OS sandbox. The command line below is the argument vector to that isolated subprocess, not an instruction to run directly in an ordinary terminal.

Use a new output file under the repository's `runs/verification` or the OS temporary directory. Resolved paths escaping those roots through symlinks are rejected, and existing files are never overwritten. The runner reserves the output before execution; an abrupt process termination can leave an incomplete file and must be reported, not counted as a result. The directories remain shared on the local machine: path checks and exclusive creation are not a sandbox against malicious concurrent writers.

```text
python -B evaluation/engineering_agent/run_paired.py --pilot --split dev --output runs/verification/NEW-DIRECTORY/paired.json
```

For example, from an already trusted launcher, create the parent subprocess as follows (the temporary output is removed when the context exits; use a new allowed repository output path to retain it):

```python
import os, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from scripts.verify_delivery import isolated_environment

with tempfile.TemporaryDirectory(prefix='sg-pilot-') as directory:
    tmp = Path(directory)
    inherited = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'COMSPEC') if k in os.environ}
    with patch('scripts.verify_delivery._installed_chromedriver', return_value=None):
        env = isolated_environment(tmp, inherited)
    env.update(HOME=directory, USERPROFILE=directory, TEMP=directory, TMP=directory,
               PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, '-B', 'evaluation/engineering_agent/run_paired.py',
                    '--pilot', '--split', 'dev', '--output', str(tmp / 'pilot.json')], env=env)
```

For final inspection use `--split all` once with another new path; the holdout is visible and tiny, not blind. The process exits nonzero even when every single-arm check passes, because multi-agent comparison is unavailable. `denominator.tasks` is the number of selected cases, `planned_arms` is twice that, and the report separately lists completed/passed single arms and unavailable multi arms. A failed worker is an error row, not an excluded case. No paired effect, significance test or superiority claim can be calculated while an arm is absent. Even after implementation, three non-independent, scripted cases provide no representative population inference, blind review or meaningful statistical power; record all failures and denominators, and obtain separately authorized expert review and a suitable evaluation design before research-quality claims.

The guard is Python-level defense in depth, not an OS network sandbox; future adapters that execute native code or spawn other processes need their own enforced sandbox and must not be enabled based on this guard alone. The protocol does not run paid models, access live sources, or validate multi-agent cancellation and recovery.
