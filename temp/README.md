# Flux Task Service

通过 Flux Insight 服务账号 OpenAPI，按包含和排除条件查询真实 Flux `task_id`，继续获取任务详情、Trace、指标、会话和产物文件。提供 HTTP JSON 接口、浏览器筛选页面和后台 ZIP 导出，以及供下游读取导出结果的版本化任务清单和 Python 读取器。

**筛选接口返回字符串类型的 task ID 列表及扫描统计；任务数据接口和后台导出可继续获取详情、Trace、metrics 与产物文件。** 服务自动跳过 `session` 记录；所有示例中的账号、分类和 ID 均为示意值。

**下游以 `data-manifest.json` 为唯一数据读取入口，按其中的引用、状态和完整性消费数据，无需推断目录结构。** 接入示例见下文“下游读取与验证”；完整字段、Schema、会话正文示例和兼容规则见 [下游数据契约对齐文档](docs/downstream-data-contract.md)。

完整 Trace 可转换为保留推理、工具调用和子 Agent 来源的 ChatML 纯文本，也可通过 `--mode user` / `--mode assistant` 仅导出用户提示词或模型回复，默认还原换行与中文字符。命令与输出说明见 [Trace 转 ChatML](docs/trace-chatml.md)。

> **已知限制 · 待后续解决：部分大任务无法拉全原始 spans。** Flux Insight `/trace/query` 返回 `1005` 时，提高本服务的大小上限不能解除上游预算。默认只读取执行视图和会话；raw 及按会话补取均需显式开启。完整背景、实测任务、官方建议和后续验收条件见 [RAW-001：上游 1005 与原始 spans 补齐待办](docs/raw-spans-follow-up.md)。执行视图和会话完整不代表原始 spans 已拉全，请检查 `raw_spans_status` 和 `raw_spans_complete`。

## 启动与访问

需要 Python 3.12+ 和 uv，在项目目录执行：

```bash
uv sync --locked
cp .env.example .env
uv run --locked flux-task-service
```

在 `.env` 中配置 `FLUX_SERVICE_SECRET_FILE` 或 `FLUX_SERVICE_SECRET`。示例文件使用 `../secret`；相对路径以进程工作目录为基准，部署时可改为绝对路径。两者同时配置时优先使用 `FLUX_SERVICE_SECRET`，同名环境变量优先于 `.env`。服务账号需要 Flux Insight OpenAPI 的 IAM `access` 权限。

如果项目移动或改名后出现 `VIRTUAL_ENV ... does not match`，说明终端或激活脚本仍指向旧环境。在当前项目目录执行以下命令，重新生成激活脚本，并按锁文件重装依赖以修复命令入口中的旧路径：

```bash
unset VIRTUAL_ENV
uv venv --allow-existing --python .venv/bin/python .venv
source .venv/bin/activate
uv sync --locked --reinstall
uv run --locked flux-task-service
```

查询返回 503 时，查看响应 JSON 的 `error.code` 和服务端的 `Service request failed` 日志。该环境警告本身不代表认证或查询失败。IAM 的网络异常、限流和临时服务错误按 `FLUX_MAX_RETRIES` 重试；凭据被拒绝时直接报错。错误日志包含错误码、上游业务码和异常类型，不记录凭据或请求正文。

| 入口 | 默认地址 | 用途 |
| --- | --- | --- |
| Task reflux | http://127.0.0.1:18080/ | 查询与查看任务、批量下载资料、筛选候选及人工复核 |
| Swagger UI | http://127.0.0.1:18080/docs | 查看参数并调试接口 |
| OpenAPI 定义 | http://127.0.0.1:18080/openapi.json | 获取机器可读的接口定义 |

通过 `FLUX_HOST`、`FLUX_PORT` 修改监听地址。以下请求示例使用默认地址。

浏览器页面包含任务、下载、候选三个入口。任务摘要自动批量读取；详情和统计独立加载，支持目录浏览、小文件预览与版本选择。下载默认获取会话与分析，可选交付文件或完整资料。批次历史由服务端分页提供，运行中也可切换查看；从批次打开的任务读取已保存资料，明确切换后才请求实时数据。操作流程和接口映射见 [前端说明](docs/frontend-workspace.md)。

## 对外接口总览

| 方法 | 路径 | 输入 | 输出 |
| --- | --- | --- | --- |
| POST | `/v1/task-ids/query` | 筛选条件、页大小、游标 | 一页 task ID、扫描统计、下一页游标 |
| POST | `/v1/task-ids/collect` | 筛选条件、扫描页大小 | 全部匹配的去重 task ID、扫描统计 |
| POST | `/v1/filter-options` | 选项字段、筛选范围、分类父路径、游标 | 当前批次选项、可用性、扫描统计、下一批游标 |
| GET | `/health/live` | 无 | 进程存活状态 |
| GET | `/health/ready` | 无 | 服务账号凭据配置状态 |

业务接口使用 `Content-Type: application/json`，成功返回 HTTP 200 和 JSON 对象。设置 `FLUX_API_KEY` 后，全部业务接口（含导出进度、取消和下载）均需传入：

```http
Authorization: Bearer <FLUX_API_KEY>
```

未设置 `FLUX_API_KEY` 时，业务接口不要求访问密钥。首页、健康检查和接口文档不要求该密钥。服务账号 secret 由服务端用于向 IAM 获取 JWT，调用方只需提供服务访问密钥。

## 获取任务信息与产物

筛选得到 ID 后，可创建后台导出，也可单独调用数据接口。Trace 和 metrics 的默认来源顺序为 **Flux Insight OpenAPI → Fornax**；未迁入旧的 Insight Web API 和 Web 消息回退。任务详情、产物、版本、GUI 报告和对话原文仅走 OpenAPI。

| 方法 | 路径 | 请求 | 返回 |
| --- | --- | --- | --- |
| POST | `/v1/task-data/detail` | `task_id` | 上游任务详情 `data`，保留字段、归档内容及状态 |
| POST | `/v1/task-data/trace` | `task_id`、可选 `source`、`start_ms`、`end_ms` | Trace 数据、来源、完整性、警告和兜底错误 |
| POST | `/v1/task-data/metrics` | `task_id`、可选 `source`、`start_ms`、`end_ms` | Token、耗时指标、来源、完整性、警告和兜底错误 |
| POST | `/v1/task-data/fornax/auth` | `action=update/reload/refresh`；update 需要 `jwt` | 凭据加载状态，不返回 JWT；详见[对齐文档](docs/trace-token-fallback.md) |
| POST | `/v1/task-data/artifacts/list` | `task_id` 或 `worker_session_id` | 当前已上传文件清单 |
| POST | `/v1/task-data/artifacts/raw` | 同上，加清单中的 `path` | 原始文件字节及下载响应头 |
| POST | `/v1/task-data/artifacts/versions` | `task_id` 或 `worker_session_id` | 已采集版本信息 |
| POST | `/v1/task-data/artifacts/version-diff` | 同上，加完整版本区间 | 已采集区间 Diff |
| POST | `/v1/task-data/artifacts/gui-test/case-result` | `task_id` 或 `worker_session_id` | 已采集 GUI 报告用例结果 |
| POST | `/v1/task-data/artifacts/gui-test/screenshots` | 同上，加 `path` 子串 | 旧采集截图的首个匹配对象字节 |
| POST | `/v1/task-data/conversation` | `task_id` 或 standalone `session_id`，加 `action` | OpenAPI 对话索引或消息批次 |

产物标识恰好传一个，必须为正 int64 数字字符串。为固定读取的会话，优先使用清单返回的 `worker_session_id`。`raw` 仅接受规范化相对路径，校验返回的身份、SHA256 和文件大小，不支持 `version_id` 读历史文件；成功可返回空文件和二进制文件，失败返回错误 JSON。新清单中的图片也可用 `raw` 下载。

Diff 接受 `from_version_id` + `to_version_id` 或 `from_conversation_version` + `to_conversation_version`，区间下界可为 0；两个区间完整提供时，上游优先使用物理版本。它只读取已采集比较，不在线计算任意区间。

对话接口 `action` 默认为 `inventory`，首次不传票据或游标；后续使用 `chunk` / `messages`，携带 inventory 返回的 `ticket`，并在需要时传入返回的 `cursor`。该接口需要 IAM `access_sensitive`；`session_id` 只用于 standalone 会话，不是 `worker_session_id` 的别名。

### Trace 与 metrics 的来源和输出

```bash
curl -X POST http://127.0.0.1:18080/v1/task-data/trace \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"76991823008039229","source":"auto"}'
```

