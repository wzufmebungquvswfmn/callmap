# 工程打标与测试造题准入

第一版以真实测试活动的证据为中心，对已导出的任务做本地分析。只读取
`data-manifest.json` 声明的引用，通过 `TaskDataReader` 校验并读取；不调用 AI、
不请求网络、不运行 trace 中的命令、不修改输入包，也不操作仓库权限。

## 使用

### 在服务中使用

通过 API 对已完成的导出批次发起后台打标。每次运行返回独立 `tagging_id`，可查看
策略、进度、按候选等级/Task ID 分页筛选，并下载候选清单或压缩结果 ZIP。
本次只更新后端输出契约，前端由接入方按 v2 字段适配。

历史运行不会被覆盖。服务启动后，持久化的未结束任务标为 interrupted，需重新
发起；已结束结果仍可读取。停止操作在当前任务读取完成后停止后续任务，剩余任务
记录为待复核。

| API | 用途 |
| --- | --- |
| `POST /v1/exports/{job_id}/tagging` | 为已完成导出创建后台打标，返回 202 |
| `GET /v1/tagging/{tagging_id}` | 状态、进度、策略、保留/排除/待复核数量 |
| `GET /v1/tagging/{tagging_id}/results` | 分页结果，支持 candidate_level、task_id、offset、limit |
| `GET /v1/tagging/{tagging_id}/tasks/{task_id}` | 单任务压缩摘要与证据位置 |
| `GET /v1/tagging/tag-schema` | 可编辑 tag 白名单与各自 JSON Schema |
| `GET /v1/tagging/{tagging_id}/task/{task_id}` | 可编辑标签集合与 ETag |
| `POST /v1/tagging/{tagging_id}/task/{task_id}` | 携带 If-Match 整体覆盖可编辑标签 |
| `GET /v1/tagging/{tagging_id}/selected` | 下载候选 JSON 清单 |
| `GET /v1/tagging/{tagging_id}/download` | 默认下载全部；重复传 `task_id` 下载所选任务的标签、索引和清单 ZIP |
| `POST /v1/tagging/{tagging_id}/cancel` | 停止后续打标 |

模型和人工可对已就绪批次进行动态打标，目前开放 `repository`、`task_classification`、
`review` 三类。每次先 GET、合并标签，再以原 ETag 作为 `If-Match` 提交完整对象。
遗漏 tag 会移除，未知 tag 或格式错误返回 422；版本冲突返回 412。详见
[动态打标接口](dynamic-tagging.md)。修订值保存在 `dynamic_tags`，自动准入与原始证据继续保留。

创建请求示例（其余 policy 字段沿用下文定义）：

```json
{
  "policy": {
    "priority_only": true,
    "allowed_test_types": ["unit", "api", "gui"]
  },
  "allow_legacy": false
}
```

所有接口沿用服务的 Bearer API key；服务只接受自身导出批次 ID，不接受客户端传入
服务器目录。未完成导出或结果尚未就绪返回 409，并发达到上限返回 429。默认最多
同时执行 2 个打标任务，可用 `FLUX_MAX_TAGGING_JOBS` 调整。数据读取放在后台线程。
单任务不可读进入 hold，继续处理其他任务；获取错误、执行错误分别保留。

结果存储在配置导出根目录的 `_tagging/<tagging_id>/` 下，包含进度 job.json、
单任务标签、索引、候选清单与 results.zip。原导出及 ZIP 不修改。候选清单中的
export_job_id/task_path 定位原数据，evidence_refs 提供代表性证据导航。

### 本地命令

在项目目录中分析一个任务或一批解压后的导出：

```bash
.venv/bin/python -m flux_task_service.tagging /path/to/export \
  --output /path/to/tagging-results
```

一个数字 ID 的任务目录也可作为输入。批量输入通过 `data-manifest.json` 定位任务；
相同 task ID 的多个快照须分别分析，不悄悄选择其中一份。输出目录须与输入目录分离。

旧包必须显式开启适配，并保留原有 `job.json` 层级：

```bash
.venv/bin/python -m flux_task_service.tagging /path/to/old-job \
  --allow-legacy --output /path/to/tagging-results
```

