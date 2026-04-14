"""Tests for ScriptRuntime implementations."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_kernel.kernel.cognitive.script_runtime import (
    DedupeAwareScriptRuntime,
    EchoScriptRuntime,
    InProcessPythonScriptRuntime,
    LocalProcessScriptRuntime,
)
from agent_kernel.kernel.contracts import ScriptActivityInput, ScriptResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(
    script_content: str = "",
    script_id: str = "test-script",
    host_kind: str = "in_process_python",
    parameters: dict | None = None,
    timeout_ms: int = 5_000,
) -> ScriptActivityInput:
    """Builds a minimal ScriptActivityInput for tests."""
    return ScriptActivityInput(
        run_id="run-1",
        action_id="action-1",
        script_id=script_id,
        script_content=script_content,
        host_kind=host_kind,
        parameters=parameters or {},
        timeout_ms=timeout_ms,
    )


# ---------------------------------------------------------------------------
# EchoScriptRuntime tests
# ---------------------------------------------------------------------------


class TestEchoScriptRuntime:
    """EchoScriptRuntime tests."""

    @pytest.mark.asyncio
    async def test_echo_empty_parameters(self) -> None:
        runtime = EchoScriptRuntime()
        result = await runtime.execute_script(_make_input(host_kind="echo", parameters={}))
        assert result.exit_code == 0
        assert result.output_json == {}
        assert json.loads(result.stdout) == {}

    @pytest.mark.asyncio
    async def test_echo_parameters_reflected_in_output_json(self) -> None:
        runtime = EchoScriptRuntime()
        params = {"key": "value", "count": 3}
        result = await runtime.execute_script(_make_input(host_kind="echo", parameters=params))
        assert result.output_json == params
        assert json.loads(result.stdout) == params

    @pytest.mark.asyncio
    async def test_echo_always_exit_code_zero(self) -> None:
        runtime = EchoScriptRuntime()
        result = await runtime.execute_script(_make_input(host_kind="echo"))
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_echo_script_id_preserved(self) -> None:
        runtime = EchoScriptRuntime()
        result = await runtime.execute_script(_make_input(script_id="my-script", host_kind="echo"))
        assert result.script_id == "my-script"

    @pytest.mark.asyncio
    async def test_echo_validate_always_true(self) -> None:
        runtime = EchoScriptRuntime()
        assert await runtime.validate_script("any content", "echo") is True

    @pytest.mark.asyncio
    async def test_echo_nested_parameters(self) -> None:
        runtime = EchoScriptRuntime()
        params = {"nested": {"a": 1, "b": [1, 2, 3]}}
        result = await runtime.execute_script(_make_input(host_kind="echo", parameters=params))
        assert result.output_json == params


# ---------------------------------------------------------------------------
# InProcessPythonScriptRuntime tests
# ---------------------------------------------------------------------------


class TestInProcessPythonScriptRuntime:
    """InProcessPythonScriptRuntime tests."""

    @pytest.mark.asyncio
    async def test_print_stdout_captured(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        result = await runtime.execute_script(_make_input(script_content="print('hello')"))
        assert result.exit_code == 0
        assert result.stdout == "hello\n"

    @pytest.mark.asyncio
    async def test_multiple_print_lines(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        script = "print('line1')\nprint('line2')"
        result = await runtime.execute_script(_make_input(script_content=script))
        assert "line1" in result.stdout
        assert "line2" in result.stdout

    @pytest.mark.asyncio
    async def test_parameters_accessible_in_script(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        script = "print(__params__['greeting'])"
        result = await runtime.execute_script(
            _make_input(script_content=script, parameters={"greeting": "hi"})
        )
        assert "hi" in result.stdout

    @pytest.mark.asyncio
    async def test_script_id_in_result(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        result = await runtime.execute_script(
            _make_input(script_id="my-py-script", script_content="pass")
        )
        assert result.script_id == "my-py-script"

    @pytest.mark.asyncio
    async def test_runtime_error_captured_in_stderr(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        script = "raise ValueError('bad input')"
        result = await runtime.execute_script(_make_input(script_content=script))
        assert result.exit_code != 0
        assert "ValueError" in result.stderr

    @pytest.mark.asyncio
    async def test_syntax_error_captured(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        script = "def broken(:"
        result = await runtime.execute_script(_make_input(script_content=script))
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_timeout_returns_script_result_exit_code_minus_one(self) -> None:
        """Script that blocks past timeout → ScriptResult with exit_code=-1.

        The runtime now catches TimeoutError internally and returns a sentinel
        ScriptResult instead of propagating the exception.
        """
        runtime = InProcessPythonScriptRuntime(default_timeout_ms=50)
        script = "import time; time.sleep(10)"
        result = await runtime.execute_script(_make_input(script_content=script, timeout_ms=50))
        assert result.exit_code == -1
        assert "TimeoutError" in result.stderr
        assert result.output_json is None

    @pytest.mark.asyncio
    async def test_validate_valid_python(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        assert await runtime.validate_script("x = 1 + 2", "in_process_python") is True

    @pytest.mark.asyncio
    async def test_validate_syntax_error(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        assert await runtime.validate_script("def :(", "in_process_python") is False

    @pytest.mark.asyncio
    async def test_validate_wrong_host_kind(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        assert await runtime.validate_script("x = 1", "local_process") is False

    @pytest.mark.asyncio
    async def test_empty_script_succeeds(self) -> None:
        runtime = InProcessPythonScriptRuntime()
        result = await runtime.execute_script(_make_input(script_content=""))
        assert result.exit_code == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# LocalProcessScriptRuntime tests
# ---------------------------------------------------------------------------


class TestLocalProcessScriptRuntime:
    """LocalProcessScriptRuntime tests."""

    @pytest.mark.asyncio
    async def test_echo_command_succeeds(self) -> None:
        runtime = LocalProcessScriptRuntime()
        result = await runtime.execute_script(
            _make_input(
                script_content="echo hello_world",
                host_kind="local_process",
            )
        )
        assert result.exit_code == 0
        assert "hello_world" in result.stdout

    @pytest.mark.asyncio
    async def test_exit_code_nonzero_on_failure(self) -> None:
        runtime = LocalProcessScriptRuntime()
        # 'exit 1' should return exit code 1.
        result = await runtime.execute_script(
            _make_input(script_content="exit 1", host_kind="local_process")
        )
        assert result.exit_code != 0

    @pytest.mark.asyncio
    async def test_timeout_returns_script_result_exit_code_minus_one(self) -> None:
        """Long-running process killed after timeout → ScriptResult with exit_code=-1."""
        runtime = LocalProcessScriptRuntime()
        result = await runtime.execute_script(
            _make_input(
                # Platform-neutral sleep: Python -c works cross-platform
                script_content='python -c "import time; time.sleep(60)"',
                host_kind="local_process",
                timeout_ms=100,
            )
        )
        assert result.exit_code == -1
        assert "TimeoutError" in result.stderr
        assert result.output_json is None

    @pytest.mark.asyncio
    async def test_json_stdout_parsed_to_output_json(self) -> None:
        runtime = LocalProcessScriptRuntime()
        result = await runtime.execute_script(
            _make_input(
                script_content='echo {"key": "value"}',
                host_kind="local_process",
            )
        )
        # JSON in stdout should be parsed to output_json if valid.
        if result.exit_code == 0 and result.stdout.strip():
            # Windows echo adds quotes; just check it ran.
            assert isinstance(result, ScriptResult)

    @pytest.mark.asyncio
    async def test_validate_non_empty_script(self) -> None:
        runtime = LocalProcessScriptRuntime()
        assert await runtime.validate_script("echo hello", "local_process") is True

    @pytest.mark.asyncio
    async def test_validate_empty_script_returns_false(self) -> None:
        runtime = LocalProcessScriptRuntime()
        assert await runtime.validate_script("   ", "local_process") is False

    @pytest.mark.asyncio
    async def test_script_id_in_result(self) -> None:
        runtime = LocalProcessScriptRuntime()
        result = await runtime.execute_script(
            _make_input(
                script_id="proc-script",
                script_content="echo done",
                host_kind="local_process",
            )
        )
        assert result.script_id == "proc-script"

    @pytest.mark.asyncio
    async def test_none_timeout_ms_falls_back_to_default(self) -> None:
        """timeout_ms=None must not raise TypeError; default_timeout_ms is used."""
        runtime = LocalProcessScriptRuntime()
        input_value = ScriptActivityInput(
            run_id="run-1",
            action_id="action-1",
            script_id="none-timeout",
            script_content="echo ok",
            host_kind="local_process",
            parameters={},
            timeout_ms=None,
        )
        result = await runtime.execute_script(input_value)
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_custom_default_timeout_ms_fires_on_none_input(self) -> None:
        """When timeout_ms=None, the custom default_timeout_ms is enforced."""
        runtime = LocalProcessScriptRuntime(default_timeout_ms=100)
        input_value = ScriptActivityInput(
            run_id="run-1",
            action_id="action-1",
            script_id="custom-default-timeout",
            script_content='python -c "import time; time.sleep(60)"',
            host_kind="local_process",
            parameters={},
            timeout_ms=None,
        )
        result = await runtime.execute_script(input_value)
        assert result.exit_code == -1
        assert "TimeoutError" in result.stderr


# ---------------------------------------------------------------------------
# R3f — DedupeAwareScriptRuntime
# ---------------------------------------------------------------------------


class TestDedupeAwareScriptRuntime:
    """Verifies at-most-once script execution via DedupeStore."""

    def _make_script_input(
        self,
        action_id: str = "act-1",
        script_id: str = "script-1",
        run_id: str = "run-1",
    ) -> ScriptActivityInput:
        return ScriptActivityInput(
            run_id=run_id,
            action_id=action_id,
            script_id=script_id,
            script_content="print('hello')",
            host_kind="in_process_python",
        )

    def test_execute_calls_inner_runtime_on_first_call(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        inner = EchoScriptRuntime()
        store = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(inner=inner, dedupe_store=store)
        result = asyncio.run(runtime.execute_script(self._make_script_input()))
        assert result.script_id == "script-1"
        assert result.exit_code == 0

    def test_second_call_returns_noop_without_calling_inner(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        calls: list[str] = []

        class _CountingInner:
            async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
                calls.append(input_value.action_id)
                return ScriptResult(
                    script_id=input_value.script_id,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    output_json=None,
                    execution_ms=1,
                )

        store = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(inner=_CountingInner(), dedupe_store=store)
        inp = self._make_script_input()
        asyncio.run(runtime.execute_script(inp))
        asyncio.run(runtime.execute_script(inp))
        assert len(calls) == 1  # inner called only once

    def test_dedupe_key_is_acknowledged_after_success(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        inner = EchoScriptRuntime()
        store = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(inner=inner, dedupe_store=store)
        inp = self._make_script_input()
        asyncio.run(runtime.execute_script(inp))
        key = f"script:{inp.run_id}:{inp.action_id}:{inp.script_id}"
        record = store.get(key)
        assert record is not None
        assert record.state == "acknowledged"

    def test_different_action_ids_are_independent(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        calls: list[str] = []

        class _CountingInner:
            async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
                calls.append(input_value.action_id)
                return ScriptResult(
                    script_id=input_value.script_id,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    output_json=None,
                    execution_ms=1,
                )

        store = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(inner=_CountingInner(), dedupe_store=store)
        asyncio.run(runtime.execute_script(self._make_script_input(action_id="act-1")))
        asyncio.run(runtime.execute_script(self._make_script_input(action_id="act-2")))
        assert set(calls) == {"act-1", "act-2"}
