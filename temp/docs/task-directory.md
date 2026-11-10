# `tasks/<task_id>/` 文件说明

位于 `exports/jobs/<job_id>/tasks/<task_id>/`，ZIP 内保留相同结构。以下列出可能生成的文件，实际取决于所选组件和获取结果。

```text
tasks/<task_id>/
├── data-manifest.json              # 版本化入口：状态、来源、范围、任务相对文件引用
├── trace/
│   ├── openapi.json                # OpenAPI 原始执行数据：步骤、会话、统计等
│   ├── fornax.json                 # Fornax spans 及查询窗口，获取到时保存
│   └── raw-spans.json              # 实验补取的 raw spans，可能仍不完整
├── conversation/
│   ├── full-trace.json             # 默认 full 模式正文，见 trace_detail.data
│   ├── messages-only.json          # messages 模式正文，仅公开消息
│   ├── inventory.json              # 分批读取时的目录和票据
│   └── <index>-<action>-<page>.json # 分批读取的 chunk / messages 原始响应
├── artifacts/
│   ├── files/
│   │   └── <path>                  # 下载并通过可用清单校验的文件，保留原路径
│   ├── manifest.json               # 初始产物清单：路径、大小、SHA256 等
│   ├── manifest-refreshed.json     # 失败恢复时成功刷新的清单
│   └── downloads.json              # 每个文件的下载、校验结果及错误
├── diffs/
│   └── <index>.json                # versions 中有效 comparisons 对应的 Diff
├── gui/
│   └── screenshots/
│       └── <index>.<ext>            # 报告引用且成功下载的截图
├── task.json                       # 任务详情：标题、状态、仓库等，见 items[].task
├── task-snapshot.json.gz            # 可用的任务归档；非 gzip 内容保存为 .bin
├── trace.json                      # 执行来源可为 OpenAPI / Fornax；会话另由 OpenAPI 获取
├── trace.tree.txt                  # Fornax 执行调用树
├── metrics.json                    # 指标及各字段来源、覆盖、完整性，见 token_sources
├── conversation.json               # 会话摘要：正文是否可用、是否完整、正文文件列表
├── versions.json                   # 已采集的版本信息
└── gui.json                        # GUI 用例报告，结果见 items[].case_result
```

新导出先读任务目录的 `data-manifest.json`，或使用 `TaskDataReader`；整批进度看 `job.json`。没有清单的历史导出仍先看 `job.json` 中的 `tasks[task_id].components`。产物下载结果看 `artifacts/downloads.json`。完整规则见 [下游数据契约对齐文档](downstream-data-contract.md)。

- `data-manifest.json` 中的文件引用相对于**当前任务目录**；原有 JSON 内部的引用仍相对于**导出／ZIP 根目录**。
- 只选 `trace` 也会保存会话；只选 `metrics` 也可能保存 `trace/openapi.json`。
- raw 默认不请求；显式请求后看 `raw_spans_status`、`raw_spans_complete` 和 `spans_file`。有 `spans_pointer="/raw_spans"` 时，数组在引用文件的该字段中。
- 产物恢复每个任务最多刷新一次清单，每个失败文件最多恢复下载一次；只下载初始清单中的路径。
- 文件存在不代表完整，缺失原因看组件错误；版本、Diff 和截图取决于上游采集结果。

注意来源与内容变化：

- `trace.json` 的 `source` 表示执行数据来源；即使为 `fornax`，其中引用的会话仍可来自 OpenAPI。执行不可用时也可能仅保留会话，此时 `format="conversation_only"`。
- `metrics.json` 保留 OpenAPI 可用耗时，Token 总量未知时独立补取 Fornax，再按需使用 `task_detail_totals`。`source` 表示基础指标来源；Token 需另读 `token_source`、`token_sources` 和 `token_field_sources`，不能由基础来源推断。任务累计值完整性未确认；Fornax 仅覆盖查询窗口。错误同时保留在指标原文和 `job.json` 组件的 `fallback_errors`，详见 [对齐规则](trace-token-fallback.md)。
- `trace/openapi.json` 和 `trace/fornax.json` 各自来源固定，两个文件可能同时存在。`trace/raw-spans.json` 则可能合并多个 OpenAPI 会话补取结果。
- `artifacts/files/` 中的文件可能按刷新后的清单下载，内容版本可能变化；查看 `downloads.json` 的 `verified_against` 和 `manifest_changed`。
- 截图通过旧接口按文件名子串获取首个匹配对象；文件名不能证明与报告 URL 精确对应，匹配信息和警告见 `job.json`。

已与当前代码核实：[导出落盘](../src/flux_task_service/exports.py)、[会话读取](../src/flux_task_service/conversation.py)、[数据来源](../src/flux_task_service/resources.py)、[产物恢复](../src/flux_task_service/artifact_downloads.py)。

早期文件结构讨论与已实现/未实现事项见 [任务目录早期改进建议（存档）](archive/task-directory-early-proposals.md)。
