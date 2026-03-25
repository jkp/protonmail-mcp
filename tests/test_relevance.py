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
