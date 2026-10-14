from windows_mcp.tree.budget import (
    DEFAULT_MAX_TREE_ELEMENTS,
    DEFAULT_TREE_TIMEOUT_SEC,
    TreeElementBudget,
    TreeWalkDeadline,
    resolve_max_tree_elements,
    resolve_tree_timeout,
)


class FakeClock:
    """Monotonic clock a test drives by hand, in seconds."""

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTreeElementBudget:
    def test_starts_unexhausted(self):
        budget = TreeElementBudget(limit=3)
        assert budget.exhausted is False
        assert budget.truncated is False
        assert budget.remaining == 3

    def test_try_consume_under_limit(self):
        budget = TreeElementBudget(limit=3)
        assert budget.try_consume() is True
        assert budget.count == 1
        assert budget.truncated is False

    def test_try_consume_reaching_limit_marks_truncated(self):
        budget = TreeElementBudget(limit=2)
        assert budget.try_consume() is True
        assert budget.try_consume() is True
        assert budget.exhausted is True
        assert budget.truncated is True

    def test_try_consume_beyond_limit_returns_false(self):
        budget = TreeElementBudget(limit=1)
        assert budget.try_consume() is True
        assert budget.try_consume() is False
        assert budget.count == 1
        assert budget.truncated is True

    def test_try_consume_batch_exceeding_remaining(self):
        budget = TreeElementBudget(limit=5)
        assert budget.try_consume(3) is True
        assert budget.try_consume(10) is False
        assert budget.count == 5
        assert budget.truncated is True

    def test_try_consume_zero_amount_does_not_change_count(self):
        budget = TreeElementBudget(limit=5)
        assert budget.try_consume(0) is True
        assert budget.count == 0

    def test_limit_is_clamped_to_at_least_one(self):
        budget = TreeElementBudget(limit=0)
        assert budget.limit == 1

    def test_remaining_never_negative(self):
        budget = TreeElementBudget(limit=2)
        budget.try_consume(10)
        assert budget.remaining == 0
        assert budget.count == 2


class TestResolveMaxTreeElements:
    def test_default_when_unset(self):
        assert resolve_max_tree_elements({}) == DEFAULT_MAX_TREE_ELEMENTS

    def test_default_when_blank(self):
        assert (
            resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "  "})
            == DEFAULT_MAX_TREE_ELEMENTS
        )

    def test_valid_override(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "150"}) == 150

    def test_invalid_value_falls_back_to_default(self):
        assert (
            resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "not-a-number"})
            == DEFAULT_MAX_TREE_ELEMENTS
        )

    def test_zero_falls_back_to_default(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "0"}) == DEFAULT_MAX_TREE_ELEMENTS

    def test_negative_falls_back_to_default(self):
        assert resolve_max_tree_elements({"WINDOWS_MCP_MAX_TREE_ELEMENTS": "-5"}) == DEFAULT_MAX_TREE_ELEMENTS


class TestTreeWalkDeadline:
    def test_starts_unexpired(self):
        clock = FakeClock()
        deadline = TreeWalkDeadline(limit_sec=10.0, clock=clock)
        assert deadline.expired is False
        assert deadline.truncated is False
        assert deadline.elapsed_sec == 0

    def test_expires_once_the_limit_is_reached(self):
        clock = FakeClock()
        deadline = TreeWalkDeadline(limit_sec=10.0, clock=clock)
        clock.advance(9.9)
        assert deadline.expired is False
        clock.advance(0.1)
        assert deadline.expired is True
        assert deadline.truncated is True

    def test_stays_expired_once_tripped(self):
        clock = FakeClock()
        deadline = TreeWalkDeadline(limit_sec=1.0, clock=clock)
        clock.advance(5.0)
        assert [deadline.expired, deadline.expired, deadline.expired] == [True, True, True]

    def test_elapsed_is_measured_from_construction(self):
        clock = FakeClock(start=100.0)
        deadline = TreeWalkDeadline(limit_sec=10.0, clock=clock)
        clock.advance(3.0)
        assert deadline.elapsed_sec == 3.0

    def test_non_positive_limit_never_expires(self):
        clock = FakeClock()
        deadline = TreeWalkDeadline(limit_sec=0, clock=clock)
        clock.advance(10_000.0)
        assert deadline.expired is False

    def test_default_limit_is_the_module_default(self):
        assert TreeWalkDeadline().limit_sec == DEFAULT_TREE_TIMEOUT_SEC


class TestResolveTreeTimeout:
    def test_default_when_unset(self):
        assert resolve_tree_timeout({}) == DEFAULT_TREE_TIMEOUT_SEC

    def test_default_when_blank(self):
        assert resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": "  "}) == DEFAULT_TREE_TIMEOUT_SEC

    def test_valid_override(self):
        assert resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": "2.5"}) == 2.5

    def test_integer_override(self):
        assert resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": " 30 "}) == 30.0

    def test_invalid_value_falls_back_to_default(self):
        assert (
            resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": "not-a-number"})
            == DEFAULT_TREE_TIMEOUT_SEC
        )

    def test_zero_falls_back_to_default(self):
        assert resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": "0"}) == DEFAULT_TREE_TIMEOUT_SEC

    def test_negative_falls_back_to_default(self):
        assert resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": "-5"}) == DEFAULT_TREE_TIMEOUT_SEC

    def test_non_finite_falls_back_to_default(self):
        for raw in ("nan", "inf", "-inf"):
            assert (
                resolve_tree_timeout({"WINDOWS_MCP_TREE_TIMEOUT": raw}) == DEFAULT_TREE_TIMEOUT_SEC
            )

    def test_reads_os_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "7")
        assert resolve_tree_timeout() == 7.0
