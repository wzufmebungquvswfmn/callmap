# 2026-11-03 验证记录

预览地址：<http://127.0.0.1:18081/>。查询使用 `../secret` 换取 IAM 服务账号 JWT。
本次未修改 18080 的旧进程。当前工作区源码的变更由 18081 自动重载。

生产分类选项仍不可用；浏览器的三级级联行为已使用固定响应验证。

## 上游只读核查

| 请求                                                   | 实测结果                                                                                      | 能说明什么                       |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------- | --------------------------- |
| POST /openapi/v1/task，三天创建时间范围，limit=3               | HTTP 200，code=0；3 行均为 task-metadata-only.v1                                               | 列表同样使用受限投影                  |
| *POST /openapi/v1/task，已知单个 task ID*                 | HTTP 200；task-metadata-only.v1                                                            | 单任务详情查询也没有分类字段              |
| 同一 task ID，include\_trace=true，limit=1               | HTTP 200，code=2002，msg=request\_failed                                                    | 当前账号无法使用该敏感模式；不能推断加权限必然返回分类 |
| 预览 /v1/filter-options，task\_is\_completed，11 月 1～3 日 | 0、1 两个选项                                                                                  | 完成标记可以从真实任务数据提取             |
| 预览 /v1/filter-options，category，同一范围                  | availability=unavailable；page\_count=1；scanned\_count=200；stop\_reason=field\_unavailable | 分类快速返回不可见提示，没有扫描完整范围        |

任务投影包含的字段名：

```text
agent_run_time_secs, cached_tokens, id, input_tokens, interaction_count,
output_tokens, permission_request_count, privacy_projection, row_source,
task_id, task_is_completed, total_execution_duration_ms, total_tokens,
worker_session_id
```

`created_by`、`task_status`、`level_1_category`、`level_2_category`、
`level_3_category` 均未出现。这里仅记录字段名，不保存密钥、JWT 或任务正文。

本地《\[OpenAPI]Flux Insight 开放接口说明》引用 Backend 50942ae / Helix b792f3c425，
说明核对日期为 2026-10-29，并明确没有核验生产部署。
说明提到 `access_sensitive`，但没有定义 `task-metadata-only.v1` 的具体白名单，
也没有列出独立的分类字典 OpenAPI。未在已检索的本地项目中找到相应后端源码。
尝试读取文档内链接指向的在线说明时，lark-cli 返回 `token_missing`，未取得新版本内容。

因此仍需上游确认：

1. 生产任务投影由哪个实现生成；列表和单任务是否固定使用 metadata-only 白名单。
2. `access_sensitive` 对这五个字段的具体效果，尤其分类字段是否纳入可返回列。
3. 是否存在支持 IAM 服务账号的分类字典接口及其路径、请求格式和父子关系定义。

不能把 Trace 模式权限拒绝当成分类缺失的确定根因，也不能把文档字段示例当成生产可用性证明。

## 验证结果

- Python：59 项通过；ruff 检查和格式检查通过；py\_compile 通过。
- DOM：4 项通过，覆盖选项分页、不可见字段、取消、失败恢复。
- 真实 Chromium 固定响应：4 项通过，覆盖三级分类、同名分支、切换父级清空子级、
  包含/排除路径提交、结果过期、分页合并、取消旧请求、失败恢复。
- 真实 Chromium 生产检查：1 项通过，核对完成标记 0/1、分类 unavailable、1 页提前结束。
  此项通过表示当前不可见状态被正确呈现，生产真实分类级联仍未验收。

浏览器脚本：[tests/ui\_browser.cjs](../../tests/ui_browser.cjs)。
页面截图：[validation-2026-11-03.png](validation-2026-11-03.png)。

```bash
FLUX_BROWSER_LIVE=1 \
FLUX_BROWSER_SCREENSHOT=/tmp/flux-task-browser.png \
node tests/ui_browser.cjs /Users/bytedance/node_modules/playwright
```

本次同时修正了页面、API 警告和 README 中过于确定的“补齐权限”表述。
它们现在准确说明字段不可见，不承诺申请权限后就能解决。
