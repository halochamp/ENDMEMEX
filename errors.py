"""Shared exception types.

Plain ValueError subclasses with no behavior of their own, so there is no
monkeypatch seam to preserve here (unlike config.py's data or activity.py/
documents.py's functions) -- a normal import binds the identical class object
into endeavor_db.py, which is all `isinstance`/`except` checks need.

No exception instance from this module crosses a process boundary: neither
`mcp_server.py` (subprocess + JSON stdout/stderr) nor `agent_delegate.py`
(JSON state files) ever pickles or otherwise serializes one -- confirmed by
grep, not assumed. The risk this deferred in an earlier session was module/
pickle identity; with no pickling anywhere in ENDMEMEX, that risk does not
apply here.
"""
from __future__ import annotations


class SessionNotFoundError(ValueError):
    """No resumable session matched an explicit identity or project."""


class AmbiguousSessionError(ValueError):
    """A project resolves to multiple resumable sessions and needs an ID."""
