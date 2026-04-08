"""HTTP service layer for cross-process deployment of KernelFacade.

This package wraps KernelFacade methods 1:1 as HTTP endpoints using Starlette,
enabling hi-agent to call agent-kernel from a separate process or container.
"""
