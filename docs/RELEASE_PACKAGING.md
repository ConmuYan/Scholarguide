# ScholarGuide End-User Release

This repository currently has no GitHub remote. This file documents how to build the versioned end-user release after a clean public source repository is created.

From a clean checkout at tag `v0.1.0`:

```bash
python scripts/build_release.py --repository ConmuYan/Scholarguide --version v0.1.0 --out dist
```

The builder uses an explicit runtime allow-list. It copies the app modules, static web UI, required sample/schema/config data, API documentation, MIT license, Apache-2.0 notice, and third-party marks as-is. It never archives the working tree wholesale. `.env`, `.pi`, `.codex`, `runs/`, PDFs, tests, evaluation outputs, research archive, and old Cloudflare demos are excluded from the install bundle.

The command produces:

- `scholarguide-v0.1.0.tar.gz`
- `install.sh` for macOS/Linux
- `install.ps1` for Windows PowerShell
- `SHA256SUMS` for the bundle and installer scripts

Attach all four files to the matching GitHub Release. The generated installers embed the exact repository and version. The user-facing command is:

```bash
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.ps1 | iex
```

The current `install/*` files are templates and cannot be run before the repository slug and version are substituted by this builder. The release installer downloads a matching checksum manifest, validates the source bundle, creates a per-user Python virtual environment, and keeps runtime data under `~/.scholarguide`. It does not upload API keys or configure a shared model.

Before making the source repository public, curate its files separately: this working directory contains local `.env`, run records, agent configuration, course PDFs, legacy deployment files, and user-specific historical docs. The release builder intentionally does not publish those items. The public source repository should be initialized from reviewed source, include the test suite and contributor/security docs, and then be tagged; do not run `git add .` against the unreviewed workspace.

The MIT license covers original ScholarGuide code only. `vendor/gptr_arxiv.py` retains its upstream Apache-2.0 license. The landing page includes third-party academic-source marks whose redistribution terms are separate; verify those terms before attaching the public release archive. This build process does not grant rights in those marks.

## Release test

```bash
python -m unittest -v tests.test_release_package
```

This builds only into a new temporary directory, extracts the bundle, starts the offline Demo CLI, and checks for excluded local material. It does not publish, contact GitHub, call models/sources, or inspect/write real user stores.
