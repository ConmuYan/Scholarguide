"""Import-time seam that lets MCP providers join the retriever without editing core.

Why this file exists
--------------------
Every runtime module (``source.py``, ``common.py``, ``webapp.py``, ``agent_tools.py``…)
is frozen by ``core_lock.json``. Adding a provider therefore cannot be a patch to
those files; it has to be a *runtime registration* performed by a non-core module
before any search happens.

What it touches
---------------
Exactly two symbols on the engine path:

1. ``source.PROVIDERS['<name>']`` — a dict insert, so ``search_batch`` will accept
   the provider (it filters with ``p in PROVIDERS``) and ``agent_tools`` will list
   it (it iterates ``PROVIDERS.items()``).
2. ``source.AcademicSource._search_one`` — wrapped once, so a registered handler
   answers for its provider and every other provider falls through to the original
   method unchanged.

The CLI/Web entry points additionally validate ``--source`` against
``common.SOURCE_NAMES``. That tuple is imported by value into several modules, so it
cannot be extended from here; the launcher ``app_mcp.py`` rebinds it in the two
modules that validate user input. Nothing is patched globally on import.

Ordering requirement: this module must be imported before the first search. Anything
that searches without importing it simply never sees MCP providers — the failure mode
is "provider absent", never "provider half-registered".
"""
from __future__ import annotations

import inspect
from typing import Callable

from common import GuideError, Paper
import source as _source

#: provider name -> handler(query, limit, filters) -> list[Paper]
_HANDLERS: dict[str, Callable] = {}
_REGISTERED_PROVIDERS: set[str] = set()

#: Every symbol this seam changes, in the order it changes it. The spike proof and
#: the docs read this list instead of restating it from memory.
MUTATIONS: list[str] = []

_ORIGINAL_SEARCH_ONE = None


def register(name: str, spec, handler: Callable[[str, int, dict | None], list[Paper]]) -> None:
    """Attach ``handler`` to ``name`` and publish the provider to the retriever."""
    name = str(name or '').strip()
    if not name:
        raise GuideError('An MCP provider needs a name.')
    if not callable(handler):
        raise GuideError(f'Provider {name} needs a callable handler.')
    if name in _HANDLERS:
        raise GuideError(f'Provider {name} is already registered.')
    if name not in _source.PROVIDERS:
        _source.PROVIDERS[name] = spec
        _REGISTERED_PROVIDERS.add(name)
        MUTATIONS.append(f"source.PROVIDERS[{name!r}]")
    _HANDLERS[name] = handler


def handlers() -> dict[str, Callable]:
    return dict(_HANDLERS)


def _dispatch(self, name: str, query: str, limit: int, filters: dict | None = None):
    handler = _HANDLERS.get(name)
    if handler is not None and name != 'lune':
        try:
            accepts_budget = 'budget' in inspect.signature(handler).parameters
        except (TypeError, ValueError):
            accepts_budget = False
        if accepts_budget:
            return handler(query, limit, filters, budget=getattr(self, 'budget', None))
        return handler(query, limit, filters)
    return _ORIGINAL_SEARCH_ONE(self, name, query, limit, filters)


def activate() -> list[str]:
    """Install the dispatcher once; safe to call repeatedly."""
    global _ORIGINAL_SEARCH_ONE
    if _ORIGINAL_SEARCH_ONE is not None:
        return list(MUTATIONS)
    _ORIGINAL_SEARCH_ONE = _source.AcademicSource._search_one
    _source.AcademicSource._search_one = _dispatch
    MUTATIONS.append('source.AcademicSource._search_one')
    return list(MUTATIONS)


def active() -> bool:
    return _ORIGINAL_SEARCH_ONE is not None


def reset_for_tests() -> None:
    """Undo the seam so a test can start from the frozen baseline."""
    global _ORIGINAL_SEARCH_ONE
    if _ORIGINAL_SEARCH_ONE is not None:
        _source.AcademicSource._search_one = _ORIGINAL_SEARCH_ONE
        _ORIGINAL_SEARCH_ONE = None
    for name in list(_REGISTERED_PROVIDERS):
        _source.PROVIDERS.pop(name, None)
    _REGISTERED_PROVIDERS.clear()
    _HANDLERS.clear()
    MUTATIONS.clear()
