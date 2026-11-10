# Trace / Token 兜底对齐规则

本规则适用于 `/v1/task-data/metrics`、Trace 的 Fornax 兜底和批量导出的 `metrics.json`。下游仍通过 `data-manifest.json` 的引用读取文件；基础来源 `source` 不再足以判断每个 Token 字段的来源。

`/v1/task-data/metrics` 的请求只接受 `task_id`、`source`、`start_ms`、`end_ms`；`conversation_mode`、`include_raw_spans`、`recover_raw_spans` 属于 Trace 请求，不会被 metrics 接收。

## 获取顺序

1. 读取 OpenAPI 执行视图，保留已有 Token 和主 Turn 耗时。
2. Token 汇总未确认完整时，复用已获取的 Trace 视图；单独查询指标时最多补读一次 `/task include_trace=true`。只读取唯一 `totals.tokenUsage`，不递归累加 usage。
3. 若 `total_tokens` 仍为 null，且允许 Fornax，独立补取模型调用 usage。已有耗时不阻止该步骤。同次请求已经获取的 Fornax spans 复用，已失败的请求不重复执行。
4. 总量仍未知时，读取当前 Task 的详情累计值。优先复用导出第一阶段保存的 `task.json` 或 Trace 视图中的详情；未取得详情响应时，最多补读一次不含 Trace 的 `/task`。

整份指标不存在时仍沿用 OpenAPI → Fornax 的回退；只有详情累计值可用时也交付指标，`source=task_detail`，耗时和模型调用数保持 null。显式 `source=openapi` 不查询 Fornax，`source=fornax` 不查询 OpenAPI。

额外视图和详情读取受请求超时限制，且不增加传输重试；独立 Fornax Token 补取使用同样的总超时预算。补取失败保留已取得的数值和错误。接口整体还受原有任务超时限制。

## 数值、来源和范围

| 字段 | 对齐含义 |
| --- | --- |
| `source` / `timing_source` | 基础指标来源；总耗时仍采用主 Turn 区间并集 |
| `token_source` | 当前选中或用于补缺的 Token 来源 |
| `token_field_sources` | 每个非 null Token 字段对应的来源；消费多个字段时需分别检查 |
| `token_sources` | 每次实际参与判断的来源快照：数值、完整性、覆盖、诊断、原始汇总证据和范围 |
| `token_coverage` | 所选来源的 `known_model_calls` 和 `model_calls`，未知为 null |
| `token_scope` | `task_trace`、`fornax_window` 或 `task_accumulated`，及相应边界 |
| `fallback_errors` | 上游读取错误；HTTP、指标文件、组件进度均保留 |

来源包括 `trace_summary`、`trace_view_totals`、`fornax_usage`、`task_detail_totals`。

补缺只使用与已有数值兼容的来源。任何已知字段发生冲突时，不用该候选补缺；保留已有值和候选快照，标记 `TOKEN_SOURCE_CONFLICT`，不声明完整。不会将不同来源的 input 和 output 相加构造 total；可以采用候选自身已确认口径的 total。已有值不会被候选的 null 清空。

Fornax 的完整性仅表示指定查询窗口的分页完成且已返回模型调用 usage 覆盖完整。`token_sources` 保留该窗口内的完整性；与 OpenAPI 耗时组合时，顶层 `tokens_complete=false` 并记录 `TOKEN_TIMING_SCOPE_MISMATCH`，不会将窗口统计冒充任务全历史。Fornax 分页中途出错时，已返回 spans 的统计继续保留，错误与分页不完整同时记录。

任务累计备用值只提取匹配 `task_id` 且 `id_kind=task` 的唯一 `items[].task`，不汇总多条或嵌套记录。字段使用 `input_tokens`、`output_tokens`、`cached_tokens`、`total_tokens` 等；只有存在非零累计值才采信这类无已知标记的详情统计。全零占位数据继续视为未知。

`task_detail_totals` 的完整性为 null（如发生来源冲突，则顶层为 false），并记录 `TASK_TOTALS_NOT_TRACE_USAGE`。它不证明逐调用 usage 完整，也不用于生成模型调用记录或补造调用数。仅有显式 total 时可以保留 total，其余缺失字段仍为 null。详情的 `total_execution_duration_ms` 与主 Turn 并集口径不同，不用于填充耗时。

