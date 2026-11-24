# Legacy Python imports

The 53 aliases in `legacy/` let existing scripts use former top-level names such as `import session`. Each alias resolves to the same module object as its `scholarguide.*` counterpart. The documented CLI entry points still work without this setup.

From a source or extracted-release directory, enable the aliases for the current shell:

```bash
export PYTHONPATH="$PWD/compat/legacy:$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

PowerShell:

```powershell
$env:PYTHONPATH = "$PWD\compat\legacy;$PWD;$env:PYTHONPATH"
```

For a dedicated virtual environment, you may instead put both absolute directory paths, one per line, in a `.pth` file in that environment's `site-packages`. This is an opt-in compatibility layer; interpreters that ignore `PYTHONPATH` and do not load that environment's `site-packages` need an explicit search path. New code should import from `scholarguide` directly.
