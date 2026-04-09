# Agent Kernel Coding Standards

本仓库以 Google 风格为基准，要求新代码和修改代码遵循以下规范：

1. Python: https://google.github.io/styleguide/pyguide.html
2. TypeScript: https://google.github.io/styleguide/tsguide.html

## 适用范围

- `agent_kernel/` 与 `python_tests/` 使用 Python 规范。
- 任何新增 JS/TS 代码使用 TypeScript 规范。

## 注释与 Docstring 要求

- 所有对外可见模块、类、函数、方法都应有清晰 docstring。
- docstring 采用 Google 风格分段（`Args`、`Returns`、`Raises` 等）。
- 参数说明必须与函数签名一致，避免遗漏或过时描述。

## 执行原则

- 新增代码必须满足规范后再合并。
- 改动历史代码时执行“增量对齐”，至少保证修改触达区域符合规范。
- 禁止以个人偏好替代项目统一风格。

## 本仓库检查命令

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```

严格 docstring 检查（推荐在源码目录执行）：

```bash
python -m ruff check agent_kernel --select D
```
