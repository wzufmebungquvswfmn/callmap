# 任务产物与下游解析对齐标准

契约版本：`1.0`。实现日期：2026-11-09。

本文描述已经实现的导出与读取行为。适用于更新代码后新生成的导出；历史导出不会自动改写。下游尚未进行实际系统接入验收，本文和配套读取器作为双方对齐依据。

**`data-manifest.json`** **是下游唯一的数据读取入口：以其中的引用、状态和完整性为读取依据。** 文件名用于存储定位，消费方无需自行推断目录结构或从文件名判断数据用途。`job.json` 用于整批进度；没有清单的历史包先经过显式适配，见第 7 节。

## 1. 交付内容与承诺边界

每个已确定 ID 的任务新增 `tasks/<task_id>/data-manifest.json`。保留现有文件名和原始上游响应；`job.json` 继续描述整批进度。`job.json`、`trace.json`、`metrics.json`、`conversation.json` 新增服务契约版本 `schema_version="1.0"`。Trace 中原有的 `format` 仍表示执行来源格式，与服务契约版本分开。

下游可以只复制一个完整的 `<task_id>/` 目录进行读取，目录名必须保持为任务 ID。读取入口不依赖父目录 `job.json`；旧版适配例外，见第 7 节。

本契约承诺：在支持的版本和内容格式内，读取器能返回数据、来源、状态和覆盖范围，明确报告无法读取的情况。契约校验通过不等于数据完整，也不等于业务分析条件已满足。

本次已实现：任务级清单、版本和结构校验、统一文件引用、获取结果与完整性区分、Python 读取器、命令行检查、显式历史适配和兼容验证。执行、raw、会话通过清单独立描述，原文件继续保留。

重新获取支持按任务或按整批创建新导出，原结果保留不变。整批重拉沿用已确定任务 ID；新 `job.json.request` 保存请求参数，`retry_of` 记录来源批次。重拉指定组件时，新包只包含本次选择的组件，不自动合并旧包。详见 [重新获取说明](export-retry.md)。

暂未实现：把 OpenAPI/Fornax 所有执行节点转换成同一种业务模型；把不同会话正文转换成统一消息列表；任意业务附件的内容校验；“严格获取”请求策略。这些内容不属于当前 `1.0` 的承诺。

## 2. 任务级清单

顶层字段：

| 字段               | 类型        | 含义                                           |
| ---------------- | --------- | -------------------------------------------- |
| `schema_version` | string    | 服务契约版本，当前 `1.0`                              |
| `task_id`        | string    | 正整数字符串；读取时与任务目录名校验一致                         |
| `finalized`      | boolean   | 清单是否完成最终写入，不代表所有数据完整                         |
| `task_status`    | string    | 导出任务的状态，例如 queued、ok、partial、error、cancelled |
| `legacy`         | boolean   | 是否由旧版适配器在内存中生成                               |
| `errors`         | object\[] | 任务级导出／超时错误，包含 `code`、`message`               |
| `datasets`       | object    | 以下八种数据条目始终存在                                 |

固定条目：`task`、`execution`、`raw_spans`、`conversation`、`metrics`、`artifacts`、`versions`、`gui`。

每个条目具有同一组字段：

| 字段         | 类型             | 约定                                                  |
| ---------- | -------------- | --------------------------------------------------- |
| `status`   | string         | `not_requested`、`available`、`partial`、`unavailable` |
| `complete` | boolean / null | 声明范围内的数据完整性；未知为 null                                |
| `source`   | string / null  | 每类数据独立记录来源；未知为 null                                 |
| `scope`    | object         | 保留查询窗口、完整性范围、覆盖信息或统计诊断；未知信息不推断                      |
| `files`    | object\[]      | 通过校验的文件引用，空时为 \[]                                   |
| `errors`   | object\[]      | 获取或契约校验错误，程序根据 `code` 判断                            |
| `warnings` | string\[]      | 人工提示，不作为程序分支依据                                      |

`available` 表示当前数据可用，但 `complete` 仍可能为 null；`partial` 表示已取得部分数据或伴随错误。由于错误可能来自回退过程，`partial` 与 `complete=true` 可以并存：前者记录获取过程的问题，后者仅描述声明范围内的内容完整性。`unavailable` 不会声明 `complete=true`。未请求的条目固定为 `complete=null`、`files=[]`。

不可用条目可以保留用于诊断的摘要文件，不能因为它有文件引用就把它当作正文可用。例如，会话摘要存在而 `content_available=false` 时，`conversation.status` 仍是 `unavailable`。

