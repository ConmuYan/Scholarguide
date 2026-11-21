# Python implementation modules

These files retain their original top-level import names; only their source-control location changed.

Install from the repository root with `python -m pip install -r requirements.txt` to make them available to scripts, tests, and development tools. `app.py` also adds this directory for direct source launches. GitHub release archives copy these modules back to the original flat layout, so installers and existing deployed commands are unchanged.

Do not rename module import paths without updating the tests, source export allow-list, and release packaging contract.