`source` 支持 `auto`（默认）、`openapi`、`fornax`。OpenAPI 使用服务账号 JWT，发送 `X-Flux-Trace-Contract: 2`，默认 `include_raw_spans=false`，获取执行 read-model；完整会话单独读取。Fornax 使用独立的个人 JWT，按 `thread_id=task_id` 自动分页拉取 spans，并复用旧脚本的格式适配与调用树逻辑。

只有 OpenAPI 没有可用执行数据时才尝试 Fornax；已有 read-model 但 raw 缺失、会话不完整或执行标记不完整，不会为此换来源。Fornax 失败仍继续尝试 OpenAPI 会话读取。metrics 保留 OpenAPI 的可用耗时；整个指标不可计算或补读 Trace 视图后 `total_tokens` 仍未知时，尝试 Fornax。Token 仍未知时可使用任务详情的非零累计值，明确标记来源和未确认完整性。同一任务的 Trace 和 metrics 共用执行读取流程，仅获取 metrics 时不请求 raw。

metrics 请求只接受 `task_id`、`source`、`start_ms`、`end_ms`。`conversation_mode`、`include_raw_spans`、`recover_raw_spans` 属于 Trace 请求，传给 metrics 会返回 422。

需要原始 spans 时，在 Trace 请求或导出请求中传 `include_raw_spans=true`。原始请求超限后，只增加一次不含 raw 的 OpenAPI read-model 回退，并记录 `openapi_raw` 错误；默认不按会话补取。raw 未请求时，`raw_spans_status="not_requested"`、`raw_spans_complete=null`，不会仅因此产生警告或将导出标记为部分可用。请求 raw 时单独报告 `complete`、`partial` 或 `unavailable`，不混用执行视图完整性。

Trace 响应包含 `task_id`、`source`、`format`、`complete`、`execution_complete`、`conversation_complete`、`conversation`、`spans`、`warnings`、`fallback_errors`。

Trace 默认使用单 ID `/task` 的 `include_trace=true` 请求完整 Trace 视图；可设置 `conversation_mode="messages"`，仅请求 `messages_only=true` 的公开消息正文。响应过大时再使用 `/task/trace` 的 inventory/票据分页读取。`conversation.documents` 保留原始正文响应或批次，`conversation.content_available` 表示是否实际读到正文，`conversation.errors` 说明不可用原因。`conversation.completeness_scope`、`scope`、`coverage`、`omitted`、`resources_not_embedded` 保留上游的覆盖范围与不嵌入资源说明；`complete=true` 始终限定于该范围，不代表所有历史及外部资源。`raw_spans_complete` 单独标记原始 spans 完整性。只导出 trace 也会尝试并保存会话，不再依赖额外勾选 conversation。

`execution_complete` 保留执行数据的完整性标记；`conversation_complete` 单独标记正文完整性；总 `complete` 只有两者都明确完整时才为 `true`。metadata-only read-model、Fornax 分页结束或模型调用输入都不能替代完整会话。如果正文接口拒绝，保留执行数据并返回 `CONVERSATION_FORBIDDEN`，整体标记不完整。OpenAPI 来源额外包含原始 `data`；Fornax 来源包含 `tree` 和查询 `window`；兜底前已获取 OpenAPI 数据时，HTTP 响应通过 `openapi_data` 一并保留。`complete` 为 `true` / `false` / `null`，未知时不会标记拉全。Fornax 的完整性仅指指定时间窗口的分页遍历；不会扩展为任务全部历史。

按会话补取是默认关闭的实验能力，需同时传 `include_raw_spans=true`、`recover_raw_spans=true`。只有原始请求返回上游 `1005` 或超过单响应限制时，才从 Task read-model 获取候选，使用仅含 `worker_session_id` 的 OpenAPI 请求补取 raw。只有一个候选时直接记录 `RAW_RECOVERY_NOT_SPLITTABLE` 并跳过。候选 ID 可能是逻辑展示 ID，不保证对应独立物理会话。开启后的预算最多 8 个候选、总计 60 秒、累计 256 MiB，并受外层超时限制。每个候选不重试临时故障；凭据或权限失败停止后续候选。

会话级查询可能包含同一物理会话中其他 Task 的 spans。服务只保留明确属于本 Task 的记录，或无 Task 标识但能由 Task read-model 的 span 证据确认的记录；按 `(trace_id, span_id)` 去重。原始 Task `data` 保留不变，补取的原始记录放在 `spans`，`raw_recovery` 给出逐会话结果、排除数量、预算错误和已恢复数量。只有 Task read-model 完整、全部候选读取完整、证据覆盖且恢复数量与 Task span 总数相等时，才将 `raw_spans_complete` 标记为 true。未拉全时保留已获取的 spans 和完整会话，不用分会话成功推断任务已经拉全。批量导出进度也保留 `raw_spans_complete` 与 `raw_recovery`。

metrics 响应包含 `task_id`、`source`、`complete`、`values`、`warnings`、`fallback_errors`，字段口径沿用上级脚本：

| `values` 字段 | 含义 |
| --- | --- |
| `input_tokens` / `output_tokens` / `reasoning_tokens` / `cached_tokens` | 模型 Token 统计；标准化后的 input 包含缓存输入，缺失或无效字段为 null |
| `total_tokens` | 同一来源的已知 input + output，包含缓存输入；详情备用来源也可保留独立报告的 total。输入计入口径未确认或总量不一致时为 null |
| `non_cached_input_tokens` | 已确认的非缓存输入，通常为 input − cached |
| `cache_creation_input_tokens` | 来源明确报告的缓存创建输入；未报告时为 null |
| `clean_total_token` | 已确认的非缓存输入 + output；缓存或计入口径未知时为 null，不将异常负值截断成零 |
| `end_to_end_latency_ms` | 主会话最早 turn 开始到最晚 turn 结束 |
| `total_latency_ms` | 主会话 turn 时间区间的并集长度，排除用户等待 |
| `interaction_latency_ms` | max(end_to_end − total, 0) |
| `latency_ms_first_resp` | 最早模型调用的首 Token 耗时；OpenAPI 未提供模型调用记录时沿用最早主 turn 的 TTFT。缺少开始时间或最早调用的 TTFT 时为 null，不用后续调用代替 |
| `model_call_count` | 模型调用数量 |

已知零值保留为 `0`，缺失、无效或未确认的 Token 值为 `null`。统计不完整时 `values` 只代表当前来源的已知量，不能直接当作全部消耗；其余可计算字段继续保留。Token 完整性独立于执行视图：`tokens_available` 表示是否有可靠的已知统计，`tokens_complete` 表示所选统计的总量／调用覆盖是否完整；`complete` 同时考虑 Token 和耗时完整性。可选的细分字段仍可能为 null。

耗时只使用有效的主会话 Turn 时间区间，没有可用区间时三个耗时值均为 `null`，真实的零耗时和零首响应保留为 `0`。`timing_coverage` 记录 `known_primary_turns`（有效区间数）、`primary_turns`（应参与统计的主 Turn 数）及 `counts_consistent`（已返回 Turn 数与显式 Session／Task Turn 计数是否一致）；Task Turn 总数包含子会话，不直接作为主 Turn 总数。`turn_timing_complete` 只有覆盖全部主 Turn、计数一致且执行来源明确完整时为 true；`first_response_available` 单独表示首响应值是否已知，`timing_complete` 要求前述区间完整且首响应已知。来源完整性未知时不标记 true。不完整仍保留已知区间的计算结果，原因写入 `timing_diagnostics`，导出 `metrics.json` 和组件进度均保留这些字段，总耗时通过 `total_latency_available` / `total_latency_complete`、首 Token 耗时通过 `first_response_available` / `first_response_complete` 分别判断。Token 补取不覆盖已有耗时。

Token 补取先复用已取得的 Trace 视图中唯一的 `totals.tokenUsage`，不遍历相加重复嵌入的 usage。单独查指标且汇总 Token 未确认完整时，最多额外读取一次 `/task` 完整视图。总量仍未知且 `source=auto` 时，独立查询 Fornax；再按需使用已保存任务详情，缺少详情响应时最多补读一次 `/task`。额外视图／详情读取不叠加传输重试，独立 Fornax Token 补取也受请求超时总预算约束。显式 `source=openapi` 不访问 Fornax；`source=fornax` 不访问 OpenAPI。

`token_source` 包括 `trace_summary`、`trace_view_totals`、`fornax_usage`、`task_detail_totals`。`token_sources` 保存各来源的数值、覆盖、范围、完整性和诊断；`token_field_sources` 指明每个已知 Token 字段的来源。只在来源数值兼容时补缺；冲突则保留原值和备用来源记录，标记 `TOKEN_SOURCE_CONFLICT`。不会把不同来源的 input/output 相加推导总量。

