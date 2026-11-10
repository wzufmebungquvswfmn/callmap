# Callmap

面向 agent 和人的 Python 静态调用图工具。指定入口函数，展开项目内部实现；
外部接口显示签名与说明；同一个函数只有一个节点，共享调用及递归保留为有向边。

## 快速开始

需要 Python 3.11+，建议 3.12。核心分析、SQLite、CLI 和网页仅使用标准库。

```sh
uv sync --python 3.12 --extra semantic --extra mcp --extra dev
.venv/bin/callmap --project ../flux_test_be --cache-dir artifacts/flux_test_be index
.venv/bin/callmap --project ../flux_test_be --cache-dir artifacts/flux_test_be serve
```

打开 `http://127.0.0.1:8765`。推荐搜索入口：

- `RunnerFactory.get_runner`：跨模块调用、共享日志函数及动态注册表。
- `app.utils.logging._ctx_prefix`：通过返回类型连接到上下文对象的方法。
- `_remove_internal_error_fields`：真实业务递归，两个调用点回到同一节点。

网页支持搜索、按深度展开、单分支折叠、缩放、从选中节点重新展开、源码详情、
外部接口说明、人工补充及 JSON/Markdown 下载。默认显示 2 层、最多 300 个节点，
裁剪时会提示；底层索引保存全项目图。
搜索可按符号类型、路径、所属类、装饰器和路由筛选。调用详情默认展示解析依据、
工厂配置分支及缺失证据，支持跳转到配置字段的定义与读写来源；工具栏的“修改影响”
关联 Git 变更与基准/当前版本的静态入口和测试调用。
工具栏可切换“下游调用”和“上游调用者”，上游视图从所选函数向调用者追溯，
箭头仍表示调用者 → 被调用者；折叠、继续展开和导出沿当前查询方向工作。
选中节点时，它及当前图中直接相连的上游/下游节点保持正常亮度，相关连线高亮；
其余节点和连线变淡并绘制在底层，不遮挡相关连线。变淡的节点仍可点击切换选择。

首页会自动显示当前项目路径和发现的入口，只有一个明确入口时自动展开调用图；
多个明确入口或只有候选时，先在左侧选择。URL 中显式指定的入口、用户手动选择的
函数优先于自动展开。入口列表支持加载更多，搜索仍可查询任意函数。

入口发现不执行项目代码，结果随 `index` 保存到 `index.sqlite3`：

- **明确入口**：`pyproject.toml` 的 `project.scripts` / `project.gui-scripts`、
  `__main__.py` 和 `if __name__ == "__main__"` 中静态定位的内部调用目标。
  未定位到内部目标的主程序代码会推荐模块节点。同一目标的多个依据合并。
- **候选入口**：可识别的 FastAPI/Flask 路由、Celery 任务、Click/Typer 命令注册，
  调用图中的注册表证据，以及没有已知内部调用者的公开顶层函数。
  后者也可能是未使用的代码，不自动展开；测试、私有函数和未被注册的方法不作为此类补充候选。
- 注册识别支持常见直接导入/别名和模块级框架工厂实例，不保证覆盖跨模块传入的
  路由对象、动态插件或全部框架。每个结果附 `confidence`、`reasons` 和源码位置；
  入口配置目标未纳入索引或有歧义时返回 `warnings`，不会猜测实际目标。

HTTP 接口为 `GET /api/entries?limit=50&offset=0`，返回 `auto_entry`、入口数量和
分页位置 `next_offset`。入口判断基于当前扫描范围，扫描过滤会影响结果；
源码或入口配置改变后查询会校验并更新索引。

## CLI

### 单文件可执行程序

`dist/callmap`（Windows 为 `dist/callmap.exe`）内置 Python、MCP 和 Jedi，
并包含 Web 页面。运行时不需要安装 Python 或创建虚拟环境：

```sh
./dist/callmap --project /path/to/project index
./dist/callmap --project /path/to/project serve
./dist/callmap mcp
./dist/callmap register
```

