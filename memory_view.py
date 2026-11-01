"""Host-only, opt-in presentation of already delivered papers.

The trusted host chooses the store once, supplies only currently visible papers,
 and decides whether this individual presentation was explicitly selected. This
module never authenticates users or chooses a store from request/model data.
Revalidation is not atomic with presentation across processes: a change after
the final check cannot be detected here. The underlying MemoryContext may
persist observed expiry tombstones or migrate a legacy preference database.
"""
from __future__ import annotations

import os

from memory_context import MemoryContext


class MemoryView:
    def __init__(self, host_store_path):
        # The path is trusted host configuration, never a presentation argument.
        self._path = os.path.abspath(os.fspath(host_store_path))

    def present(self, papers, *, enabled=False, selected=False, explicit_fields=None):
        """Return papers and a generic status; never change the supplied objects.

        Only exact lists of exact dictionaries with unique nonempty string IDs
        are eligible. Years are integer calendar years 1..9999; all other values
        are unknown. An optional set of explicit request fields blocks conflicts
        using MemoryContext's existing rules. The caller owns synchronization of
        the input and must not persist this presentation as a task or checkpoint.
        """
        fallback = {'papers': papers, 'status': 'not_applied', 'safety': 'safe'}
        if enabled is not True or selected is not True:
            return fallback
        ctx = None
        result = fallback
        try:
            if type(papers) is not list or any(type(p) is not dict for p in papers):
                return fallback
            ids = [p.get('id') for p in papers]
            if any(type(i) is not str or not i.strip() for i in ids) or len(set(ids)) != len(ids):
                return fallback
            fields = set() if explicit_fields is None else explicit_fields
            ctx = MemoryContext.capture(self._path, enabled=True)
            ctx.revalidate()
            if ctx.hints(explicit_fields=fields).get('prefer_recent') is True:
                def key(paper):
                    year = paper.get('year')
                    return (0, -year) if type(year) is int and 1 <= year <= 9999 else (1, 0)
                ordered = sorted(papers, key=key)
                ctx.revalidate()
                result = {'papers': ordered, 'status': 'applied', 'safety': 'safe'}
        except Exception:
            result = fallback
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    result = fallback
        return result
