# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的表。模型在 `src/models/workflow.py`。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析及签核。数据库端口只发布到本机。

已存在的库上新增表（如 `corrections`）由 `create_all` 自动建立；已存在表上新增列由 `src.main.ensure_schema` 在启动时检查并补齐（当前会给 `analysis_runs` 补 `ledger_snapshot` 列）。历史分析的该列为空时，计算按旧口径直接读取固化的观测清单。
