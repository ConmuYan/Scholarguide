# Install a release

ScholarGuide publishes versioned source bundles and native per-user installers on GitHub Releases. The public repository slug is set by the release workflow; replace `ConmuYan/Scholarguide` below with that slug after the repository is created.

## macOS and Linux

One-command install:

```sh
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.sh | sh
```

The script downloads the versioned source archive and SHA-256 manifest over HTTPS, verifies the archive, creates a Python virtual environment under the user's data directory, and installs the pinned runtime dependencies. It does not require administrator permissions.

For review-before-execution, download the installer and inspect it first:

```sh
curl -fsSLO https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.sh
less install.sh
sh install.sh
```

## Windows PowerShell

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.ps1 | iex
```

For review-before-execution, save and inspect `install.ps1` before running it. The installer uses the current user's `%LOCALAPPDATA%` for app versions and `%USERPROFILE%\.scholarguide` for data; it adds a user-level `bin` directory to PATH and does not modify machine-wide settings.

## First launch

```text
scholarguide web --open
```

The local Web/API binds to loopback only. Configure a model and optional source credentials in Models and Sources. Use offline Demo before making any live provider calls. API keys and run data stay on that user's machine unless the user independently configures an external endpoint.

The CLI entry point also accepts existing commands, for example `scholarguide doctor` and `scholarguide demo --scenario satisfied`.

## Upgrade

```bash
scholarguide update --check    # report the latest release, change nothing
scholarguide update --yes      # install it
```

`update` downloads that release's own installer, verifies its SHA-256 against the release `SHA256SUMS`, and runs it. The installer keeps every version in its own directory and only moves the `current-version` marker, so:

- keys, provider profiles, the quota ledger, the quota schedule and all run history live in `~/.scholarguide` (`%USERPROFILE%\.scholarguide`) and are **not** read, rewritten or deleted by an update;
- the previously installed version stays on disk, which makes a manual rollback possible;
- nothing is upgraded unless you pass `--yes`; a network failure or checksum mismatch leaves the current version running;
- running `update` from a source checkout is refused with a hint to use `git pull` instead.

Restart the service afterwards so the new build is loaded.

## Upgrading from v0.1.0 or v0.1.1

`scholarguide update` shipped in **v0.1.2**, so the first hop has to be a normal install. Run the current installer once; it adds the new version next to the old one and repoints the launcher:

```bash
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.sh | sh
scholarguide update --check     # from here on, updates are one command
```

Windows:

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.ps1 | iex
scholarguide update --check
```

What stays exactly as it was: everything in `~/.scholarguide` (`%USERPROFILE%\.scholarguide`) — API keys, provider profiles, the Lune quota ledger and schedule, saved sessions and run history. The installer only adds `releases/vX.Y.Z/` and rewrites the `current-version` marker; the old version directory is left in place.

> Windows users who installed v0.1.0–v0.1.2 with Git Bash: those releases looked for the install directory under `%LOCALAPPDATA%`, which does not match the shell installer's layout. v0.1.3 detects both layouts and exports `SG_INSTALL_ROOT` from the launcher, so `scholarguide update` works after this one install.

## Upgrade and data

Each version is installed into a separate version directory; the launch wrapper points to the newly installed release. User settings and history stay under `~/.scholarguide` (Windows: `%USERPROFILE%\.scholarguide`) and are not removed by install or upgrade. The release installer refuses to overwrite an already installed version directory. To switch versions, rerun that version's installer; inspect and back up user data before manually removing old versions.

## Build a release

After creating the public GitHub repository and reviewing licensing and release contents:

```bash
python scripts/build_release.py --repository ConmuYan/Scholarguide --version v0.1.4 --out dist
```

This produces a strict allow-listed source archive, Unix and Windows installers, and `SHA256SUMS`. The build rejects nonempty output directories. It excludes `.env`, `runs/`, tests, evaluations, research archives, local agent configuration, PDFs, and old Cloudflare demos. This release asset is suitable for installation, not a complete research/development checkout.

A release tag `v*` triggers `.github/workflows/release.yml`. The workflow runs the offline release-package test and publishes these assets. The local source checkout is not automatically uploaded; the GitHub repository must be created and its clean source committed separately.

## Trust and compatibility

The checksum detects corruption and mismatch between the downloaded bundle and manifest; since both are downloaded from the same GitHub release, it is not an independent signature. Review installers and release provenance. The source bundle is plain source code; it does not include a Python interpreter.

ScholarGuide's original code uses the MIT license. The vendored `gptr_arxiv.py` retains its upstream Apache-2.0 license. Source logos are third-party marks, not covered by MIT; review their owners' terms before redistributing them publicly. See `NOTICE.md`.
