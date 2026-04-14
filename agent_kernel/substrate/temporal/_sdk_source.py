"""Vendored Temporal SDK source loader.

When ``external/temporal-sdk-python/`` is present (added via ``git subtree``),
this module prepends its Python source tree to ``temporalio.__path__`` so that
all pure-Python modules resolve from the vendored copy.  The installed wheel
remains in ``__path__`` as a fallback so that the compiled Rust extension
(``temporalio.bridge.temporal_sdk_bridge``) is still found without rebuilding
from source.

Resolution order after patching::

    temporalio.__path__ = [
        .../external/temporal-sdk-python/temporalio,   # vendored source (pure Python)
        .../site-packages/temporalio,                  # installed wheel  (Rust .pyd/.so)
    ]

Only the first call has an effect; subsequent calls are no-ops (idempotent).
If the vendored tree is absent (e.g. a clean pip-only install), the function
returns silently and the normal installed wheel is used.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading

_logger = logging.getLogger(__name__)

# Absolute path to the vendored temporalio Python tree.
_VENDOR_TEMPORALIO: str = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),  # .../substrate/temporal/
        "..",
        "..",
        "..",
        "external",
        "temporal-sdk-python",
        "temporalio",
    )
)

# Mutable dict avoids a module-level global statement inside the function.
_STATE: dict[str, bool] = {"patched": False}
_LOCK = threading.Lock()


def ensure_vendored_source() -> None:
    """Prepend vendored Temporal Python source to ``temporalio.__path__``.

    Safe to call multiple times; only the first call performs the patch.
    If the vendored tree does not exist on disk, the call is a no-op.
    """
    if _STATE["patched"]:  # fast path — no lock needed after first call
        return
    with _LOCK:
        if _STATE["patched"]:  # second check inside lock
            return

        vendor_abs = os.path.abspath(_VENDOR_TEMPORALIO)
        if not os.path.isdir(vendor_abs):
            # Vendored tree absent — fall back to installed wheel silently.
            _STATE["patched"] = True
            return

        try:
            import temporalio as _pkg
        except ImportError:
            # temporalio not installed at all — callers will surface a clear error.
            _STATE["patched"] = True
            return

        installed_paths: list[str] = list(_pkg.__path__)
        if vendor_abs in installed_paths:
            # Already first entry (e.g. re-imported after reload).
            _STATE["patched"] = True
            return

        _pkg.__path__ = [vendor_abs, *installed_paths]  # type: ignore[assignment]

        # Patch bridge subpackage so the Rust extension is still found in the
        # installed wheel.  ``temporalio/bridge/__init__.py`` is an empty docstring
        # — importing it has no side effects.
        try:
            import temporalio.bridge as _bridge

            vendor_bridge = os.path.join(vendor_abs, "bridge")
            installed_bridge = [
                os.path.join(p, "bridge")
                for p in installed_paths
                if os.path.isdir(os.path.join(p, "bridge"))
            ]
            _bridge.__path__ = [vendor_bridge, *installed_bridge]  # type: ignore[assignment]
        except ImportError:
            pass

        importlib.invalidate_caches()
        _STATE["patched"] = True
        _logger.debug(
            "Temporal vendored source activated: %s (installed fallback: %s)",
            vendor_abs,
            installed_paths,
        )