`register` 默认只预览配置；生成的 MCP 命令直接指向二进制。请先将它放到
稳定路径，再使用 `register --apply` 注册，之后移动文件需重新注册。

从源码构建并验证：

```sh
uv sync --python 3.12 --extra binary --extra mcp --extra semantic --extra dev
.venv/bin/python scripts/build_binary.py
.venv/bin/python scripts/verify_binary.py dist/callmap --output dist/smoke-report.json
```

构建产物匹配构建机器的操作系统和 CPU 架构；macOS ARM64、macOS Intel、
Linux 和 Windows 需分别构建。PyInstaller 单文件启动时会解压到临时目录，
首次启动有额外开销；索引及人工注释仍保存在项目 `.callmap/` 或指定缓存目录。
macOS 构建默认使用本地签名，公开分发时需另行使用 Developer ID 签名和公证。

二进制不会包含待分析项目的第三方依赖源码。需要分析外部库声明和文档时，
通过 `--library-root /path/to/site-packages` 指定；打包中的 Jedi 使用内置解释器，
不自动启动目标项目的虚拟环境。

所有全局选项放在子命令之前。默认缓存位于被分析项目的 `.callmap/`；
使用 `--cache-dir` 可把索引放到项目外。

```sh
# 定位入口（短名有歧义时会报出候选；推荐限定名或稳定 ID）
callmap --project /path/to/project entries
callmap --project /path/to/project entries --limit 100 --offset 0
callmap --project /path/to/project search get_runner
callmap --project /path/to/project search FluxClient --symbol-type class
callmap --project /path/to/project search '' --route /submit
callmap --project /path/to/project references transport --kind definition
callmap --project /path/to/project references transport --kind write
callmap --project /path/to/project impact --base HEAD

# -1 为全部可达节点；默认最多 500 个节点，可调整
callmap --project /path/to/project expand app.module.entry --depth -1 --max-nodes 2000
callmap --project /path/to/project expand app.module.entry --format mermaid
callmap --project /path/to/project expand app.module.entry --code --output graph.json

# 直接查询谁调用了这个函数；默认 depth=1，-1 追溯全部上游
callmap --project /path/to/project callers app.module.entry
callmap --project /path/to/project callers app.module.entry --depth -1 --max-nodes 2000
callmap --project /path/to/project callers app.module.entry --format mermaid
callmap --project /path/to/project callers app.module.entry --compact --budget 20000
callmap --project /path/to/project context app.module.entry --direction upstream --code

# 有预算的精简 JSON；原 CLI 完整导出及网页接口继续保留
callmap --project /path/to/project expand app.module.entry --compact --budget 20000 --prefer-internal
callmap --project /path/to/project expand app.module.entry --context-entry app.cli.main --compact

# 按需读取一个节点或生成有字符预算的上下文
callmap --project /path/to/project node app.module.entry
callmap --project /path/to/project context app.module.entry --budget 12000 --code

# 手动补充接口说明；也可以使用 node ID
callmap --project /path/to/project annotate sdk.client.send --doc '参数与返回值说明'

# 为待确认节点指定实际目标；多个 --target 表示多个可能实现
callmap --project /path/to/project annotate n_xxx --doc '当前配置的回调' --target app.worker.run

# 外部库源码/类型声明搜索路径、额外源码根与排除目录
callmap --project /path/to/project --library-root /path/to/site-packages index
callmap --project /path/to/project --source-root src --exclude 'tests/**' index
callmap --project /path/to/project --scan-mode filesystem index

# 可选 Jedi：对原生规则未解析的调用作类型推断，可能明显更慢
callmap --project /path/to/project --jedi index
```

`--budget` 是**字符预算**，不是 token 预算。输出会标记截断；源码不截在代码块中间。
节点 ID 可用于下一次 `node` / `expand` 继续读取。每次查询都会校验源码指纹。

