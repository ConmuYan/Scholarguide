# ScholarGuide

A local research assistant for finding papers, checking abstract-level evidence, and carrying a reading question into a continuing conversation. ScholarGuide runs on your computer. You configure your own model provider and any source credentials; API keys stay in your local profile store.

## Install

Choose a versioned release command from the GitHub Releases page.

macOS / Linux:

```bash
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/latest/download/install.ps1 | iex
```

The installer downloads the matching release archive, verifies its SHA-256 checksum, creates a per-user virtual environment under your home directory, and installs the pinned dependencies. It does not require administrator rights. Review the installer before piping it into a shell, or download it first and inspect it.

Requirements: Python 3.11 or newer. The Windows installer looks for `py -3` or `python`; macOS/Linux looks for `python3` or `python`.

Start the local web app:

```bash
scholarguide web --open
```

If `scholarguide` is not on `PATH`, restart the terminal or add the installer's user-level bin directory to `PATH`. Data is stored separately in `~/.scholarguide` (Windows: `%USERPROFILE%\.scholarguide`) and is not removed by installing a new version.

## Configure

1. Open the local page printed by the command.
2. Go to **Models**, add your provider's API key, and test the connection.
3. Go to **Sources** to configure optional source credentials. Use offline Demo first if you do not want to make provider calls.

Keep it current with `scholarguide update --check` / `scholarguide update --yes`; your keys, profiles, quota ledger and history in `~/.scholarguide` are never rewritten.

Keys are stored locally under your user profile. Search and Chat may call your selected model and academic sources and can incur provider charges or use account quotas. ScholarGuide does not provide shared API credits.

## API

The local API is available at `http://127.0.0.1:8765` by default. Open `http://127.0.0.1:8765/openapi.json` for the machine-readable contract. The documented versioned endpoints include `/api/v1/search`, `/api/v1/chat`, and `/api/v1/operations/{id}`. [Installation guide](docs/INSTALL.md) | [API contract](docs/API.md)

The service listens on loopback only and does not provide user authentication. Do not expose its port to the public internet. For a team deployment, put it behind an authenticated gateway and enforce per-user access and budgets.

## License and Notices

Original ScholarGuide code is licensed under MIT. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). Adapted and third-party assets/dependencies retain their own terms; review the notices before redistribution.

**The versioned release bundle is for end-user installation.** It does not include FrontierLitBench data or evaluation tools. Get these from the [source repository](https://github.com/ConmuYan/Scholarguide), checking out the tag matching your installed release:

```bash
git clone https://github.com/ConmuYan/Scholarguide.git
cd Scholarguide
git checkout <release-tag>
```

See [`frontierlitbench/EVALUATION_GUIDE.md`](https://github.com/ConmuYan/Scholarguide/blob/main/frontierlitbench/EVALUATION_GUIDE.md) for the reviewed FB-1.1 tasks, ChatGPT and other product imports, scoring, and reusable baseline procedure.
