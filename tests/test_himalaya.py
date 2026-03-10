"""Tests for himalaya subprocess wrapper."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from protonmail_mcp.himalaya import HimalayaClient, HimalayaError


@pytest.fixture
def client() -> HimalayaClient:
    return HimalayaClient(bin_path="himalaya", timeout=10)


@pytest.fixture
def client_with_account() -> HimalayaClient:
    return HimalayaClient(
        bin_path="himalaya",
        timeout=10,
        account="work",
        config_path="/custom/config.toml",
    )


def _mock_process(stdout: str = "", stderr: str = "", returncode: int = 0) -> AsyncMock:
    """Create a mock asyncio subprocess."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestHimalayaClientRun:
    async def test_basic_command(self, client: HimalayaClient) -> None:
        output = json.dumps([{"name": "INBOX"}])
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=output)) as mock_exec:
            result = await client.run("folder", "list")
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args[0] == "himalaya"
            assert "--output" in args
            assert "json" in args
            assert "folder" in args
            assert "list" in args
            assert result == output

    async def test_account_flag(self, client_with_account: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")) as mock_exec:
            await client_with_account.run("folder", "list")
            args = mock_exec.call_args[0]
            assert "--account" in args
            assert "work" in args

    async def test_config_path_flag(self, client_with_account: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")) as mock_exec:
            await client_with_account.run("folder", "list")
            args = mock_exec.call_args[0]
            assert "--config" in args
            assert "/custom/config.toml" in args

    async def test_nonzero_exit_raises(self, client: HimalayaClient) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(stderr="folder not found", returncode=1),
        ):
            with pytest.raises(HimalayaError, match="folder not found"):
                await client.run("folder", "list")

    async def test_error_contains_returncode(self, client: HimalayaClient) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(stderr="bad", returncode=2),
        ):
            with pytest.raises(HimalayaError) as exc_info:
                await client.run("folder", "list")
            assert exc_info.value.returncode == 2

    async def test_timeout(self, client: HimalayaClient) -> None:
        proc = _mock_process()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(HimalayaError, match="timed out"):
                await client.run("folder", "list")
            proc.kill.assert_called_once()


class TestHimalayaClientRunJson:
    async def test_parse_json_list(self, client: HimalayaClient) -> None:
        data = [{"name": "INBOX"}, {"name": "Sent"}]
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=json.dumps(data))):
            result = await client.run_json("folder", "list")
            assert result == data

    async def test_parse_json_object(self, client: HimalayaClient) -> None:
        data = {"id": "42", "subject": "Test"}
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=json.dumps(data))):
            result = await client.run_json("message", "read", "42")
            assert result == data

    async def test_invalid_json_raises(self, client: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="not json")):
            with pytest.raises(HimalayaError, match="parse"):
                await client.run_json("folder", "list")


class TestHimalayaClientRunWithStdin:
    async def test_stdin_piped(self, client: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="ok")) as mock_exec:
            result = await client.run("template", "send", stdin="From: a@b.com\n\nHello")
            # Verify stdin was passed to communicate
            proc = mock_exec.return_value
            proc.communicate.assert_called_once_with(input=b"From: a@b.com\n\nHello")
            assert result == "ok"


class TestHimalayaClientAccountOverride:
    async def test_per_call_account_override(self, client: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")) as mock_exec:
            await client.run("folder", "list", account="travel")
            args = mock_exec.call_args[0]
            assert "--account" in args
            assert "travel" in args

    async def test_per_call_override_beats_default(self, client_with_account: HimalayaClient) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")) as mock_exec:
            await client_with_account.run("folder", "list", account="personal")
            args = mock_exec.call_args[0]
            idx = args.index("--account")
            assert args[idx + 1] == "personal"