## 独立判断可用性和完整性

| 业务维度 | 可用性 | 完整性 |
| --- | --- | --- |
| Token | `tokens_available`：至少一个 Token 统计字段已知 | `tokens_complete`：所选口径的总量、调用覆盖及执行范围已确认 |
| 总耗时 | `total_latency_available` | `total_latency_complete`：主 Turn 区间覆盖、计数及执行来源完整 |
| 首 Token 耗时 | `first_response_available` | `first_response_complete`：首响应值已知且主 Turn / 执行覆盖已确认 |

`timing_complete` 保留兼容语义：总耗时与首响应均完整。整体 `complete` 同时考虑 Token、耗时和读取错误。存在执行缺口、必需来源 coverage 的 partial/error/missing、上游错误或未知完整性时，不标记完整。来源自己的原始判定保留在 `token_sources`，不通过补取清除历史证据。

例如，有主 Turn 区间但没有 TTFT，任务详情累计 Token 为 120：

```json
{
  "source": "flux_insight_openapi",
  "token_source": "task_detail_totals",
  "tokens_available": true,
  "tokens_complete": null,
  "total_latency_available": true,
  "total_latency_complete": true,
  "first_response_available": false,
  "first_response_complete": false,
  "timing_complete": false,
  "complete": false
}
```

这是选取字段示例，具体数值仍从 `values` 读取。只分析总耗时的消费方可接受 `partial`，检查 `total_latency_available` 及 `total_latency_complete`，无需因首 Token 缺失而丢弃已知总耗时。接受 partial 不等于接受未知值；null 不转换为 0。

## 凭据更新与鉴权错误

`POST /v1/task-data/fornax/auth` 使用服务现有 API Key 鉴权规则，支持以下请求：

| 请求体 | 行为 |
| --- | --- |
| `{"action":"update","jwt":"<新个人 JWT>"}` | 更新当前进程内凭据，不写磁盘；空值或已过期值拒绝 |
| `{"action":"reload"}` 或 `{}` | 清除内存缓存，重新读取已配置凭据 |
| `{"action":"refresh"}` | 显式执行一次 Chrome SSO → IAM 刷新，允许自动刷新关闭时手动触发 |

成功返回 `{"status":"loaded","action":"...","upstream_verified":false}`，不返回 JWT。加载成功不表示已通过 Fornax 上游鉴权；下一次真实查询验证权限。刷新需要 desktop 依赖、本机 Chrome SSO 登录和 IAM 可访问；不会修改自动刷新开关或 OpenAPI 服务账号凭据。

每次使用前检查配置值；JWT 文件内容变化会取代之前的内存缓存。环境变量值优先于 JWT 文件；reload 不重读 `.env`，环境配置修改仍需重启。内存更新不跨进程共享，也不跨重启持久化。

- 过期且自动刷新关闭：`FORNAX_TOKEN_EXPIRED`。
- 已被上游 401 拒绝且无法取得新凭据：`FORNAX_UNAUTHORIZED`，不重复发送相同凭据。
- 未配置：`FORNAX_AUTH_NOT_CONFIGURED`。
- SSO / IAM 刷新失败：`FORNAX_REFRESH_FAILED`。
- 独立 Token 补取超时：`FORNAX_TOKEN_TIMEOUT`。

完全没有可用 Trace / 指标时，HTTP 502 的 `error.fallback_errors` 保留具体来源错误；仍有可用指标时返回 HTTP 200 和 partial 语义，错误放在 `fallback_errors`。因此 Mongo 无记录叠加个人 JWT 失效可以明确定位，不能仅根据 HTTP 200 判断已完整成功。

更新凭据后，可通过原有批量或单任务 retry 接口重新获取。历史归档不会自动变更。

## 验收场景

回归覆盖：耗时已知且 Token 缺失时独立 Fornax 补取；强制 OpenAPI 来源不访问 Fornax；详情非零累计值及全零／外任务排除；Trace 完全失败仍交付详情统计；来源冲突保留原值；usage 缺失和 Fornax 分页鉴权失败；执行链缺口与已知数值并存；JWT 过期、内存更新、文件轮换、重读和显式刷新；导出复用已保存详情并通过下游读取校验。