指标缺失值使用 `null`，真实零值保留为 `0`。空 spans 数组不能独立证明任务没有执行；raw 未请求、未取得和已取得空结果由状态区分。

## 3. 文件引用与内容格式

清单中的文件引用格式如下：

```json
{
  "path": "trace/openapi.json",
  "format": "trace-read-model.v2",
  "pointer": "",
  "role": "data"
}
```

- `path` 统一相对于当前任务目录，使用 `/` 分隔。拒绝绝对路径、`..`、不规范路径和指向任务目录外的符号链接。
- `pointer` 是 JSON Pointer；空字符串表示文件整体。支持 `~0`、`~1` 转义和规范的数组索引。
- `role` 表示用途，例如 `data`、`summary`、`initial-manifest`、`refreshed-manifest`、`download-index`、`artifact`、`snapshot`、`diff`、`screenshot`。
- `content_kind` 是可选的会话内容类型标记：`full-trace`、`messages-only`、`inventory`、`chunk`、`messages`；其他引用为 null。会话摘要使用 `role=summary`，原始会话响应保留 `role=data`，通过 `content_kind` 区分目录响应与正文响应。
- **原有文件内部的引用不改写**，例如 `conversation.json.files` 和 `downloads.json[].file` 仍相对于导出根目录。独立交付任务目录时，使用新清单中的引用，不直接拼接旧字段。

支持的内容格式：

| `format`                       | 读取后的内容             | 校验边界                                   |
| ------------------------------ | ------------------ | -------------------------------------- |
| `trace-read-model.v2`          | OpenAPI 执行对象       | 对象、版本标识、sessions 数组；节点内部保留上游结构         |
| `flux.normalized-spans.v1`     | Fornax 适配后的执行记录数组  | 对象数组；保留现有适配后的节点字段                      |
| `fornax.spans.v1`              | Fornax 原始 spans 数组 | 对象数组，与适配后的执行记录分开                       |
| `json-records.v1`              | OpenAPI raw 记录数组   | 对象数组；不将其当作统一业务节点模型                     |
| `flux.metrics.v1`              | 指标对象               | 固定核心指标字段、数值或 null、布尔完整性字段              |
| `flux.conversation-summary.v1` | 会话摘要               | 来源、正文可用性、完整性、文件数组、错误数组                 |
| `flux.downloads.v1`            | 下载结果数组             | 路径、ok/error 状态；成功有 file，失败有 error      |
| `json-object.v1`               | 上游原始对象             | 仅保证 JSON 对象；正文、任务详情、版本、GUI 等业务字段保持上游结构 |
| `bytes.v1`                     | 原始字节               | 文件存在且位于任务目录内；不解析内容                     |

原始 JSON 必须合法；拒绝 NaN、Infinity 和重复对象键。未知格式明确返回 `UNSUPPORTED_FORMAT`，不猜测解析方式。

`execution` 不会因仅有会话而变成会话数据：这种情况下它是 `unavailable`，会话由 `conversation` 单独提供。默认不请求 raw，`raw_spans` 为 `not_requested`。Fornax raw 引用原始来源文件，不将适配后的 spans 冒充原始记录。

指标核心字段：`input_tokens`、`output_tokens`、`reasoning_tokens`、`cached_tokens`、`total_tokens`、`clean_total_token`、`end_to_end_latency_ms`、`total_latency_ms`、`interaction_latency_ms`、`latency_ms_first_resp`、`model_call_count`。这些键必须存在，值可为 null。`non_cached_input_tokens`、`cache_creation_input_tokens` 为可选扩展。Token 和调用数必须是整数或 null，耗时可为整数、浮点数或 null；不将字符串或布尔值转换成数值。

指标可用性分为 `tokens_available`、`total_latency_available`、`first_response_available`；对应完整性为 `tokens_complete`、`total_latency_complete`、`first_response_complete`。这些是可选扩展，旧文件缺少时按未知处理。`timing_complete` 仍要求总耗时及首响应均完整；已有数值不因其他维度缺失而清空。

Token 与耗时可能来自不同来源。`token_sources` 保留逐来源数值、覆盖、完整性、诊断和范围，`token_field_sources` 标记每个已知字段的来源。`task_detail_totals` 只表示任务详情累计值，完整性未确认；Fornax 的 `token_scope.kind=fornax_window` 限定于时间窗口。两者都不能自动推断完整任务 Trace 总量。冲突保留原值及备用来源；上游错误记录在 `fallback_errors`。详见 [Trace / Token 兜底对齐规则](trace-token-fallback.md)。