`callers` 使用与 `expand` 相同的图结构和导出选项，返回 `direction="upstream"`。
节点 `depth` 表示距离查询函数的上游层数，`incoming_count` 表示当前扫描配置下的
入边数（多个调用点分别计数），包括本页/深度范围外的关系。边始终保留
`source=调用者`、`target=被调用者`，以及调用位置、状态、证据和合成边标记；
共享调用和递归不重复创建节点，候选及人工指定目标同样可反查。
图保留可见节点之间的全部边；直接调用者是 `target == entry` 的边的 `source`。
深度或节点上限触发时返回裁剪提示，可从 `omitted_frontier` 的节点继续查上游。
HTTP 对应接口为 `GET /api/callers?symbol=app.module.entry&depth=1&max_nodes=300`。

## 给 agent 使用的 MCP

安装 `mcp` extra 后，推荐全局启动：`callmap mcp`（stdio）。
一个注册可查询多个 Python 项目，首次查询传入 `project_path` 的绝对路径，
后续可使用返回的 `analysis_context` 固定查询范围。
服务不把自身工作目录当成业务项目，也不维护可被其他请求切换的“当前项目”。
无默认项目且缺少路径时会返回明确错误，不会索引工具安装目录。

### 一次配置，后续按调用选择项目

```sh
uv sync --python 3.12 --extra mcp

# 预览：自动检测 PATH 中的 Codex、Claude Code、OpenCode，不写配置
.venv/bin/callmap register

# 应用到已安装客户端的用户级配置
.venv/bin/callmap register --apply

# 单独选择客户端；替换已有同名注册需显式 --replace
.venv/bin/callmap register --client codex --client claude --apply --replace

# 全局 MCP 的 --cache-dir 是缓存根，各项目仍分别存放
.venv/bin/callmap --cache-dir /absolute/path/caches register --apply
```

注册使用当前 Python 环境的绝对路径和 `-m callmap mcp`，不绑定业务项目。
请保持此环境与 Callmap 安装可用；移动/删除虚拟环境后需重新注册。
Codex、Claude Code 使用各自原生命令；OpenCode 合并用户配置中的 `mcp` 字段。
已有配置修改前会备份，失败时恢复；OpenCode 支持 JSON/JSONC，写回时保留配置值，
注释和原排版在备份中保留。各客户端独立报告结果，未安装客户端会跳过。
第三方客户端可复制预览输出中的 `mcpServers` 片段，按客户端格式接入。
新安装的客户端仍需单独注册一次。

重新加载客户端或新建会话后，检查十一个工具是否可见。启动过程不建索引；
首次查询某个项目才读取源码，后续查询自动校验并复用索引。
大项目首次分析可能超过客户端工具调用超时，应调高超时，
或提前用 CLI 向该项目对应的全局缓存子目录建索引。

客户端配置示例，替换为自己的绝对路径：

```json
{
  "mcpServers": {
    "callmap": {
      "command": "/absolute/path/callmap/.venv/bin/python",
      "args": ["-m", "callmap", "mcp"]
    }
  }
}
```

暴露 `find_entry_points`、`search_functions`、`expand_calls`、`find_callers`、`read_symbol`、`get_context`、
`index_status`、`annotate_symbol`、`open_analysis_context`、`find_references` 和
`impact_of_changes`。所有工具都有可选的 `project_path` 和 `analysis_context` 参数。
全局模式下首次查询必须传入路径，后续可只传上下文标识；
旧方式 `callmap --project <目录> --cache-dir <索引目录> mcp`
仍兼容，省略参数时使用显式绑定的默认项目，传入其他项目时使用独立全局缓存。
原有 CLI 查询和网页仍使用 `--project`，默认当前目录。

不知道入口名时，先调用 `find_entry_points`，传入项目的绝对路径 `project_path`。
该工具支持 `limit` / `offset` 分页和与其他工具相同的扫描过滤参数，
根据 `reasons` 选择入口后，将它的 `id` 传给 `expand_calls`。

