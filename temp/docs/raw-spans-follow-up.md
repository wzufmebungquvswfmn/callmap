# RAW-001：上游 1005 与原始 spans 补齐待办

**状态：未解决，保留待办。记录日期：2026-11-06。**

部分大任务的原始 `raw_spans` 超过 Flux Insight OpenAPI 自身预算，当前客户端无法保证拉全。后续平台提供分块、分页或预算调整能力，或 Fornax 认证恢复时，重新评估此项。不要仅删除警告或提高客户端大小上限后将其标记为完成。

## 影响与边界

- 接口：`POST /openapi/v1/trace/query`，`include_raw_spans=true`。
- 上游业务码 `1005` 表示 Span 数、输入 BSON 或响应 JSON 超限；HTTP 状态可能仍为 200。
- `1005` 与本服务的 `DOWNLOAD_LIMIT_EXCEEDED` 不同。本服务已将单响应及会话补取累计上限提高到 256 MiB，但不能修改上游预算。
- 执行 read-model 和会话可以成功读取，原始 spans 仍然缺失。分别检查 `execution_complete`、`conversation_complete`、`raw_spans_complete`；总 `complete` 只覆盖当前执行视图和会话范围。

## 官方文档建议

依据项目内的 [Flux Insight 开放接口说明](../[OpenAPI]Flux%20Insight%20开放接口说明/[OpenAPI]Flux%20Insight%20开放接口说明.md)，可搜索“1005 请关闭 raw spans 后重试”及“错误处理建议”：

> `/trace/query`：不建议直接重试。1005 请关闭 raw spans 后重试，仍超限时联系服务方。

> `/task` 共享视图：1005 改用 messages_only / simplified 或 /task/trace 渐进读取。

后者用于会话或视图读取，不能当作原始 spans 的分页接口。当前文档没有提供 `/trace/query` raw spans 的分页游标、时间分片或客户端预算调整参数。

按 `worker_session_id` 拆分是本服务基于已有参数实现的有限补取尝试，**不是官方文档承诺能解决 1005 的方案**。

## 已实现的处理

**默认流程已收敛为执行视图和完整会话。** `include_raw_spans=false`、`recover_raw_spans=false`；未请求 raw 时状态为 `not_requested`、完整性为 null，不会仅因此显示部分可用。下面的 raw 回退仅在显式请求 raw 时执行，按会话补取还需显式开启 `recover_raw_spans=true`。此待办及实验代码保留，默认导出不承担其开销。

1. Task 原始 spans 请求返回 `1005` 或本地大小超限后，关闭 raw，保留 OpenAPI read-model。
2. 多会话任务按候选会话 ID 补取：最多 8 个候选、总计 60 秒、累计 256 MiB；每个候选不重试临时故障，认证失败停止后续候选。
3. 只有一个候选时无法拆分，记录 `RAW_RECOVERY_NOT_SPLITTABLE` 并跳过，避免重复无收益的大查询。
4. 会话 ID 可能是逻辑展示 ID，不能据此断定存在独立 Mongo 物理会话。查询结果可能包含其他任务的 spans，需要过滤任务归属、去重并核对 Task 证据和数量。
5. 补取结果通过 `raw_recovery` 和 `raw_spans_complete` 返回并落盘；保留已取得的执行视图、会话和部分 spans。

相关代码：

- [raw_recovery.py](../src/flux_task_service/raw_recovery.py)：有界会话补取、任务归属过滤和覆盖核对。
- [resources.py](../src/flux_task_service/resources.py)：OpenAPI 优先及 raw 超限处理。
- [config.py](../src/flux_task_service/config.py)：补取预算与大小配置。
- [test_data.py](../tests/test_data.py)：HTTP 合同和补取边界验证。

## 实测证据

原批次：`d2fbdf09b9bb470a8650790cd8af4ad2`。

| Task ID | 分会话实测 | 结论 |
| --- | --- | --- |
| `97651448908683785` | 主会话 `1005`，6 个子会话候选 `3004`（指定 Session 无 Span） | raw 未补齐；展示会话不能直接作为物理存储分片 |
| `97651448824797705` | 唯一候选会话仍返回 `1005`，约 26.6 秒 | 已改为单候选直接跳过补取 |
| `97651449059744265` | 唯一候选会话仍返回 `1005`，约 24.6 秒 | 已改为单候选直接跳过补取 |
| `97651448908880393` | 提高本地上限后成功读取 531 个 raw spans，响应约 117.5 MiB | 这一项是本地大小限制，已解决，不属于待解决的上游 1005 |

验证导出：`fde1c92afaf6448295a916192bbfe512`，包含第一个任务的执行视图和完整会话，但 `raw_spans_complete=false`、恢复数量为 0。本地导出可能被清理，因此后续复测以 Task ID 和接口结果为准。

当时 Fornax 验证返回 `FORNAX_UNAUTHORIZED`。恢复 Fornax 后仍需核对数据范围和字段，不能默认与 OpenAPI raw 完全等价。

## 何时重新处理

- [ ] 联系 Flux Insight 服务方，确认这三个样本各自触发的是 Span 数、输入量还是响应量预算，以及能否安全调整。
- [ ] 确认是否新增 raw 分页、分块下载、可靠的物理会话枚举或其他可缩小范围的接口。
- [ ] 若恢复 Fornax，验证任务过滤、主会话及子 Agent 覆盖、时间范围和原始字段差异，再决定是否用于补齐。
- [ ] 使用上述三个样本重新验证；执行预算必须包含全部请求、分页、认证刷新和回退，不允许无限重试。

## 完成条件

找到可用的受限读取路径，能明确核对任务归属、覆盖范围和去重后的原始 spans 数量；已有执行视图和会话继续保留；无法读取的分片准确报告。只有确认原始 spans 的完整性后，才允许 `raw_spans_complete=true`。

如果只能取得不同字段或不同范围的 Fornax 记录，应独立标明来源及完整性范围，不将其包装成已经补齐 OpenAPI 原始数据。
