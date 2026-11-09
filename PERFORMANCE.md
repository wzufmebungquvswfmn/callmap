# 查询缓存与性能验证

本轮优化针对同一进程中的重复查询，适用于常驻 MCP 服务和 Web 服务。
源码变化时仍执行完整分析，首次 `context_entry` 分析也仍需建立入口范围的图。

## 缓存方式

- 内部保留最近的已解码 SQLite 快照，避免每次查询读取、解码整张调用图。
- 文件内容哈希按设备、inode、权限、大小、mtime_ns、ctime_ns 复用。
  因此同大小写入、恢复 mtime、原子替换和权限变化都能使哈希失效。
  每次仍重新枚举文件，检查新增、删除、Git 忽略规则和扫描过滤变化。
- 分析器文件和已读取的库声明也采用同样的哈希校验。依赖包版本每次检查。
- SQLite 主文件和 WAL 的文件状态，以及固定连接上的 `PRAGMA data_version`，
  共同检测其他连接的提交。数据库文件被替换时重建版本监测连接。
- 人工标注按文件状态校验，改变后重新解析内容并更新查询视图。
  新快照会清理旧的查询视图、邻接索引和入口分析缓存。
- 查询视图复用节点查找表、上下游邻接表、静态范围和未解析调用点总数。
  仅对访问到的邻接表执行过滤和排序。

内部快照和查询视图只供读取。`read_node`、`expand`、`callers` 返回需要部分的
独立副本；`ensure`、`graph` 返回完整独立副本。修改返回结果不会污染后续查询。
MCP、HTTP、CLI 的单节点读取使用 `read_node`，避免复制全图。

常驻进程保留解码后的数据，因此会增加内存占用。每个 store 只保留当前基础快照和
最近一个查询视图；入口分析缓存仍最多保存 8 个入口。

## 复测

```sh
.venv/bin/python scripts/measure_performance.py /absolute/path/to/project \
  --symbol app.services.engine.runners.factory.RunnerFactory.get_runner \
  --cache /tmp/callmap-performance-index \
  --output artifacts/performance-after-optimization.json \
  --repeats 5
```

脚本不执行项目代码，使用 AST 模式、关闭 Jedi，并在同一进程内测量：

1. 强制索引一次，记录冷索引耗时及图规模。
2. 重复 index_status、展开 2 层、查直接调用者和读取源码，取中位数。
3. 分别记录首次入口分析和缓存后的入口查询。
4. 记录预算 JSON 的构建时间。

对照不同版本时，将保存的旧版本包目录加入 `PYTHONPATH`，并为两次运行分别使用
独立 cache；设置相同的 `PYTHONHASHSEED`、项目、环境和查询参数。
报告中的 `semantic_digest` 校验节点、调用边、解析说明、调用点事实、完整度和恢复
查询结果，排除索引快照版本与构建耗时等运行信息。

本机对照记录位于 `artifacts/performance-before-optimization.json` 和
`artifacts/performance-after-optimization.json`。结果不包含 MCP 传输、CLI 启动和
模型阅读耗时；独立 CLI 进程不能复用前一个进程的内存快照。

Flux 示例包含 545 个 Python 文件、11,468 个节点和 36,603 条边；5 次采样中位数：

| 操作 | 优化前 | 优化后 | 提速 |
| --- | ---: | ---: | ---: |
| index_status | 671 ms | 125 ms | 5.4 倍 |
| 展开 2 层 | 737 ms | 112 ms | 6.6 倍 |
| 直接调用者 | 712 ms | 106 ms | 6.7 倍 |
| 单节点源码 | 582 ms | 101 ms | 5.7 倍 |
| 缓存后的入口查询 | 679 ms | 106 ms | 6.4 倍 |

展开、上游和入口查询的语义摘要均与旧版本一致。首次索引两次测量约 38 秒，
本轮没有改变全量分析算法。汇总记录位于 `artifacts/performance-comparison.json`。

## 缓存失效验证

`tests/test_cache.py` 覆盖重复请求避免源码/SQLite 重读、同大小且恢复 mtime 的修改、
原子替换、文件新增删除、返回结果修改、入口范围隔离、跨 store 标注更新、采用其他
store 的新索引，以及文件状态信号固定时仍能检测 WAL 提交。还验证强制刷新和依赖
包版本变化会使缓存失效。