## 4. 下游读取方式

### 按业务用途读取指标

只需要指标时，直接读取 `metrics` 并检查业务所需的完整性，不以整包 `validate()` 为前置条件。下面的函数默认要求 Token 和耗时均完整；调用方可以分别调整要求，以及是否接受 `partial` 状态。

```python
from flux_task_service.export_contract import TaskDataReader


def read_metrics_for_analysis(
    task_dir, *, accept_partial=False, require_tokens=True, require_timing=True
):
    reader = TaskDataReader(task_dir)
    metrics = reader.read_dataset("metrics")
    accepted = {"available", "partial"} if accept_partial else {"available"}
    if metrics["status"] not in accepted:
        raise ValueError({"status": metrics["status"], "errors": metrics["errors"]})
    candidates = [
        doc["content"]
        for doc in metrics["documents"]
        if doc["reference"]["role"] == "data"
        and doc["reference"]["format"] == "flux.metrics.v1"
    ]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one metrics payload")
    payload = candidates[0]
    if require_tokens and payload.get("tokens_complete") is not True:
        raise ValueError("Token totals are not confirmed complete")
    if require_timing and payload.get("timing_complete") is not True:
        raise ValueError("Timing is not confirmed complete")
    return payload
```

函数返回完整指标对象，保留 `values`、来源、覆盖和诊断信息。仅分析 Token 时可传 `require_timing=False`；只使用已知部分统计时，可以显式放宽相应条件。接受 `partial` 不会自动放宽 Token／耗时完整性要求。`null` 仍然表示未知，调用方不能将其换成 0。

正常情况下，其他数据集的文件损坏不会影响这条指标读取路径。所有读取仍依赖合法且已完成的顶层清单；顶层清单损坏、版本不支持或未完成时必须报错。

### 读取会话正文

`read_dataset(name)` 返回清单中的同一组条目字段，并增加：

```json
{
  "documents": [
    {"reference": {"path": "...", "format": "...", "pointer": "", "role": "data"},
     "content": {}}
  ]
}
```

会话的 `documents` 可能同时包含摘要、inventory、直读响应和分页正文。下面按引用用途跳过摘要和 inventory，实际取出直读响应的 `trace_detail.data`（缺少 data 包装时保留 trace\_detail 对象）或分页响应的 `data`；不根据路径或文件名分类：

```python
from flux_task_service.export_contract import TaskDataReader


def read_conversation_bodies(task_dir, *, accept_partial=False):
    reader = TaskDataReader(task_dir)
    conversation = reader.read_dataset("conversation")
    accepted = {"available", "partial"} if accept_partial else {"available"}
    if conversation["status"] not in accepted:
        raise ValueError(
            {"status": conversation["status"], "errors": conversation["errors"]}
        )
    if not accept_partial and conversation["complete"] is not True:
        raise ValueError("Conversation is not confirmed complete")
    bodies = []
    for doc in conversation["documents"]:
        ref, raw = doc["reference"], doc["content"]
        if ref["role"] == "summary":
            continue
        kind = ref.get("content_kind")
        if kind == "inventory":
            continue
        if kind in {"full-trace", "messages-only"}:
            detail = raw.get("trace_detail")
            if not isinstance(detail, dict):
                # 未取得正文的直读响应可能与随后成功的分页响应一同保留。
                continue
            body = detail.get("data", detail)
        elif kind in {"chunk", "messages"}:
            body = raw.get("data", raw)
        else:
            raise ValueError("Missing or unsupported conversation content_kind")
        bodies.append({"reference": ref, "content": body})
    if not bodies:
        raise ValueError("No conversation body response was found")
    return {"scope": conversation["scope"], "bodies": bodies}
```

返回值是**上游正文对象／批次及其引用**，不是统一消息列表。不同批次可以有不同业务结构，可能包含步骤、消息或其他追踪内容；消费方需按实际来源结构继续提取所需字段。保留清单中的批次顺序，不能直接将批次数当成消息数，也不应未经业务去重就拼成一段对话。完整性始终限定于返回的 `scope`。

`content_kind` 是本次补充的可选字段，新导出以及旧包的内存适配都会生成；已经生成且未带该字段的清单保持原样。上述正文示例遇到缺少标记时明确报错，此类包需要重新导出以生成新清单，消费方不自行从文件名猜测。现有新清单不会因 `allow_legacy=True` 而被自动改写。

### 二进制文件与整包验收

`content` 保留声明格式对应的内容，不统一节点或消息字段。`documents` 仅包含 JSON 引用，二进制文件仍列在 `files` 中，按需读取：