任务详情累计值只提取匹配当前 Task 的 `items[].task`，全零且无已知标记的累计值不采信；`task_detail_totals` 的完整性未确认，不能代表完整 Trace 消耗，也不补造调用数。任务详情的 `total_execution_duration_ms` 不代替主 Turn 时间并集。只有任务累计值可用时，指标 `source=task_detail`，耗时和调用数仍为 null。Fornax Token 只对查询窗口负责，与 OpenAPI 耗时组合时不会声明整任务 Token 已完整。上游失败同时保留在 HTTP、`metrics.json` 和组件进度的 `fallback_errors`。

凭据操作和完整对齐规则见 [Trace / Token 兜底对齐文档](docs/trace-token-fallback.md)。

### 创建批量导出

```bash
curl -X POST http://127.0.0.1:18080/v1/exports \
  -H 'Content-Type: application/json' \
  -d '{
    "task_ids":["76991823008039229","76991823008039230"],
    "components":["task","trace","metrics","artifacts","versions","gui","conversation"]
  }'
```

成功返回 HTTP **202**、`job_id` 和初始进度。`task_ids` 与 `filters` 恰好提供一个：传 ID 时保序去重，最多 10,000 个且不能超过 `FLUX_MAX_TASK_IDS`；传 `filters` 时服务先全量收集再拉数据，仍受收集页数、候选任务数与超时限制。可选 `start_ms` / `end_ms` 仅控制 Fornax 查询窗口，缺省为截至请求时的最近 120 天。

`conversation_mode` 在单独 Trace 请求及批量导出中均支持 `full`（默认完整视图）和 `messages`（仅公开消息）。

`include_raw_spans`、`recover_raw_spans` 在单独 Trace 和导出请求中均默认 false；后者要求前者为 true。默认流程获取执行视图与会话，避免大 raw 查询和无收益的会话拆分。

`components` 默认包含上例全部七项；可只选需要的信息。产物文件逐个下载，空文件也保留。版本接口枚举可用 `comparisons` 时继续拉对应 Diff；当前文档注明 `comparisons` 可能为 `null`，此时保留版本信息和提示，不承诺已拉全历史 Diff。GUI 报告中的图片引用会尝试通过旧截图 OpenAPI 下载；未采集截图会单独记错。对话默认请求完整 Trace 视图，必要时使用 inventory/票据分页；messages 模式仅获取公开消息文本，原始响应保留。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/exports/{job_id}` | 查询状态、进度和每项结果 |
| GET | `/v1/exports/{job_id}/download` | 默认下载完整 ZIP；传 `task_id` 下载指定任务，未就绪返回 409 |
| POST | `/v1/exports/{job_id}/cancel` | 取消后台工作，保留已获取数据并打包 |
| POST | `/v1/exports/{job_id}/retry` | 按原批次已确定的全部任务 ID 重新获取，覆盖原批次 |
| POST | `/v1/exports/{job_id}/tasks/{task_id}/retry` | 重新获取原批次中的一个任务，覆盖该任务结果 |

进度响应主要字段：`status`、`task_count`、`completed_count`、`tasks`、`bytes_written`、`archive_ready`、`error`。`tasks` 按 task ID 索引，包含任务状态和各组件的状态、来源、完整性、文件、警告、错误；不在进度响应中返回大块 Trace 或文件内容。

`GET /v1/exports/{job_id}` 支持按任务切片，避免大批次一次返回全部记录：`include_tasks=false` 只返回批次进度，`task_id` 精确读取单个任务，`offset` 与 `limit`（1～1000）分页读取 `tasks`。响应中的 `tasks_total` 是任务记录总数，`tasks_offset`、`tasks_limit` 和 `has_more_tasks` 描述本次切片。默认仍返回全部 `tasks`，兼容现有调用方；取消和重试接口也接受同样的切片参数。

任务状态包括 `queued`、`running`、`completed`、`partial`、`failed`、`cancelled`、`interrupted`。**以 `archive_ready=true` 判断 ZIP 是否可下载**，不要只依赖 `status`。某个文件、组件或任务失败不阻止其他信息落盘。服务重启后可读取既有任务进度和 ZIP；中断的任务不自动续跑，创建新任务可重新拉取。

组件因字节上限或写入异常失败后，只要最终清单和打包成功，仍可交付部分 ZIP。最终清单、打包或归档就绪状态保存失败时才不发布归档；不能仅凭“触发过字节上限”判断 ZIP 不可下载。归档可下载也不表示所需数据全部完整。

### 下载单个任务或指定任务集合

原始导出下载支持与打标下载一致的 `task_id` 查询参数：

```http
GET /v1/exports/{job_id}/download
GET /v1/exports/{job_id}/download?task_id=111
GET /v1/exports/{job_id}/download?task_id=111&task_id=222
```

不传参数时直接返回原完整 ZIP。选择任务时，从已发布 ZIP 裁剪该任务目录下的全部文件，
包括已有 Trace、会话、产物和 `data-manifest.json`；保留相对路径，解压后可继续使用
`TaskDataReader` 和本地打标工具。不会重新拉取上游，也不会修改原导出。

子集 ZIP 的 `job.json.tasks`、任务数量、请求任务范围和 `task_ids.json` 同步缩小；
若有 `selection.json`，其 ID 列表和 count 也同步缩小，原筛选 count 保留为 source_count，
扫描和分页统计仍来自原筛选过程。`job.json.download_selection` 记录所选任务及原批次任务数，
批次状态和错误保留来源信息；完成数按所选终态任务记录计，bytes_written 按包内数据字节重算。

重复 ID 去重并按原批次任务顺序输出。多选使用重复参数，不使用逗号拼接。
非法或空 ID 返回 422，任一任务不在已发布包中返回 404；参数数量上限为
`FLUX_MAX_TASK_IDS`。原包损坏或裁剪失败返回 503。并发重试替换原包时，一次子集下载仍使用
同一个已打开的 ZIP 快照；临时下载包在响应完成后删除。

### 重新获取单个任务或整批

上述两个 `/retry` 接口支持空请求体或 `{}`，默认沿用原组件和已保存参数，重新向上游获取数据，成功受理返回 HTTP 202 和原 `job_id`。结果和 ZIP 在原批次内覆盖，后续查进度、取消和下载仍使用原 ID。

- 整批重拉使用原批次已经确定的任务 ID，不重新执行筛选条件。
- 单任务重拉要求该任务属于原批次，保留其他任务和完整任务列表。执行期间重复提交相同范围、相同参数的重拉会返回当前作业；不同重拉请求返回 409，等待当前作业结束。
- 可传 `{"components":["versions"]}` 只重拉版本，也可覆盖会话模式、raw 开关和 Fornax 时间边界。仅选部分组件时，被重拉任务的旧组件文件会清理，其他任务保持不变。
- `job.json.request` 始终保存完整批次范围；单任务参数保存在 `task_requests`，最近一次重拉保存在 `retry_request` 和 `retry_of`。历史批次缺少完整请求时，沿用 ID 和组件，其余未覆盖参数使用当前默认值，并在 warnings 中明确提示。
- 页面当前批次停止后，可点「重新获取整批」或任务行内的「重新获取」；「重新查询进度」仍只查询状态。

接口参数、curl 示例、历史批次处理及错误码见 [重新获取说明](docs/export-retry.md)。上游缺少数据或凭据不可用时，重新获取仍可能部分失败，不保证重试后自动变成完全成功。

### 导出目录与文件说明

单个任务的文件用途与读取顺序见 [tasks/<task_id>/ 简要说明](docs/task-directory.md)（已按当前代码核实）。

新导出为已确定 ID 的任务生成 `data-manifest.json`，支持任务目录独立交付。清单固定描述 `task`、`execution`、`raw_spans`、`conversation`、`metrics`、`artifacts`、`versions`、`gui` 八类数据；未请求或不可用的数据也有明确状态。消费方使用清单和 `TaskDataReader`，下表中的文件名用于人工定位与排查。

数据保存在服务所在机器的 `exports/jobs/<job_id>/`，可通过 `FLUX_EXPORT_DIR` 修改。每次导出创建独立目录，获取到的内容逐项落盘，处理结束后生成 `export.zip`。ZIP 包内保持相同的数据目录结构，不包含 `export.zip` 自身。

选择 `task` 组件时，整批先按并发上限获取所有任务的基础信息，收到响应立即保存各自的 `task.json`（及可用任务归档），再开始拉取 Trace、指标、产物等其它组件。基础信息不会在第二阶段重复请求；某个任务基础信息获取失败会记录组件错误，其它组件仍继续处理。基础信息阶段完成后，任务状态为 `queued`，`completed_count` 仅统计全部组件处理结束的任务。未选择 `task` 时直接处理所选组件。

```text
exports/jobs/<job_id>/
├── job.json
├── task_ids.json
├── selection.json                  # 使用 filters 时生成
├── export.zip                      # 磁盘上的下载包
└── tasks/<task_id>/
    ├── data-manifest.json          # 任务级数据契约，路径相对本任务目录
    ├── task.json
    ├── task-snapshot.json.gz        # 或 task-snapshot.bin，归档可用时生成
    ├── trace.json
    ├── trace.tree.txt               # 采用 Fornax 时生成
    ├── trace/
    │   ├── openapi.json
    │   ├── fornax.json              # 实际获取到 Fornax 数据时生成
    │   └── raw-spans.json           # 实验补取取得 raw 时生成
    ├── metrics.json
    ├── conversation.json
    ├── conversation/
    │   ├── full-trace.json          # full 模式
    │   ├── messages-only.json       # messages 模式
    │   ├── inventory.json           # 渐进读取时生成
    │   └── <index>-<action>-<page>.json
    ├── artifacts/
    │   ├── manifest.json
    │   ├── manifest-refreshed.json  # 失败恢复时成功刷新清单后生成
    │   ├── downloads.json
    │   └── files/<path>
    ├── versions.json
    ├── diffs/<index>.json
    ├── gui.json
    └── gui/screenshots/<index>.<ext>
