"""Tests for LLM-based relevance scoring."""

from unittest.mock import AsyncMock, patch


def _make_results(n: int) -> list[dict]:
    return [
        {
            "id": i,
            "from": f"sender{i}@example.com",
            "subject": f"Subject {i}",
            "summary": f"Summary of email {i}",
        }
        for i in range(1, n + 1)
    ]


class TestScoreRelevance:
    async def test_filters_low_relevance(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[5, 2, 4, 1, 3],
        ):
            filtered = await score_relevance("physio", results, api_key="test")

        # Only scores >= 3 survive
        assert len(filtered) == 3
        assert filtered[0]["relevance_score"] == 5
        assert filtered[1]["relevance_score"] == 4
        assert filtered[2]["relevance_score"] == 3

    async def test_preserves_order(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(3)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[4, 5, 3],
        ):
            filtered = await score_relevance("test", results, api_key="test")

        # Order preserved from reranker, not re-sorted by relevance
        assert filtered[0]["id"] == 1
        assert filtered[1]["id"] == 2
        assert filtered[2]["id"] == 3

    async def test_returns_top_3_when_all_filtered(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[1, 2, 1, 2, 1],
        ):
            filtered = await score_relevance("nonsense", results, api_key="test")

        # All below threshold, but returns top 3 as fallback
        assert len(filtered) == 3

    async def test_no_api_key_returns_all(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)
        filtered = await score_relevance("test", results, api_key="")
        assert len(filtered) == 5

    async def test_llm_failure_returns_all(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            side_effect=Exception("API down"),
        ):
            filtered = await score_relevance("test", results, api_key="test")

        assert len(filtered) == 5

    async def test_score_count_mismatch_returns_all(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[5, 3],  # Only 2 scores for 5 results
        ):
            filtered = await score_relevance("test", results, api_key="test")

        assert len(filtered) == 5

    async def test_custom_threshold(self) -> None:
        from email_mcp.relevance import score_relevance

        results = _make_results(5)

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[5, 4, 3, 2, 1],
        ):
            filtered = await score_relevance("test", results, api_key="test", threshold=4)

        assert len(filtered) == 2


class TestScoreRelevanceRaw:
    """Raw scores let search() run relevance concurrently with the reranker."""

    async def test_returns_scores_in_input_order(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[4, 2, 5],
        ):
            scores = await score_relevance_raw("q", _make_results(3), api_key="test")

        assert scores == [4, 2, 5]

    async def test_returns_none_on_llm_failure(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            side_effect=RuntimeError("router down"),
        ):
            assert await score_relevance_raw("q", _make_results(3), api_key="test") is None

    async def test_returns_none_on_count_mismatch(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        with patch(
            "email_mcp.relevance._llm_score",
            new_callable=AsyncMock,
            return_value=[4, 2],
        ):
            assert await score_relevance_raw("q", _make_results(3), api_key="test") is None

    async def test_no_api_key_is_a_noop(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        assert await score_relevance_raw("q", _make_results(3), api_key="") is None

    async def test_batches_large_result_sets(self) -> None:
        """The model miscounts past ~40 results, so split and concatenate."""
        from email_mcp.relevance import score_relevance_raw

        sizes: list[int] = []

        async def fake(prompt, api_key, count):
            sizes.append(count)
            return [5] * count

        with patch("email_mcp.relevance._llm_score", new_callable=AsyncMock, side_effect=fake):
            scores = await score_relevance_raw("q", _make_results(68), api_key="k")

        assert scores is not None
        assert len(scores) == 68
        assert sizes == [30, 30, 8]

    async def test_returns_none_when_a_batch_miscounts(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        async def fake(prompt, api_key, count):
            return [5] * (count - 1)  # always one short

        with patch("email_mcp.relevance._llm_score", new_callable=AsyncMock, side_effect=fake):
            assert await score_relevance_raw("q", _make_results(68), api_key="k") is None

    async def test_returns_none_when_a_batch_raises(self) -> None:
        from email_mcp.relevance import score_relevance_raw

        async def fake(prompt, api_key, count):
            raise TimeoutError

        with patch("email_mcp.relevance._llm_score", new_callable=AsyncMock, side_effect=fake):
            assert await score_relevance_raw("q", _make_results(68), api_key="k") is None


class TestApplyRelevanceFilter:
    """The parallel path filters using scores it already has, not a second call."""

    @staticmethod
    def _pair(n: int = 5):
        from types import SimpleNamespace

        results = [SimpleNamespace(pm_id=f"m{i}") for i in range(n)]
        formatted = [{"id": i, "subject": f"S{i}"} for i in range(n)]
        return formatted, results

    def test_keeps_at_or_above_threshold(self) -> None:
        from email_mcp.tools.searching import _apply_relevance_filter

        formatted, results = self._pair()
        kept = _apply_relevance_filter(
            formatted, results, {"m0": 5, "m1": 1, "m2": 3, "m3": 2, "m4": 4}
        )

        assert [f["id"] for f in kept] == [0, 2, 4]
        assert kept[0]["relevance_score"] == 5

    def test_falls_back_to_top_three_when_nothing_passes(self) -> None:
        from email_mcp.tools.searching import _apply_relevance_filter

        formatted, results = self._pair()
        kept = _apply_relevance_filter(formatted, results, {f"m{i}": 1 for i in range(5)})

        assert [f["id"] for f in kept] == [0, 1, 2]

    def test_missing_score_is_treated_as_irrelevant(self) -> None:
        from email_mcp.tools.searching import _apply_relevance_filter

        formatted, results = self._pair(3)
        kept = _apply_relevance_filter(formatted, results, {"m0": 5})

        assert [f["id"] for f in kept] == [0]
