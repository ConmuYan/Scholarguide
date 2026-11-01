# Install a release

ScholarGuide publishes versioned source bundles and native per-user installers on GitHub Releases. The public repository slug is set by the release workflow; the slug is `ConmuYan/Scholarguide`.

## macOS and Linux

One-command install:

```sh
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/download/vX.Y.Z/install.sh | sh
```

The script downloads the versioned source archive and SHA-256 manifest over HTTPS, verifies the archive, creates a Python virtual environment under the user's data directory, and installs the pinned runtime dependencies. It does not require administrator permissions.

For review-before-execution, download the installer and inspect it first:

```sh
curl -fsSLO https://github.com/ConmuYan/Scholarguide/releases/download/vX.Y.Z/install.sh
less install.sh
sh install.sh
```

## Windows PowerShell

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/download/vX.Y.Z/install.ps1 | iex
```

For review-before-execution, save and inspect `install.ps1` before running it. The installer uses the current user's `%LOCALAPPDATA%` for app versions and `%USERPROFILE%\.scholarguide` for data; it adds a user-level `bin` directory to PATH and does not modify machine-wide settings.

## First launch

```text
scholarguide web --open
```

The local Web/API binds to loopback only. Configure a model and optional source credentials in Models and Sources. Use offline Demo before making any live provider calls. API keys and run data stay on that user's machine unless the user independently configures an external endpoint.

The CLI entry point also accepts existing commands, for example `scholarguide doctor` and `scholarguide demo --scenario satisfied`.

## Upgrade and data

Each version is installed into a separate version directory; the launch wrapper points to the newly installed release. User settings and history stay under `~/.scholarguide` (Windows: `%USERPROFILE%\.scholarguide`) and are not removed by install or upgrade. The release installer refuses to overwrite an already installed version directory. To switch versions, rerun that version's installer; inspect and back up user data before manually removing old versions.

## Build a release

After creating the public GitHub repository and reviewing licensing and release contents:

```bash
python scripts/build_release.py --repository ConmuYan/Scholarguide --version v0.1.0 --out dist
```

This produces a strict allow-listed source archive, Unix and Windows installers, and `SHA256SUMS`. The build rejects nonempty output directories. It excludes `.env`, `runs/`, tests, evaluations, research archives, local agent configuration, PDFs, and old Cloudflare demos. This release asset is suitable for installation, not a complete research/development checkout.

A release tag `v*` triggers `.github/workflows/release.yml`. The workflow runs the offline release-package test and publishes these assets. The local source checkout is not automatically uploaded; the GitHub repository must be created and its clean source committed separately.

## Trust and compatibility

The checksum detects corruption and mismatch between the downloaded bundle and manifest; since both are downloaded from the same GitHub release, it is not an independent signature. Review installers and release provenance. The source bundle is plain source code; it does not include a Python interpreter.

ScholarGuide's original code uses the MIT license. The vendored `gptr_arxiv.py` retains its upstream Apache-2.0 license. Source logos are third-party marks, not covered by MIT; review their owners' terms before redistributing them publicly. See `NOTICE.md`.