```

上图列出所有可能的文件，不代表每个任务都会生成全部文件。文件取决于所选 `components`、上游数据是否存在以及读取、校验和写入是否成功；`full-trace.json` 和 `messages-only.json` 通常按所选模式生成其一。只选 `trace` 也会保存会话；只选 `metrics` 也可能保存其计算来源 `trace/openapi.json`。

#### 导出根目录

| 文件 | 内容 | 使用方式 |
| --- | --- | --- |
| `job.json` | 导出状态、进度、每个任务及组件的来源、文件、警告和错误 | 查看整批进度和 `archive_ready`；任务数据读取以各自的 `data-manifest.json` 为准 |
| `task_ids.json` | 本次选中的去重 Task ID 数组 | 确认导出任务范围 |
| `selection.json` | 使用 `filters` 时收集任务 ID 的结果，包括扫描、分页和排除统计 | 核查筛选阶段的结果；直接提供 `task_ids` 时不生成 |
| `export.zip` | 已落盘结果的压缩下载包，部分失败或取消时也可包含成功数据 | 通过下载接口获取，或直接读取磁盘文件 |

#### 每个 Task 目录

下表中的路径相对于 `tasks/<task_id>/`。

| 文件或目录 | 内容 | 使用方式与限制 |
| --- | --- | --- |
| `data-manifest.json` | 契约版本、任务身份、finalized 标记，以及八类数据的来源、状态、完整性、覆盖范围和文件引用 | 下游唯一数据入口；引用相对本任务目录，文件位置和 JSON Pointer 由读取器处理；finalized=true 不代表所有数据完整 |
| `task.json` | Task OpenAPI 返回的任务详情，包括 `items`、分页信息及可选归档状态 | 主要任务信息在 `items[].task`：标题、状态、时间、仓库、分类和关联会话等；不是完整执行正文 |
| `task-snapshot.json.gz` / `task-snapshot.bin` | Task 返回可用归档时，从 `tos_detail.content_base64` 解码得到的原始字节 | gzip 内容保留为 `.json.gz`，其他字节保留为 `.bin`；存在归档不代表当前在线会话和外部资源都已包含 |
| `trace.json` | 采用的 Trace 来源、执行与会话完整性、raw 状态、警告及数据引用；Fornax 来源还可包含规整后的 spans 和查询窗口 | OpenAPI 的大块执行数据通过 `data_file` 引用，会话通过 `conversation.file` / `conversation.files` 引用 |
| `trace/openapi.json` | `/trace/query` 成功返回的原始 `data`，包括 `meta`、`summary`、`sessions` 等 | 用于查看执行步骤、来源和覆盖范围；显式请求 raw 且返回成功时还包含 `raw_spans` |
| `trace/fornax.json` | Fornax 获取的原始 spans、分页完整性、查询时间窗口及可能的错误 | 只在实际取得 Fornax 数据时保存；完整性限于该时间窗口，不等于任务全部历史 |
| `trace/raw-spans.json` | 开启实验性会话补取后取得并过滤、去重的 raw spans 数组 | 仅在补取确实取得记录时生成，可能仍不完整；以 `raw_spans_complete` 和 `raw_recovery` 为准 |
| `trace.tree.txt` | Fornax spans 转换出的可读调用树 | 便于人工查看调用层级；不能代替完整会话 |
| `metrics.json` | 指标来源、`values`、Token／耗时完整性、调用覆盖、证据和诊断 | Token 字段为数量，耗时字段为毫秒；未知 Token 为 null，已知零值为 0。结合 `tokens_complete`、`token_coverage` 判断已知统计是否覆盖全量 |
| `conversation.json` | 会话模式、正文可用性、完整性、覆盖范围、错误及原始响应文件列表 `files` | 会话摘要，不嵌入大块正文；下游通过任务清单的 role 和 content_kind 区分摘要、inventory 与正文 |
| `conversation/full-trace.json` | full 模式下 `/task`、`include_trace=true` 的原始 `data` | 完整视图在 `trace_detail.data`，覆盖范围及遗漏资源在 `trace_detail.meta` |
| `conversation/messages-only.json` | messages 模式下 `/task`、`messages_only=true` 的原始 `data` | 只代表该模式覆盖的公开消息正文，不能当成包含推理、工具和媒体的完整执行数据 |
| `conversation/inventory.json` | 渐进读取返回的目录、票据及元信息 | 用于核查后续批次范围，本身不保证已取得完整正文 |
| `conversation/<index>-<action>-<page>.json` | 使用票据读取的 chunk 或 messages 批次原始 `data` | 由任务清单引用定位；有批次不等于已拉全，批次数也不等于消息数 |
| `artifacts/manifest.json` | 可下载产物清单：路径、文件数、大小、SHA256、会话和更新时间等 | 核查初始下载范围；实际上已取得的文件以任务清单和 downloads.json 中的成功记录为准 |
| `artifacts/manifest-refreshed.json` | 失败恢复时重新读取的清单 | 原清单保留不变；成功刷新后才生成，不保证与原清单同一版本 |
| `artifacts/downloads.json` | 每个产物的下载结果，包括原路径、状态、保存路径、字节数、尝试次数、校验清单及错误 | `verified_against` 区分 initial / refreshed；变化时保留 `initial_expected`、`verified_expected`；`first_error` 保留首次失败原因 |
| `artifacts/files/<path>` | 通过 OpenAPI raw 下载并通过校验的原始文件字节 | 保留清单路径，因此 `artifacts/a.md` 会保存到 `artifacts/files/artifacts/a.md`；空文件也会保存 |
| `versions.json` | 上游已采集的版本信息 | 不保证包含全部历史；没有可用 `comparisons` 时不会自动生成 Diff |
| `diffs/<index>.json` | 由版本接口枚举的可用比较区间取得的 Diff 响应 | 仅保存已成功读取的比较；没有文件不代表零变更 |
| `gui.json` | GUI 用例结果接口返回的数据，报告结果位于 `items[].case_result` | 依赖上游已采集报告，不是 Token 或耗时指标 |
| `gui/screenshots/<index>.<ext>` | GUI 报告引用且成功下载的图片 | 通过旧截图接口获取；可能只取得部分截图，具体结果见 `job.json` |

#### 下游读取与验证

下载 ZIP 并解压后，使用项目 Python 环境中的 `TaskDataReader`。也可以单独交付完整的 `<task_id>/` 目录，保持任务 ID 目录名即可。新清单使用服务契约 `schema_version="1.0"`；读取器拒绝未 finalized 的清单、未知主版本和不支持的内容格式。

只分析 Token 时直接读取指标，不以整包校验为前置条件。下面的例子允许 partial，但要求 Token 统计已确认完整；耗时分析另需检查 `timing_complete`。

```python
from flux_task_service.export_contract import TaskDataReader

reader = TaskDataReader("/path/to/tasks/123")
metrics = reader.read_dataset("metrics")
if metrics["status"] not in {"available", "partial"}:
    raise ValueError(metrics["errors"])
payload = next(
    doc["content"]
    for doc in metrics["documents"]
    if doc["reference"]["format"] == "flux.metrics.v1"
)
if payload.get("tokens_complete") is not True:
    raise ValueError("Token totals are not confirmed complete")
