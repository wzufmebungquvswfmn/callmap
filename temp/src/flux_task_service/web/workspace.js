(() => {
  "use strict";
  const $ = (id) => document.getElementById(id), api = window.FluxAPI;
  const selected = new Set(), summaries = new Map();
  let pageIds = [], queryEnabled = false, currentIdentity = null, detailTab = "overview", detailController;
  let previewUrls = [];
  let snapshotBatch = null, summaryController, serverHistory = [], historyOffset = 0, historyBusy = false, currentBatchEntry;
  const historyStatus = el("span", "", "history-status"), historyMore = button("更早批次", () => refreshHistory(true)), historyRefresh = button("刷新", () => refreshHistory());
  const statuses = {queueing: "排队中", running: "进行中", "input-required": "等待输入", committed: "已提交", finished: "已完成", completed: "已完成", interrupted: "已中断", failed: "失败", "rolled-back": "已回退", passed: "通过", success: "通过", skipped: "跳过", blocked: "阻塞"};
  const types = {unit: "单元测试", api: "接口测试", gui: "界面测试", integration: "集成测试", performance: "性能测试", other: "其他测试"};
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function button(text, action, className = "secondary-button") {
    const node = el("button", text, className); node.type = "button"; node.addEventListener("click", action); return node;
  }
  function note(text, warning = false) { return el("p", text, warning ? "detail-warning" : "detail-note"); }
  function readHistory(key) {
    try { const value = JSON.parse(localStorage.getItem(key) || "[]"); return Array.isArray(value) ? value.filter((v) => v && /^[a-f0-9]{32}$/.test(v.id)).slice(0, 20) : []; } catch { return []; }
  }
  function remember(key, item) {
    const history = readHistory(key), previous = history.find((value) => value.id === item.id);
    try { localStorage.setItem(key, JSON.stringify([{...previous, ...item, date: item.date || previous?.date || formatDate(Date.now())}, ...history.filter((value) => value.id !== item.id)].slice(0, 20))); } catch { /* History is optional when browser storage is unavailable. */ }
    renderHistory();
  }
  function renderHistory() {
    for (const [id, key, ready] of [["export-history", "flux.exports", false], ["tagging-history", "flux.exports", true], ["tagging-job-history", "flux.tagging", false]]) {
      const select = $(id), value = select.value;
      let entries = key === "flux.exports" && serverHistory.length ? serverHistory : readHistory(key);
      if (key === "flux.exports" && currentBatchEntry) entries = [currentBatchEntry, ...entries.filter((entry) => entry.id !== currentBatchEntry.id)];
      const shown = entries.filter((item) => !ready || item.ready);
      const signature = JSON.stringify(shown);
      if (select.dataset.entries === signature) continue;
      select.dataset.entries = signature;
      select.replaceChildren(new Option("选择历史批次", ""));
      const labels = {running: "获取中", queued: "等待中", packaging: "打包中", partial: "部分可用", completed: "已完成", cancelled: "已取消", interrupted: "已中断", failed: "失败"};
      for (const entry of shown) select.add(new Option(`${entry.date} · ${entry.count ?? "?"} 个任务 · ${labels[entry.status] || (entry.ready ? "已就绪" : "未完成")}`, entry.id));
      select.value = value;
    }
  }
  async function refreshHistory(more = false) {
    if (historyBusy) return;
    historyBusy = true; historyRefresh.disabled = true; historyMore.disabled = true;
    try {
      const offset = more ? historyOffset : 0;
      const data = await api.request(`v1/exports?offset=${offset}&limit=20`, undefined, {timeout: 10000});
      const entries = data.jobs.map((job) => ({id: job.job_id, date: formatDate(job.created_at), count: job.task_count, status: job.status, ready: job.archive_ready}));
      serverHistory = more ? [...serverHistory, ...entries.filter((entry) => !serverHistory.some((old) => old.id === entry.id))] : entries;
      historyOffset = offset + entries.length; historyMore.hidden = historyOffset >= data.total;
      historyStatus.textContent = ""; renderHistory();
    } catch (error) { historyStatus.textContent = `历史未更新：${error.message}`; }
    finally { historyBusy = false; historyRefresh.disabled = false; historyMore.disabled = false; }
  }
  document.querySelector(".batch-history").append(historyRefresh, historyMore, historyStatus, button("新建批次", () => {
    window.FluxUI.newExport(); $("export-history").value = ""; $("go-candidates").disabled = true;
    history.replaceState(null, "", "#exports"); refreshHistory();
  }, "primary-button"));
  function navigate(view) {
    view = view.split("?")[0];
    if (!["tasks", "exports", "tagging"].includes(view)) view = "tasks";
    for (const section of document.querySelectorAll("[data-workspace]")) section.hidden = section.dataset.workspace !== view;
    for (const tab of document.querySelectorAll("[data-view]")) {
      if (tab.dataset.view === view) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    }
    if (location.hash.split("?")[0] !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view !== "tasks") refreshHistory();
  }
  for (const tab of document.querySelectorAll("[data-view]")) tab.addEventListener("click", () => navigate(tab.dataset.view));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
  function syncSelection() {
    $("selection-count").textContent = `已选择 ${selected.size} 个任务`;
    $("select-page").checked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
    $("select-page").indeterminate = pageIds.some((id) => selected.has(id)) && !$("select-page").checked;
    $("select-page").disabled = !queryEnabled;
    $("export-selection").disabled = !queryEnabled || !selected.size;
    for (const checkbox of $("task-list").querySelectorAll("input")) checkbox.disabled = !queryEnabled;
  }
  function taskSummary(data, id) {
    const body = data.data || data;
    const item = (body.items || []).find((entry) => String(entry.task?.task_id ?? entry.task_id ?? entry.id) === id) || body.items?.[0];
    return item?.task || body.task || body;
  }
  function renderTasks() {
    $("task-list").replaceChildren(...pageIds.map((id) => {
      const row = el("div", undefined, "task-list-row"), checkbox = el("input"); checkbox.type = "checkbox";
      checkbox.checked = selected.has(id); checkbox.setAttribute("aria-label", `选择任务 ${id}`);
      checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(id) : selected.delete(id); syncSelection(); window.FluxUI.refresh(); });
      const info = el("div", undefined, "task-name"), summary = summaries.get(id);
      info.append(button(summary?.title || summary?.task_title || `任务 ${id}`, () => openTask(id), "task-title"));
      if (summary) info.append(el("small", [id, statuses[summary.task_status || summary.status], summary.created_by || summary.owner, formatDate(summary.task_created_at || summary.created_at)].filter(Boolean).join(" · ")));
      row.append(checkbox, info, button("查看", () => openTask(id), "text-button")); return row;
    }));
    syncSelection();
  }
  document.addEventListener("flux-task-page", (event) => {
    pageIds = event.detail.ids;
    const available = new Set(event.detail.allIds);
    for (const id of selected) if (!available.has(id)) selected.delete(id);
    renderTasks();
    loadSummaries();
  });
  document.addEventListener("flux-query-state", (event) => { queryEnabled = event.detail.enabled; syncSelection(); });
  $("select-page").addEventListener("change", () => { for (const id of pageIds) $("select-page").checked ? selected.add(id) : selected.delete(id); renderTasks(); window.FluxUI.refresh(); });
  $("export-selection").addEventListener("click", () => { window.FluxUI.newExport(); $("data-export-scope").value = "selected"; navigate("exports"); window.FluxUI.refresh(); });
  $("collect-all").addEventListener("click", () => window.FluxUI.collectAll());
  async function loadSummaries(force = false) {
    summaryController?.abort(); const controller = new AbortController(); summaryController = controller;
    const ids = pageIds.filter((id) => force || !summaries.has(id));
    if (!ids.length) { summaryButton.disabled = false; summaryButton.textContent = "更新摘要"; return; }
    summaryButton.disabled = true; summaryButton.textContent = "读取摘要…";
    try {
      const data = await api.request("v1/tasks/summaries", {task_ids: ids}, {signal: controller.signal, timeout: 30000});
      if (controller.signal.aborted) return;
      for (const [id, summary] of Object.entries(data.tasks)) summaries.set(id, summary);
      renderTasks(); summaryButton.textContent = data.missing.length ? `${data.missing.length} 项摘要不可用 · 重试` : "更新摘要";
    } catch (error) { if (!controller.signal.aborted) { summaryButton.textContent = "摘要未读到 · 重试"; summaryButton.title = error.message; } }
    finally { if (summaryController === controller) summaryButton.disabled = false; }
  }
  const summaryButton = button("更新摘要", () => loadSummaries(true), "text-button");
  $("selection-count").after(summaryButton);
  $("data-export-scope").addEventListener("change", () => { $("manual-export").hidden = $("data-export-scope").value !== "manual"; });
  const presets = {all: ["task", "trace", "metrics", "artifacts", "versions", "gui", "conversation"], analysis: ["task", "trace", "metrics", "conversation"], files: ["task", "artifacts", "versions"]};
  const costNote = note(""); document.querySelector(".preset-bar").after(costNote);
  function updateCost() { costNote.textContent = $("data-export-components").querySelector('[value="artifacts"]').checked ? "包含全部产物文件，文件较多时获取时间较长。" : "只获取所选记录，不下载产物文件。"; }
  for (const preset of document.querySelectorAll("[data-preset]")) preset.addEventListener("click", () => {
    if ($("data-export-scope").disabled) return;
    for (const input of $("data-export-components").querySelectorAll("input")) input.checked = presets[preset.dataset.preset].includes(input.value);
    for (const option of document.querySelectorAll("[data-preset]")) option.setAttribute("aria-pressed", String(option === preset));
    updateCost();
  });
  $("data-export-components").addEventListener("change", () => { for (const preset of document.querySelectorAll("[data-preset]")) preset.setAttribute("aria-pressed", "false"); updateCost(); });
  updateCost();
  $("include-raw").addEventListener("change", () => { $("recover-raw").disabled = !$("include-raw").checked; if (!$("include-raw").checked) $("recover-raw").checked = false; });
  $("resume-export").addEventListener("click", () => window.FluxUI.resumeExport($("resume-export-id").value.trim()));
  $("export-history").addEventListener("change", () => { if ($("export-history").value) window.FluxUI.resumeExport($("export-history").value); });
  document.addEventListener("flux-export-update", ({detail: job}) => {
    const entry = {id: job.job_id, date: formatDate(job.created_at || Date.now()), count: job.task_count, status: job.status, ready: job.archive_ready};
    currentBatchEntry = entry;
    if (serverHistory.length) serverHistory = [entry, ...serverHistory.filter((item) => item.id !== entry.id)];
    remember("flux.exports", entry);
    if (!document.querySelector('[data-workspace="exports"]').hidden) history.replaceState(null, "", `#exports?batch=${job.job_id}`);
    $("go-candidates").disabled = !job.archive_ready;
    $("export-history").value = job.job_id;
  });
  document.addEventListener("flux-export-ready", ({detail}) => { $("tagging-history").value = detail.job_id; $("tagging-export-id").value = detail.job_id; $("tagging-export-id").dispatchEvent(new Event("input")); });
  document.addEventListener("flux-export-new", () => { currentBatchEntry = null; $("export-history").value = ""; $("go-candidates").disabled = true; });
  $("go-candidates").addEventListener("click", () => navigate("tagging"));
  $("tagging-history").addEventListener("change", () => {
    $("tagging-export-id").value = $("tagging-history").value; $("tagging-export-id").dispatchEvent(new Event("input"));
  });
  $("tagging-job-history").addEventListener("change", () => { $("tagging-job-id").value = $("tagging-job-history").value; if ($("tagging-job-id").value) $("tagging-resume").click(); });
  document.addEventListener("flux-tagging-update", ({detail: job}) => remember("flux.tagging", {id: job.tagging_id, count: job.task_count, ready: job.archive_ready}));
  $("add-command-rule").addEventListener("click", () => {
    const row = el("div", undefined, "command-rule"), input = el("input"), select = el("select");
    input.placeholder = "例如：npm run test"; input.setAttribute("aria-label", "测试命令");
    select.setAttribute("aria-label", "测试类型"); for (const [key, label] of Object.entries(types)) select.add(new Option(label, key));
    const sync = () => { $("tagging-rules").value = JSON.stringify([...$("command-rules").children].filter((r) => r.querySelector("input").value.trim()).map((r) => ({prefix: r.querySelector("input").value.trim().split(/\s+/), test_type: r.querySelector("select").value}))); };
    row.append(input, select, button("×", () => { row.remove(); sync(); }, "icon-button"));
    row.lastChild.setAttribute("aria-label", "删除测试命令"); row.addEventListener("input", sync); row.addEventListener("change", sync);
    $("command-rules").append(row); input.focus();
  });
  $("refresh-options").addEventListener("click", async () => {
    $("refresh-options").disabled = true; $("options-status").textContent = "正在读取可选分类…";
    try { await window.FluxUI.refreshOptions(); $("options-status").textContent = "可选分类已更新。"; } catch (error) { $("options-status").textContent = error.message; }
    finally { $("refresh-options").disabled = false; }
  });
  // Credentials remain in memory and are never included in history entries.
  $("settings-auth").append($("auth-section"));
  $("auth-section").hidden = false;
  $("open-settings").addEventListener("click", () => $("settings-dialog").showModal());
  document.addEventListener("flux-auth-required", () => { if (!$("settings-dialog").open) $("settings-dialog").showModal(); });
  async function updateAuth(action) {
    const body = {action}; if (action === "update") body.jwt = $("fornax-token").value.trim();
    if (action === "update" && !body.jwt) { $("settings-status").textContent = "请填写访问凭据。"; return; }
    $("settings-status").textContent = "正在更新…";
    try { await api.request("v1/task-data/fornax/auth", body); $("fornax-token").value = ""; $("settings-status").textContent = "凭据已加载，读取资料时会验证可用性。"; }
    catch (error) { $("settings-status").textContent = error.message; }
  }
  $("fornax-form").addEventListener("submit", (event) => { event.preventDefault(); updateAuth("update"); });
  for (const b of document.querySelectorAll("[data-auth]")) b.addEventListener("click", () => updateAuth(b.dataset.auth));
  for (const dialog of document.querySelectorAll("#task-dialog,#settings-dialog")) dialog.querySelector(".close-dialog").addEventListener("click", () => dialog.close());
  function clearPreviews() { for (const url of previewUrls) URL.revokeObjectURL(url); previewUrls = []; }
  $("task-dialog").addEventListener("close", () => { detailController?.abort(); clearPreviews(); });
  const sourceBar = el("div", undefined, "detail-source"); $("detail-tabs").before(sourceBar);
  function showSource() {
    sourceBar.replaceChildren(el("span", snapshotBatch ? "已保存资料" : "实时资料"));
    if (snapshotBatch) sourceBar.append(button("查看实时资料", () => { snapshotBatch = null; showSource(); renderDetail(); }, "text-button"));
  }
  function openTask(id = "", batch = null) {
    snapshotBatch = batch; showSource();
    $("identity-kind").value = "task_id"; $("task-open-id").value = id;
    if (!$("task-dialog").open) $("task-dialog").showModal();
    if (id) loadIdentity(); else { detailController?.abort(); currentIdentity = null; $("task-detail-content").replaceChildren(note("输入任务编号")); $("task-open-id").focus(); }
  }
  $("open-task").addEventListener("click", () => openTask());
  $("task-open-form").addEventListener("submit", (event) => { event.preventDefault(); snapshotBatch = null; showSource(); loadIdentity(); });
  function loadIdentity() {
    const id = $("task-open-id").value.trim(), kind = $("identity-kind").value;
    if (!/^[1-9][0-9]{0,18}$/.test(id) || BigInt(id) > 9223372036854775807n) { $("task-detail-content").replaceChildren(note("请输入有效的数字编号。", true)); return; }
    currentIdentity = {[kind]: id}; detailTab = kind === "task_id" ? "overview" : kind === "session_id" ? "conversation" : "artifacts";
    $("task-dialog-title").textContent = {task_id: "任务详情", worker_session_id: "工作会话", session_id: "独立会话"}[kind];
    $("task-dialog-subtitle").textContent = id;
    for (const tab of $("detail-tabs").children) tab.hidden = kind === "worker_session_id" ? !["artifacts", "versions", "gui"].includes(tab.dataset.detail) : kind === "session_id" ? tab.dataset.detail !== "conversation" : false;
    renderDetail();
  }
  for (const tab of $("detail-tabs").children) tab.addEventListener("click", () => { if (currentIdentity) { detailTab = tab.dataset.detail; renderDetail(); } });
  const formatNumber = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "暂无数据";
  function formatDate(value) {
    if (value == null || value === "") return "";
    const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false});
  }
  const duration = (value) => typeof value !== "number" ? "暂无数据" : value < 1000 ? `${Math.round(value)} 毫秒` : value < 60000 ? `${(value / 1000).toFixed(1)} 秒` : `${(value / 60000).toFixed(1)} 分钟`;
  const textContent = (value) => typeof value === "string" ? value : Array.isArray(value) ? value.map(textContent).filter(Boolean).join("\n") : value && typeof value === "object" ? textContent(value.text ?? value.content ?? value.message ?? value.body) : "";
  function visit(value, fn, depth = 0) {
    if (!value || typeof value !== "object" || depth > 20) return;
    if (Array.isArray(value)) { for (const item of value) visit(item, fn, depth + 1); return; }
    if (fn(value) === false) return;
    for (const child of Object.values(value)) visit(child, fn, depth + 1);
  }
  async function renderDetail() {
    detailController?.abort(); clearPreviews();
    const controller = new AbortController(); detailController = controller;
    const identity = {...currentIdentity}, content = $("task-detail-content"), tab = detailTab, savedBatch = snapshotBatch;
    const request = savedBatch ? window.FluxSnapshot.create(savedBatch, identity.task_id, controller.signal) : (path, body = identity, options = {}) => api.request(`v1/task-data/${path}`, body, {...options, signal: controller.signal});
    for (const b of $("detail-tabs").children) { if (b.dataset.detail === tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); }
    content.replaceChildren(note("正在读取…"));
    const fragment = el("div");
    try {
      if (tab === "overview") {
        const taskSection = el("section"), metricSection = el("section");
        taskSection.append(note("读取任务信息…")); metricSection.append(note("读取统计…"));
        fragment.append(taskSection, metricSection); content.replaceChildren(fragment);
        const loadTask = async () => { try {
          const value = await request("detail"); if (controller.signal.aborted) return;
          const task = taskSummary(value, identity.task_id);
          if (!savedBatch) { summaries.set(identity.task_id, task); renderTasks(); }
          taskSection.replaceChildren(el("h2", task.title || task.task_title || "任务概览"));
          const list = el("dl", undefined, "detail-meta");
          for (const [label, value] of [["当前状态", statuses[task.task_status || task.status]], ["归属人", task.created_by], ["创建时间", formatDate(task.task_created_at || task.created_at)], ["分类", [task.level_1_category, task.level_2_category, task.level_3_category].filter(Boolean).join(" / ")]]) {
            if (typeof value === "string" || typeof value === "number") list.append(el("dt", label), el("dd", String(value)));
          }
          taskSection.append(list);
          const description = textContent(task.description || task.query || task.prompt);
          if (description) taskSection.append(el("p", description, "detail-note"));
        } catch (error) { if (!controller.signal.aborted) taskSection.replaceChildren(note(error.message, true), button("重试任务信息", loadTask)); } };
        const loadMetrics = async () => { try {
          const metrics = await request("metrics"); if (controller.signal.aborted) return;
          const values = metrics.values || {}, grid = el("div", undefined, "detail-metrics");
          for (const [label, value] of [["总 Token", formatNumber(values.total_tokens)], ["模型处理耗时", duration(values.total_latency_ms)], ["首次响应", duration(values.latency_ms_first_resp)], ["输入 Token", formatNumber(values.input_tokens)], ["输出 Token", formatNumber(values.output_tokens)], ["模型调用", formatNumber(values.model_call_count)]]) {
            const item = el("div", undefined, "detail-metric"); item.append(el("span", label), el("strong", value)); grid.append(item);
          }
          metricSection.replaceChildren(grid);
          if (metrics.complete !== true) metricSection.append(note("统计可能不完整；暂无数据的项目未按零计算。", true));
          const more = el("details"); more.append(el("summary", "更多统计"));
          for (const [label, value] of [["缓存输入", formatNumber(values.cached_tokens)], ["推理 Token", formatNumber(values.reasoning_tokens)], ["总历时", duration(values.end_to_end_latency_ms)], ["交互等待", duration(values.interaction_latency_ms)]]) more.append(note(`${label}：${value}`));
          metricSection.append(more);
        } catch (error) { if (!controller.signal.aborted) metricSection.replaceChildren(note(error.message, true), button("重试统计", loadMetrics)); } };
        fragment.append(button(savedBatch ? "下载已保存资料" : "下载此任务资料", async (event) => {
          if (savedBatch) {
            const b = event.currentTarget; b.disabled = true;
            try { api.save(await api.request(`v1/exports/${savedBatch}/download?task_id=${identity.task_id}`, undefined, {binary: true, signal: controller.signal}), `task_${identity.task_id}.zip`); }
            catch (error) { if (!controller.signal.aborted) taskSection.append(note(error.message, true)); }
            finally { b.disabled = false; }
          } else { $("task-dialog").close(); window.FluxUI.newExport(); $("data-export-scope").value = "manual"; $("manual-task-ids").value = identity.task_id; $("manual-export").hidden = false; window.FluxUI.refresh(); navigate("exports"); }
        }, "primary-button"));
        await Promise.allSettled([loadTask(), loadMetrics()]);
      } else if (tab === "conversation") {
        const inventory = await request("conversation", {...identity, action: "inventory"});
        const tickets = [], seen = new Set();
        function findTickets(value, action = "chunk") {
          if (Array.isArray(value)) { value.forEach((item) => findTickets(item, action)); return; }
          if (!value || typeof value !== "object") return;
          for (const [key, mode] of [["ticket", action], ["batch_ticket", "chunk"], ["session_ticket", "messages"]]) {
            if (typeof value[key] === "string" && !seen.has(`${mode}:${value[key]}`)) { seen.add(`${mode}:${value[key]}`); tickets.push({ticket: value[key], action: mode}); }
          }
          for (const [key, child] of Object.entries(value)) findTickets(child, ["sessions", "session", "messages"].includes(key) ? "messages" : action);
        }
        findTickets(inventory);
        let count = 0, round = 0; const messageIds = new Set();
        const messageSearch = el("input"), messageMode = el("select"), messageList = el("div");
        messageSearch.type = "search"; messageSearch.placeholder = "搜索已读取的会话"; messageSearch.setAttribute("aria-label", "搜索会话");
        messageMode.setAttribute("aria-label", "会话内容范围"); messageMode.add(new Option("用户与助手", "main")); messageMode.add(new Option("含工具与思考", "all"));
        const controls = el("div", undefined, "inline-form"); controls.append(messageSearch, messageMode); fragment.append(controls, messageList);
        function filterMessages() { for (const row of messageList.children) row.hidden = (messageMode.value === "main" && !["user", "assistant"].includes(row.dataset.role)) || !row.textContent.toLowerCase().includes(messageSearch.value.toLowerCase()); }
        messageSearch.addEventListener("input", filterMessages); messageMode.addEventListener("change", filterMessages);
        function appendMessages(data) {
          visit(data, (message) => {
            const kindRoles = {user_message: "user", assistant_message: "assistant", agent_narration: "assistant", reasoning: "reasoning", system_note: "system", context_message: "system", tool_call: "tool", tool_result: "tool"};
            const payload = message.payload?.data;
            const role = message.role || message.author?.role || kindRoles[message.kind];
            const text = textContent(message.content ?? message.text ?? payload?.text ?? payload?.content ?? payload?.summary);
            if (text && role) {
              const key = message.id || message.message_id;
              if (key && messageIds.has(key)) return false;
              if (key) messageIds.add(key);
              const row = el("article", undefined, "detail-message");
              row.dataset.role = role;
              if (role === "user") round += 1;
              const time = formatDate(message.created_at || message.timestamp || message.timing?.started_at);
              if (role === "user" || time) row.append(el("small", [role === "user" ? `第 ${round} 轮` : "", time].filter(Boolean).join(" · ")));
              row.append(el("strong", ({user: "用户", assistant: "助手", system: "系统", tool: "工具", reasoning: "思考过程"})[role] || "消息"), el("pre", text));
              messageList.append(row); count += 1; return false;
            }
          });
          filterMessages();
        }
        appendMessages(inventory);
        const status = note("会话内容"), more = button("继续读取", loadMore);
        let index = 0, cursor, loading = false, cursorSet = new Set(), complete = true;
        fragment.prepend(status); fragment.append(more);
        async function loadMore() {
          if (loading || index >= tickets.length || controller.signal.aborted) return;
          loading = true; more.disabled = true;
          try {
            const ticket = tickets[index], data = await request("conversation", {...identity, ...ticket, ...(cursor ? {cursor} : {})});
            if (controller.signal.aborted) return;
            const meta = data.meta || data.data?.meta || data;
            const hidden = meta.projection?.mode === "metadata_only" || meta.redaction?.level === "metadata_only";
            if (!hidden) appendMessages(data);
            fragment.append(more); complete = complete && meta.complete === true && !hidden;
            if (meta.has_more) {
              if (!meta.next_cursor || cursorSet.has(meta.next_cursor)) throw new Error("会话分页未能继续，请重新读取。");
              cursor = meta.next_cursor; cursorSet.add(cursor);
            } else { index += 1; cursor = undefined; cursorSet = new Set(); }
            status.textContent = `${count} 条已读取消息${index < tickets.length ? " · 还有更多内容" : " · 已读完可用批次"}${!complete ? " · 完整性未确认" : ""}${hidden ? " · 正文未开放" : ""}`;
            more.hidden = index >= tickets.length;
          } catch (error) { if (error.name !== "AbortError") status.textContent = error.message; }
          finally { loading = false; more.disabled = false; }
        }
        if (tickets.length) await loadMore();
        else { more.hidden = true; status.textContent = count ? `${count} 条可用消息` : "没有可读取的会话正文，可能尚未采集或缺少权限。"; }
      } else if (tab === "trace") {
        const data = await request("trace");
        fragment.append(note(data.execution_complete === true ? "执行记录已完整获取" : "当前执行记录可能不完整", data.execution_complete !== true));
        fragment.append(window.FluxViews.execution(data));
      } else if (tab === "artifacts") {
        const data = await request("artifacts/list"), files = data.files || [];
        const fileIdentity = data.worker_session_id ? {worker_session_id: data.worker_session_id} : identity;
        fragment.append(window.FluxViews.files(files, (path, body, options) => request(path, {...fileIdentity, ...body}, options), controller.signal, (blob) => { const url = URL.createObjectURL(blob); previewUrls.push(url); return url; }));
      } else if (tab === "versions") {
        const data = await request("artifacts/versions"), versions = data.versions || data.items || [], comparisons = data.comparisons || [];
        fragment.append(el("h3", "已采集版本"));
        for (const version of versions) fragment.append(note(`版本 ${version.version_id ?? version.version ?? "未知"}${version.created_at ? ` · ${version.created_at}` : ""}`));
        if (!versions.length) fragment.append(note("没有可列出的版本。"));
        const form = el("form", undefined, "inline-form"), from = el("select"), to = el("select"), kind = el("select"), output = el("div");
        kind.add(new Option("文件版本", "version_id")); kind.add(new Option("会话版本", "conversation_version")); kind.setAttribute("aria-label", "比较类型");
        for (const [input, label] of [[from, "起始版本"], [to, "结束版本"]]) input.setAttribute("aria-label", label);
        const advanced = el("details"), manualFrom = el("input"), manualTo = el("input"); advanced.append(el("summary", "指定其他版本编号"));
        for (const [input, label] of [[manualFrom, "起始编号"], [manualTo, "结束编号"]]) { input.type = "number"; input.min = "0"; input.step = "1"; input.placeholder = label; input.setAttribute("aria-label", label); advanced.append(input); }
        function fillVersions() {
          const key = kind.value, ids = new Set([0]);
          for (const version of versions) { const value = version[key] ?? (key === "version_id" ? version.version : undefined); if (Number.isSafeInteger(value)) ids.add(value); }
          for (const comparison of comparisons) for (const prefix of ["from_", "to_"]) if (Number.isSafeInteger(comparison[prefix + key])) ids.add(comparison[prefix + key]);
          const choices = [...ids].sort((a, b) => a - b);
          for (const select of [from, to]) { select.replaceChildren(); for (const id of choices) select.add(new Option(id === 0 ? "初始状态" : `版本 ${id}`, String(id))); }
          from.value = String(choices.length > 1 ? choices[choices.length - 2] : 0); to.value = String(choices.at(-1));
        }
        kind.addEventListener("change", fillVersions); fillVersions();
        const submit = el("button", "比较版本", "primary-button"); submit.type = "submit";
        form.append(kind, from, to, submit, advanced); fragment.append(form, output);
        for (const comparison of comparisons) {
          const lower = comparison.from_version_id ?? comparison.from_conversation_version, upper = comparison.to_version_id ?? comparison.to_conversation_version;
          if (lower == null || upper == null) continue;
          fragment.insertBefore(button(`版本 ${lower} → ${upper}`, () => { kind.value = comparison.from_version_id != null ? "version_id" : "conversation_version"; fillVersions(); advanced.open = false; from.value = lower; to.value = upper; form.requestSubmit(); }), form);
        }
        form.addEventListener("submit", async (event) => {
          event.preventDefault(); const lower = Number(advanced.open ? manualFrom.value : from.value), upper = Number(advanced.open ? manualTo.value : to.value);
          if (advanced.open && (!manualFrom.value || !manualTo.value)) { output.replaceChildren(note("请填写两个版本编号。", true)); return; }
          if (![lower, upper].every(Number.isSafeInteger) || lower < 0 || lower > upper) { output.replaceChildren(note("请选择有效的版本区间。", true)); return; }
          submit.disabled = true; output.replaceChildren(note("正在比较…"));
          try {
            const diff = await request("artifacts/version-diff", {...identity, [`from_${kind.value}`]: lower, [`to_${kind.value}`]: upper});
            if (controller.signal.aborted) return;
            output.replaceChildren(); let count = 0;
            visit(diff, (entry) => { const text = entry.diff || entry.patch || entry.content; if (typeof text === "string") { output.append(el("h3", entry.path || entry.file_path || "版本变更"), el("pre", text, "detail-code")); count += 1; return false; } });
            if (!count) output.append(note("此区间没有可展示的已采集变更。"));
          } catch (error) { if (error.name !== "AbortError") output.replaceChildren(note(error.message, true)); }
          finally { submit.disabled = false; }
        });
      } else if (tab === "gui") {
        const data = await request("artifacts/gui-test/case-result"), items = data.items || data.cases || [];
        fragment.append(el("h3", "测试结果"));
        if (!items.length) fragment.append(note("尚未采集到测试报告。"));
        for (const [index, item] of items.entries()) {
          const result = item.case_result || item, section = el("section", undefined, "detail-message");
          section.append(el("h3", result.name || result.title || item.case_name || `用例 ${index + 1}`));
          section.append(note(statuses[result.status || result.result] || "结果待确认"));
          const description = textContent(result.message || result.description || result.error);
          if (description) section.append(note(description));
          const screenshot = result.screenshot || result.screenshot_path;
          if (typeof screenshot === "string" && screenshot) section.append(button("查看截图", async (event) => {
            const b = event.currentTarget; b.disabled = true;
            try {
              const path = screenshot.split("?")[0].split("/").at(-1), blob = await request("artifacts/gui-test/screenshots", {...identity, path}, {binary: true});
              if (controller.signal.aborted) return;
              const url = URL.createObjectURL(blob); previewUrls.push(url);
              const image = el("img"); image.src = url; image.alt = result.name || `用例 ${index + 1} 截图`; image.style.maxWidth = "100%";
              section.append(image, button("下载截图", () => api.save(blob, path))); b.hidden = true;
            } catch (error) { if (error.name !== "AbortError") section.append(note(error.message, true)); }
            finally { b.disabled = false; }
          }));
          fragment.append(section);
        }
      }
      if (!controller.signal.aborted) content.replaceChildren(fragment);
    } catch (error) {
      if (controller.signal.aborted) return;
      content.replaceChildren(note(error.message, true), button("重新读取", renderDetail));
    }
  }
  function attachReview(container, taggingId, taskId, exportJobId) {
    const details = el("details"), status = note(""), form = el("form", undefined, "review-form");
    const summary = el("summary", "复核内容"); summary.hidden = true;
    details.append(summary, form, status);
    const dialog = el("dialog", undefined, "settings-dialog review-dialog"), heading = el("header", undefined, "drawer-heading");
    dialog.setAttribute("aria-label", "人工复核");
    const close = button("×", () => dialog.close(), "icon-button"); close.setAttribute("aria-label", "关闭人工复核");
    const transcript = window.FluxTranscript?.create(exportJobId, taskId), layout = el("div", undefined, "review-layout");
    if (transcript) layout.append(transcript.element);
    layout.append(details);
    heading.append(el("h2", `复核任务 ${taskId}`), close); dialog.append(heading, layout);
    container.append(button("人工复核与标签", () => { document.body.append(dialog); dialog.showModal(); details.open = true; transcript?.load(); }));
    dialog.addEventListener("close", () => { transcript?.close(); dialog.remove(); });
    let loaded = false, snapshot, etag, busy = false;
    const path = `v1/tagging/${taggingId}/task/${taskId}`;
    details.addEventListener("toggle", async () => {
      if (!details.open || loaded || busy) return;
      busy = true; status.textContent = "正在读取标签…";
      try {
        const [response, schema] = await Promise.all([api.request(path, undefined, {response: true}), api.request("v1/tagging/tag-schema")]);
        snapshot = await response.json(); etag = response.headers.get("ETag");
        if (!etag) throw new Error("没有获取到标签版本，请重新打开。");
        const supported = schema.schema?.properties || {};
        form.replaceChildren();
        function field(label, input) { const wrapper = el("label", label); input.setAttribute("aria-label", label); wrapper.append(input); form.append(wrapper); return input; }
        const decision = el("select");
        for (const [key, label] of [["", "不添加复核意见"], ["keep", "保留"], ["hold", "待复核"], ["exclude", "排除"]]) decision.add(new Option(label, key));
        decision.value = snapshot.review?.decision || ""; decision.disabled = !supported.review;
        field("复核结论", decision);
        const reason = field("复核原因", el("textarea")); reason.value = snapshot.review?.reason || ""; reason.maxLength = 4000;
        const category = field("任务分类", el("input")); category.value = snapshot.task_classification?.category || ""; category.maxLength = 256; category.disabled = !supported.task_classification;
        const subcategory = field("细分类别", el("input")); subcategory.value = snapshot.task_classification?.subcategory || ""; subcategory.maxLength = 256; subcategory.disabled = !supported.task_classification;
        const repoDetails = el("details"), repoRows = el("div"), repositoryCopies = [];
        repoDetails.append(el("summary", "关联仓库"), repoRows);
        const purposes = {target_code: "目标代码", reference_code: "参考代码", skill_knowledge: "知识资料", artifact_output: "交付产物", execution_context: "运行环境", unknown: "用途待确认"};
        function repoRow(repo = {name: "", categories: ["user_specified"], purposes: ["target_code"], states: ["mentioned"], association: "candidate"}) {
          const copy = structuredClone(repo), row = el("div", undefined, "review-form"), name = el("input");
          name.value = copy.name; name.placeholder = "组织/仓库"; name.setAttribute("aria-label", "仓库名称"); name.maxLength = 256;
          const selectedPurposes = new Set(copy.purposes), group = el("fieldset"); group.append(el("legend", "仓库用途"));
          for (const [key, text] of Object.entries(purposes)) {
            const label = el("label", text), check = el("input"); check.type = "checkbox"; check.checked = selectedPurposes.has(key);
            check.addEventListener("change", () => { check.checked ? selectedPurposes.add(key) : selectedPurposes.delete(key); }); label.prepend(check); group.append(label);
          }
          const entry = {copy, name, selectedPurposes, deleted: false}; repositoryCopies.push(entry);
          row.append(name, group, button("移除仓库", () => { entry.deleted = true; row.remove(); }, "text-button")); repoRows.append(row);
        }
        for (const repo of snapshot.repository?.repositories || []) repoRow(repo);
        if (supported.repository) repoDetails.append(button("添加仓库", () => repoRow()));
        form.append(repoDetails);
        const save = el("button", "保存复核", "primary-button"); save.type = "submit"; form.append(save);
        form.onsubmit = async (event) => {
          event.preventDefault(); if (save.disabled) return;
          const next = structuredClone(snapshot);
          if (decision.value && !reason.value.trim()) { status.textContent = "请填写复核原因。"; reason.focus(); return; }
          if (supported.review && (decision.value !== (snapshot.review?.decision || "") || reason.value.trim() !== (snapshot.review?.reason || ""))) {
            if (decision.value) next.review = {...snapshot.review, source: "human", decision: decision.value, reason: reason.value.trim()}; else delete next.review;
          }
          if (supported.task_classification && (category.value.trim() !== (snapshot.task_classification?.category || "") || subcategory.value.trim() !== (snapshot.task_classification?.subcategory || ""))) {
            if (category.value.trim()) {
              next.task_classification = {...snapshot.task_classification, source: "human", category: category.value.trim()};
              if (subcategory.value.trim()) next.task_classification.subcategory = subcategory.value.trim(); else delete next.task_classification.subcategory;
            } else delete next.task_classification;
          }
          if (supported.repository) {
            const repositories = repositoryCopies.filter((entry) => !entry.deleted).map((entry) => ({...entry.copy, name: entry.name.value.trim(), purposes: [...entry.selectedPurposes]}));
            if (repositories.some((repo) => !repo.name || !repo.purposes.length)) { status.textContent = "请填写仓库名称，并至少选择一种用途。"; return; }
            if (JSON.stringify(repositories) !== JSON.stringify(snapshot.repository?.repositories || [])) next.repository = {...snapshot.repository, source: "human", repositories};
          }
          save.disabled = true; status.textContent = "正在保存…";
          try {
            const updated = await api.request(path, next, {headers: {"If-Match": etag}, response: true});
            snapshot = await updated.json(); etag = updated.headers.get("ETag");
            status.textContent = "复核已保存。自动筛选结论保持独立。";
            document.dispatchEvent(new CustomEvent("flux-review-saved", {detail: {taggingId, taskId, tags: snapshot}}));
          } catch (error) {
            status.textContent = error.message;
            if (error.status === 412) {
              status.append(button("重新读取标签", () => { loaded = false; details.open = false; form.replaceChildren(); setTimeout(() => { details.open = true; }, 0); }));
              return;
            }
          } finally { save.disabled = false; }
        };
        loaded = true; status.textContent = "";
      } catch (error) { status.textContent = error.message; }
      finally { busy = false; }
    });
  }
  window.FluxWorkspace = {openTask, selectedIds: () => [...selected], remember, refreshHistory, types, attachReview};
  const initialBatch = new URLSearchParams(location.hash.split("?")[1] || "").get("batch");
  renderHistory(); navigate(location.hash.slice(1)); window.FluxUI.refresh();
  if (initialBatch && location.hash.startsWith("#exports")) window.FluxUI.resumeExport(initialBatch);
})();
