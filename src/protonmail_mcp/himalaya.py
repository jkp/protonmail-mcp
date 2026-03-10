"""Async subprocess wrapper for the himalaya CLI."""

import asyncio
import json
import time
from typing import Any

import structlog

logger = structlog.get_logger()


class HimalayaError(Exception):
    """Error from himalaya CLI execution."""

    def __init__(self, message: str, returncode: int = -1) -> None:
        super().__init__(message)
        self.returncode = returncode


class HimalayaClient:
    """Wraps himalaya CLI calls as async subprocess invocations."""

    def __init__(
        self,
        bin_path: str = "himalaya",
        timeout: int = 30,
        account: str | None = None,
        config_path: str | None = None,
    ) -> None:
        self.bin_path = bin_path
        self.timeout = timeout
        self.account = account
        self.config_path = config_path

    def _build_args(self, *args: str, account: str | None = None) -> list[str]:
        """Build the full argument list for a himalaya command.

        himalaya global options (--output, --config) go before the subcommand,
        while --account is a subcommand-level flag and goes after.
        """
        cmd = [self.bin_path, "--output", "json"]

        if self.config_path:
            cmd.extend(["--config", self.config_path])

        cmd.extend(args)

        effective_account = account or self.account
        if effective_account:
            cmd.extend(["--account", effective_account])

        return cmd

    async def run(
        self,
        *args: str,
        stdin: str | None = None,
        account: str | None = None,
    ) -> str:
        """Run a himalaya command and return raw stdout."""
        cmd = self._build_args(*args, account=account)
        # Extract subcommand (e.g., "envelope list", "template send") for logging
        subcommand = " ".join(a for a in args if not a.startswith("--") and a != self.bin_path)
        log = logger.bind(subcommand=subcommand)
        log.debug("himalaya.exec", cmd=cmd)

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin else None,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin.encode() if stdin else None),
                timeout=self.timeout,
            )
        except TimeoutError:
            proc.kill()
            elapsed = time.monotonic() - t0
            log.error("himalaya.timeout", elapsed_s=round(elapsed, 2), timeout=self.timeout)
            raise HimalayaError(
                f"himalaya command timed out after {self.timeout}s: {' '.join(cmd)}",
                returncode=-1,
            )

        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            stderr_str = stderr_bytes.decode().strip()
            log.error(
                "himalaya.error",
                returncode=proc.returncode,
                stderr=stderr_str,
                elapsed_s=round(elapsed, 2),
            )
            raise HimalayaError(stderr_str, returncode=proc.returncode or -1)

        log.info("himalaya.ok", elapsed_s=round(elapsed, 2), bytes=len(stdout_bytes))
        return stdout_bytes.decode()

    async def run_json(self, *args: str, account: str | None = None) -> Any:
        """Run a himalaya command and parse the JSON output."""
        raw = await self.run(*args, account=account)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("himalaya.json_parse_error", error=str(e))
            raise HimalayaError(f"Failed to parse himalaya JSON output: {e}") from e