已知入口名时，例如 agent 调用 `search_functions` 的参数：

```json
{"project_path": "/absolute/path/project", "query": "RunnerFactory.get_runner"}
```

随后调用 `expand_calls`：

```json
{
  "project_path": "/absolute/path/project",
  "symbol": "app.services.engine.runners.factory.RunnerFactory.get_runner",
  "depth": 2,
  "max_nodes": 100
}
```

反查上游时调用 `find_callers`，默认只查询直接调用者，`depth=-1` 追溯所有可达上游：

```json
{
  "project_path": "/absolute/path/project",
  "symbol": "app.services.engine.runners.factory.RunnerFactory.get_runner",
  "depth": 1,
  "max_nodes": 100,
  "budget": 20000
}
```

`find_callers` 与 `expand_calls` 使用相同的精简响应、字符预算、分页和扫描过滤参数，
支持 `context_entry` 缩小参数推断范围。上游分页的 `continuation.tool` 为
`find_callers`，将 `continuation.arguments` 原样传给该工具读取下一页。

建议给 agent 的要求：

> 使用 Callmap 分析当前 Python 项目，首次调用显式传入当前项目绝对路径
> `project_path`，后续复用返回的 `analysis_context`。先检查 `index_status` 的错误，
> 再搜索入口、展开两三层调用，
> 按需用 `read_symbol` 获取源码。结论附路径和行号，区分静态定位、候选与待确认。
> 检查截断和候选 `evidence`，继续追踪必要分支；候选不代表必然运行路径。

服务初始化说明和工具描述也包含上述路径选择规则。

### 精简展开、字符预算与分页

MCP `expand_calls` 默认 `compact=true`、`include_docs=false`、
`prefer_internal=true`、`budget=20000`。节点只带 ID、符号、位置、签名、类型和
展开信息，边保留调用位置、状态和与目标对应的必要证据。源码和完整文档用
`read_symbol` 按需读取。外部节点仍可见，但不附整段库文档；
`internal_only=true` 可隐藏外部节点，并返回隐藏数量。

`budget` 限制**实际返回的 JSON 文本字符数**，不是估算 token 数。
MCP 只返回一个文本块，避免在文本和 structuredContent 中重复传输调用图。
每页都保留完整节点与边端点，不截断 JSON。最小预算为 2000；
单条完整记录仍无法容纳时明确要求增加预算。

| 字段 | 含义及下一步 |
| --- | --- |
| `graph_truncated` / 兼容字段 `truncated` | 深度或节点数限制；增加限制，或从 `graph_omitted_frontier` 的节点 ID 继续展开 |
| `response_truncated` | 当前响应预算不足；使用返回的 `continuation.arguments` 查询下一页 |
| `response_characters` / `response_budget` | 当前 JSON 文本大小与字符预算 |
| `response_total_*` / `response_omitted_*` | 本次展开总数与当前页未包含的节点/边数量；总数不包括图展开上限之外的节点 |
| `next_cursor` / `response_snapshot` | 分页位置和查询快照；源码或查询范围变化时拒绝旧快照，需从第一页重新查询 |
| `content_omitted` / `evidence_omitted_count` | 整段源码/文档或额外证据的省略提示 |

分页可直接把 `continuation.arguments` 交给 `expand_calls`。
页间节点可能重复，按节点和边的 ID 合并即可收齐本次展开。
`compact=false` 可读取完整字段和证据，仍受预算约束；
`include_docs=true` 显式请求文档，过长的整段内容会省略并标记。
签名、调用表达式和证据表达式过长时也会标明缩略，原文从调用方源码核对。

### 候选来源与入口范围

默认索引保留全项目可观察的调用者。`argument-flow` 证据包含参数、实际候选值、
目标 ID、调用者 ID、调用者位置及参数表达式；证据与对应目标匹配，
不会把其他候选的来源附到当前边。测试节点有 `is_test` 标记，证据有 `test_source`。

