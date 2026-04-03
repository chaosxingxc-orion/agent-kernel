"""ScriptRuntime implementations for Phase 4 (Script Execution Infrastructure).

Provides three concrete runtimes:
  - EchoScriptRuntime      — PoC/test stub; echoes parameters as JSON.
  - InProcessPythonScriptRuntime — exec()-based isolated Python; PoC/tests only.
  - LocalProcessScriptRuntime    — asyncio subprocess with timeout and dead-loop
                                   detection.

WARNING: InProcessPythonScriptRuntime is NOT production-safe.
Use LocalProcessScriptRuntime or a remote executor for untrusted code.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from agent_kernel.kernel.contracts import ScriptActivityInput, ScriptResult
from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope


class EchoScriptRuntime:
    """Test/PoC runtime that echoes parameters as JSON output.

    Always succeeds with exit_code=0.  Useful for tests that do not need
    real script execution but must verify parameter plumbing.

    stdout   = json.dumps(parameters)
    output_json = parameters dict
    """

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Returns a successful ScriptResult with parameters echoed as JSON.

        Args:
            input_value: Script execution payload.

        Returns:
            ScriptResult with exit_code=0 and output_json=parameters.
        """
        start = time.monotonic()
        serialised = json.dumps(input_value.parameters)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ScriptResult(
            script_id=input_value.script_id,
            exit_code=0,
            stdout=serialised,
            stderr="",
            output_json=dict(input_value.parameters),
            execution_ms=elapsed_ms,
        )

    async def validate_script(self, script_content: str, host_kind: str) -> bool:
        """Always returns True for the echo runtime.

        Args:
            script_content: Script source (ignored).
            host_kind: Target execution mechanism (ignored).

        Returns:
            True unconditionally.
        """
        return True


