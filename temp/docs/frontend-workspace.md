# Task reflux 前端

前端由 FastAPI 同源提供，无需额外安装 Node 或运行前端构建。页面分为任务、下载、候选。接口文档收在连接设置中。

## 操作流程

1. 选择时间、人员或分类，查询后按页浏览，也可加载全部。每页自动批量读取标题、人员、状态和创建时间，不逐项调用详情。列表可以跨页勾选，修改筛选后必须重新查询才允许使用旧结果下载。
2. 点任务打开详情，查看概览统计、会话、执行步骤、文件、版本和测试报告。也可直接输入任务编号；工作会话仅开放文件、版本与报告，独立会话仅开放会话内容。
3. 勾选任务后点准备下载，或手动输入编号。默认会话与分析；交付文件、完整资料均可自定义。包含文件时提示获取成本。创建后收起配置，直接显示结果、当前阶段、文件进度和下载操作；新建批次可重新配置。
4. 已完成资料批次可直接进入候选筛选。测试类型、问题线索和测试命令使用表单设置。证据按需展开，复核和标签编辑使用中文字段。
5. 候选任务可跨页勾选；“选中保留项”替换当前选择，扫描当前筛选范围并优先使用复核结论，无复核时使用自动判定。“下载所选资料”下载原批次的对应任务，“下载所选打标结果”下载对应标签结果。两者均使用重复的 `task_id` 查询参数；空选择禁止下载。切换打标批次清空选择，普通筛选和翻页保留选择。
6. 人工复核窗口并排显示已保存对话和标签表单。对话可切换助手回复、用户输入、全部消息，也可复制或下载 TXT；切换模式不清空标签输入。保存后列表更新复核结论，自动判定保持独立。

## 接口映射

| 工作流 | 接口 |
| --- | --- |
| 查询、加载全部、更新可选分类 | `POST /v1/task-ids/query`、`/v1/task-ids/collect`、`/v1/filter-options` |
| 批量任务摘要 | `POST /v1/tasks/summaries`，每批最多 200 个编号；仅投影列表展示字段 |
| 概览、统计、执行过程 | `POST /v1/task-data/detail`、`/metrics`、`/trace` |
| 会话 | `POST /v1/task-data/conversation`；内部处理 inventory、chunk、messages、票据及游标 |
| 文件 | `POST /v1/task-data/artifacts/list`、`/raw`；下载优先固定清单返回的工作会话 |
| 版本比较 | `POST /v1/task-data/artifacts/versions`、`/version-diff`；支持物理版本或会话版本、零基线 |
| 测试报告与截图 | `POST /v1/task-data/artifacts/gui-test/case-result`、`/screenshots` |
| 备用凭据 | `POST /v1/task-data/fornax/auth`；更新、重新读取、刷新 |
| 资料获取 | `POST /v1/exports`；`GET /v1/exports/{id}`、`/{id}/download`；`POST /{id}/cancel`、`/{id}/retry`、`/{id}/tasks/{task_id}/retry` |
| 批次历史与进度 | `GET /v1/exports?offset=0&limit=20`；`GET /v1/exports/{id}/overview` 返回计数与阶段，不返回整批任务或请求参数 |
| 批次任务分页 | `GET /v1/exports/{id}/tasks?offset=0&limit=50&status=all&search=`；状态支持 all、active、issues、ok |
| 已保存资料 | `GET /v1/exports/{id}/tasks/{task_id}/datasets/{dataset}`；文档按 offset/limit 分页；`.../file?path=` 只允许清单中登记的附件 |
| 所选任务输出 | `GET /v1/exports/{id}/download?task_id=111&task_id=222`；`GET /v1/tagging/{id}/download?task_id=111&task_id=222` |
| 复核对话预览 | `GET /v1/chatml/{export_job_id}/{task_id}?mode=assistant\|user\|all`，读取纯文本，保持换行，不执行正文中的 HTML；assistant 模式由后端过滤推理与工具调用 |
| 候选筛选 | `POST /v1/exports/{id}/tagging`；`GET /v1/tagging/{id}`、`/{id}/results`、`/{id}/tasks/{task_id}`、`/{id}/selected`、`/{id}/download`；`POST /{id}/cancel` |
| 人工标签 | `GET /v1/tagging/tag-schema`；`GET/POST /v1/tagging/{id}/task/{task_id}`，保留未修改标签并使用 ETag 防止覆盖他人修改 |
| 连接状态 | `/health/ready` 显示服务和凭据状态；`/health/live` 保留供进程探活 |

## 展示与状态

- 原始接口 JSON 不作为页面内容。文件正文、Diff 和测试证据以纯文本渲染，不执行 HTML。
- 未知统计显示“暂无数据”，真实零值保留；不完整记录明确提示，不把下载包就绪当作资料完整。
- 查看新任务、切换详情页或关闭详情会取消旧请求，避免旧响应覆盖当前任务。
- 服务访问密钥和 Fornax 凭据不写入浏览器存储。资料批次从服务端分页读取；本浏览器最近 20 条记录只作备用。筛选历史仍保存在本浏览器。手动输入编号也可恢复批次。
- 批次运行与当前查看分开：切换、新建不会取消已有后台任务；旧响应不能覆盖新选择。URL 保存当前批次，刷新后继续查看。短暂网络故障自动重新查进度，不重发创建或重试操作。
- 从批次任务行打开的是已保存资料，不调用上游。用户可明确切换到实时资料。未请求、缺失或尚未完成的数据会说明原因；不会静默使用实时数据填充。
- 任务概览和统计独立加载，局部失败可单独重试。会话可按角色与正文搜索；执行过程按会话、轮次和步骤展开；文件按目录浏览、每次最多展示 100 项，小文本和常见位图可预览；版本默认从已采集编号中选择。
- 人工复核不改写自动候选判定，未修改标签保持原来源。版本冲突时保留当前输入并提示重新读取。
- 旧数据的正文结构不一定能直接展示；没有可识别内容时明确显示空状态，完整资料仍可通过下载中心取得。

## 前端文件

- `api.js`：同源请求、访问密钥、错误处理和下载。
- `snapshot.js`：将清单中的已保存文档适配到详情视图，严格分离实时请求。
- `detail_views.js`：文件目录、受限预览和执行层级的独立展示组件。
- `transcript.js`：复核窗口中的纯文本对话预览、模式切换、复制和 TXT 下载。
- `ui.js`：筛选、日期、分页、收集及导出状态机。
- `tagging.js`：候选策略、后台进度、结果与证据。
- `workspace.js`：导航、任务选择、详情、历史、连接设置和人工标签表单。
- `ui.css`：既有交互控件基础样式；`workspace.css`：工作区布局和响应式呈现。

`tests/ui_workspace_browser.cjs` 使用真实 Chromium 和本地服务页面，模拟业务响应，覆盖任务至下载的连贯操作、分页、795 文件目录、受限预览、慢指标、批次切换和刷新恢复、快照隔离、复核冲突与窄屏布局。`tests/test_workspace_api.py` 验证批量摘要、历史、状态分页、快照读取、鉴权和文件路径边界。现有 DOM 与浏览器回归继续验证筛选和后台导出状态机。