```json
{
  "project_path": "/absolute/path/project",
  "symbol": "app.shared._call_with_auto_auth",
  "context_entry": "app.cli.metrics_main",
  "exclude_tests": true,
  "depth": 3,
  "budget": 20000
}
```

`context_entry` 从指定内部入口的静态可达调用者传播参数，避免合并无关入口传入的
回调；已观察到具体接收对象时优先使用该对象，减少注解基类带来的额外子类候选。
它不是运行轨迹，也不是每个调用点独立的完整控制流分析：同入口内多次调用仍可合并，
未知入口参数和已知注册成员仍保留保守候选。索引未纳入的动态插件无法据此补全。
查询不在该入口范围内的函数会返回 `context_entry_reaches_symbol=false`，
不能把空边解释为函数没有调用。`read_symbol` 也可带 `context_entry` 读取该范围的节点。

`exclude_tests=true` 排除常见 tests/test 目录及 test_*/ *_test.py 文件；
`excludes` 可指定其他 glob，显式列表覆盖服务启动时的排除配置。
首次查询或 `open_analysis_context` 返回的 `analysis_context` 绑定项目路径、有效扫描
配置、源码/分析器/库环境指纹、人工标注版本、入口范围和 Git 提交。后续查询只传标识
即可复用这些设置；continuation 和 recovery.next_query 自动带上标识。
源码或配置变化时旧标识明确失效，需要打开新上下文。未使用标识时仍需重复扫描选项。
不同配置使用独立缓存，不维护可被其他请求切换的“当前扫描配置”。

MCP `search_functions` 返回 `{result, snapshot, analysis_context, ...}`，
`get_context` 返回 `{text, snapshot, analysis_context, ...}`；使用旧版返回格式的客户端
需适配。Python API 的 `GraphStore.search()` 仍返回列表，`GraphStore.context()` 仍
返回 Markdown 字符串。完整示例见 [Agent 查询工作流](AGENT_WORKFLOWS.md)。

### 索引覆盖审计

`index_status` 的 `coverage` 提供：

- `indexed_files`：实际纳入本次索引的 Python 文件。
- `skipped_files` / `skipped_directories`：跳过位置及原因，包括 Git 忽略规则、
  嵌套仓库未列入 Git 文件列表、配置排除、缓存目录、隐藏目录、不跟随的目录链接等。
- `effective_config`：实际枚举方式、项目根、源码根、排除规则、库路径和 Jedi 设置。
  源码根用于模块名解析，扫描范围由项目根、枚举方式和排除规则共同决定。
- `totals` / `next_offset`：文件清单总量和后续页位置；
  默认每类 100 条，通过 `file_offset` / `file_limit`（最多 2000）翻页。
- `audit_complete` / `audit_errors`：目录审计是否完成及无法读取的位置。
- `workspace_coverage=selected_python_files_only`：没有解析错误仅说明纳入范围内的
  分析没有报错，不代表已覆盖整个工作区。已排除目录的内部文件不会继续枚举。

默认 `scan_mode="git"` 保留 Git 文件列表规则，但另行审计未列入的 Python 文件。
如果子项目被 Git 文件列表遗漏，可单独用子项目绝对路径查询，或在各工具调用中传入
`scan_mode="filesystem"`，明确纳入嵌套仓库和 Git 忽略的 Python 文件。
该模式仍遵守常见缓存/虚拟环境目录排除及 `excludes`，并不等于扫描所有文件。

### 多项目缓存

默认缓存根为 `${XDG_CACHE_HOME:-~/.cache}/callmap/projects/`。
各项目使用其规范化绝对路径的 SHA-256 前 24 位作为子目录，
同名不同位置的项目不会混用缓存或人工说明，符号链接到同一目录会共用缓存。
使用额外扫描选项的索引放在配置哈希子目录，避免不同配置互相重建同一份快照。
`index_status` 返回实际项目 `root`、`cache_dir` 和 `cache_hit`，可用于核对。
全局模式不向业务项目写入 `.callmap/`；索引和人工说明保存在各自缓存子目录。
服务至多保留 32 个项目的 store 对象，磁盘快照不因切换项目而删除。

