"""Bounds what a single tree capture may spend — in elements and in wall time.

Some applications expose enormous flat lists/grids through UI Automation
(e.g. an unfiltered inventory grid with thousands of rows). Walking and
serializing every row can stall the target application for minutes and can
produce a response too large for the calling client to handle. TreeElementBudget
gives Tree.get_state a cheap, dependency-free way to stop early once a
configurable element count is reached, while still returning a partial —
clearly marked as truncated — result instead of hanging or failing outright.

An element count alone does not bound time: the per-element cost is set by the
target's accessibility provider, so a slow provider stretches a capture that
stays well inside the element budget (measured: a nine-window desktop exhausts
the 500-element budget and still takes ~9s), and a provider that hands back a
subtree which appends no output nodes consumes no budget at all. TreeWalkDeadline
adds the missing wall-clock ceiling, in the same shape: the walk stops at the
deadline and the caller returns a partial result marked as truncated.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from time import perf_counter

logger = logging.getLogger(__name__)

DEFAULT_MAX_TREE_ELEMENTS = 500
DEFAULT_TREE_TIMEOUT_SEC = 10.0

TRUNCATION_REASON_ELEMENTS = "elements"
TRUNCATION_REASON_TIME = "time"
TRUNCATION_REASON_BOTH = "elements+time"


def resolve_max_tree_elements(env: "os._Environ[str] | dict[str, str] | None" = None) -> int:
    """Read the tree-capture element limit from the environment.

    Args:
        env: Mapping to read from. Defaults to ``os.environ``.

    Returns:
        The configured limit, or ``DEFAULT_MAX_TREE_ELEMENTS`` if the
        variable is unset, blank, non-numeric, or not a positive integer.
    """
    source = os.environ if env is None else env
    raw = source.get("WINDOWS_MCP_MAX_TREE_ELEMENTS", "")
    if not raw.strip():
        return DEFAULT_MAX_TREE_ELEMENTS
    try:
        parsed = int(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid WINDOWS_MCP_MAX_TREE_ELEMENTS value %r, using default %d",
            raw,
            DEFAULT_MAX_TREE_ELEMENTS,
        )
        return DEFAULT_MAX_TREE_ELEMENTS
    if parsed < 1:
        logger.warning(
            "WINDOWS_MCP_MAX_TREE_ELEMENTS %r must be >= 1, using default %d",
            parsed,
            DEFAULT_MAX_TREE_ELEMENTS,
        )
        return DEFAULT_MAX_TREE_ELEMENTS
    return parsed


class TreeElementBudget:
    """Tracks how many output-affecting elements a tree traversal has captured."""

    def __init__(self, limit: int = DEFAULT_MAX_TREE_ELEMENTS) -> None:
        self.limit = max(1, limit)
        self.count = 0
        self.truncated = False

    @property
    def exhausted(self) -> bool:
        return self.count >= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)

    def try_consume(self, amount: int = 1) -> bool:
        """Register `amount` newly captured elements.

        Returns False (and marks the budget truncated) once the limit is
        reached; the caller should then stop appending/recursing further.
        `count` is hard-capped at `limit` — a single over-large `amount`
        cannot push it past the configured budget.
        """
        if amount <= 0:
            return not self.exhausted
        if self.exhausted:
            self.truncated = True
            return False
        if amount > self.remaining:
            self.count = self.limit
            self.truncated = True
            return False
        self.count += amount
        if self.exhausted:
            self.truncated = True
        return True


def resolve_tree_timeout(env: "os._Environ[str] | dict[str, str] | None" = None) -> float:
    """Read the tree-capture wall-clock limit, in seconds, from the environment.

    Args:
        env: Mapping to read from. Defaults to ``os.environ``.

    Returns:
        The configured limit, or ``DEFAULT_TREE_TIMEOUT_SEC`` if the variable is
        unset, blank, non-numeric, or not a positive finite number.
    """
    source = os.environ if env is None else env
    raw = source.get("WINDOWS_MCP_TREE_TIMEOUT", "")
    if not raw.strip():
        return DEFAULT_TREE_TIMEOUT_SEC
    try:
        parsed = float(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid WINDOWS_MCP_TREE_TIMEOUT value %r, using default %.1f",
            raw,
            DEFAULT_TREE_TIMEOUT_SEC,
        )
        return DEFAULT_TREE_TIMEOUT_SEC
    if not math.isfinite(parsed) or parsed <= 0:
        logger.warning(
            "WINDOWS_MCP_TREE_TIMEOUT %r must be a positive finite number of seconds, "
            "using default %.1f",
            raw,
            DEFAULT_TREE_TIMEOUT_SEC,
        )
        return DEFAULT_TREE_TIMEOUT_SEC
    return parsed


class TreeWalkDeadline:
    """Tracks how long a tree traversal has been running.

    The clock starts on construction — Tree.get_state builds one per capture,
    exactly like it builds a fresh TreeElementBudget. `expired` is a pure read:
    the underlying clock is monotonic, so once it reports True it keeps
    reporting True, and callers can check it as often as they like.
    """

    def __init__(
        self,
        limit_sec: float = DEFAULT_TREE_TIMEOUT_SEC,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.limit_sec = float(limit_sec)
        self._clock = clock
        self._started_at = clock()

    @property
    def elapsed_sec(self) -> float:
        return self._clock() - self._started_at

    @property
    def expired(self) -> bool:
        """Whether the capture has used up its wall-clock budget."""
        if self.limit_sec <= 0:
            return False
        return self.elapsed_sec >= self.limit_sec

    @property
    def truncated(self) -> bool:
        """Alias of `expired`, mirroring TreeElementBudget.truncated."""
        return self.expired