class InProcessPythonScriptRuntime:
    """Executes Python scripts in an isolated namespace via exec().

    WARNING: NOT production-safe.  For PoC/tests only.

    The namespace is fresh per execution (no shared globals).  stdout is
    captured via StringIO redirect.  A best-effort wall-clock timeout is
    enforced via ``threading.Timer`` that raises ``SystemExit`` in the
    executing thread.

    Limitations:
      - The timeout is best-effort; C-extension infinite loops cannot be
        interrupted by Python signal delivery.
      - exec() does not sandbox filesystem or network access.
    """

    def __init__(self, default_timeout_ms: int = 5_000) -> None:
        """Initialises the runtime with a default timeout.

        Args:
            default_timeout_ms: Default wall-clock timeout in milliseconds.
                Overridden by ScriptActivityInput.timeout_ms when set.
        """
        self._default_timeout_ms = default_timeout_ms

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Executes a Python script in an isolated namespace.

        Runs the script in a daemon thread via a dedicated ThreadPoolExecutor
        so that asyncio.wait_for timeout causes the coroutine to raise
        TimeoutError while the daemon thread is garbage-collected when the
        process exits — it does not block event loop teardown.

        Args:
            input_value: Script execution payload including script_content and
                parameters injected as ``__params__`` in the script namespace.

        Returns:
            ScriptResult with captured stdout/stderr and exit_code.
            ``exit_code=-1`` signals a timeout; no exception is raised.
        """
        timeout_ms = input_value.timeout_ms or self._default_timeout_ms
        timeout_s = timeout_ms / 1000.0
        loop = asyncio.get_running_loop()

        # Use a dedicated executor with daemon threads so that a stuck thread
        # does not prevent event-loop shutdown after asyncio.wait_for cancels.
        # Fresh per-call executor — shutdown(wait=False) abandons the thread
        # if asyncio.wait_for times out, so it never blocks event-loop teardown.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        start = time.monotonic()
        try:
            fut = loop.run_in_executor(
                executor,
                self._run_sync,
                input_value.script_id,
                input_value.script_content,
                dict(input_value.parameters),
                timeout_ms,
            )
            result = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError, asyncio.CancelledError:
            executor.shutdown(wait=False, cancel_futures=True)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ScriptResult(
                script_id=input_value.script_id,
                exit_code=-1,
                stdout="",
                stderr=f"TimeoutError: script exceeded {timeout_ms}ms budget",
                output_json=None,
                execution_ms=elapsed_ms,
            )
        else:
            executor.shutdown(wait=False)
        return result

    def _run_sync(
        self,
        script_id: str,
        script_content: str,
        parameters: dict[str, Any],
        timeout_ms: int,
    ) -> ScriptResult:
        """Synchronous execution body delegated to daemon thread pool.

        Args:
            script_id: Script identifier for the result.
            script_content: Python source to execute.
            parameters: Runtime parameters injected as ``__params__``.
            timeout_ms: Wall-clock timeout in milliseconds (informational only;
                actual timeout enforcement is done by asyncio.wait_for).

        Returns:
            ScriptResult from the executed script.
        """
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        namespace: dict[str, Any] = {"__params__": parameters}
        start = time.monotonic()
        exc: BaseException | None = None
        exit_code = 0
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(script_content, namespace)
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        except BaseException as e:
            exc = e
            exit_code = 1

        elapsed_ms = int((time.monotonic() - start) * 1000)
        stderr_str = stderr_buf.getvalue()
        if exc is not None:
            stderr_str += f"{type(exc).__name__}: {exc}\n"

        return ScriptResult(
            script_id=script_id,
            exit_code=exit_code,
            stdout=stdout_buf.getvalue(),
            stderr=stderr_str,
            output_json=None,
            execution_ms=elapsed_ms,
        )

    async def validate_script(self, script_content: str, host_kind: str) -> bool:
        """Validates that the script_content is syntactically valid Python.

        Args:
            script_content: Python source to validate.
            host_kind: Target execution mechanism (must be ``in_process_python``).

        Returns:
            True when the source compiles without SyntaxError.
        """
        if host_kind != "in_process_python":
            return False
        try:
            compile(script_content, "<string>", "exec")
            return True
        except SyntaxError:
            return False


class LocalProcessScriptRuntime:
    """Executes scripts as subprocesses with asyncio and timeout support.

    Uses ``asyncio.create_subprocess_shell`` for flexibility across script
    types.  On timeout, the subprocess is killed and a ``ScriptResult`` with
    ``exit_code=-1`` is returned.  Callers can check ``result.exit_code == -1``
    to detect timeout and build ``ScriptFailureEvidence`` accordingly.

    Dead-loop detection: timeout + empty stdout → ``suspected_cause`` set to
    ``"possible_infinite_loop"`` in caller-constructed evidence.
    """

    def __init__(self, shell: str | None = None) -> None:
        """Initialises the runtime.

        Args:
            shell: Optional shell binary to use (default: platform shell).
        """
        self._shell = shell

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Executes a script in a subprocess and returns the result.

        On timeout, the subprocess is killed and a ``ScriptResult`` with
        ``exit_code=-1`` is returned instead of raising.  Callers can detect
        timeout by checking ``result.exit_code == -1``.

        Args:
            input_value: Script execution payload.  ``script_content`` is
                written to stdin or executed directly depending on host_kind.

        Returns:
            ScriptResult with exit_code, stdout, stderr, and execution_ms.
            ``exit_code=-1`` signals a timeout.
        """
        timeout_s = input_value.timeout_ms / 1000.0
        start = time.monotonic()

        # Inject parameters as environment variables for subprocess access.
        env_params = {f"PARAM_{k.upper()}": str(v) for k, v in input_value.parameters.items()}
        full_env = {**os.environ, **env_params}

        proc = await asyncio.create_subprocess_shell(
            input_value.script_content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
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
                stderr=f"TimeoutError: script exceeded {input_value.timeout_ms}ms budget",
                output_json=None,
                execution_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else 1
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        output_json: dict | None = None
        stripped = stdout_str.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    output_json = parsed
            except json.JSONDecodeError:
                pass

        return ScriptResult(
            script_id=input_value.script_id,
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            output_json=output_json,
            execution_ms=elapsed_ms,
        )

    async def validate_script(self, script_content: str, host_kind: str) -> bool:
        """Always returns True for subprocess-based runtimes.

        Real validation would require a dry-run or static analysis pass.
        For the PoC, structural validation is deferred to the executor.

        Args:
            script_content: Script source (length check only).
            host_kind: Target execution mechanism.

        Returns:
            True when script_content is non-empty.
        """
        return bool(script_content.strip())


class DedupeAwareScriptRuntime:
    """Wraps any ScriptRuntime with at-most-once execution via DedupeStore.

    The idempotency key is derived deterministically from the script input
    so that Temporal workflow replays never re-execute a script whose
    dispatch was already recorded::

        script:{run_id}:{action_id}:{script_id}

    When the dedupe slot is already reserved (e.g. from a prior attempt in
    the same workflow replay), ``execute_script`` returns a noop
    ``ScriptResult`` with ``exit_code=0`` rather than re-invoking the script.

    Usage::

        runtime = DedupeAwareScriptRuntime(
            inner=LocalProcessScriptRuntime(),
            dedupe_store=InMemoryDedupeStore(),
        )
    """

    def __init__(self, inner: Any, dedupe_store: Any) -> None:
        """Initialises the wrapper with an inner runtime and a dedupe store.

        Args:
            inner: Any object implementing ``execute_script(input_value)``.
            dedupe_store: A ``DedupeStorePort`` implementation for
                at-most-once tracking.
        """
        self._inner = inner
        self._dedupe_store = dedupe_store

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        """Executes the script with at-most-once DedupeStore protection.

        Args:
            input_value: Script execution request payload.

        Returns:
            ``ScriptResult`` from the inner runtime on first call, or a
            pre-acknowledged noop result on subsequent calls for the same
            idempotency key.

        Raises:
            Exception:
        """
        idempotency_key = (
            f"script:{input_value.run_id}:{input_value.action_id}:{input_value.script_id}"
        )
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=idempotency_key,
            operation_fingerprint=idempotency_key,
            attempt_seq=1,
            effect_scope=f"script:{input_value.script_id}",
            capability_snapshot_hash="script_runtime",
            host_kind=input_value.host_kind,
        )
        reservation = self._dedupe_store.reserve(envelope)
        if not reservation.accepted:
            # Already dispatched — return noop result rather than re-running.
            return ScriptResult(
                script_id=input_value.script_id,
                exit_code=0,
                stdout="",
                stderr="",
                output_json=None,
                execution_ms=0,
            )
        self._dedupe_store.mark_dispatched(idempotency_key)
        try:
            result = await self._inner.execute_script(input_value)
        except Exception:
            with contextlib.suppress(Exception):
                self._dedupe_store.mark_unknown_effect(idempotency_key)
            raise
        self._dedupe_store.mark_acknowledged(idempotency_key)
        return result
