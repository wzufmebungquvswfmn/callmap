# 模型与人工动态打标接口

动态接口管理单任务的**完整可编辑标签集合**。模型与人类使用相同格式和现有 Bearer API key。
只有已生成可下载结果（`archive_ready=true`）的打标批次允许读取或修改。

## 接口

| 方法与路径 | 行为 |
| --- | --- |
| `GET /v1/tagging/tag-schema` | 返回固定 tag 白名单、每个 tag 的完整 JSON Schema 与覆盖约定 |
| `GET /v1/tagging/{tagging_id}/task/{task_id}` | 返回可直接合并、提交的 tag 对象，并在响应头返回 `ETag` |
| `POST /v1/tagging/{tagging_id}/task/{task_id}` | 使用请求体整体替换当前可编辑标签；必须携带 GET 所得的 `If-Match` |
| `GET /v1/tagging/{tagging_id}/tasks/{task_id}` | 保留原有完整详情接口；同时包含 `dynamic_tags`、`dynamic_tag_revision`、`dynamic_tag_updated_at` |
| `GET /v1/tagging/{tagging_id}/download` | 默认下载完整批次；重复传入 `task_id` 可下载指定任务 |

注意单数 `task` 返回可编辑对象，复数 `tasks` 返回任务详情；复数接口的整个响应不能作为 POST 请求体。
接口定义也可从 `/openapi.json` 获取。当前动态标签 schema 版本为 `flux.dynamic-tags.v1`。

## 覆盖与并发约定

1. 每次修改前先 GET 单数 `task` 接口，读取当前标签和响应头 `ETag`。
2. 在客户端合并修改，包括 tag 内部数组与对象；服务端不做深度合并。
3. POST 完整合并结果，以 `If-Match` 请求头原样携带 ETag，包括双引号。
4. 成功返回 200、已保存的完整 tag 对象及新 ETag。
5. 如果收到 412，说明版本过期或 If-Match 不匹配；必须重新 GET、重新合并。不能仅换 ETag 后重发旧对象。

遗漏的 tag 会删除；提交 `{}` 会清空可编辑集合；`{"review": null}` 等写法无效。
清空后不会重新自动填入规则标签。修改某个 tag 时，其余需要保留的 tag 必须一起提交。
相同内容再次覆盖也会增加版本；不同任务、不同批次不能共用 ETag。不接受 `If-Match: *` 或弱 ETag 绕过检查。

例如模型添加复核意见，同时保留其他 tag：

```python
import httpx

url = f"{base_url}/v1/tagging/{tagging_id}/task/{task_id}"
headers = {"Authorization": f"Bearer {api_key}"}

current = httpx.get(url, headers=headers)
current.raise_for_status()
tags = current.json()
tags["review"] = {
    "source": "model",
    "annotator": "repository-reviewer",
    "decision": "hold",
    "reason": "仓库提及已确认，但尚无实际读取证据。",
}

saved = httpx.post(
    url,
    headers={**headers, "If-Match": current.headers["ETag"]},
    json=tags,
)
# 412 时重新读取并合并，不能使用这份旧 tags 直接覆盖。
saved.raise_for_status()
```

## 首批 tag 及固定格式

所有层级均拒绝未定义字段，不进行字符串与数字等类型的自动转换。
各 tag 必须声明 `source`，只允许 `human`、`model`、`rule`；可选 `annotator`（非空字符串）和 `note`。
`source` / `annotator` 是调用者声明的标注来源，不是服务端认证出的用户身份。
完整必填项、枚举、长度上限和嵌套结构以 `GET /v1/tagging/tag-schema` 为准。

### `repository`

包含 `repositories` 数组，可为空。每条仓库必填 `name`、`categories`、`purposes`、`states`：

- `name`：规范的命名空间/仓库名，如 `org/backend`；不能填写 URL 或绝对本地路径。
- `platform`：可选平台/主机名，未知时为 null。
- `categories`：上游字段、用户指定、运行时上下文、提交元数据、工具执行的固定枚举，与规则提取一致。
- `purposes`：`target_code`、`reference_code`、`skill_knowledge`、`artifact_output`、`execution_context`、`unknown`。
- `states`：规则提取已有状态，以及 `read_observed`、`modified_observed`、`unknown`；应按证据填写，不能将提及等同于实际操作。
- `evidence`：可选证据数组，限定数据集、相对文件路径、JSON Pointer、来源、状态、用途、片段、分支及 revision 等字段。校验位置格式，但不自动证明原文或语义正确。
- `association`：可选 `candidate` 或 `observed`，默认 `candidate`。

同一个 `name` / `platform` 组合不可重复，分类数组中的值也不能重复。例如：