values = payload["values"]  # null 表示未知，不能换成 0。
```

读取会话时使用 `read_dataset("conversation")`：`role=summary` 是摘要，其他引用用 `content_kind` 区分 `inventory`、`full-trace`、`messages-only`、`chunk` 和 `messages`。跳过摘要和 inventory 后，按响应类型读取正文。正文仍保留上游结构，当前没有统一消息列表；完整可执行示例见 [对齐文档的下游读取方式](docs/downstream-data-contract.md#4-下游读取方式)。已有清单缺少 `content_kind` 时不能可靠区分用途，需要重新导出，不由消费方从文件名猜测。

执行、raw、会话和指标分别检查状态及完整性。仅有会话时 `execution` 可以为 unavailable；默认不请求 raw 时 `raw_spans` 为 not_requested。`available` 不自动代表完整，`complete=null` 表示未知，完整性始终限定于清单中的 `scope`。

**路径规则：新清单中的 `files[].path` 相对于当前任务目录。原有 JSON 内部的引用仍相对于导出／ZIP 根目录。** 例如清单可以引用 `trace/openapi.json`，旧 `trace.json.data_file` 则可能为 `tasks/123/trace/openapi.json`。消费方使用新清单或读取器处理路径和 Pointer；单独 HTTP Trace 接口仍返回内联 `data` 和 `conversation.documents`。

整包交付验收时，使用 `reader.validate()` 或以下命令检查全部引用：

```bash
uv run --locked python -m flux_task_service.export_contract /path/to/tasks/123
```

退出码 0 表示契约与引用检查通过，不代表全部数据完整。业务只需要某类数据时可单独读取，不因其他类数据的文件损坏提前退出；顶层清单仍必须合法且已 finalized。读取器接受解压目录，不直接读取 ZIP。

没有新清单的历史包显式使用 `TaskDataReader(task_dir, allow_legacy=True)`，或在命令行追加 `--allow-legacy`。旧版适配要求保留原 `job.json` 和 `tasks/<task_id>/` 层级，在内存中生成清单，不修改旧文件；已有新清单不因该选项而被自动改写。

机器可读定义：[任务清单 Schema](docs/schemas/task-manifest.schema.json)、[指标 Schema](docs/schemas/metrics.schema.json)、[会话摘要 Schema](docs/schemas/conversation-summary.schema.json)。错误码、故障处理和验收边界统一见 [对齐文档](docs/downstream-data-contract.md)。

#### 产物下载失败的有界恢复

一次导出中，每个 Task 最多额外刷新一次产物清单，每个失败文件最多恢复下载一次；首次与恢复下载均不叠加传输层重试，JWT 过期仍允许一次凭据刷新。校验不一致、上游下载失败或可恢复读取错误才进入恢复；权限拒绝、身份或路径不匹配、大小超限及本地写入失败不靠重下解决。额外刷新和恢复下载共用 `FLUX_ARTIFACT_RECOVERY_TIMEOUT_SECONDS` 时间预算，默认 60 秒，也受 Task 总超时限制。

原清单路径是本次下载目标，刷新后新增的文件不会自动加入。刷新后使用新清单校验后续文件：大小或 SHA256 变化时记录 `manifest_changed=true` 及前后预期值，不声称取得了原版本；路径消失时记录 `ARTIFACT_REMOVED` 并跳过下载。持续不一致记录 `ARTIFACT_INTEGRITY_ERROR`，上游无法下载 worker 文件记录 `ARTIFACT_DOWNLOAD_FAILED`，预算耗尽记录 `ARTIFACT_RECOVERY_TIMEOUT`。刷新失败保留 `recovery_error`，后续失败文件不再次刷新；其他文件继续处理。

`job.json` 中的 artifacts 组件包含 `manifest_refresh_attempted`、`refreshed_manifest_file`、`manifest_refresh_error`、`recovered_count` 和 `manifest_changed_count`；逐文件详情在 `downloads.json`。失败或未通过校验的内容不会保存为成功产物，空文件则正常保留。

仍存在清单刷新后无法恢复的上游样本，错误及后续核查条件见 [产物下载恢复实测记录](docs/artifact-recovery.md)。

新产物是异步上传的当前内容，仓库文件范围仅为 `flux/changes` 和 `flux/.local`；不代表全仓库或任意历史版本。GUI、版本、Diff 依赖采集数据，对话/任务归档依赖敏感权限。未获取的内容通过组件结果说明，不输出伪造的空成功。

## 导出后的工程打标与测试造题准入

后端支持对完成的导出批次后台打标、查看进度、按候选等级筛选，以及下载候选清单和
打标 ZIP。API 入口为 `POST /v1/exports/{job_id}/tagging`（默认请求体 `{}`），其余
接口见 [服务打标接口](docs/test-admission.md#在服务中使用)。

打标同时提取关联仓库，按来源和用途分类，并保留状态、分支与证据位置。
结果位于 `recorded_only.repository.extraction`，随详情、索引、候选清单和 ZIP 保存，
不改变原始 `repo_path` 或测试准入判定。规则范围及未知情况见
[仓库提取结论与实现](docs/repository-extraction-conclusions.md#当前打标实现)。

模型和人工可通过 `GET/POST /v1/tagging/{tagging_id}/task/{task_id}` 动态打标。
首批支持 `repository`、`task_classification`、`review`，每类都有严格格式；
先 GET 后合并，以 `If-Match` 携带 ETag 整体覆盖，防止并发修改丢失。
白名单及格式由 `GET /v1/tagging/tag-schema` 提供，详见
[动态打标接口](docs/dynamic-tagging.md)。

下载接口默认返回完整批次；可通过
`GET /v1/tagging/{tagging_id}/download?task_id=111&task_id=222`
批量下载选定任务，ZIP 内的索引和候选清单同步限定在所选范围。
新输出采用 `flux.test-admission.v2` 压缩摘要，前端接入需按该契约适配。
导出结果可通过本地命令进行测试证据打标与准入过滤：

```bash
.venv/bin/python -m flux_task_service.tagging /path/to/export \
  --output /path/to/tagging-results
```

默认按五类证据判断：仅测试执行调用和/或平台测试分类时待复核；技能调用、审查执行
或报告命中任一类即可成为候选，命中三类及以上为优先候选。`--priority-only` 只保留
优先候选。失败数量和报告问题数量仅记录，不决定默认等级；证据归属不明时仍复核。
其余重要标签仅记录，不参与默认准入。
输出单任务标签、总索引及 `selected-tasks.json`。历史包须显式使用 `--allow-legacy`。
输入、输出目录分离，采集接口不变。字段、支持规则、策略和限制见
[工程打标与测试造题准入](docs/test-admission.md)。

## 共用筛选参数 `filters`

### 按用户筛选

支持按任务归属账号筛选，接口字段为 `created_by`。页面点击 **「按用户筛选」** 即可填写账号；也可在「添加筛选条件」中选择「用户（任务归属人）」。重复点击快捷入口会定位已有的用户包含条件，不会重复添加。

账号可用逗号、空格、分号或换行分隔，页面自动去重，每行最多 100 个账号。同一行内匹配任一账号；切换为「排除」即可排除这些账号的任务。用户条件与时间、状态等其他包含条件同时生效，页面默认的昨天时间范围仍保留，可按需调整或删除。仅填写用户包含条件也能查询。

此处填写的是上游任务的归属账号，不是姓名模糊搜索，也不自动将邮箱、姓名或飞书 ID 转换成账号。请使用任务数据中实际记录的账号值。

API 查询示例：

```bash
curl -X POST http://127.0.0.1:18080/v1/task-ids/query \
  -H 'Content-Type: application/json' \
  -d '{
    "filters": {
      "filter_fields": [
        {"field_name":"created_by","query_type":"in","values":["zhangsan","lisi"]}
      ]
    },
    "limit": 200
  }'
