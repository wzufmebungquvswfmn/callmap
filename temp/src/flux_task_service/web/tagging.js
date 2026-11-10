(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  if (!$("tagging-start")) return;
  const state = {id: null, busy: false, polling: false, timer: null, offset: 0, revision: 0};
  const selectedTasks = new Set(), downloads = new Set();
  let pageRecords = [], exportId = null, ready = false, selectionRevision = 0, selecting = false;
  const levels = {priority_candidate: "优先候选", ordinary_candidate: "普通候选", review: "待复核", excluded: "排除"};
  const typeLabels = {unit: "单元测试", api: "接口测试", gui: "界面测试", integration: "集成测试", performance: "性能测试", other: "其他测试"};
  const evidence = {result_observed: "观察到测试结果", invocation_observed: "仅观察到测试调用", setup_only: "仅测试准备", no_match: "未命中测试规则", unknown: "未知"};
  const reasons = {
    TEST_RESULT_WITH_PROBLEM_SIGNAL: "测试结果包含失败或报告问题线索",
    TEST_EXECUTION_RESULT_OBSERVED: "观察到实际测试结果",
    PROBLEM_SIGNAL_REQUIRED_NOT_OBSERVED: "当前策略要求问题线索，尚未观察到",
    TEST_EVIDENCE_ASSOCIATION_UNCONFIRMED: "测试证据归属待确认",
    TEST_INVOCATION_WITHOUT_RECOGNIZED_RESULT: "有测试调用，结果待确认",
    TEST_RESULT_EXECUTION_STATE_UNKNOWN: "结果未确认实际执行的用例",
    NO_EXECUTED_CASES_IN_RECOGNIZED_RESULTS: "结果仅有阻塞、未执行或跳过",
    TEST_TYPE_NOT_ALLOWED: "测试类型不在当前范围",
    INSUFFICIENT_ANALYSIS_COVERAGE: "分析覆盖不足，需复核",
    TEST_SETUP_WITHOUT_EXECUTION_EVIDENCE: "仅测试准备，未观察到执行证据",
    NO_RECOGNIZED_TEST_EXECUTION_EVIDENCE: "未命中已知测试执行证据",
    TAGGING_CANCELLED: "停止前尚未处理", MANIFEST_MISSING: "缺少数据清单",
    MANIFEST_NOT_FINALIZED: "数据导出尚未完成",
  };
  const node = (tag, text, className) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  async function request(path, body, binary = false) {
    if (window.FluxAPI) return window.FluxAPI.request(path, body, {binary});
    const key = $("api-key").value.trim(), headers = {};
    if (key) headers.Authorization = `Bearer ${key}`;
    const options = {headers};
    if (body !== undefined) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    if (!response.ok) {
      const value = await response.json();
      throw new Error(value.error?.message || `请求失败 (${response.status})`);
    }
    return binary ? response.blob() : response.json();
  }
  function updateButtons() {
    $("tagging-start").disabled = state.busy || !/^[a-f0-9]{32}$/.test($("tagging-export-id").value.trim());
    $("tagging-resume").disabled = state.busy;
    $("tagging-cancel").hidden = !state.busy;
    updateSelection();
  }
  function updateSelection() {
    const page = $("tagging-select-page");
    if (!page) return;
    page.checked = pageRecords.length > 0 && pageRecords.every((record) => selectedTasks.has(record.task_id));
    page.indeterminate = pageRecords.some((record) => selectedTasks.has(record.task_id)) && !page.checked;
    page.disabled = !ready || selecting || !pageRecords.length;
    $("tagging-selection-count").textContent = selecting ? "正在选择保留项…" : `已选 ${selectedTasks.size} 个（跨页）`;
    $("tagging-select-kept").disabled = !ready || selecting;
    $("tagging-clear-selection").disabled = !selectedTasks.size && !selecting;
    $("tagging-download-source").disabled = !ready || !exportId || !selectedTasks.size || selecting || downloads.has(`${state.id}:source`);
    $("tagging-download-subset").disabled = !ready || !selectedTasks.size || selecting || downloads.has(`${state.id}:tags`);
    for (const input of $("tagging-rows").querySelectorAll('input[data-task-id]')) { input.checked = selectedTasks.has(input.dataset.taskId); input.disabled = !ready || selecting; }
  }
  function resetSelection() { selectedTasks.clear(); pageRecords = []; exportId = null; ready = false; selectionRevision += 1; selecting = false; updateSelection(); }
  async function selectKept() {
    const id = state.id, revision = ++selectionRevision;
    const params = new URLSearchParams({limit: 200, task_id: $("tagging-search").value.trim()});
    if (!/^[0-9]*$/.test(params.get("task_id"))) return;
    if ($("tagging-filter").value) params.set("candidate_level", $("tagging-filter").value);
    selecting = true; updateSelection();
    const chosen = new Set();
    try {
      for (let offset = 0, total = Infinity; offset < total; offset += 200) {
        params.set("offset", offset);
        const page = await request(`v1/tagging/${id}/results?${params}`);
        if (revision !== selectionRevision || id !== state.id) return;
        total = page.total;
        if (!page.records.length && offset < total) throw new Error("结果分页未能继续，请重新选择。");
        for (const record of page.records) if ((record.dynamic_tags?.review?.decision || record.decision) === "keep") chosen.add(record.task_id);
      }
      selectedTasks.clear(); for (const id of chosen) selectedTasks.add(id);
    } catch (error) { if (revision === selectionRevision) $("tagging-status").textContent = `选择失败：${error.message}`; }
    finally { if (revision === selectionRevision) { selecting = false; updateSelection(); } }
  }
  async function downloadSubset(kind) {
    const id = state.id, source = exportId, ids = [...selectedTasks];
    if (!ready || !ids.length || (kind === "source" && !source) || selecting) return;
    const key = `${id}:${kind}`; if (downloads.has(key)) return;
    downloads.add(key); updateSelection();
    const params = new URLSearchParams(); for (const taskId of ids) params.append("task_id", taskId);
    try {
      const blob = await request(`${kind === "source" ? `v1/exports/${source}` : `v1/tagging/${id}`}/download?${params}`, undefined, true);
      const filename = `flux_${kind === "source" ? "export" : "tagging"}_${kind === "source" ? source : id}_selected_${ids.length}.zip`;
      if (window.FluxAPI) window.FluxAPI.save(blob, filename);
      else { const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    } catch (error) { if (id === state.id) $("tagging-status").textContent = `所选任务下载失败：${error.message}`; }
    finally { downloads.delete(key); updateSelection(); }
  }
  function renderDetail(value, content) {
    content.replaceChildren();
    const core = value.core || {};
    const signal = core.problem_signal === true ? "观察到失败或报告问题线索" : core.problem_signal === false ? "未观察到问题线索" : "问题线索未知";
    content.append(node("p", `${evidence[core.test_evidence_level] || "证据未知"} · ${signal}`));
    if (core.coverage?.rule_search_complete === false) content.append(node("p", "规则分析覆盖不足，未命中不代表没有测试。"));
    const selected = new Set((value.admission?.selected_run_keys || []).map((run) => `${run.origin}:${run.run_id}`));
    const associations = {matched: "归属当前任务", mismatch: "归属其它任务", unknown: "归属待确认", conflict: "结果存在冲突"};
    const runStates = {executed: "有已执行用例", attempted: "仅观察到调用", not_executed: "没有已执行用例", unknown: "执行情况未知"};
    const countLabels = {passed: "通过", failed: "失败", blocked: "阻塞", skipped: "跳过", not_run: "未执行"};
    for (const run of core.runs || []) {
      const section = node("section", undefined, "tagging-run");
      section.append(node("strong", `测试结果 · ${typeLabels[run.test_type] || "测试类型待确认"} · ${runStates[run.execution_state] || "未知"}`));
      section.append(node("p", `${associations[run.association] || "归属待确认"} · ${selected.has(`${run.origin}:${run.run_id}`) ? "可进入缺陷提取" : "仅保留记录"}`));
      section.append(node("p", Object.entries(countLabels).map(([key, label]) => `${label} ${run.counts?.[key] ?? "未知"}`).join(" / ")));
      if (run.reported_issue_count != null) section.append(node("p", `报告问题 ${run.reported_issue_count} 项，后续需校验。`));
      for (const ref of run.evidence || []) {
        if (ref.excerpt) section.append(node("pre", ref.excerpt, "tagging-evidence"));
        section.append(node("p", `证据位置：${ref.path}${ref.pointer || ""}`, "export-source"));
      }
      content.append(section);
    }
    const other = value.recorded_only || {}, statistics = other.tool_statistics || {};
    const repository = other.repository || {}, extraction = repository.extraction;
    if (repository.repo_path) content.append(node("p", `上游仓库字段：${repository.repo_path}`));
    if (extraction) {
      const section = node("section", undefined, "tagging-run");
      section.append(node("strong", "关联仓库（来源与用途分类）"));
      section.append(node("p", "仓库提及仅作为候选；挂载、准备与提交记录分别保留，不代表实际读取或修改。"));
      if (!extraction.repositories?.length) section.append(node("p", "未知：当前数据未提取到明确仓库身份。"));
      const states = {registered: "上游登记", mentioned: "提及，待确认", mounted: "运行时挂载", commit_recorded: "有提交记录", prepare_attempted: "尝试准备", prepare_failed: "准备失败", prepared: "准备成功", remote_verified: "已核实远端", access_failed: "访问失败", access_attempted: "尝试访问"};
      for (const repo of extraction.repositories || []) {
        const detail = node("details");
        detail.append(node("summary", `${repo.name} · ${repo.platform || "平台未知"} · ${(repo.categories || []).map((key) => extraction.category_labels?.[key] || key).join(" / ")}`));
        detail.append(node("p", `用途：${(repo.purposes || ["unknown"]).map((key) => extraction.purpose_labels?.[key] || key).join(" / ")}`));
        for (const ref of repo.evidence || []) {
          detail.append(node("p", `${extraction.category_labels?.[ref.category] || ref.category} · ${states[ref.state] || ref.state}${ref.branch ? ` · 分支 ${ref.branch}` : ""}${ref.revision ? ` · revision ${ref.revision}` : ""}`));
          detail.append(node("p", `该条证据的用途：${(ref.purposes || ["unknown"]).map((key) => extraction.purpose_labels?.[key] || key).join(" / ")}`));
          if (ref.excerpt) detail.append(node("pre", ref.excerpt, "tagging-evidence"));
          if (ref.result_excerpt) detail.append(node("pre", ref.result_excerpt, "tagging-evidence"));
          detail.append(node("p", `证据位置：${ref.path}${ref.pointer || ""}`, "export-source"));
        }
        section.append(detail);
      }
      if (extraction.unresolved?.length) section.append(node("p", `另有 ${extraction.unresolved.length} 条本地路径或身份不明的记录，详见标签 JSON。`));
      content.append(section);
    }
    content.append(node("p", `其它记录：工具调用 ${statistics.tool_call_count ?? "未知"} 次，失败 ${statistics.tool_failed_count ?? "未知"} 次。`));
    if (other.platform?.title) content.append(node("p", `任务标题：${other.platform.title}`));
    const agents = other.platform?.agents;
    if (agents && typeof agents === "object") content.append(node("p", `Agent / 配置模型：${Object.values(agents).filter(Boolean).map((agent) => `${agent.agent || "未知"} / ${agent.model || "未知"}`).join("；")}`));
    for (const error of value.analysis_errors || []) content.append(node("p", `分析提示：${reasons[error.code] || error.code}`));
    if (value.dynamic_tags?.review) content.append(node("p", `人工复核：${({keep: "保留", exclude: "排除", hold: "待复核"})[value.dynamic_tags.review.decision]} · ${value.dynamic_tags.review.reason}`));
    if (value.dynamic_tags?.task_classification) content.append(node("p", `任务分类：${value.dynamic_tags.task_classification.category}`));
  }
  async function loadRows() {
    if (!state.id || $("tagging-results").hidden) return;
    const id = state.id, revision = ++state.revision;
    const search = $("tagging-search").value.trim();
    if (!/^[0-9]*$/.test(search)) { $("tagging-status").textContent = "Task ID 搜索只接受数字。"; return; }
    const params = new URLSearchParams({offset: state.offset, limit: 50, task_id: search});
    if ($("tagging-filter").value) params.set("candidate_level", $("tagging-filter").value);
    try {
      const page = await request(`v1/tagging/${id}/results?${params}`);
      if (id !== state.id || revision !== state.revision) return;
      pageRecords = page.records;
      const sourceId = exportId;
      const rows = page.records.map((record) => {
        const row = node("tr"); row.dataset.taskId = record.task_id;
        const title = node("th", undefined, "export-task-id"); title.scope = "row";
        const open = node("button", record.task_id, "text-button"); open.type = "button";
        const check = node("input"); check.type = "checkbox"; check.dataset.taskId = record.task_id; check.setAttribute("aria-label", `选择候选任务 ${record.task_id}`);
        check.addEventListener("change", () => { check.checked ? selectedTasks.add(record.task_id) : selectedTasks.delete(record.task_id); updateSelection(); });
        open.addEventListener("click", () => window.FluxWorkspace?.openTask(record.task_id, sourceId)); title.append(check, open);
        const decision = {keep: "保留", exclude: "排除", hold: "待复核"}[record.decision] || record.decision;
        const level = node("td"); level.append(node("span", `${levels[record.candidate_level] || record.candidate_level} · ${decision}`, `tagging-level tagging-${record.candidate_level}`));
        const review = record.dynamic_tags?.review;
        if (review) level.append(node("p", `复核：${({keep: "保留", hold: "待复核", exclude: "排除"})[review.decision] || "未定"}`, "review-decision"));
        const activity = node("td", evidence[record.test_evidence_level] || "未知");
        if (record.test_types?.length) activity.append(node("p", record.test_types.map((type) => typeLabels[type] || "其他测试").join(" / "), "export-source"));
        const detail = node("td");
        for (const reason of record.reason_codes || []) detail.append(node("p", reasons[reason] || reason, "export-source"));
        const expand = node("details"), summary = node("summary", "查看证据与其它标签"); expand.append(summary);
        let loaded = false;
        expand.addEventListener("toggle", async () => {
          if (!expand.open || loaded) return;
          loaded = true;
          const content = node("div", "读取中…", "tagging-detail"); expand.append(content);
          try {
            const value = await request(`v1/tagging/${id}/tasks/${record.task_id}`);
            renderDetail(value, content);
          } catch (error) { loaded = false; content.textContent = error.message; }
        });
        detail.append(expand);
        if (window.FluxWorkspace) window.FluxWorkspace.attachReview(detail, id, record.task_id, sourceId);
        row.append(title, level, activity, detail); return row;
      });
      if (!rows.length) { const row = node("tr"), cell = node("td", "没有符合条件的任务。", "export-table-empty"); cell.colSpan = 4; row.append(cell); rows.push(row); }
      $("tagging-rows").replaceChildren(...rows);
      $("tagging-visible-count").textContent = `${page.total} 个匹配任务`;
      $("tagging-page-info").textContent = `第 ${Math.floor(state.offset / 50) + 1} / ${Math.max(1, Math.ceil(page.total / 50))} 页`;
      $("tagging-previous").disabled = state.offset === 0;
      $("tagging-next").disabled = state.offset + 50 >= page.total;
      updateSelection();
    } catch (error) { $("tagging-status").textContent = `结果查询失败：${error.message}`; }
  }
  async function poll() {
    if (!state.id) return;
    if (state.polling) { state.timer = setTimeout(poll, 100); return; }
    clearTimeout(state.timer);
    const id = state.id; state.polling = true;
    try {
      const job = await request(`v1/tagging/${id}`);
      if (id !== state.id) return;
      exportId = job.export_job_id || null; ready = Boolean(job.archive_ready);
      document.dispatchEvent(new CustomEvent("flux-tagging-update", {detail: job}));
      const labels = {queued: "等待开始", running: "正在打标", packaging: "正在准备结果", completed: "打标完成", partial: "打标完成，部分任务需复核", cancelled: "已停止", failed: "打标失败", interrupted: "服务中断，请重新发起打标"};
      $("tagging-status").textContent = `${labels[job.status] || job.status} · ${job.completed_count} / ${job.task_count} 个任务 · 本次策略：${job.policy?.require_problem_signal ? "仅问题线索候选" : "有测试结果的候选"}${job.error ? ` · ${job.error.message}` : ""}`;
      $("tagging-export-id").value = job.export_job_id;
      state.busy = ["queued", "running", "packaging"].includes(job.status); updateButtons();
      $("tagging-selected").disabled = !job.archive_ready; $("tagging-download").disabled = !job.archive_ready;
      if (job.archive_ready) {
        $("tagging-results").hidden = false;
        $("tagging-summary").replaceChildren(...[["保留", "keep"], ["排除", "exclude"], ["待复核", "hold"]].map(([label, key]) => {
          const card = node("div", undefined, "export-stat"); card.append(node("strong", String(job.decision_counts[key] || 0)), node("span", label)); return card;
        }));
        await loadRows();
      } else if (state.busy) state.timer = setTimeout(poll, 1500);
    } catch (error) {
      if (id !== state.id) return;
      $("tagging-status").textContent = `进度查询失败：${error.message} · 后台仍可能在继续。`;
      if (error.retryable || error.status >= 500) state.timer = setTimeout(poll, 5000);
    } finally { state.polling = false; }
  }
  async function start() {
    if ($("tagging-start").disabled) return;
    try {
      const rules = JSON.parse($("tagging-rules").value);
      if (!Array.isArray(rules)) throw new Error("自定义入口必须是 JSON 数组");
      const policy = {require_problem_signal: $("tagging-priority").checked, command_rules: rules,
        allowed_test_types: [...$("tagging-types").querySelectorAll("input:checked")].map((input) => input.value)};
      state.busy = true; updateButtons();
      $("tagging-status").textContent = "正在创建打标任务…";
      const job = await request(`v1/exports/${$("tagging-export-id").value.trim()}/tagging`, {policy, allow_legacy: $("tagging-legacy").checked});
      resetSelection();
      state.id = job.tagging_id; state.offset = 0; state.revision += 1;
      $("tagging-job-id").value = state.id; $("tagging-results").hidden = true;
      $("tagging-selected").disabled = true; $("tagging-download").disabled = true;
      await poll();
    } catch (error) { state.busy = false; updateButtons(); $("tagging-status").textContent = `创建失败：${error.message}`; }
  }
  async function download(selected) {
    const id = state.id, button = $(selected ? "tagging-selected" : "tagging-download");
    if (!id || button.disabled) return;
    button.disabled = true;
    try {
      const blob = await request(`v1/tagging/${id}/${selected ? "selected" : "download"}`, undefined, true);
      const url = URL.createObjectURL(blob), link = node("a"); link.href = url;
      link.download = `flux_${selected ? "selected" : "tagging"}_${id}.${selected ? "json" : "zip"}`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { $("tagging-status").textContent = `下载失败：${error.message}`; }
    finally { if (id === state.id) button.disabled = false; }
  }
  document.addEventListener("flux-export-ready", (event) => { if (!state.busy) { $("tagging-export-id").value = event.detail.job_id; updateButtons(); } });
  $("tagging-export-id").addEventListener("input", updateButtons);
  $("tagging-start").addEventListener("click", start);
  $("tagging-resume").addEventListener("click", async () => {
    const id = $("tagging-job-id").value.trim();
    if (!/^[a-f0-9]{32}$/.test(id)) { $("tagging-status").textContent = "请输入有效的打标任务 ID。"; return; }
    clearTimeout(state.timer); state.id = id; state.offset = 0; state.revision += 1;
    resetSelection();
    $("tagging-results").hidden = true; $("tagging-selected").disabled = true; $("tagging-download").disabled = true;
    await poll();
  });
  $("tagging-cancel").addEventListener("click", async () => { try { if (state.id) await request(`v1/tagging/${state.id}/cancel`, {}); } catch (error) { $("tagging-status").textContent = error.message; } });
  $("tagging-selected").addEventListener("click", () => download(true));
  $("tagging-download").addEventListener("click", () => download(false));
  for (const [id, event] of [["tagging-search", "input"], ["tagging-filter", "change"]]) $(id).addEventListener(event, () => { selectionRevision += 1; selecting = false; updateSelection(); state.offset = 0; loadRows(); });
  $("tagging-previous").addEventListener("click", () => { state.offset = Math.max(0, state.offset - 50); loadRows(); });
  $("tagging-next").addEventListener("click", () => { state.offset += 50; loadRows(); });
  $("tagging-select-page").addEventListener("change", () => { for (const record of pageRecords) $("tagging-select-page").checked ? selectedTasks.add(record.task_id) : selectedTasks.delete(record.task_id); updateSelection(); });
  $("tagging-select-kept").addEventListener("click", selectKept);
  $("tagging-select-kept").title = "跨页选择当前筛选内的保留任务，优先采用复核结论";
  $("tagging-clear-selection").addEventListener("click", () => { selectionRevision += 1; selecting = false; selectedTasks.clear(); updateSelection(); });
  $("tagging-download-source").addEventListener("click", () => downloadSubset("source"));
  $("tagging-download-subset").addEventListener("click", () => downloadSubset("tags"));
  $("tagging-selected").textContent = "下载自动候选清单";
  $("tagging-download").textContent = "下载整批打标结果";
  document.addEventListener("flux-review-saved", ({detail}) => { if (detail.taggingId === state.id) { selectionRevision += 1; selecting = false; loadRows(); updateSelection(); } });
  updateButtons();
})();