```python
reader = TaskDataReader("/path/to/tasks/97651448908683785")
for ref in reader.manifest.datasets["artifacts"].files:
    if ref.role == "artifact":
        content = reader.read_file(ref)
```

`read_file` 仅接受该清单已声明的引用。读取器在读取时再次检查引用和内容；某类数据的文件损坏会明确抛出 `ContractError`，不影响调用方另行读取其他类数据。

整包交付验收时，再检查全部引用：

```python
reader = TaskDataReader("/path/to/tasks/97651448908683785")
report = reader.validate()
if not report["valid"]:
    raise ValueError(report["errors"])
```

`validate()` 适用于整包验收，会报告任意数据集的结构或引用问题；按业务用途读取时，应采用前面的局部读取方式。命令行同样检查整个解压后的任务目录：

```bash
uv run --locked python -m flux_task_service.export_contract \
  /path/to/tasks/97651448908683785
```

输出 JSON 报告。退出码 0 表示契约和引用检查通过；退出码 1 表示结构、引用、版本等校验失败。上游获取失败但已准确标记为 unavailable 的数据可以通过契约校验。报告中的状态必须由下游继续判断，不能把退出码 0 当成全部数据完整。

当前读取器接受解压后的目录，不直接读取 ZIP。`validate()` 检查二进制文件位置与存在性，不重新加载所有二进制内容或计算哈希；`read_file()` 才读取字节。

## 5. 完整性、文件与故障处理

1. 任务进入导出时先写 `finalized=false` 的清单；基础信息阶段继续保持 false。
2. 单任务结束时写最终清单。整批取消时，已确定 ID 的未执行任务也写最终清单，其所请求数据标记不可用，任务状态为 cancelled。
3. ZIP 打包前再次生成清单并检查引用。取消和部分失败可以正常打包。
4. 文件写入采用临时文件后替换；清单计入导出字节上限，重复更新只计算当前清单大小。
5. 某个组件因字节上限或写入异常失败时，只要最终清单写入和打包成功，仍可交付部分 ZIP，`archive_ready=true`。最终清单写入、打包或归档就绪状态保存失败时，才不发布归档，`archive_ready=false` 并记录导出失败；最终清单失败时，不能保证每个任务都有 finalized=true 的清单。是否达到过字节上限不能单独决定归档是否可下载。
6. 进程被强制终止时，未完成清单保持 false，读取器返回 `MANIFEST_NOT_FINALIZED`；当前没有自动续跑或自动补齐清单。
7. 数据内容／引用校验失败时，保留原文件，将错误记录到对应清单条目和任务的 `contract_errors`，移除不可用引用。任务不会仍被声明为完全成功。

完整性范围分别判断：

- 执行和 raw 各自使用独立标记。未知执行完整性不从旧版总 `complete` 推断。
- 会话保留 `mode`、`scope`、`coverage`、`omitted`、`resources_not_embedded`、`completeness_scope`。
- Fornax 保留查询窗口；该窗口完整不扩展成全部历史完整。
- metrics 的 Token 和耗时完整性、覆盖及诊断保留在 `scope` 和指标原文中。
- artifacts 的 `complete` 仅表示初始清单选中的路径都下载成功，不代表任务全部历史产物。清单刷新、版本变化和逐文件校验信息仍在下载索引中。
- task、versions、gui 没有足够证据证明全部历史完整时，`complete=null`。

附件下载仍沿用大小／SHA256 可用时校验的现有实现。`bytes.v1` 不承诺业务 JSON 的字段正确、CSV 的列正确或图片与报告精确匹配。GUI 旧截图接口的子串匹配警告保留在清单里。

## 6. 版本、Schema 与错误码

同一主版本新增可选字段、增加遵循同一包装结构的数据条目允许兼容；读取器忽略不认识的附加字段。删除或重命名字段、改类型、改含义需要升级主版本。当前支持 `1.x`，未知主版本拒绝。新增内容格式也必须升级读取器支持，不会因为主版本相同而自动接受。

机器可读结构定义：

- [任务清单 Schema](schemas/task-manifest.schema.json)
- [指标 Schema](schemas/metrics.schema.json)
- [会话摘要 Schema](schemas/conversation-summary.schema.json)

Schema 由运行时模型生成，自动测试核对文件与模型一致。跨字段状态一致性、JSON Pointer、文件存在性、任务目录身份、有限数值等检查还需运行读取器，不能仅靠 JSON Schema。

重新生成 Schema：