```

相同 `filters` 可用于 `/v1/task-ids/collect` 和 `/v1/exports`。在页面选择「当前筛选条件的全部任务」后拉取数据，也会应用用户条件。排除用户时，把对应条件放入 `exclude_fields`，并保留至少一个包含条件；只传排除条件不支持查询。配置了服务访问密钥时，请求还需传入前文说明的 Authorization 请求头。

### 参数与匹配规则

三个筛选接口都要求提供 `filters` 对象；请求不接受未定义字段。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `filter_fields` | 条件对象数组 | `[]` | 包含条件；与分类路径展开后的条件合计最多 15 条 |
| `query_and_or` | string | `"and"` | 支持 `and`、`or`；包含条件超过一条时禁止 `or` |
| `task_ids` | string[] | `[]` | 限定候选 ID，最多 200 个；可仅凭此字段查询 |
| `category_path` | string[] | `[]` | 按一级到三级顺序填写完整分类路径，最多 3 级 |
| `exclude_task_ids` | string[] | `[]` | 直接排除 ID，最多 200 个 |
| `exclude_fields` | 条件对象数组 | `[]` | 最多 15 条；命中任意一条即排除 |
| `exclude_category_paths` | string[][] | `[]` | 最多 15 条路径，每条 1～3 级；路径内各级同时匹配即排除 |

每个条件对象的结构如下：

```json
{
  "field_name": "task_status",
  "field_type": "string",
  "query_type": "in",
  "values": ["running", "interrupted"]
}
```

`field_type` 可省略，唯一支持值为 `"string"`。可用字段及算子：

| `field_name` | `query_type` | 含义 | 包含查询的索引条件 |
| --- | --- | --- | --- |
| `created_by` | `in` | 归属人账号 | 是 |
| `worker_session_id` | `in` | Worker 会话 ID | 是 |
| `task_status` | `in` | 任务状态 | 是 |
| `task_is_completed` | `in` | 是否完成，字符串 `"0"` / `"1"` | 是 |
| `task_created_at` | `range` | 创建时间 | 是 |
| `updated_at` | `range` | 更新时间 | 是 |
| `task_updated_at` | `range` | 任务更新时间 | 否 |
| `work_mode` | `in` | 工作模式 | 否 |
| `level_1_category` | `in` | 一级分类 | 否 |
| `level_2_category` | `in` | 二级分类 | 否 |
| `level_3_category` | `in` | 三级分类 | 否 |

校验与匹配规则：

- 至少有一个包含条件或 `task_ids`。只传排除条件不支持查询。
- 只要存在包含条件，就必须包含至少一个表中的索引字段，即使同时提供了 `task_ids`。分类、工作模式和 `task_updated_at` 可搭配创建时间范围使用。
- 不同包含条件默认全部满足；一个 `in` 条件内的多个值任选其一。`category_path` 会展开为各级分类包含条件，与 `filter_fields` 一起生效。
- `in` 的 `values` 为 1～100 个非空字符串，单值最多 1024 字符。状态、完成标记、ID 均按字符串传入。
- `range` 的 `values` 必须为两个 `YYYY-MM-DD HH:MM:SS` 字符串；空串表示该端不限，下界不能大于上界。时间按上游语义原样传递，服务不做时区转换。
- `task_ids`、`exclude_task_ids` 中每个 ID 必须为 1～64 位数字字符串，不能全为零。调用方应保留字符串类型，避免大整数精度损失。
- 多条排除条件取并集，包含与排除重叠时排除优先。排除分类路径内各级取 AND，多条路径取 OR。
- `work_mode` 由本服务按响应中的 `task.work_mode` 匹配，包含和排除均支持；字段缺失时返回错误。其他字段排除通过上游按候选 ID 查询后做差集，排除查询失败时整次请求失败。

页面使用的状态值：`queueing`、`running`、`input-required`、`committed`、`finished`、`interrupted`、`failed`、`rolled-back`。工作模式值：`general`、`coding`、`data_analysis`、`requirement`。API 对这些 `in` 值只做字符串校验，实际匹配由上游数据或本服务字段匹配决定。

包含和排除组合示例：查询“研发”分类，排除指定人员、“研发 / 测试”分支和指定 task ID。

```json
{
  "filters": {
    "filter_fields": [
      {
        "field_name": "task_created_at",
        "query_type": "range",
        "values": ["2026-11-01 00:00:00", "2026-11-03 23:59:59"]
      }
    ],
    "category_path": ["研发"],
    "exclude_fields": [
      {"field_name": "created_by", "query_type": "in", "values": ["zhangsan"]}
    ],
    "exclude_category_paths": [["研发", "测试"]],
    "exclude_task_ids": ["76991823008039229"]
  }
}
```

## 单页查询 `POST /v1/task-ids/query`

适合大范围查询和按需加载。每次扫描一页上游记录，然后跳过 session、匹配工作模式、去重并应用排除条件。

| 请求参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `filters` | object | 是 | — | 共用筛选参数 |
| `limit` | integer | 否 | `200` | 本次扫描记录数，1～200 |
| `cursor` | string / null | 否 | `null` | 首次查询省略；后续使用上一页的 `next_cursor` |

```bash
curl -X POST http://127.0.0.1:18080/v1/task-ids/query \
  -H 'Content-Type: application/json' \
  -d '{
    "filters": {
      "filter_fields": [
        {"field_name": "task_status", "query_type": "in", "values": ["interrupted"]}
      ]
    },
    "limit": 200,
    "cursor": null
  }'
```

如已配置访问密钥，为以上及后续 curl 请求添加 `-H 'Authorization: Bearer <FLUX_API_KEY>'`。

响应示例：

```json
{
  "task_ids": ["76991823008039229", "76991823008039230"],
  "count": 2,
  "scanned_count": 4,
  "skipped_session_count": 1,
  "has_more": true,
  "next_cursor": "cGFnZTI",
  "excluded_count": 1
}
```

| 响应字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_ids` | string[] | 本页保留的 task ID，页内去重，保留首次出现顺序 |
| `count` | integer | `task_ids` 的长度 |
| `scanned_count` | integer | 上游包含查询本页返回的原始记录数，含 session 和后续被过滤的记录 |
| `skipped_session_count` | integer | 本页跳过的 session 记录数 |
| `excluded_count` | integer | 本页工作模式包含筛选后，被排除条件剔除的唯一任务数 |
| `has_more` | boolean | 上游是否还有下一页 |
| `next_cursor` | string / null | 下一页游标；遍历结束时为 `null` |

**以 `has_more` 判断是否结束，不能以 `count` 或 `task_ids` 是否为空判断。** 整页只有 session、工作模式不匹配或全部被排除时，仍可能 `has_more=true`。

下一页保持相同的 `filters` 和 `limit`，将返回的 `next_cursor` 原样写入 `cursor`。游标最长 8192 字符，使用 base64url 格式；示例游标仅用于展示结构。接口不支持 `offset`，也不返回 `total`。跨页去重由调用方完成；只指定一个唯一 task ID 时实际扫描页大小最多为 50。

## 全量收集 `POST /v1/task-ids/collect`

服务端自动翻页，跨页去重，直到上游遍历结束后一次返回。适合范围明确、结果量可控的查询。

| 请求参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `filters` | object | 是 | — | 共用筛选参数 |
| `limit` | integer | 否 | `200` | 每次上游扫描记录数，1～200；单个唯一 task ID 时最多 50。旧字段 `page_size` 仍作为兼容别名接受 |

```bash
curl -X POST http://127.0.0.1:18080/v1/task-ids/collect \
  -H 'Content-Type: application/json' \
  -d '{
    "filters": {
      "filter_fields": [
        {"field_name": "created_by", "query_type": "in", "values": ["zhangsan"]},
        {"field_name": "task_created_at", "query_type": "range",
         "values": ["2026-11-01 00:00:00", "2026-11-03 23:59:59"]}
      ]
    },
    "limit": 200
  }'
```

响应示例：

```json
{
  "task_ids": ["76991823008039229", "76991823008039230"],
  "count": 2,
  "scanned_count": 4,
  "skipped_session_count": 1,
  "page_count": 1,
  "exhausted": true,
  "excluded_count": 1
}
```

`task_ids` 为本次全部保留的唯一 ID，保留首次出现顺序；`count` 是数组长度。`scanned_count`、`skipped_session_count` 为各包含查询页的累计记录数，`excluded_count` 为跨页排除的唯一任务数，`page_count` 为包含查询页数。所有扫描统计均不包含额外排除查询。

成功响应的 `exhausted` 固定为 `true`，表示本次遍历到末尾；没有匹配结果时返回空数组。默认最多扫描 100 页、保留至多 10,000 个排除前候选任务，整次查询超时为 120 秒。达到上限、超时或中途失败时返回错误，不返回部分收集结果。大范围查询可使用单页接口，由调用方持续翻页。

上游没有跨页冻结快照；遍历结束不能保证并发写入期间的数据属于同一时间点，也不能将任务列表当作实时执行状态。

## 筛选选项 `POST /v1/filter-options`

从指定筛选范围的真实任务字段提取选项，支持分类三级级联。选项反映当前扫描批次可见的数据，不是全局分类字典。当前网页使用固定选项，不调用此接口。

| 请求参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `filters` | object | 是 | — | 限定选项来源的包含和排除条件 |
| `field` | string | 否 | `"category"` | `category`、`created_by`、`task_status`、`task_is_completed`、`worker_session_id`、`task_id` |
| `category_path` | string[] | 否 | `[]` | 请求分类的父路径，最多 2 级；其他字段必须为空 |
| `limit` | integer | 否 | `200` | 每页扫描记录数，1～200；单个唯一 task ID 时最多 50。旧字段 `page_size` 仍作为兼容别名接受 |
| `cursor` | string / null | 否 | `null` | 后续批次传入上次返回的 `next_cursor`，格式限制同单页接口 |

分类请求中的顶层 `category_path` 决定获取哪一级选项：

