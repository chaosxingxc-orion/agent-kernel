"""Subprocess-isolated script runtime for agent-kernel.

Executes Python scripts in a separate subprocess with timeout and
output size limits. Provides real isolation from the kernel process.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field

from agent_kernel.kernel.contracts import ScriptActivityInput, ScriptResult


@dataclass(frozen=True, slots=True)
class SubprocessScriptConfig:
    """Configuration for the subprocess script runtime.

    Attributes:
        timeout_s: Maximum execution time in seconds.
        max_output_bytes: Maximum captured stdout size in bytes.
        python_executable: Python interpreter path for subprocess invocation.

    """

    timeout_s: float = 30.0
    max_output_bytes: int = 1_048_576  # 1 MB
    python_executable: str = field(default_factory=lambda: sys.executable)


class SubprocessScriptRuntime:
    """Executes Python scripts in a subprocess with timeout and output limits.

    Each script execution spawns a fresh Python subprocess, providing real
    isolation from the kernel process. stdout and stderr are captured and
    truncated to ``config.max_output_bytes``.

    Timeout is enforced via ``asyncio.wait_for``; on expiry the subprocess
    is killed and a ``ScriptResult`` with ``exit_code=-1`` is returned.
    """

    def __init__(self, config: SubprocessScriptConfig | None = None) -> None:
        """Initialise the runtime with optional configuration.

        Args:
            config: Runtime configuration. Uses defaults when omitted.

        """
        self._config = config or SubprocessScriptConfig()

    async def validate_script(self, script_content: str, host_kind: str) -> bool:
        """Validate that script_content is syntactically valid Python.

        Args:
            script_content: Python source to validate.
            host_kind: Target execution mechanism (informational only).

        Returns:
            True when the source parses without SyntaxError, False otherwise.

        """
        try:
            ast.parse(script_content)
            return True
        except SyntaxError:
            return False

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Execute a Python script in an isolated subprocess.

        The script content is written to a temporary file, executed via the
        configured Python interpreter, and the result collected. On timeout
        the subprocess is killed and a result with ``exit_code=-1`` is
        returned rather than raising.

        Args:
            input_value: Script execution payload with content and parameters.

        Returns:
            ScriptResult with captured stdout/stderr, exit_code, and timing.
            ``exit_code=-1`` signals a timeout; ``exit_code=2`` signals a
            syntax validation failure before subprocess launch.

        """
        # Validate syntax before spawning subprocess.
        syntax_ok = await self.validate_script(input_value.script_content, input_value.host_kind)
        if not syntax_ok:
            return ScriptResult(
                script_id=input_value.script_id,
                exit_code=2,
                stdout="",
                stderr="SyntaxError: script_content failed syntax validation",
                output_json=None,
                execution_ms=0,
            )

        timeout_s = self._config.timeout_s
        max_bytes = self._config.max_output_bytes
        python_exec = self._config.python_executable

        tmppath: str | None = None
        start = time.monotonic()
        try:
            # Write script to a temporary file.
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(input_value.script_content)
                tmppath = tmp.name

            proc = await asyncio.create_subprocess_exec(
                python_exec,
                tmppath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_s,
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return ScriptResult(
                    script_id=input_value.script_id,
                    exit_code=-1,
                    stdout="",
                    stderr=f"TimeoutError: script exceeded {timeout_s}s budget",
                    output_json=None,
                    execution_ms=elapsed_ms,
                )

            elapsed_ms = int((time.monotonic() - start) * 1000)
            exit_code = proc.returncode if proc.returncode is not None else 1

            stdout_str = stdout_bytes[:max_bytes].decode("utf-8", errors="replace")
            stderr_str = stderr_bytes[:max_bytes].decode("utf-8", errors="replace")

            return ScriptResult(
                script_id=input_value.script_id,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                output_json=None,
                execution_ms=elapsed_ms,
            )
        finally:
            if tmppath is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmppath)