只放行带问题线索的优先候选：

```bash
.venv/bin/python -m flux_task_service.tagging /path/to/export \
  --priority-only --output /path/to/priority-results
```

输出包括：

- `tasks/<task_id>.json`：供人工筛选的压缩摘要，包括结论、标签、关键结果和证据位置。
- `index.json`：本次所有任务的决定、原因、输入位置和策略快照。
- `selected-tasks.json`：仅当前策略决定 `keep` 的记录，可交给后续流程。

退出码 0 表示完成批量分析，不表示存在候选，也不表示所有业务数据完整。
任务清单不可读、未 finalized 等错误会记录为 `hold`，继续分析其他任务，退出码为 1。
局部读取/解析错误保存在单任务输出中，不自动停止整批。

## 核心输出

结果版本为 `flux.test-admission.v2`，分析器版本为 1.5。默认只保存压缩摘要，
不另存完整分析快照；原始正文、完整命令、报告、平台详情和数据清单仍在输入导出中。
策略在批次 `job.json` / `index.json` 中保存一次。

| 字段 | 含义 |
| --- | --- |
| `title` / `summary` | 任务标题及中文筛选结论，一眼看清候选等级、决定和命中数量 |
| `admission` | 候选等级、决定、原因码及中文原因、命中类别数量和列表 |
| `core.evidence_matches` | 五类证据的命中状态，每类最多一条短摘要和一个证据引用 |
| `core.test_types` / `test_evidence_level` | 识别到的测试类型和证据状态 |
| `core.problem_signal` | 当前任务是否观察到失败用例或报告问题；没有已归属记录时为 null |
| `core.runs` / `runs_total` | 最多 3 条关键测试/审查摘要及总记录数，优先展示问题报告和失败调用 |
| `core.observed_test_run_count` / `test_result_record_count` | 分析全部记录所得的执行结果/报告记录数，不是展示条数或用例总数 |
| `core.rule_search_complete` / `notes` | 覆盖是否完整，以及命令未解析、归属不明、摘要省略等提示 |
| `recorded_only` | 仅记录的辅助标签摘要，完整结构也写入索引记录的 `tags` |
| `dynamic_tags` / `dynamic_tag_revision` / `dynamic_tag_updated_at` | 服务端可编辑标签、版本及最近覆盖时间；模型/人工标签可与自动结果对照 |
| `references` | 最多 8 个去重后的证据位置：任务相对路径和 JSON Pointer |
| `analysis_errors` / `export_errors` | 按数据集和错误码合并的错误计数 |

### 辅助标签与索引

单任务结果的 `recorded_only` 与 `index.json` 每条 `records[].tags` 完全一致；
`selected-tasks.json` 和分页结果 API 同样携带 `tags`。保留、排除和待复核任务均记录
辅助标签，无须逐个读取任务文件才能做列表筛选。所有这些字段只记录，不参与当前
候选等级判断；默认等级仍按五类测试证据计算。

| `tags` 下的字段 | 保存的信息 |
| --- | --- |
| `platform.task_classification` | 平台原任务分类；保留分类值，省略长原因正文 |
| `platform.logic_categories` | 分段一级/二级/三级分类和 story_stage，去重后保留全部不同组合 |
| `platform` 其它字段 | 任务状态、完成状态、工作/worker 模式、来源、模型/agent 配置、开发及缺陷计数 |
| `tool_statistics` | 工具调用、失败、取消、非零退出、未知退出、复杂命令未解析等计数 |
| `activities` / `skills` | 已观察到的编辑、提问、测试、Git 提交/推送、lint/typecheck/build/format 活动分类计数；技能调用、文档读取及遥测中出现的技能名称 |
| `observed_edit_file_count` | 从编辑调用中识别到的不同文件数，省略完整编辑路径清单 |
| `repository` | 已知仓库路径、提交数量、是否 SDD、代码仓数量、修改文件/目录及增删行等平台指标 |
| `test_results` | 全部已归属记录的测试类型和执行状态、各归属状态记录数、失败/阻塞结果记录数、单份报告问题数最大值 |
| `metrics` | 指标来源、Token/耗时完整性及全部已知数值指标，省略诊断和证据明细 |
| `artifact_file_count` / `versions` | 产物数、版本数、comparison 数、最新版本及完整快照是否可用 |
| `data_quality` | 各数据集的 status/complete、导出状态、历史适配标志、规则覆盖状态及分析/导出错误码 |