| `category_path` | 返回选项 |
| --- | --- |
| `[]` | 一级分类 |
| `["研发"]` | “研发”下的二级分类 |
| `["研发", "测试"]` | “研发 / 测试”下的三级分类 |

服务补齐父级条件。`filters` 不能预先包含当前请求层或更深层的分类，已有祖先条件必须与顶层路径一致，否则返回 422。切换父级后，调用方应清空下级选择并重新请求。

```bash
curl -X POST http://127.0.0.1:18080/v1/filter-options \
  -H 'Content-Type: application/json' \
  -d '{
    "field": "category",
    "filters": {
      "filter_fields": [
        {"field_name": "task_created_at", "query_type": "range",
         "values": ["2026-11-01 00:00:00", "2026-11-03 23:59:59"]}
      ]
    },
    "category_path": ["研发", "测试"],
    "limit": 200
  }'
```

响应示例：

```json
{
  "field": "category",
  "options": [
    {
      "value": "自动化",
      "label": "自动化",
      "task_count": 12,
      "category_path": ["研发", "测试", "自动化"]
    }
  ],
  "category_path": ["研发", "测试"],
  "category_level": 3,
  "matched_task_count": 12,
  "scanned_count": 12,
  "page_count": 1,
  "missing_value_count": 0,
  "redacted_value_count": 0,
  "unavailable_value_count": 0,
  "availability": "available",
  "exhausted": true,
  "next_cursor": null,
  "stop_reason": null,
  "source": "task_data",
  "cached": false,
  "warnings": []
}
```

| 响应字段 | 含义 |
| --- | --- |
| `field` | 当前选项字段 |
| `options` | 按值排序、按值去重的选项数组 |
| `options[].value` / `label` | 选项原始字符串值和展示名称，当前两者相同 |
| `options[].task_count` | 本批关联该值的唯一任务数 |
| `options[].category_path` | 分类选项的完整路径；非分类选项为 `null` |
| `category_path` / `category_level` | 本次父路径与请求层级；非分类字段为 `[]` / `null` |
| `matched_task_count` | 本批通过包含和排除筛选的唯一任务数，含没有可用选项值的任务 |
| `scanned_count` / `page_count` | 本批包含查询扫描记录数及页数，不含额外排除查询 |
| `missing_value_count` | 本批目标字段缺失、为空或只有空白的唯一任务数 |
| `redacted_value_count` | 本批人员字段被识别为脱敏而跳过的唯一任务数 |
| `unavailable_value_count` | 本批目标字段被 `task-metadata-only.*` 投影隐藏的唯一任务数 |
| `availability` | `available`：可用；`partial`：还有批次或部分值缺失/不可见；`unavailable`：无可见选项且存在投影隐藏或脱敏 |
| `exhausted` | 是否从本次游标起遍历到末尾 |
| `next_cursor` | 下一批游标，遍历结束为 `null` |
| `stop_reason` | `batch_limit`：达到单批页数；`field_unavailable`：整页投影隐藏目标字段而提前停止；否则为 `null` |
| `source` | 固定为 `"task_data"` |
| `cached` | 是否命中服务端选项缓存 |
| `warnings` | 字段缺失、脱敏、投影隐藏或批次未完成的提示字符串数组 |

默认每批最多扫描 5 页，即最多 1000 条原始记录。`exhausted=false` 且 `next_cursor` 非空时，保持 `field`、`filters`、`category_path`、`limit` 不变，传入游标继续加载。调用方按选项值合并去重，跨批次 `task_count` 不能直接相加，可能重复计数。

没有匹配任务时返回可用的空选项；字段不可见则通过计数、`availability` 和 `warnings` 说明。投影隐藏不能单独证明缺少 IAM 权限。`exhausted=true` 也不保证字段齐全或分类全集完整。真实请求失败返回错误，不伪装为空选项。

默认缓存 60 秒，缓存键包含完整请求参数，同一请求并发未命中共用一次扫描；TTL 为 0 可禁用缓存。选项查询默认独立超时为 20 秒。

## 错误输出与健康检查

业务错误统一返回以下结构，`upstream_code` 仅在可用时出现，不返回上游原始响应或凭据：

```json
{
  "error": {
    "code": "UPSTREAM_FORBIDDEN",
    "message": "Service account access was denied",
    "upstream_code": 2002
  }
}
```

参数校验错误会额外包含位置和原因，不回显提交值：

```json
{
  "error": {
    "code": "INVALID_QUERY",
    "message": "Invalid query",
    "details": [
      {"location": ["body", "limit"], "message": "Input should be less than or equal to 200"}
    ]
  }
}
```

| HTTP 状态 | `error.code` | 含义及处理 |
| --- | --- | --- |
| 401 | `UNAUTHORIZED` | 调用方访问密钥缺失或不正确，检查 Bearer 请求头 |
| 422 | `INVALID_QUERY` | 参数不合法，查看 `details` |
| 422 | `QUERY_LIMIT_EXCEEDED` | 收集、选项候选任务数或扫描页数达到上限，缩小范围或改用单页查询；排除查询也可能触发页数上限 |
| 502 | `FILTER_FIELD_UNAVAILABLE` | 响应缺少工作模式筛选所需字段 |
| 502 | `UPSTREAM_PROTOCOL_ERROR`、`UPSTREAM_PAGINATION_ERROR`、`UPSTREAM_FILTER_MISMATCH` | 上游响应格式、游标或指定 ID 匹配异常 |
| 502 | `UPSTREAM_FORBIDDEN`、`UPSTREAM_REJECTED` | 上游权限拒绝或查询被拒绝 |
| 503 | `AUTH_NOT_CONFIGURED`、`IAM_UNAVAILABLE`、`IAM_AUTH_FAILED`、`UPSTREAM_UNAUTHORIZED` | 服务端凭据不可用、IAM 获取 JWT 失败或 JWT 刷新后仍被拒绝 |
| 503 | `UPSTREAM_UNAVAILABLE`、`UPSTREAM_RETRIES_EXHAUSTED` | 网络请求失败或上游可重试错误耗尽重试次数 |
| 504 | `QUERY_TIMEOUT` | 整次查询超时，包括排队、认证、扫描和排除 |

`GET /health/live` 成功返回：

```json
{"status": "alive"}
```

`GET /health/ready` 成功返回：

```json
{"status": "ready", "check": "credential_config"}
```

ready 只检查 secret 已配置且可读取，不请求 IAM 或 Flux，不证明上游连通或权限可用。凭据不可用时返回 HTTP 503、`AUTH_NOT_CONFIGURED`。

新增数据接口和导出错误：

| HTTP 状态 | `error.code` | 含义 |
| --- | --- | --- |
| 404 | `TASK_NOT_FOUND`、`EXPORT_NOT_FOUND` | 任务详情或导出任务不存在 |
| 409 | `EXPORT_NOT_READY` | ZIP 尚未准备好，继续查询进度 |
| 422 | `INVALID_ARTIFACT_PATH`、`DOWNLOAD_LIMIT_EXCEEDED`、`EXPORT_LIMIT_EXCEEDED` | 路径不规范、单次响应或导出数据达到字节上限 |
| 429 | `EXPORT_BUSY` | 活动导出数量已满，稍后重试 |
| 502 | `TRACE_UNAVAILABLE`、`METRICS_UNAVAILABLE`、`ARTIFACT_INTEGRITY_ERROR` | 无可用 Trace/指标，或产物校验失败 |
| 503 | `FORNAX_AUTH_NOT_CONFIGURED`、`FORNAX_TOKEN_EXPIRED`、`FORNAX_UNAUTHORIZED`、`FORNAX_REFRESH_FAILED`、`FORNAX_UNAVAILABLE`、`EXPORT_WRITE_FAILED` | Fornax 认证、请求或导出文件写入失败 |

批量导出会把这些错误写入组件结果，保留其他成功信息。`FORNAX_*` 的兜底错误也会出现在 Trace/metrics 的 `fallback_errors` 中。

## 网页查询与文件输出

打开服务首页即可使用，无需前端构建。页面默认筛选今天创建的任务，日期控件按北京时间填写；可添加包含/排除条件，状态、工作模式、完成标记和各级分类使用固定多选项。分类各级独立选择，不自动推断父子关系。人员账号、task ID 和 Worker 会话 ID 支持逗号、空格、分号或换行分隔。来源平台目前为“待配置”，不参与查询。

页面的“拉取任务信息与产物”可选择已加载 ID 或当前筛选条件全部任务，勾选所需信息后创建后台导出；进度区展示每项结果，完成后下载 ZIP，取消按钮会取消服务端任务并保留已获取数据。

页面筛选调用 `/v1/task-ids/query`，每次扫描 50、100 或 200 条记录，支持上一页/下一页。列表显示当前页，顶部数量和复制、下载内容为**所有已加载页的去重 ID**，尚未加载的页不会导出。