## 解析规则与边界

- AST 提取函数、类、模块及显式 `Call`，按词法作用域追踪导入、相对导入、
  再导出、简单赋值、类型注解、实例、继承及 `self` 字段。
- 外部工厂/方法的返回类型从 `.py/.pyi` 声明读取，支持前向引用、导入别名、
  继承方法和连续调用。对缺少注解的 `logging.getLogger`、C 实现的
  `contextvars.ContextVar` 等提供明确的标准库类型模型；推断边仍为候选。
- 沿已知调用点传递位置参数、关键字参数、可静态拆解的 `*`/`**` 参数及默认值，
  再推断返回值、回调及构造参数写入的实例字段。分析按候选类型集合迭代至稳定，
  不是按每次运行生成独立实例；未知输入仍可能产生额外运行时目标。
- lambda 建成独立节点，保留参数、词法闭包、函数体调用和返回值；
  传入包装器的函数/lambda 可传播到 `call(token)` 等调用位置。
  同一 lambda 的多次调用共享节点；匿名节点以所属作用域内的 lambda 顺序编号，
  在它之前新增/删除 lambda 会改变该编号。
- 对已识别的 stdlib Executor `map` / `submit` 保留合成的调度回调边，
  `reason=executor-callback`、`synthetic=true`、`status=candidate`。
  `submit` 传播传入参数，`map` 传播可静态观察的字面量迭代元素；未知迭代元素不猜类型。
  未知回调保留参数表达式和调用位置，任意同名 `pool.map` 不会按 Executor 猜测。
  合成回调边和构造边均不计入显式调用点数量。
- `asyncio.to_thread`、事件循环的 `run_in_executor`、`Context.run` 也使用
  明确的执行模型，生成 `execution-callback` 候选边，支持参数传播和反查调用者。
  证据包含包装器 API、函数实参、参数名、执行方式及源码行列。
  `create_task(worker())` / `ensure_future(worker())` 保留已有的 `worker` 调用边，
  增补协程提交证据；不重复生成调用边，也不把协程参数当作普通函数参数。
- 下游展开默认按进入包装器的调用点选择回调，支持直接参数、局部别名、默认值、
  关键字参数和多层直接转发；共享包装器的不同上下文仍共用一个函数节点。
  其他上下文的目标保存在节点的 `callback_candidates` 中，网页提供折叠列表及来源。
  精简 MCP/JSON 提供 `callback_candidate_count` 和 `callback_candidates_query`，
  按该查询展开包装器自身即可查看全部候选及证据。
  未解析的输入保留待确认边或 `callback_context_unknown`，不会把其他调用点的目标
  当作当前输入；经过任意变换或动态队列的参数仍可能无法精确绑定。
  全项目索引和上游查询保留所有已识别的候选调用关系。
- 每条边的 `resolution` 解释解析依据 `basis`、成立条件 `conditions`、
  未知部分 `unknowns` 和当前查询路径的静态候选数量 `candidate_count`；
  `indexed_candidate_count` 保留筛选前的候选数量。
  `runtime_guaranteed` 始终为 false；`resolved` 只表示静态定位到目标，
  不代表调用点一定执行，也不保证运行时对象、插件或重绑定与静态结果一致。
  依据区分导入、词法绑定、实例/注解、参数/返回值传播、注册表、执行模型和人工标注。
- 精简输出默认保留 `explanation`，说明调用位置、依据、来源摘要、成立条件和缺口。
  `callsite.guard_paths` 将可静态识别的工厂返回条件传递到方法目标，支持局部别名、
  参数改名转发、嵌套工厂和提前返回。配置字段关联定义、初始化与覆盖写入的候选来源，
  实际配置未知时保留条件，不猜测运行值。`resolution.gaps` 提供分类缺口和补查动作。