平台分类按原值记录，即使它与执行证据不一致也不覆盖。辅助标签不包含 AI 场景判断。
数据集的 `complete` 沿用输入契约声明，null 表示未知；某项无来源时不伪造“已确认不存在”。
活动和结果计数表示已观察到的记录，0 不证明原任务没有这类活动；应结合覆盖状态使用。
测试类型、执行状态和问题统计从全部分析记录生成，不因 `core.runs` 的 3 条展示上限丢失。

例如列表可直接读取 `records[].tags.platform.logic_categories`、
`records[].tags.tool_statistics.tool_failed_count` 和
`records[].tags.data_quality.rule_search_complete`。

`runs[].counts` 未提供的字段保持 null，真实 0 保留为 0。不同次结果不累加成全局
用例数。筛选仍基于全部识别记录，摘要截取不会影响准入规则。`runs_total` 包括未归属
记录；这类记录不参与准入，摘要保留 `association` 帮助识别。

`admission.evidence_refs` 替代 v1 的 `selected_run_keys`，只提供代表性证据导航，
引用当前任务结果的 `references[].id`，不代表完整的缺陷提取范围。
排除/待复核任务的列表为空。后续阶段按候选清单的输入位置读取原导出，再确认报告
归属、版本和缺陷内容；不要仅凭摘要执行缺陷提取。

这是输出契约升级，接入方需要适配 v2；已有历史批次保持原格式，新执行的批次使用
压缩结果。不会自动重写旧文件或 ZIP。

## 默认准入规则

| 条件 | 决定 |
| --- | --- |
| 命中五类证据中的至少三类 | 优先候选 / keep |
| 命中测试技能调用、缺陷审查执行、测试/审查报告中任一类，且总数不足三类 | 普通候选 / keep |
| 仅命中测试执行调用和/或平台明确报告测试场景 | 待复核 / hold |
| 报告属于其他任务、归属未知或同 run 结果冲突，且无其它测试场景证据 | 待复核 / hold；该报告不用于准入 |
| 在规则覆盖满足条件的记录中，只观察到技能准备或没有测试活动信号 | exclude |
| 没有命中测试证据，但覆盖不足、存在核心数据解析问题 | 待复核 / hold |

分析器 1.2 起，候选等级依据五类证据的命中组合和不同类别数量。五类分别为：
test_execution_call（测试执行调用）、test_skill_invocation（测试技能调用）、
defect_review_execution（缺陷审查执行）、test_report（测试/审查报告）、
platform_test_scenario（平台明确报告测试场景）。前者和最后一类是辅助证据，另外
三类是强证据。同类调用、复测、报告或平台分类重复出现不增加类别数。审查脚本仅
计入审查执行，不同时计入普通测试执行调用；支持的终端测试结果计入报告类。

静态缺陷审查同样属于本系统的测试场景，记录为 other 类型。只有读取技能文档或只有
文件名不触发准入；阻塞、未完成验证和缺少通过数量描述执行状态，不另设成功门槛。
分类来源与任务正文/执行事实不合并为无来源的标签，便于后续核查。
总体任务取消、模型、Token、工具错误数、平台缺陷数量不单独触发准入或排除。
失败数量和报告问题数量不决定默认等级。`priority_only=true` 或 `--priority-only`
只保留命中至少三类证据的优先候选；待复核仍为 hold。原有 `require_problem_signal`
保留为显式的附加过滤开关，默认 false；开启时，没有问题线索的候选会被排除，但
其候选等级仍按类别数计算。“未观察到问题”不是“确认没有缺陷”。