```bash
uv run --locked python -m flux_task_service.export_contract --write-schemas docs/schemas
```

常见读取错误：

| 错误码                                                    | 含义                  |
| ------------------------------------------------------ | ------------------- |
| `MANIFEST_MISSING`                                     | 无新清单，且未启用历史适配       |
| `MANIFEST_NOT_FINALIZED`                               | 导出尚未最终完成            |
| `INVALID_MANIFEST`                                     | 清单类型、必需条目或状态关系不符合约定 |
| `UNSUPPORTED_VERSION` / `UNSUPPORTED_FORMAT`           | 版本或内容格式不支持          |
| `TASK_ID_MISMATCH`                                     | 目录名与清单任务身份不一致       |
| `FILE_UNAVAILABLE` / `INVALID_PATH`                    | 文件缺失、无法读取或引用越界      |
| `INVALID_JSON` / `INVALID_PAYLOAD` / `INVALID_POINTER` | JSON、内容结构或引用位置不合法   |
| `UNKNOWN_DATASET` / `UNKNOWN_REFERENCE`                | 请求了清单未声明的数据或引用      |
| `UNSUPPORTED_LEGACY`                                   | 缺少旧格式适配所需的任务索引      |

`DATA_PENDING`、`DATA_UNAVAILABLE` 和原有上游错误码记录获取状态，不等同于契约结构损坏。读取异常的类型为 `ContractError`，通过 `.code` 获取错误码，不解析提示文本。

## 7. 历史产物兼容（我尽可能避免无意义的兼容）

历史适配必须显式开启：

```python
reader = TaskDataReader("/old-job/tasks/97651448908683785", allow_legacy=True)
```

命令行增加 `--allow-legacy`。适配器要求原有 `job.json` 及 `tasks/<task_id>/` 层级，使用组件索引在内存中生成 `legacy=true` 的清单，不修改旧文件。

已覆盖的历史形态：Trace 内嵌 `data`；通过 `data_file` 引用执行文件；内嵌 spans；`spans_file` 和可选 Pointer；Fornax 执行及原始来源文件；`conversation_only`；旧指标和会话摘要缺少新扩展字段。缺少核心字段或内容格式未知时报告错误，不按“看起来像 JSON”猜测业务格式。

旧格式未提供完整性或 raw 请求意图时保持未知，不推断已经拉全。现有新清单即使损坏，也不会通过 `allow_legacy` 绕过校验。

## 8. 验收与验证记录

自动验证覆盖：

| 验收项     | 检查内容                                                                   |
| ------- | ---------------------------------------------------------------------- |
| 来源与存储位置 | OpenAPI 内嵌／外部执行、raw Pointer／独立文件、Fornax 原始与适配记录                        |
| 降级      | conversation-only、raw 未请求、部分下载失败、未知完整性                                 |
| 独立交付    | 单任务目录复制后可读取会话摘要与分页正文                                                   |
| 严格解析    | 未知版本和格式、字段缺失、错误数值类型、NaN、重复 JSON 键                                      |
| 引用      | 丢失文件、无效 Pointer、目录穿越、越界符号链接、身份不一致                                      |
| 故障隔离    | 指标无效仍能读取任务信息；空二进制文件正常保留                                                |
| 文档示例    | 实际执行指标／正文读取函数；无关文件损坏不阻断指标；按 content\_kind 跳过摘要和 inventory，重命名文件后仍能读取正文 |
| 生命周期    | 初始清单未完成、取消后最终清单、ZIP 包含完整清单；组件限额失败仍可交付部分归档，最终清单限额失败不发布归档                |
| 历史兼容    | 必须显式启用、依赖原 job 索引、只读不改写、未知完整性不推断                                       |
| 版本维护    | 同主版本附加字段兼容、公开 Schema 与运行时模型一致                                          |

本地历史产物另做只读验证：14 次导出中发现 61 个任务目录，均通过兼容读取和引用检查；其中包含 partial、unavailable 等状态。该结果不表示这些任务的数据均完整。

验证入口：`tests/test_export_contract.py` 和 `tests/test_data.py`。全量测试、Ruff 静态检查、格式检查及 Python 语法检查通过；测试环境有一条既有的 Starlette/httpx 弃用提示，不影响验证结果。命令行读取器也已对本地历史任务运行验证并返回 `valid=true`。

下游接入验收仍需用自己的消费程序验证实际所需字段，明确接受哪些状态、是否要求 Token／耗时／raw 完整、以及哪些附件需要业务内容 Schema。完成该接入验证后，才可确认具体下游业务已经稳定消费。