- `callsite` 保留位置/关键字实参、可安全确定的字面量或单一常量绑定、
  省略参数的默认值，以及 if/else、短路表达式、循环、异常处理和 match 的路径条件。
  `execution` 区分普通调用、等待结果、线程/执行器/后台协程提交、上下文执行和
  包含注册表写入的调用。网页详情和连线提示、JSON/MCP、Markdown 导出均可查看。
  业务角色依据参数和条件判断，不自动给调用贴“正式阶段”等业务标签。
- 节点 `completeness.calls` / `completeness.callers` 分别报告索引内已识别关系数、
  当前图列出的关系数和是否已列全；`state=none_identified` 表示没有识别到关系。
  上游入口的直接调用者可能已列全，同时更深的上游仍因深度或节点上限被裁剪。
  顶层 `completeness` 汇总扫描范围、解析错误、相关/索引内未解析调用点和未展开节点数。
  `recovery` 指明尚未展开或未解析的来源节点，附带保持项目、入口及扫描过滤的
  `next_query`。网页提供继续查询和核对源码操作。
  MCP/有预算 JSON 的 `response_listed` / `response_complete` 单独描述当前页，
  `recovery` 也参与分页；使用 continuation 获取后续记录。
  精简 JSON 使用紧凑序列化；小预算完整视图必要时也压缩 JSON 空白，
  以 `serialization=compact` 标记。预算仍不足时，明确列出省略的详情字段。
- 注册表支持字典字面量、下标赋值、字面量 `update`、注册函数、直接注册装饰器、
  返回注册装饰器的工厂以及注册表跨模块别名。每次注册保留键与实现的关联，
  支持从简单常量类属性/property getter 提取键。工厂返回的类也可以继续追踪。
  常量键筛选已知成员；参数键保留全部已知注册候选，不因几个固定调用者而缩窄。
  注册只表示“源码可能执行这项注册”，不保证模块已加载或分支已执行。
- 候选边的 `evidence` 保存参数来源、外部声明位置或注册位置/键。网页详情、
  JSON、MCP 及上下文导出可以查看依据。内部函数仍只保存一个节点。
- 类型辅助的方法分派会包括已索引子类中的实现，并标记 `candidate`。
  这是候选集合，不保证运行时一定调用，也不保证覆盖所有动态实现。
- `resolved` 表示静态名称定位；`candidate` 表示推断；`unresolved` 表示待确认；
  `manual` 表示人工指定。边保留调用表达式、行列及解析依据。
- 类节点有到 `__init__` 的构造关系。它是合成边，`reason=constructor`，
  不计入显式调用点统计。多个调用点/多个候选可产生多条边。
- 未知表达式按“所属函数 + 表达式”合并，调用位置仍逐条保留。
  对该未知节点指定目标会影响同函数中所有相同表达式的调用。
- 函数 ID 是 `相对文件路径::限定名称` 的 SHA-256 前 24 位，前缀为 `n_`。
  改函数体或插入行不改变 ID；移动文件或重命名改变 ID。重复定义加 `@2` 等序号。
- 不执行被分析项目；默认不导入其依赖。外部说明从已有 Python 环境或
  `--library-root` 的 `.py/.pyi` 静态读取，内置接口从 Python 获取。
  没安装/不可读的库显示“暂无说明”，可以人工补充。没有自动联网检索文档。
  接收对象的外部类型已知而库未安装时，保留外部接口候选，设置
  `declaration_available=false`；已有类声明却找不到方法时，保留为待确认。
- 源码根默认是 `src/`（存在时）和项目根。Git 仓库遵守已跟踪文件及未忽略文件列表；
  非 Git 目录排除常见缓存/虚拟环境目录，额外范围可用 `--exclude` 配置。