`coverage.rule_search_complete` 只表示支持的工程规则已搜索声明范围：不能证明现实中
没有测试，也不把 Fornax 窗口完整扩展成全部历史完整。复杂 shell、正文省略、缺少
命令参数、未支持来源等情况保留未知。正向证据可来自 partial 数据；负向排除需要
足够覆盖。默认不要求 raw、Token 或耗时完整，也不以整包 validate 为前置条件。

## 已支持的规则

- 测试命令：pytest、Python `-m pytest/unittest`、Go/Cargo/.NET `test`、
  Node `--test`、Jest/Vitest/Mocha、Playwright/Cypress、npm/pnpm/yarn/emo 的
  `test`/`test:unit` 入口。
- 常见 literal shell `-c`、env 赋值、timeout/gtimeout、uv run、npx/pnpm exec 包装。
- bits-code-guard / bits-code-guard-doubao 的 start.py、finish.py、generate_report.py 执行入口。
- 不把 echo/cat/搜索内容、技能正文、`--help`、`--collect-only`、列举测试当作测试执行。
  含 here-doc、命令替换、反引号或无法解析的命令保留待复核，不猜测其脚本行为。
- lint、typecheck、build、format、Git commit/push 分别记录；不当作测试。
- 测试/问题结果：已识别测试调用的 pytest 终端摘要、Jest/Vitest `Tests` 摘要；
  `unit_test/report@2` JSON；JUnit `<testcase>` XML；GUI `items[].case_result`
  为明确用例数组或 `{cases: [...]}` 的格式。
- `0.0.0-bits-code-guard` JSONL 的 capability_summary / finding。
  报告路径通过下载索引的原路径关联到本任务已成功执行的 generate_report --report-dir，
  才用于问题线索优先级。只有未归属的历史报告时继续复核，不把文件存在当作当前执行。
- GUI 用例状态支持 PASS/PASSED、FAIL/FAILED、BLOCKED、SKIP/SKIPPED、NOT_RUN。
  GUI 的其他上游形态输出 `UNSUPPORTED_TEST_RESULT_SHAPE`，不猜测。
- 只在单一命令段、结果未标记截断时解析终端摘要；复合命令的整体状态不赋给每个子命令。
- 测试报告中的问题仅称为“报告问题数”，失败用例不直接判定为业务缺陷。

工程入口识别表示观察到调用请求，不能证明 shell 条件分支最终执行了该命令；只有
支持的执行结果才提升为 `result_observed`。整次调用成功/失败不等于各测试用例成功/失败。

执行视图/正文使用稳定 span 或 call ID 去重，没有可靠 ID 时仅保留带位置的记录。
单测报告核对 task/session；没有归属 ID 的 JUnit 报告只作为待复核证据。仅存在报告
文件名不触发准入。JSON/JSONL/XML 附件通过清单引用读取，默认分析单文件上限 5 MB；其他附件
引用继续记录。读取/校验成功不等于附件业务结论正确。

## 自定义入口与策略

用 `--policy policy.json` 提供配置，例如：

```json
{
  "require_problem_signal": false,
  "priority_only": false,
  "allowed_test_types": ["unit", "api", "gui"],
  "command_rules": [
    {"prefix": ["emo", "run", "test:api"], "test_type": "api"},
    {"prefix": ["python3", "scripts/run_integration.py"], "test_type": "integration"}
  ],
  "test_skills": ["bits-unit-test-gen", "flux-api-test", "flux-sdt"],
  "max_artifact_bytes": 5000000
}
```

命令规则按解析后的 token 前缀匹配，程序路径使用 basename。配置仅补充已知入口，
不会解析未知脚本内容；自定义 API/GUI 入口可形成测试候选，未知结果仍单独记录。策略不接受未知字段
或工具错误数/耗时等辅助阈值，以免默认准入逐渐偏离测试证据。

## 验证边界

自动检查覆盖命令示例误命中、技能读取、实际结果、partial/unknown、重复视图、
报告归属、局部阻塞、无关文件损坏、可配置入口、独立输出和输入不变。
真实本地样本覆盖开发/工程检查、bits-code-guard-doubao 审查与依赖准备失败的 Go 测试。
未知格式仍需按真实数据逐步增加有明确约定的适配器；存在其它测试证据时不因未知结果
反过来否定测试场景。
