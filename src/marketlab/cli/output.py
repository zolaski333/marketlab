"""Exit codes and structured output for the CLI (§29.3, §29.4).

Two audiences, one set of facts
-------------------------------
A person reading a terminal wants a table; a scheduler wants a line it can
parse. Both get the *same* records here: :meth:`Emitter.event` takes a payload
and renders it either as human text or as one canonical JSON object per line.
Nothing is reported to one audience and not the other, which is what stops the
machine-readable log from quietly becoming a second, thinner truth.

JSON goes to stdout, progress to stderr
---------------------------------------
So ``marketlab status --json | jq`` works while progress messages still reach a
terminal. A tool that interleaved both on stdout would produce a log that is
neither readable nor parseable.

Exit codes are part of the contract
-----------------------------------
A CLI that exits 0 whatever happened cannot be used in a pipeline, and this is
a platform whose whole value is that failures are visible. Each code below
names a distinct thing an operator would do differently.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import IntEnum
from typing import Any, TextIO

from marketlab.core.canonical import canonical_json

__all__ = ["Emitter", "ExitCode"]


class ExitCode(IntEnum):
    """What the process exit status means."""

    OK = 0
    """The command did what it said."""

    FAILED = 1
    """An unexpected platform failure. A bug, or an environment problem."""

    USAGE = 2
    """Bad invocation. Reserved for the argument parser's own errors."""

    CONFIGURATION = 3
    """The configuration is invalid, missing, or contradicts a declared run."""

    INTEGRITY = 4
    """A scientific check failed: a broken hash chain, a replay divergence, a
    run re-declared with different parameters. The data is not to be trusted
    until a human looks at it."""

    NO_DATA = 5
    """The command was well-formed but there was nothing to act on. Distinct
    from OK on purpose: 'resolved 0 forecasts because none exist' must not
    look like 'resolved 0 forecasts because none were due'."""


class Emitter:
    """Writes the same facts as human text or as JSON lines."""

    __slots__ = ("_err", "_json", "_out", "_quiet")

    def __init__(
        self,
        *,
        as_json: bool = False,
        quiet: bool = False,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        self._json = as_json
        self._quiet = quiet
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr

    @property
    def as_json(self) -> bool:
        return self._json

    def event(self, event: str, **payload: Any) -> None:
        """Record one thing that happened."""
        if self._json:
            print(canonical_json({"event": event, **payload}), file=self._out)
            return
        if self._quiet:
            return
        print(f"{event}: {_human(payload)}" if payload else event, file=self._out)

    def table(self, title: str, rows: Mapping[str, Any]) -> None:
        """A block of key/value facts."""
        if self._json:
            print(canonical_json({"event": title, **dict(rows)}), file=self._out)
            return
        if self._quiet:
            return
        print(f"\n{title}", file=self._out)
        width = max((len(key) for key in rows), default=0)
        for key, value in rows.items():
            print(f"  {key.ljust(width)}  {value}", file=self._out)

    def progress(self, message: str) -> None:
        """Something in flight. Never machine-consumed, so never JSON."""
        if self._quiet or self._json:
            return
        print(message, file=self._err)

    def error(self, message: str, *, code: ExitCode) -> None:
        """A failure, on stderr in both modes so a JSON consumer's stdout stays
        clean for results."""
        if self._json:
            print(
                canonical_json({"event": "error", "code": int(code), "message": message}),
                file=self._err,
            )
        else:
            print(f"error: {message}", file=self._err)


def _human(payload: Mapping[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in payload.items())
