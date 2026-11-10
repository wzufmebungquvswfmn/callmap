# 文档索引

`docs/` 只保留描述当前行为的文档；按日期记录的一次性联调、诊断和旧方案讨论统一放在 [`archive/`](archive/)，仅作追溯，不代表当前实现。

## 当前文档

| 文档 | 内容 |
| --- | --- |
| [下游数据契约对齐标准](downstream-data-contract.md) | `data-manifest.json` 字段、读取方式、兼容规则和验收口径 |
| [`tasks/<task_id>/` 文件说明](task-directory.md) | 单个任务目录会生成哪些文件，以及如何判断来源和完整性 |
| [重新获取任务数据](export-retry.md) | 单任务和整批重拉接口、参数覆盖与错误处理 |
| [Trace / Token 兜底对齐规则](trace-token-fallback.md) | 指标、Token 来源补缺、完整性判断和 Fornax 凭据错误 |
| [Trace 转 ChatML](trace-chatml.md) | 把完整 Trace 或消息导出为 ChatML 纯文本 |
| [工程打标与测试造题准入](test-admission.md) | 本地打标命令、服务接口和准入规则 |
| [RAW-001：上游 1005 与原始 spans 补齐待办](raw-spans-follow-up.md) | 尚未解决的 raw spans 上游限制与后续验收条件 |
| [产物下载恢复与未解决的上游问题](artifact-recovery.md) | 有界恢复策略和仍失败的上游样本 |
| [从 Flux 任务中识别关联仓库](repository-extraction-conclusions.md) | 26 个样本上的研究结论与方案取舍，尚未实现 |
| [Schema](schemas/) | 任务清单、指标和会话摘要的机器可读定义 |

## 历史记录

以下文档描述当时的验证环境或一次性问题，字段可见性和结果以当前上游响应为准。

| 文档 | 记录内容 |
| --- | --- |
| [2026-11-03 验证记录](archive/validation-2026-11-03.md) | 早期生产只读核查与页面截图 |
| [2026-11-04 固定筛选记录](archive/fixed-filters-2026-11-04.md) | 固定筛选项和字段映射的首次联调 |
| [2026-11-04 任务查询分页](archive/pagination-2026-11-04.md) | 页面从 collect 切换到单页查询的变更记录 |
| [2026-11-05 任务信息与产物接入验证](archive/task-data-2026-11-05.md) | Trace、指标和产物接入的首个生产样本 |
| [2026-11-09 Trace / Token 导出诊断](archive/trace-token-export-diagnosis-2026-11-09.md) | 批次 `4f8b65f0` 的个例诊断 |
| [任务目录早期改进建议](archive/task-directory-early-proposals.md) | 早期文件结构讨论和已实现/未实现事项 |
| [Token 归一化样本](archive/token-normalization-samples.json) | 个例指标样本数据 |