- 尚不建完整控制流图，不做完整变量生命周期分析。多次赋值保留保守候选；
  运行时新增的注册项、外部插件、反射、动态导入、猴子补丁及无法追踪的回调仍可能待确认。
  注册装饰器作为推断依据，不额外伪造函数体调用边；其他定义时装饰器/默认参数调用、
  未建立模型的隐式协议调用仍不展开。
  这些情况不是“无调用”，静态图不等于完整运行轨迹。
- 每阶段最多迭代 16 轮，每个参数/返回摘要最多 128 条类型事实。
  `analysis` 报告收敛轮次与饱和数量，图的 `inference_incomplete` 标识达到推断上限。
  同行同列的嵌套表达式（如 `factory()()`）分别计作调用点。

## 缓存与人工补充

`index.sqlite3` 保存自动分析快照；`annotations.json` 独立保存人工补充，可单独纳入 Git。

未改变的项目直接复用快照；源码、配置、分析器代码、环境或已读取的库源码变化时，
采用**保守全量重建**。尚未实现按反向依赖局部重建。
新增到自定义 library-root 的库如果没有安装元数据，请使用 `index --force`。

常驻 MCP/Web 进程复用已解码快照、邻接索引和查询视图；文件哈希按文件状态复用，
每次仍重新枚举文件并检测新增、删除和过滤变化。SQLite 文件状态与 `data_version`
共同检测其他连接的提交，人工标注变化也会刷新查询视图。公开查询返回独立副本，
修改返回数据不会污染后续结果。独立 CLI 进程无法复用前一个进程的内存缓存。
缓存机制与性能对照见 [查询缓存与性能验证](PERFORMANCE.md)。

说明绑定源码指纹。人工调用目标在调用方/目标/环境变化时停止应用并提示复核；
旧说明保留并显示过期标记。重新分析不会覆盖人工文件。

## Git 修改影响

`impact_of_changes` / CLI `impact` 关联变更符号与基准/当前版本的静态上游入口、
测试调用和动态调用缺口。支持删除、文件改名及 `--patch-file` 输入；函数改名分别作为
旧符号删除和新符号新增追溯。基准通过 Git archive 读取到缓存，不检出工作树，
不执行项目代码。结果报告排除的测试范围、未映射文件及追溯/响应裁剪。
测试关联仅说明静态关系，`test_coverage_verified=false`、`runtime_complete=false`；
入口类别区分 startup、framework、registry、public_root，不能证明实际部署路径。
边界、分页与使用示例见 [Agent 查询工作流](AGENT_WORKFLOWS.md)。

## 验证

```sh
.venv/bin/python -m compileall -q callmap
.venv/bin/ruff check callmap tests scripts
.venv/bin/pytest -q
.venv/bin/python scripts/verify_flux.py ../flux_test_be
```

测试覆盖递归/菱形共享、继承、同名消歧、变量遮蔽、嵌套作用域、类型注解、
未知回调、稳定 ID、缓存失效、人工补充过期、字符预算、语法错误、外部说明、
外部工厂/参数/返回值传播、注册键关联及候选边、HTTP API、MCP stdio，以及浏览器交互。
试用反馈回归还覆盖 lambda/包装器、Executor 回调与参数传播、未知回调来源、
入口隔离、测试排除、目标与证据对应、单文本块预算分页、快照失效及嵌套仓库覆盖审计。
浏览器用例在 macOS 本机 Chrome 上运行，
没有该浏览器时明确跳过。

`scripts/verify_flux.py` 只静态分析 `flux_test_be`，不启动后端、不连接业务服务。
它检查真实跨文件调用、真实递归、两个工厂的候选与注册源码完全匹配、
外部返回类型解析、全图引用完整性、推断收敛和缓存复用，
报告及示例图保存在 `artifacts/flux-verification/`。
本次增强的验证结果另存于 `artifacts/flux-verification-v2/`，保留首版报告供对比。
