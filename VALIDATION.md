# 首版验证记录

日期：2026-11-03。目标项目：`../flux_test_be`，仅静态读取，工作区未修改。

## 结果

| 项目 | 实测 |
| --- | ---: |
| Python 文件 | 545 |
| 唯一节点 | 12,147 |
| 项目内节点（函数、类、模块） | 6,462 |
| 外部接口节点 | 434 |
| 待确认表达式节点 | 5,251 |
| 显式调用点 | 32,782 |
| 调用关系（含候选与构造关系） | 35,835 |
| 静态已定位关系 | 21,066 |
| 候选关系 | 7,254 |
| 待确认关系 | 7,515 |
| 源文件解析错误 | 0 |
| 冷启动建索引（含保存） | 4.149 秒 |
| 未修改项目读取（含指纹检查） | 0.475 秒 |

以上是本机、默认 AST 模式的一次测量；数量是索引规模及解析状态，不代表准确率。
约 23% 的显式调用点仍待确认，主要包含动态注册、缺少类型信息的对象调用等。
可选 Jedi 已通过独立测试，未用于上述全项目扫描。

## 验证范围

- 20 项自动测试全部通过：`20 passed in 34.31s`。
- Python 编译检查和 Ruff 检查通过。
- MCP stdio 完成初始化、工具列表、调用图查询及源码读取。
- Chrome 完成搜索、递归图展示、分支折叠、缩放、说明保存与刷新、JSON 下载。
- 实际 `flux_test_be` 页面浏览器无 JavaScript 错误；390px 宽度无页面横向溢出。
- 全图节点 ID、边 ID 无重复，所有边的两端均存在。
- `RunnerFactory.get_runner` 的跨文件直接调用与源码核对一致；
  注册表返回的 `cls(**kwargs)` 没有被猜成某个运行器。
- `_remove_internal_error_fields` 的两个递归调用点均回到同一个函数节点。
- `_ctx_prefix` 能沿返回类型展开到 `RequestContext.to_log_prefix` 和 `to_log_parts`。
- 未启动 `flux_test_be` 后端，未执行它的业务测试、访问数据库或业务服务。

## 可复查产物

- `artifacts/flux-verification/report.json`：机器可读统计与检查项。
- `artifacts/flux-verification/runner-factory.json`：工厂入口调用图。
- `artifacts/flux-verification/recursion.json`：真实递归示例。
- `artifacts/flux-verification/logging-context.json`：跨模块类型辅助调用链。
- `artifacts/flux-verification/runner-context.md`：供 agent 使用的上下文。
- `artifacts/flux-verification/preview.png`：实际页面截图。

## 当前限制

静态图不能覆盖全部 Python 动态行为。未知调用可人工指定目标；候选调用保留依据。
外部依赖缺少本地源码或类型声明时可能没有接口说明，支持人工添加。
缓存复用已经实现；源码变化后目前全量重建，尚未实现局部增量更新。
MCP 服务及配置示例已提供，未自动修改用户的客户端接入设置。
