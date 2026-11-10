# 重新获取任务数据

重试会更新原批次，始终返回原 `job_id`，不创建新批次。整批重试覆盖全部已选任务；单任务重试只覆盖该任务，保留其他任务的数据、记录和完整任务 ID 列表。ZIP 重新打包并覆盖原下载地址。

## 接口

| 方法 | 路径 | 范围 |
| --- | --- | --- |
| POST | `/v1/exports/{job_id}/retry` | 原批次已经确定的全部任务 ID |
| POST | `/v1/exports/{job_id}/tasks/{task_id}/retry` | 原批次中的一个任务 |

两者均返回 HTTP 202 和原批次当前进度。配置 `FLUX_API_KEY` 时使用 Bearer 鉴权。

状态、取消和重试响应都支持 `include_tasks=false`、`offset` 和 `limit`（1～1000）对 `tasks` 做切片；默认仍返回全部任务记录，避免改变现有调用方。切片响应的 `tasks_total`、`tasks_offset`、`tasks_limit` 和 `has_more_tasks` 描述本次范围。

同一服务进程内，正在执行或打包的重试收到相同范围、相同显式参数的请求时，直接返回当前作业，不再次调度上游读取。不同范围或参数的请求返回 409；普通首次导出未结束时也返回 409。重试结束后再次调用会重新读取上游并覆盖原批次，ID 仍不变。这是批次身份的幂等，不是永久缓存第一次请求的结果。

## 默认行为与参数覆盖

请求体可省略或传 `{}`。整批使用原批次保存的参数和已经确定的任务 ID，不重新执行筛选。单任务优先沿用该任务上次重试的参数，否则使用批次参数。整批重试会统一全部任务的参数，并清除单任务参数覆盖。

| 可选字段 | 行为 |
| --- | --- |
| `components` | 重新获取的组件，例如 `["versions"]`；省略或 null 时继承，空数组不合法 |
| `conversation_mode` | `full` / `messages`；省略或 null 时继承 |
| `include_raw_spans` | 是否请求 raw；省略或 null 时继承 |
| `recover_raw_spans` | 是否启用实验性 raw 恢复；省略或 null 时继承 |
| `start_ms` / `end_ms` | Fornax 时间边界；省略时继承，显式 null 清除边界 |

参数合并后重新校验：raw 恢复必须同时启用 raw，开始时间不得晚于结束时间。任务数、并发数、超时和导出字节限制仍然有效，保留的其他任务文件也计入批次字节上限。

被重拉任务的旧目录在后台处理开始时清理，再写入本次结果；显式只选部分组件时，该任务仅保留本次请求的组件。旧附件、旧会话分片等不会混进新结果。失败或取消也以本次状态和已获取数据为准，不回退旧结果。需要保留旧快照时，请在重试前下载 ZIP。

重试受理后 `archive_ready=false`，原下载接口暂时返回 409；完成打包后恢复下载。服务重启后未完成的作业标记为 interrupted，旧 ZIP 不会被当成本次新结果发布，可以再次对原 ID 发起重试。

## 示例

```bash
curl -X POST http://127.0.0.1:18080/v1/exports/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/retry \
  -H 'Content-Type: application/json' -d '{}'
```

单任务只重新获取版本信息：

```bash
curl -X POST http://127.0.0.1:18080/v1/exports/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/tasks/111/retry \
  -H 'Content-Type: application/json' -d '{"components":["versions"]}'
```

原批次有两个任务时，响应仍保留两个任务，节选如下：

```json
{
  "job_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "status": "queued",
  "task_count": 2,
  "archive_ready": false,
  "retry_of": {
    "job_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "scope": "task",
    "task_id": "111",
    "parameters": "saved_request"
  }
}
```

后续使用同一个 ID：

```text
GET  /v1/exports/{job_id}
GET  /v1/exports/{job_id}/download
POST /v1/exports/{job_id}/cancel
```

`request.task_ids` 和 `task_ids.json` 始终包含完整批次范围，`request.filters=null`。`request` 保存批次默认参数，`task_requests` 保存单任务覆盖；`retry_request` 保存最近一次实际执行的请求，`retry_of` 记录范围。状态和请求信息也写入 ZIP 内的 `job.json`，服务重启后仍可读取。

## 历史批次与页面

任务 ID 优先从 `task_ids.json` 恢复，否则使用已保存请求的明确 ID，或数量与 task_count 一致的任务索引。范围不完整或不一致时明确报错。

没有保存请求的历史批次沿用原组件，其余未覆盖参数使用当前默认值，并在 warnings 中提示，`retry_of.parameters="legacy_defaults"`。时间边界为 null 时按重新请求时的默认窗口计算；需要固定窗口请显式传入边界。

页面「重新获取整批」和任务行「重新获取」会更新原批次进度，单任务重试后仍显示完整批次。提交和执行期间禁用重试按钮，请求受理失败时保留当前结果。页面组件勾选不会改变重试参数。「重新查询进度」只恢复轮询，不重新获取数据。

## 错误与结果判断

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 404 | `EXPORT_NOT_FOUND` | 批次不存在 |
| 404 | `EXPORT_TASK_NOT_FOUND` | 任务不属于该批次 |
| 409 | `EXPORT_STILL_RUNNING` | 已有不同范围／参数的操作或首次导出尚未结束 |
| 409 | `EXPORT_RETRY_EMPTY` | 原批次未选中任务 |
| 409 | `EXPORT_RETRY_SCOPE_UNAVAILABLE` | 无法恢复完整原任务范围 |
| 409 | `EXPORT_RETRY_INVALID_SOURCE` | 保存的请求或任务索引损坏 |
| 422 | `INVALID_QUERY` | 参数不合法 |
| 422 | `QUERY_LIMIT_EXCEEDED` | 重试任务数超过限制 |
| 429 | `EXPORT_BUSY` | 活动作业数量已满 |

受理成功不保证上游问题恢复。请查看任务状态和数据清单，以 `archive_ready` 判断能否下载。