```json
{
  "repository": {
    "source": "model",
    "repositories": [
      {
        "name": "org/backend",
        "platform": "github.com",
        "categories": ["user_specified"],
        "purposes": ["target_code"],
        "states": ["mentioned"],
        "association": "candidate"
      }
    ]
  }
}
```

这个示例表示完整集合只有 `repository`；实际更新时先与 GET 结果合并，以保留已有其它 tag。

### `task_classification`

必填 `category`（非空字符串）；可选 `subcategory` 和 `reason`。例如：

```json
{
  "source": "model",
  "category": "代码审查",
  "subcategory": "仓库理解",
  "reason": "任务主要阅读工程实现并整理问题。"
}
```

### `review`

必填 `decision`（`keep` / `exclude` / `hold`）和非空 `reason`。例如：

```json
{
  "source": "human",
  "decision": "hold",
  "reason": "需补充仓库关联证据。"
}
```

这是人工/模型复核意见，**不会改写自动 `admission`，也不会改变按自动准入决定生成的候选清单成员**。
调用方如需按复核意见筛选，可读取记录中的 `dynamic_tags.review.decision`。

## 原始标签、持久化与下载

尚无动态修改时，GET 会从已有规则结果初始化 `repository` 和可用的 `task_classification`，标记 `source=rule`，版本为 0；不创建默认复核意见。
初始标签仅取现有规则结果，没有上游请求或额外模型调用。历史结果没有相应规则字段时，该 tag 不出现。
历史标签无法满足当前编辑 schema 时也不自动预填，完整原值仍可通过复数 `tasks` 详情中的 `recorded_only` 查看。

POST 只覆盖 `dynamic_tags`。原有 `recorded_only`、`core`、`admission` 与证据继续保留，可以与修订标签对照。
任务详情、分页记录、候选清单及 ZIP 内各记录都会带出当前动态标签及版本。页面详情可展开“可编辑标签”查看。

动态修改存储于 `_tagging/<tagging_id>/dynamic-tags.sqlite3`。标签和版本在同一 SQLite 事务中替换；并发写入先取得写锁再比较 ETag，失败时回滚。重启后仍可读取。
原始分析 JSON 和初始 ZIP 保持自动分析快照；**获取含当前动态标签的导出应使用下载 API**。迁移或备份批次时也需保留 SQLite 文件。
下载 API 为一次请求读取一份已提交的动态标签快照，保证 ZIP 内详情、索引和清单版本一致；下载期间的后续修改会出现在下一次请求中。

## 按任务批量下载

复用原有下载接口，不传参数时行为不变：

```http
GET /v1/tagging/{tagging_id}/download
```

下载单个或多个指定任务，使用重复的 `task_id` 查询参数：

```http
GET /v1/tagging/{tagging_id}/download?task_id=111
GET /v1/tagging/{tagging_id}/download?task_id=111&task_id=222
```

ZIP 只包含所选任务的 `tasks/{task_id}.json`，每个文件携带当前动态标签。
`index.json` 的记录、任务数量和决定计数也限定在所选范围；`selected-tasks.json`
仍只包含所选范围中自动准入决定为 `keep` 的任务。明确选中的排除或待复核任务也会下载，
只是不会进入候选清单。下载筛选不会修改原批次，后续无参数下载仍返回完整结果。

重复 ID 自动去重，输出沿用原批次顺序。任一 ID 不属于此批次时整个请求返回 404，
不静默忽略；空值、非法 ID、逗号拼接的 ID 返回 422。使用 `task_id=111&task_id=222`，
不要使用 `task_id=111,222`。参数数量上限沿用 `FLUX_MAX_TASK_IDS`，重复参数也计入数量上限。
下载格式仍为打标 JSON 结果 ZIP，不附带原始 Trace 或源码产物。

## 错误状态

| HTTP 状态 | 含义 |
| --- | --- |
| 401 | 缺少或无效的 API key |
| 404 | 打标批次或任务不存在，或任务不属于该批次 |
| 409 | 结果尚不可下载，暂不允许修改 |
| 412 | ETag 过期、不属于此任务或格式不匹配，重新 GET 合并 |
| 422 | 未知 tag、缺少必填项、字段类型/枚举/结构不符 |
| 428 | 缺少 `If-Match`，必须先 GET |
| 503 | 存储不可用或写锁等待超时，可在恢复后重新 GET 合并 |

## 扩展 tag

在 `src/flux_task_service/tagging/tag_schema.py` 中定义继承 `StrictModel` / `TagMetadata` 的 Pydantic 模型，并加入 `TAG_MODELS` 注册表。
服务启动时由同一注册表生成请求校验模型、OpenAPI 和发现接口的 JSON Schema；扩展后重启服务即可生效。
如需从自动结果预填新 tag，另在 `initial_tags()` 增加转换。新增字段宜可选，变更已有字段语义或删除 tag 时应明确版本迁移方案。