| 输出方式 | 内容格式 |
| --- | --- |
| 复制 / TXT | 每行一个 task ID |
| CSV | 首行为 `task_id`，后续每行一个 ID，UTF-8 编码并带 BOM |
| JSON | 字符串数组，如 `["76991823008039229", "76991823008039230"]`，不包含 API 统计字段 |

文件名为 `flux_task_ids_partial_YYYY-MM-DD.<格式>` 或 `flux_task_ids_all_YYYY-MM-DD.<格式>`：`partial` 表示尚有未加载页，`all` 表示已加载到末尾。网页文件由浏览器生成，HTTP 业务接口直接返回 JSON。

修改筛选条件、访问密钥或页大小后，旧结果标记过期，重新查询后才能复制或下载。翻页失败或取消会保留已加载结果，可重试；“停止等待”取消浏览器等待，服务端仍受自身超时限制。页面访问密钥只保存在当前页面内存中，服务账号 secret 和 JWT 只在服务端使用。

## 配置与运行限制

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLUX_SERVICE_SECRET` | 未设置 | 服务账号 secret，优先于 secret 文件 |
| `FLUX_SERVICE_SECRET_FILE` | 未设置 | secret 文件路径；`.env.example` 设置为 `../secret` |
| `FLUX_RAW_RECOVERY_MAX_SESSIONS` | 8 | raw 超限后的 OpenAPI 会话补取候选数上限 |
| `FLUX_RAW_RECOVERY_TIMEOUT_SECONDS` | 60 | 会话补取总时间预算，秒 |
| `FLUX_RAW_RECOVERY_MAX_BYTES` | 268435456 | 会话补取累计响应字节预算，256 MiB |
| `FLUX_API_KEY` | 未设置 | 所有业务接口的 Bearer 访问密钥 |
| `FLUX_HOST` / `FLUX_PORT` | `127.0.0.1` / `18080` | 监听地址和端口 |
| `FLUX_REQUESTS_PER_SECOND` | `8` | 每进程 Task OpenAPI 请求频率，最大 10 |
| `FLUX_MAX_RETRIES` | `3` | 传输、限流及上游可重试错误的重试次数 |
| `FLUX_REQUEST_TIMEOUT_SECONDS` | `30` | 单次网络请求超时，秒 |
| `FLUX_QUERY_TIMEOUT_SECONDS` | `120` | 单页/收集查询总超时，秒，含排队 |
| `FLUX_MAX_CONCURRENT_QUERIES` | `4` | 同时处理的业务查询数 |
| `FLUX_MAX_PAGES` | `100` | 收集及每组排除查询最大页数；也限制选项单批页数 |
| `FLUX_MAX_TASK_IDS` | `10000` | 收集或选项单批最大候选任务数，排除前计算 |
| `FLUX_OPTIONS_CACHE_TTL_SECONDS` | `60` | 选项缓存秒数，0 禁用 |
| `FLUX_OPTIONS_CACHE_SIZE` | `128` | 最多缓存的选项请求数 |
| `FLUX_OPTIONS_PAGES_PER_BATCH` | `5` | 选项单批扫描页数 |
| `FLUX_OPTIONS_QUERY_TIMEOUT_SECONDS` | `20` | 选项查询总超时，秒 |
| `FLUX_BASE_URL` | `https://flux-insight.byted.org` | Flux OpenAPI 域名 |
| `FLUX_IAM_URL` | `https://cloud.bytedance.net/auth/api/v1/jwt` | 服务账号 JWT 获取地址 |

JWT 在进程内缓存并提前刷新；上游返回 401 / code=2001 时重新获取一次 JWT 并重试。传输错误、限流和部分上游错误有限退避重试。多进程或多副本部署时，QPS 限制和 JWT 缓存各自独立，需要按服务账号总调用预算分配 QPS。

单页接口不受整次收集的 10,000 个候选任务或 100 页限制，但每次请求仍受超时及额外排除查询页数限制。任何接口都不将游标异常或查询失败当作正常遍历结束。

Trace/导出新增配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLUX_FORNAX_JWT` / `FLUX_FORNAX_JWT_FILE` | 未设置 | Fornax 个人 JWT；变量值优先，示例文件使用 `../jwt` |
| `FLUX_FORNAX_REFRESH_FROM_CHROME` | `false` | 可选本机个人 JWT 自动刷新；需要 desktop extra 和 Chrome SSO 登录 |
| `FLUX_FORNAX_BASE_URL` | `https://fornax.bytedance.net` | Fornax 域名 |
| `FLUX_FORNAX_WORKSPACE_ID` | `7590084809894139650` | 沿用上级脚本的 workspace |
| `FLUX_FORNAX_PLATFORM` | `inner_cozeloop` | Fornax platform |
| `FLUX_FORNAX_RANGE_DAYS` | `120` | 缺省查询窗口，天 |
| `FLUX_FORNAX_MIN_INTERVAL_SECONDS` | `1.1` | 每进程 Fornax 请求最小间隔，含分页和重试 |
| `FLUX_EXPORT_DIR` | `exports/jobs` | 任务文件和 ZIP 保存目录 |
| `FLUX_MAX_EXPORT_JOBS` | `4` | 同时活动的导出任务数 |
| `FLUX_EXPORT_TASK_TIMEOUT_SECONDS` | `600` | 每个任务和单独数据接口的总超时，秒 |
| `FLUX_ARTIFACT_RECOVERY_TIMEOUT_SECONDS` | `60` | 每个 Task 的额外清单刷新和文件恢复下载共享时间预算，秒 |
| `FLUX_MAX_DOWNLOAD_BYTES` | `268435456` | 单次解码后的上游响应上限，256 MiB |
| `FLUX_MAX_EXPORT_BYTES` | `2147483648` | 单次导出任务包的写入数据上限，2 GiB，包含任务清单、不含 ZIP 的重复存储空间；组件触发上限后仍可能交付部分 ZIP |

Fornax 在缺少可用执行数据、指标或需要独立补取 Token 时读取个人 JWT。JWT 已过期时直接报告 `FORNAX_TOKEN_EXPIRED`，不发送过期凭据。收到 401 后重新读取或刷新一次；若凭据未变化则立即失败，不再发送相同 JWT。已被拒绝的 JWT 在进程内共享记录，后续任务直接报告认证失败；每次读取都检查配置凭据变化；外部更新 JWT 文件会覆盖内存缓存。也可调用 `POST /v1/task-data/fornax/auth`，使用 `{"action":"update","jwt":"<新个人 JWT>"}` 更新进程内凭据、`{"action":"reload"}` 重读配置，或 `{"action":"refresh"}` 显式进行一次 Chrome SSO 刷新（允许在自动刷新关闭时手动触发）。返回 `upstream_verified=false` 表示仅加载成功；下次真实查询才验证上游鉴权。内存更新不跨进程共享、不跨重启持久化；reload 读取启动时设置与 JWT 文件，不重读 `.env`。可开启 `FLUX_FORNAX_REFRESH_FROM_CHROME=true` 保留旧脚本的本机 SSO 刷新能力：仅从 Chrome 读取 IAM 域下的认证 Cookie 换个人 JWT，缓存于进程内，不写回文件，也不将个人凭据用于 OpenAPI。启用时使用 `uv run --locked --extra desktop flux-task-service` 启动，需要本机 Chrome 已登录 SSO；服务器部署可保持关闭。示例中的两个凭据文件均相对于进程工作目录。导出文件不会自动清理，可按业务保留周期整理 `FLUX_EXPORT_DIR`。

## 开发验证与联调记录

```bash
uv run --locked python -m pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

测试使用模拟 IAM/OpenAPI，覆盖过滤校验、分页、session 跳过、去重、排除、游标异常、容量限制、鉴权、重试、超时，以及选项级联、字段可见性和缓存。

数据契约测试还覆盖任务清单与 Schema、来源和存储形态兼容、会话 content_kind、文档中的正文／指标读取示例、文件引用校验、故障隔离、取消与部分归档、历史包只读适配。详细验收口径见 [对齐文档的验证记录](docs/downstream-data-contract.md#8-验收与验证记录)。

前端验证命令：

```bash
node tests/ui_behavior.cjs /path/to/node_modules/jsdom
node tests/ui_browser.cjs /path/to/node_modules/playwright
```

浏览器测试需安装 Playwright 和 Chromium，并启动预览；默认预览地址为 `http://127.0.0.1:18081`，可通过 `FLUX_BROWSER_URL` 修改。

历史生产联调、分页改造和样本验证记录已归档到 [`docs/archive/`](docs/archive/)，用于说明当时的验证范围；实际字段可见性和匹配结果以当前上游响应为准。当前文档入口见 [`docs/README.md`](docs/README.md)。
