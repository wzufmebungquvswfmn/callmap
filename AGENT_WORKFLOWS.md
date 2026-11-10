# Agent 查询工作流

## 六项改进

1. 每条调用边提供 `explanation`：位置、解析依据、来源摘要、成立条件和缺口类别。
   精简输出也保留解释；预算不足时仅保留较短的条件/未知项，并明确标记省略数量。
2. `callsite.guard_paths` 保留工厂返回路径的条件与来源。支持直接工厂、局部别名、
   参数改名转发、嵌套工厂和提前返回。各路径之间为“或”，路径内条件为“且”。
   配置字段关联定义/初始化及覆盖写入的候选来源；不读取或猜测应用运行时配置。
3. MCP 返回 `analysis_context`，绑定有效扫描配置、源码/分析器/库环境指纹、
   人工标注版本、入口范围与 Git 提交。后续工具可只传上下文标识，变化时明确失效。
4. 搜索支持类型、路径、所属类、装饰器、路由和入口类别；`find_references` 查询
   字段/变量定义、初始化、读取和写入。入口类别区分 startup、framework、registry、
   public_root；`deployment_verified` 始终为 false。
5. `resolution.gaps` / `recovery` 区分未知接收类型、未知回调、注册表选择、配置值、
   外部声明缺失和来源传播边界。补查建议可跳到来源符号或引用索引。
6. `impact_of_changes` / CLI `impact` 将 Git 变更符号与基准/当前版本的静态入口及
   测试调用关联，支持删除和文件改名，附带动态调用缺口、排除的测试范围和裁剪信息。

## 固定 MCP 查询上下文

先调用：

```json
{
  "tool": "open_analysis_context",
  "arguments": {
    "project_path": "/absolute/path/to/project",
    "exclude_tests": true,
    "scan_mode": "git"
  }
}
```

然后使用返回的标识：

```json
{
  "tool": "expand_calls",
  "arguments": {
    "symbol": "app.service.submit",
    "analysis_context": "ctx_..."
  }
}
```

搜索、入口、引用、源码、上下游、Markdown 和影响查询均接受 `analysis_context`。
分页 continuation 和 recovery.next_query 自动带上标识。不要在原上下文内更改扫描
配置；需要新范围或源码/标注变化后，应打开新上下文。

`annotate_symbol` 在写入前验证上下文，写入后返回新的标识和 previous_analysis_context。
标识由当前 MCP 服务维护，重启服务或标识被淘汰后需要重新打开。
Python/库环境指纹不代表实际应用配置值已知。

MCP 的 `search_functions` 现在返回 `{result, snapshot, analysis_context, ...}`；
`get_context` 返回 `{text, snapshot, analysis_context, ...}`。原 Python API 的
`GraphStore.search()` 仍返回列表，`GraphStore.context()` 仍返回 Markdown 字符串。

## 搜索与引用

```sh
callmap --project /path/to/project search FluxClient --symbol-type class
callmap --project /path/to/project search '' --owner FluxClient
callmap --project /path/to/project search '' --route /submit
callmap --project /path/to/project references transport --kind definition
callmap --project /path/to/project references transport --kind write
```

`entry_kind` 可为 startup、framework、registry、public_root。字段规范名根据可观察
类型推断；类型不明确时，工厂条件的配置来源仅按字段名列出候选，并标记 binding。
函数调用、字段引用和框架注册都属于静态证据，不能证明线上部署路径或行为覆盖。

## Git 修改影响

```sh
callmap --project /path/to/project impact --base HEAD
callmap --project /path/to/project impact --base HEAD~1 --patch-file change.patch
```

默认比较基准提交与当前工作树，包括 staged/unstaged 的已跟踪 Python 文件变化。
传入 patch 时要求 Git unified diff；hunk 必须匹配指定基准和当前已索引源码。
基准由本地 Git archive 安全读取到缓存，不检出或修改工作树，也不执行项目代码。
基准分析与当前版本使用相同显式排除条件。未跟踪文件、Git submodule 内容和未索引
文件可能不在基准分析范围内，结果包含 baseline_scope / omitted_files。

文件改名匹配同名局部符号；函数改名作为旧符号删除与新符号新增分别追溯。
每侧最多追溯 max_nodes 个节点、12 层调用路径，并列出最多 8 个入口/测试关联。
达到上限时 truncated=true；test_coverage_verified=false，runtime_complete=false。
模块级且无法映射到可调用符号的变化会列入 omitted_files，需结合引用索引核对。
大规模变更通过 limit、offset、next_offset 和 MCP 字符预算分页。

网页在调用详情中展示工厂条件和缺口，提供引用来源跳转、搜索筛选及“修改影响”视图。
