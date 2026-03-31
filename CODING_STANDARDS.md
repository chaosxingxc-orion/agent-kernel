# Agent Kernel Coding Standards

本项目从即日起采用以下两份规范作为强制编码风格基线：

1. Python: https://google.github.io/styleguide/pyguide.html
2. TypeScript: https://google.github.io/styleguide/tsguide.html

## 适用范围

- `python_src/` 与 `python_tests/` 必须遵循 Google Python Style Guide。
- `src/` 与 `tests/` 必须遵循 Google TypeScript Style Guide。

## 执行原则

- 新增代码、重构代码、修复代码都必须遵循上述规范。
- 若历史代码与规范冲突，修改触达处按“增量对齐”原则修正到规范风格。
- 评审与合并以该规范为准，不再使用个人偏好作为风格依据。

## 本仓库执行命令

- TypeScript 类型与风格门禁：`npm run lint:ts`
- TypeScript 测试：`npm test`
- Python 风格门禁：`npm run lint:py`
- Python 测试：`python -m pytest -q`
