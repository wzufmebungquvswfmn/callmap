# 固定筛选项变更与验证（2026-11-04）

页面状态、工作模式、是否完成和三级分类改为固定多选，不再调用 `/v1/filter-options`。
归属人、Task ID、Worker 会话 ID 使用多值文本输入；支持逗号、空格、分号、换行和去重。
来源平台保留占位，不发送筛选条件。旧动态选项 API 保持兼容。

分类列表按用户提供内容录入：一级 9 项、二级 24 项、三级 6 项。
各级独立使用 `in`，同字段多值 OR，不同包含字段 AND；每条排除条件独立命中即剔除。
不从给定列表推断父子关系。

## 字段映射依据

核对 Flux Insight 生产页面 release `1.0.0.518` 的前端资源：

- [状态映射资源](https://cdn-tos-cn.bytedance.net/obj/archi/deploy/bits/flux_insight/306944/static/js/async/8387.b24b7674.js)：`queueing`、`running`、`input-required`、`committed`、`finished`、`interrupted`、`failed`、`rolled-back`。
- [工作模式映射资源](https://cdn-tos-cn.bytedance.net/obj/archi/deploy/bits/flux_insight/306944/static/js/async/7746.e6bc3743.js)：默认模式 `general`、代码编写 `coding`、数据分析 `data_analysis`、需求创作 `requirement`。

生产只读请求确认：`work_mode` 作为 OpenAPI 条件返回 code=1002；`level_1_category` 与时间范围组合可正常返回任务。
因此服务只将其他条件传给上游，在每页任务响应上匹配 `work_mode`。
排除工作模式按候选 ID 查询并匹配，同样不向上游发送不支持的字段。
所需工作模式字段缺失时明确失败，避免将无法判断的记录静默当成不匹配。

## 验证

- Python 测试 63 项通过；ruff 检查、格式检查和修改文件的 py_compile 通过。
- DOM 行为测试 4 项通过，覆盖固定列表、多选映射、人员输入、去重、清空、搜索、排除、占位、错误重试和结果过期。
- Chromium 桌面/手机测试 2 项通过，覆盖实际多选、提交内容、移除标签和手机宽度无横向溢出；已查看两份截图。
- Chromium 测试中的结果来自固定响应，不作为生产任务数量证明。
- 生产工作模式检查：2026-11-03 23:50:00～23:59:59，扫描 82 条记录，其中真实任务 68 条。包含 `general` 或 `coding` 得到 54 个任务；排除这两个模式得到 14 个任务，均与独立读取任务字段后的集合对比一致。

本次临时测试依赖安装于 `/tmp/flux-filter-test-deps`；未写入项目运行依赖。
预览地址 `http://127.0.0.1:18081/`，本次以工作区服务账号文件启动。
