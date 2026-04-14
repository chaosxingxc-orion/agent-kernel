"""Tests for SubprocessScriptRuntime — uses real subprocess, no mocking."""

import asyncio

from agent_kernel.kernel.cognitive.script_runtime_subprocess import (
    SubprocessScriptConfig,
    SubprocessScriptRuntime,
)
from agent_kernel.kernel.contracts import ScriptActivityInput


def _make_input(script_content: str, *, timeout_ms: int = 10_000) -> ScriptActivityInput:
    """Build a minimal ScriptActivityInput for testing."""
    return ScriptActivityInput(
        run_id="test-run",
        action_id="test-action",
        script_id="test-script",
        script_content=script_content,
        host_kind="subprocess",
        parameters={},
        timeout_ms=timeout_ms,
    )


class TestSubprocessScriptRuntime:
    """Integration tests that use a real subprocess — no mocking."""

    def test_hello_world_succeeds(self) -> None:
        """print('hello') should produce success=True and 'hello' in stdout."""
        runtime = SubprocessScriptRuntime()
        result = asyncio.run(runtime.execute_script(_make_input('print("hello")')))
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_syntax_error_returns_failure(self) -> None:
        """Invalid syntax should return exit_code=2 without spawning subprocess."""
        runtime = SubprocessScriptRuntime()
        result = asyncio.run(runtime.execute_script(_make_input("def f(:")))
        assert result.exit_code == 2
        assert result.exit_code != 0

    def test_nonzero_exit_returns_failure(self) -> None:
        """sys.exit(1) should return exit_code=1."""
        runtime = SubprocessScriptRuntime()
        result = asyncio.run(runtime.execute_script(_make_input("import sys; sys.exit(1)")))
        assert result.exit_code == 1

    def test_output_captured(self) -> None:
        """Printed marker should appear in captured stdout."""
        runtime = SubprocessScriptRuntime()
        result = asyncio.run(runtime.execute_script(_make_input('print("marker_xyz")')))
        assert "marker_xyz" in result.stdout

    def test_stderr_captured_on_error(self) -> None:
        """Unhandled exception traceback should appear in captured stderr."""
        runtime = SubprocessScriptRuntime()
        result = asyncio.run(runtime.execute_script(_make_input('raise ValueError("boom")')))
        assert result.exit_code != 0
        assert "boom" in result.stderr

    def test_timeout_returns_failure(self) -> None:
        """Script exceeding timeout should return exit_code=-1."""
        config = SubprocessScriptConfig(timeout_s=0.5)
        runtime = SubprocessScriptRuntime(config=config)
        result = asyncio.run(
            runtime.execute_script(_make_input("import time; time.sleep(100)", timeout_ms=500))
        )
        assert result.exit_code == -1
        assert "Timeout" in result.stderr or "timeout" in result.stderr.lower()

    def test_script_id_preserved_in_result(self) -> None:
        """ScriptResult.script_id must match the input script_id."""
        runtime = SubprocessScriptRuntime()
        inp = ScriptActivityInput(
            run_id="r1",
            action_id="a1",
            script_id="my-unique-script-id",
            script_content="pass",
            host_kind="subprocess",
        )
        result = asyncio.run(runtime.execute_script(inp))
        assert result.script_id == "my-unique-script-id"


class TestValidateScript:
    """Unit tests for the validate_script helper."""

    def test_valid_script_returns_true(self) -> None:
        runtime = SubprocessScriptRuntime()
        assert asyncio.run(runtime.validate_script("x = 1 + 2", "subprocess")) is True

    def test_syntax_error_returns_false(self) -> None:
        runtime = SubprocessScriptRuntime()
        assert asyncio.run(runtime.validate_script("def f(:", "subprocess")) is False

    def test_empty_script_returns_true(self) -> None:
        """An empty string is syntactically valid Python."""
        runtime = SubprocessScriptRuntime()
        assert asyncio.run(runtime.validate_script("", "subprocess")) is True
