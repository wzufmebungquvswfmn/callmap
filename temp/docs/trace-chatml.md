# Trace 转 ChatML 纯文本

服务接口：`GET /v1/chatml/{job_id}/{task_id}`，其中 `job_id` 为创建导出时返回的
批次 ID，`task_id` 必须属于该批次。接口读取服务导出目录中的已完成任务数据，
不重新请求上游；部分完成的任务只要有可读取的对话数据，也可以转换。

```bash
curl 'http://127.0.0.1:18080/v1/chatml/<job_id>/<task_id>?mode=user' \
  -o prompts.chatml.txt
```

GET 参数通过 URL query 传递，不需要 JSON 请求体：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `mode` | `all` | `all` / `user` / `assistant`，可重复传入组合输出 |
| `exclude_role` | 无 | 排除指定角色，可重复传入；支持 system、developer、user、assistant、tool、function |
| `visible_escapes` | `false` | 将正文换行、制表符等显示为可见转义序列 |
| `allow_legacy` | `false` | 显式允许兼容历史任务目录 |

只过滤 `tool` 消息：

```text
GET /v1/chatml/<job_id>/<task_id>?exclude_role=tool
```

组合输出用户提示词和模型回复：

```text
GET /v1/chatml/<job_id>/<task_id>?mode=user&mode=assistant
```

组合参数使用同一个参数名重复传入。多个 `mode` 取并集，不重复输出消息；包含
`all` 时选择全部消息，再应用 `exclude_role` 排除条件。多个排除角色也可以组合，
例如 `?exclude_role=tool&exclude_role=system`。结果始终保持原有时间顺序。
`exclude_role=tool` 只排除工具结果，保留系统提示、推理和助手发起的工具调用参数；
若只需要用户提示词与模型回复，使用 `mode=user&mode=assistant`。

成功响应为 `text/plain; charset=utf-8`，正文保持 ChatML 格式；没有符合模式的
消息时返回 200 和空正文。响应头 `X-ChatML-Mode` 为模式，`X-ChatML-Messages`
为输出消息数，`X-ChatML-Source-Messages` 为源消息数，`X-ChatML-Warnings`
为来源诊断警告数。警告不混入正文；详细诊断可使用下述 CLI 查看。
`X-ChatML-Mode` 为逗号分隔的所选模式，`X-ChatML-Excluded-Roles` 为排除角色。
默认无需鉴权。仅配置 `FLUX_API_KEY` 时需要添加
`-H 'Authorization: Bearer <api_key>'`。

批次或任务不存在返回 404；任务正在导出或清单未完成返回 409
（`EXPORT_NOT_READY`）；对话未导出、文件缺失或数据无法转换返回 409
（`CHATML_UNAVAILABLE`）；参数错误返回 422。错误使用服务统一 JSON 格式。

读取单任务目录（以 `data-manifest.json` 为入口）：

```bash
uv run --locked python -m flux_task_service.chatml \
  /path/to/tasks/97651448908683785 -o /path/to/trace.chatml.txt
```

也可以直接指定 `conversation/full-trace.json`，或其中已解包的完整正文 JSON。
历史任务目录需要显式添加 `--allow-legacy`；已有清单缺少 `content_kind` 时，
重新导出任务，或显式指定完整正文文件。

通过 `--mode` 选择输出内容，默认值为 `all`：

| 模式 | 输出内容 |
| --- | --- |
| `all` | 完整内容，包含系统提示、用户消息、助手回复、推理摘要和工具调用及结果 |
| `user` | 仅 `user` 消息，即已记录的用户提示词 |
| `assistant` | 仅助手回复和过程说明，不包含推理摘要、工具调用参数或工具结果 |

CLI 同样支持 `--mode user --mode assistant` 组合模式，以及
`--exclude-role tool` 排除角色；排除多个角色时重复传入 `--exclude-role`。

例如，只导出用户提示词或模型输出：

```bash
uv run --locked python -m flux_task_service.chatml \
  /path/to/conversation/full-trace.json --mode user -o /path/to/prompts.chatml.txt

uv run --locked python -m flux_task_service.chatml \
  /path/to/conversation/full-trace.json --mode assistant -o /path/to/replies.chatml.txt
```

筛选包含主会话和子 Agent 会话，仍保留 ChatML 头部和时间顺序；没有匹配消息时
输出为空。筛选不改变源消息、覆盖范围和诊断信息，也不重建上游遗漏的提示词。
stderr JSON 的 `messages` 为实际输出消息数，`source_messages` 为源消息数，
`mode` 为本次选择的模式。

默认输出 UTF-8 纯文本，JSON 字符串中的 `\n`、`\t`、`\u4e2d` 分别还原为
实际换行、制表符和中文。工具参数及结果中的 JSON 对象也会解包为缩进文本，
字符串值保持实际字符。正文原本包含的字面量 `\n` 不会再次强制解码，
避免损坏正则表达式、路径和代码。

若要将正文控制字符显示为可见转义序列，添加 `--visible-escapes`。
这种模式每条消息的正文为一行，ChatML 消息之间仍使用真正换行。

输出示意：

```text
<|im_start|>user session="main"
请检查这个文件。
<|im_end|>
<|im_start|>assistant session="main" channel="analysis"
先查看文件内容。
<|im_end|>
<|im_start|>assistant session="main" to="Bash" call_id="call_1"
command: cat example.py
<|im_end|>
<|im_start|>tool session="main" name="Bash" call_id="call_1"
print("你好")
<|im_end|>
```

这是供人阅读的 ChatML 文本，使用 `<|im_start|>` / `<|im_end|>` 分隔消息。
头部补充 `session`、`channel`、`to`、`name` 和 `call_id`，用于保留主／子 Agent、
推理／说明以及工具调用关联；不是供模型直接训练的原始 token 序列。

转换保留可取得的用户消息、助手正文、推理摘要、系统提示、工具参数和结果、
子 Agent 内部步骤。按时间稳定排序，工具结果使用完成时间；无时间信息时保留
原始顺序。相同步骤 ID 的重复投影只输出一次，不按文本内容去重。
未被步骤投影覆盖的 OpenAI／Anthropic 模型输出和工具原始输出会补入。
多模态非文本块以结构化文本保留，不会下载媒体。

转换不重建上游未记录的模型请求上下文，不将执行统计节点伪造为消息。
完整性和覆盖范围、遗漏模型输入、未记录输出等诊断写入 stderr 的 JSON，
不会混进正文。完整 Trace 的 `complete=true` 仍限定于来源声明的覆盖范围，
不代表原始模型上下文和外部资源全部可用。

Python 调用：

```python
from flux_task_service.chatml import load_trace

transcript = load_trace("/path/to/conversation/full-trace.json")
text = transcript.render()
prompts = transcript.render(mode="user")
replies = transcript.render(mode="assistant")
without_tools = transcript.render(exclude_roles=["tool"])
dialogue = transcript.render(mode=["user", "assistant"])
escaped_replies = transcript.render(mode="assistant", visible_escapes=True)
warnings = transcript.warnings
```
