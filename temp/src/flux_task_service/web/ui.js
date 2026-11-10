(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const config = JSON.parse($("ui-config")?.textContent || "{}");
  // Status and mode values verified against the Flux Insight web client.
  const choices = {
    task_status: [
      ["queueing", "排队中"], ["running", "运行中"], ["input-required", "等待输入"],
      ["committed", "已提交"], ["finished", "已完成"], ["interrupted", "已中断"],
      ["failed", "失败"], ["rolled-back", "已回退"],
    ],
    work_mode: [["general", "默认模式"], ["coding", "代码编写"], ["data_analysis", "数据分析"], ["requirement", "需求创作"]],
    task_is_completed: [["0", "未完成"], ["1", "已完成"]],
    level_1_category: ["快速问答", "调研", "需求创作", "代码工程", "测试", "研发流程", "监控运维", "数据分析", "其它"].map((value) => [value, value]),
    level_2_category: ["Flux 能力问答&操作", "信息查询", "信息处理", "产品&市场调研", "技术调研", "灵感脑暴", "原型生成", "PRD 生成", "技术方案生成", "代码开发", "code review", "代码理解", "完整测试", "接口测试", "单元测试", "GUI 测试", "开发集成", "线上发布", "监控分析", "运维操作", "数据分析", "数据打标", "无效", "未分类"].map((value) => [value, value]),
    level_3_category: ["需求开发", "代码重构", "性能优化", "项目开发", "单测代码生成", "其它"].map((value) => [value, value]),
  };
  const fields = [
    {key: "task_created_at", label: "任务创建时间", type: "date", indexed: true},
    {key: "created_by", label: "用户（任务归属人）", type: "manual", indexed: true},
    {key: "task_status", label: "任务状态", type: "fixed", indexed: true},
    {key: "work_mode", label: "工作模式", type: "fixed"},
    {key: "source_platform", label: "来源平台（待配置）", type: "placeholder"},
    {key: "task_is_completed", label: "是否完成", type: "fixed", indexed: true},
    {key: "level_1_category", label: "一级分类", type: "fixed"},
    {key: "level_2_category", label: "二级分类", type: "fixed"},
    {key: "level_3_category", label: "三级分类", type: "fixed"},
    {key: "worker_session_id", label: "Worker 会话 ID", type: "manual", indexed: true},
    {key: "task_id", label: "Task ID", type: "manual"},
    {key: "task_updated_at", label: "任务更新时间", type: "date"},
    {key: "updated_at", label: "记录更新时间", type: "date", indexed: true},
  ];
  const state = {rows: [], revision: 0, resultRevision: -1, ids: [], querying: false, queryController: null};
  let pagination = null;
  let sequence = 0;
  let toastTimer;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, action) {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 2600);
  }

  function beijingNow() {
    const parts = new Intl.DateTimeFormat("sv-SE", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
  }

  function apiTime(value) {
    if (!value) return "";
    const formatted = value.replace("T", " ");
    return formatted.length === 16 ? `${formatted}:00` : formatted;
  }

  const DAY = 86400000;
  const beijingValue = (time) => new Date(time + 8 * 3600000).toISOString().slice(0, 19);
  const beijingTimestamp = (value) => value ? Date.parse(`${value}+08:00`) : NaN;
  const latestAvailableTime = () => Date.parse(`${beijingNow().slice(0, 10)}T00:00:00+08:00`) - 1000;

  function markChanged() {
    state.revision += 1;
    $("query-error").hidden = true;
    if (state.resultRevision >= 0 && state.resultRevision !== state.revision) {
      $("result-badge").textContent = "条件已修改";
      $("result-badge").className = "badge stale";
      $("result-message").textContent = "条件已修改，请重新查询。";
    }
    updateExportButtons();
    updatePagination();
    try {
      const filters = buildFilters();
      const includes = filters.filter_fields.length + (filters.category_path?.length ? 1 : 0) + (filters.task_ids?.length ? 1 : 0);
      const excludes = (filters.exclude_fields?.length || 0) + (filters.exclude_category_paths?.length || 0) + (filters.exclude_task_ids?.length ? 1 : 0);
      $("condition-summary").textContent = `${includes} 项包含 · ${excludes} 项排除`;
    } catch {
      $("condition-summary").textContent = "请设置至少一个包含的查询范围";
    }
  }

  function buildFilters() {
    const filters = {query_and_or: "and", filter_fields: [], exclude_fields: [], exclude_category_paths: [], exclude_task_ids: []};
    const seen = new Set();
    let hasIndex = false;
    for (const row of state.rows) {
      const field = fields.find((item) => item.key === row.key);
      const excluding = row.mode === "exclude";
      if (field.type === "placeholder") continue;
      let values;
      if (field.type === "date") {
        values = [apiTime(row.from.value), apiTime(row.to.value)];
        if (!values[0] && !values[1]) continue;
        if (values[0] && values[1] && values[0] > values[1]) throw new Error(`${field.label}的开始时间不能晚于结束时间。`);
        if ([row.from.value, row.to.value].some((value) => value && beijingTimestamp(value) > latestAvailableTime())) throw new Error("OpenAPI 按 T+1 提供数据，请选择北京时间昨天或更早的时间。");
      } else {
        values = [...row.values];
        if (!values.length) continue;
        const max = row.key === "task_id" ? 200 : 100;
        if (values.length > max) throw new Error(`${field.label}最多选择 ${max} 个值。`);
      }
      if (row.key === "task_id") {
        if (values.some((value) => !/^[0-9]{1,64}$/.test(value) || /^0+$/.test(value))) throw new Error("Task ID 需为非零数字字符串。");
        if (excluding) filters.exclude_task_ids.push(...values);
        else {
          if (filters.task_ids) throw new Error("包含 Task ID 请合并到同一行，可填写多个 ID。");
          filters.task_ids = values;
        }
        continue;
      }
      const condition = {field_name: row.key, query_type: field.type === "date" ? "range" : "in", values};
      if (excluding) filters.exclude_fields.push(condition);
      else {
        if (seen.has(row.key)) throw new Error(`包含「${field.label}」请合并到同一行，可选择多个值。`);
        seen.add(row.key);
        filters.filter_fields.push(condition);
        hasIndex ||= Boolean(field.indexed);
      }
    }
    if (!hasIndex && !filters.task_ids?.length) throw new Error("先设置一个包含的时间范围、人员、状态、完成标记或 Task ID，再进行查询。");
    if (filters.filter_fields.length + (filters.category_path?.length || 0) > 15) throw new Error("包含条件过多，请减少到 15 项以内。");
    return filters;
  }

  function friendlyError(data, status) {
    const code = data?.error?.code;
    const messages = {
      AUTH_NOT_CONFIGURED: "服务账号凭据尚未配置，请设置服务的 secret 文件或环境变量。",
      IAM_UNAVAILABLE: "暂时无法连接账号认证服务，请检查网络后重试。",
      IAM_AUTH_FAILED: "服务账号认证失败，请检查凭据。",
      UPSTREAM_UNAUTHORIZED: "上游拒绝了服务账号，请检查账号凭据和权限。",
      UPSTREAM_FORBIDDEN: "服务账号没有这次查询所需的权限。",
      UPSTREAM_UNAVAILABLE: "暂时无法连接数据服务，请稍后重试。",
      UPSTREAM_RETRIES_EXHAUSTED: "上游暂时繁忙，重试后仍未成功，请稍后再试。",
      QUERY_LIMIT_EXCEEDED: "查询范围过大，请缩小时间范围或增加包含条件。",
      QUERY_TIMEOUT: "查询超时，请缩小范围后重试。",
      UPSTREAM_PAGINATION_ERROR: "上游分页异常，本次没有返回部分结果，请重试。",
      UPSTREAM_PROTOCOL_ERROR: "上游返回格式异常，请检查服务端接口版本。",
      FILTER_FIELD_UNAVAILABLE: "当前任务响应缺少工作模式，无法准确完成筛选。",
      UPSTREAM_FILTER_MISMATCH: "上游返回了范围外的任务，本次查询已停止。",
      UNAUTHORIZED: "请填写正确的访问密钥后重试。",
      UPSTREAM_REJECTED: "上游不接受这组筛选条件，请检查条件值或字段权限。",
    };
    if (status === 401) $("auth-section").hidden = false;
    return messages[code] || (code === "INVALID_QUERY" ? "筛选条件不符合要求，请检查条件值、时间范围和分类路径。" : `请求失败（${status}），请检查服务状态。`);
  }

  async function apiRequest(path, body, signal) {
    if (window.FluxAPI) return window.FluxAPI.request(path, body, {signal});
    const key = $("api-key").value.trim();
    if (config.api_key_required && !key) throw new Error("请先填写访问密钥。");
    const headers = {"Content-Type": "application/json"};
    if (key) headers.Authorization = `Bearer ${key}`;
    let response;
    try {
      response = await fetch(path, {method: "POST", headers, body: JSON.stringify(body), signal});
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("无法连接服务，请确认服务已启动，并通过服务首页打开页面。");
    }
    let data;
    try { data = await response.json(); }
    catch { throw new Error("服务没有返回有效数据，请重试。"); }
    if (!response.ok) throw new Error(friendlyError(data, response.status));
    return data;
  }

  function addCondition(key = "task_status", initial = false) {
    if (state.rows.length >= 15) { toast("最多添加 15 行条件。"); return; }
    const row = {id: ++sequence, key, mode: "include", values: new Set()};
    row.element = el("div", "condition-row");
    row.element.dataset.conditionId = String(row.id);
    const head = el("div", "condition-head");
    const fieldSelect = el("select", "field-select");
    fieldSelect.setAttribute("aria-label", "筛选字段");
    for (const field of fields) fieldSelect.add(new Option(field.label, field.key));
    fieldSelect.value = key;
    const mode = el("select", "mode-select");
    mode.setAttribute("aria-label", "包含或排除");
    mode.add(new Option("包含", "include"));
    mode.add(new Option("排除", "exclude"));
    mode.addEventListener("change", () => {
      row.mode = mode.value;
      mode.classList.toggle("exclude", row.mode === "exclude");
      markChanged();
    });
    fieldSelect.addEventListener("change", () => {
      row.key = fieldSelect.value;
      row.values.clear();
      renderValue(row);
      markChanged();
    });
    const remove = button("×", "remove-row", () => {
      row.disposeTimeline?.();
      state.rows = state.rows.filter((item) => item !== row);
      row.element.remove();
      markChanged();
    });
    remove.setAttribute("aria-label", "删除这行筛选条件");
    head.append(fieldSelect, mode, remove);
    row.valueElement = el("div", "condition-value");
    row.element.append(head, row.valueElement);
    state.rows.push(row);
    $("condition-list").append(row.element);
    renderValue(row, initial);
    markChanged();
    return row;
  }

  function addUserCondition() {
    const row = state.rows.find((item) => item.key === "created_by" && item.mode === "include") || addCondition("created_by");
    row?.valueElement.querySelector("textarea")?.focus();
  }

  function renderValue(row, initial = false) {
    row.disposeTimeline?.();
    row.valueElement.replaceChildren();
    const type = fields.find((field) => field.key === row.key).type;
    if (type === "date") {
      renderTimeRange(row, initial);
    } else if (type === "fixed") renderFixedPicker(row);
    else if (type === "manual") renderManual(row);
    else row.valueElement.append(el("p", "value-note", "来源平台选项待配置，当前不参与筛选。"));
  }

  function renderTimeRange(row, initial) {
    row.from = el("input"); row.to = el("input");
    for (const input of [row.from, row.to]) {
      input.type = "datetime-local"; input.step = "1";
      input.setAttribute("form", "filter-form");
      input.max = beijingValue(latestAvailableTime());
    }
    row.from.setAttribute("aria-label", "开始时间（北京时间）");
    row.to.setAttribute("aria-label", "结束时间（北京时间）");
    if (initial) {
      row.from.value = beijingValue(latestAvailableTime() - DAY + 1000);
      row.to.value = beijingValue(latestAvailableTime());
    }
    let maxTime = latestAvailableTime(), minTime = maxTime - 3 * DAY + 1000, snap = 3600000;
    const timeline = el("div", "time-range");
    const precise = el("details", "time-precise");
    const selected = el("summary", "time-selected");
    selected.title = "点击精确编辑时间（北京时间）";
    const startLabel = el("span"), endLabel = el("span");
    selected.append(startLabel, el("span", "time-separator", "—"), endLabel);
    const editor = el("div", "time-editor");
    const range = el("div", "date-range");
    range.append(row.from, el("span", "", "—"), row.to);
    editor.append(range, el("p", "small-hint", "北京时间，精确到秒；任意一端留空表示不限制该边界。"));
    precise.append(selected, editor);
    const quickDates = el("div", "quick-dates");
    for (const [label, days] of [["昨天", 1], ["近 7 天", 7], ["近 30 天", 30]]) quickDates.append(button(label, "text-button", () => {
      if (state.querying) return;
      row.from.value = beijingValue(latestAvailableTime() - days * DAY + 1000);
      row.to.value = beijingValue(latestAvailableTime());
      row.from.dispatchEvent(new Event("change"));
    }));
    row.valueElement.append(quickDates);
    const axis = el("div", "time-axis");
    axis.tabIndex = 0;
    axis.setAttribute("role", "group");
    axis.setAttribute("aria-label", "时间轴，滚轮缩放，拖动空白处平移");
    const track = el("div", "time-track"), fill = el("div", "time-fill");
    const ticks = el("div", "time-ticks");
    axis.append(track, fill, ticks);
    const handles = [button("", "time-handle time-handle-start", () => {}), button("", "time-handle time-handle-end", () => {})];
    for (const [index, handle] of handles.entries()) {
      handle.setAttribute("role", "slider");
      handle.setAttribute("aria-label", index === 0 ? "拖动开始时间" : "拖动结束时间");
      handle.setAttribute("aria-orientation", "horizontal");
      axis.append(handle);
    }
    const footer = el("div", "time-footer");
    const rulerLabel = el("span", "time-ruler-label");
    const help = el("div", "time-help");
    help.hidden = true;
    help.textContent = "滚轮：缩放 · Shift + 滚轮：平移\n空白处拖动：平移 · 双击：适配所选范围\n双击轴外端点：跳转到端点（也可聚焦后按 Enter）\n时间轴聚焦后：← → 平移，+ − 缩放，Home 回到昨天视野，F 适配选区，R 选择昨天\n端点聚焦后：← → 微调，Shift 加速，Alt 精确到秒\n手机：单指平移，双指缩放 · 点击上方时间可精确编辑";
    help.id = `time-help-${row.id}`;
    const helpToggle = button(window.matchMedia?.("(pointer: coarse)").matches ? "双指缩放 · 单指平移 ⌨" : "滚轮缩放 · 拖动平移 ⌨", "time-help-toggle", () => {
      help.hidden = !help.hidden; helpToggle.setAttribute("aria-expanded", String(!help.hidden));
    });
    helpToggle.setAttribute("aria-label", "查看时间轴操作和快捷键");
    helpToggle.setAttribute("aria-expanded", "false");
    helpToggle.setAttribute("aria-controls", help.id);
    const expand = button("⤢", "time-expand", openDialog);
    expand.setAttribute("aria-label", "放大时间轴");
    expand.setAttribute("aria-haspopup", "dialog");
    expand.title = "放大时间轴，显示更宽视野";
    footer.append(rulerLabel, helpToggle, expand);
    timeline.append(precise, axis, footer, help);
    const slot = el("div", "time-inline");
    slot.append(timeline); row.valueElement.append(slot);
    const dialog = el("dialog", "time-dialog");
    const heading = el("div", "time-dialog-heading");
    const title = el("h2", "", "选择时间范围");
    title.id = `time-dialog-title-${row.id}`;
    dialog.setAttribute("aria-labelledby", title.id);
    const close = button("×", "time-dialog-close", () => closeDialog());
    close.setAttribute("aria-label", "关闭放大时间轴");
    heading.append(title, close);
    const dialogBody = el("div", "time-dialog-body");
    dialog.append(heading, dialogBody);
    function openDialog() {
      if (state.querying || dialog.open) return;
      precise.open = false; help.hidden = true; helpToggle.setAttribute("aria-expanded", "false");
      const width = axis.clientWidth, span = maxTime - minTime, center = (maxTime + minTime) / 2;
      slot.style.minHeight = `${timeline.getBoundingClientRect().height}px`;
      document.body.append(dialog); dialogBody.append(timeline);
      expand.hidden = true;
      dialog.showModal(); document.body.classList.add("time-modal-open");
      const enlargedSpan = span * axis.clientWidth / Math.max(1, width);
      setView(center - enlargedSpan / 2, enlargedSpan);
      axis.focus();
    }
    function closeDialog(restoreFocus = true) {
      if (!dialog.open) return;
      const width = axis.clientWidth, span = maxTime - minTime, center = (maxTime + minTime) / 2;
      dialog.close(); slot.append(timeline); dialog.remove();
      document.body.classList.remove("time-modal-open"); slot.style.minHeight = "";
      precise.open = false; help.hidden = true; helpToggle.setAttribute("aria-expanded", "false"); expand.hidden = false;
      const inlineSpan = span * axis.clientWidth / Math.max(1, width);
      setView(center - inlineSpan / 2, inlineSpan);
      if (restoreFocus && expand.isConnected) expand.focus();
    }
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDialog(); });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll("button,summary,input,[tabindex]")].filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (!first) return;
      const active = document.activeElement;
      if (!focusable.includes(active) || (event.shiftKey ? active === first : active === last)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      }
    });
    dialog.addEventListener("click", (event) => {
      const rect = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) closeDialog();
    });
    const intervals = [
      [1000, "1 秒"], [2000, "2 秒"], [5000, "5 秒"], [10000, "10 秒"], [15000, "15 秒"], [30000, "30 秒"],
      [60000, "1 分钟"], [5 * 60000, "5 分钟"], [15 * 60000, "15 分钟"], [30 * 60000, "30 分钟"],
      [3600000, "1 小时"], [3 * 3600000, "3 小时"], [6 * 3600000, "6 小时"], [12 * 3600000, "12 小时"],
      [DAY, "1 天"], [2 * DAY, "2 天"], [7 * DAY, "1 周"], [14 * DAY, "2 周"],
      [30 * DAY, "1 个月", 1], [90 * DAY, "3 个月", 3], [365 * DAY, "1 年", 12],
    ];
    const snapSteps = [1000, 5000, 15000, 30000, 60000, 5 * 60000, 15 * 60000, 30 * 60000, 3600000, 3 * 3600000, 6 * 3600000, 12 * 3600000, DAY];
    const boundSpan = (span) => Math.max(Math.floor(Math.max(2, axis.clientWidth / 78) * 1000), Math.min(5 * 365 * DAY, span));
    function setView(start, span) {
      span = boundSpan(span);
      maxTime = Math.min(latestAvailableTime(), start + span);
      minTime = maxTime - span;
      sync();
    }
    function fitSelection() {
      const start = beijingTimestamp(row.from.value), end = beijingTimestamp(row.to.value);
      if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start > latestAvailableTime()) return;
      const length = boundSpan(end - start);
      setView(start - length * .15, length * 1.3);
    }
    function zoom(factor, fraction = .5) {
      const span = maxTime - minTime;
      const anchor = minTime + fraction * span;
      const nextSpan = boundSpan(span * factor);
      setView(anchor - fraction * nextSpan, nextSpan);
    }
    function drawTicks() {
      const span = maxTime - minTime;
      const target = span / Math.max(2, axis.clientWidth / 78);
      const interval = intervals.find(([duration]) => duration >= target) || intervals.at(-1);
      const [duration, label, months] = interval;
      snap = snapSteps.find((step) => step >= duration / 12) || DAY;
      rulerLabel.textContent = `每格 ${label}`;
      axis.dataset.tickInterval = String(duration);
      axis.dataset.viewStart = String(minTime); axis.dataset.viewEnd = String(maxTime);
      ticks.replaceChildren();
      let tickTime;
      if (months) {
        const date = new Date(minTime + 8 * 3600000);
        const index = date.getUTCFullYear() * 12 + date.getUTCMonth();
        const aligned = Math.floor(index / months) * months;
        tickTime = Date.UTC(Math.floor(aligned / 12), aligned % 12, 1) - 8 * 3600000;
      } else {
        const origin = duration >= 7 * DAY ? Date.parse("1970-01-05T00:00:00+08:00") : -8 * 3600000;
        tickTime = origin + Math.ceil((minTime - origin) / duration) * duration;
      }
      for (let count = 0; tickTime <= maxTime && count < 80; count++) {
        if (tickTime >= minTime) {
          const value = beijingValue(tickTime);
          const text = months ? (months === 12 ? `${value.slice(0, 4)}年` : value.slice(0, 7).replace("-", "/")) : duration < 60000 ? value.slice(11, 19) : duration >= DAY || value.slice(11, 16) === "00:00" ? value.slice(5, 10).replace("-", "/") : value.slice(11, 16);
          const tick = el("span", "time-tick", text);
          const fraction = (tickTime - minTime) / span;
          tick.style.left = `${fraction * 100}%`;
          if (fraction < .06 || fraction > .94) {
            tick.style.transform = fraction < .06 ? "none" : "translateX(-100%)";
            tick.style.setProperty("--tick-anchor", fraction < .06 ? "0%" : "100%");
          }
          tick.title = value.replace("T", " "); ticks.append(tick);
        }
        if (months) {
          const date = new Date(tickTime + 8 * 3600000);
          tickTime = Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + months, 1) - 8 * 3600000;
        } else tickTime += duration;
      }
    }
    function sync() {
      drawTicks();
      const values = [beijingTimestamp(row.from.value), beijingTimestamp(row.to.value)];
      const positions = values.map((value, i) => Number.isFinite(value) ? Math.max(0, Math.min(100, (value - minTime) / (maxTime - minTime) * 100)) : i * 100);
      const shortTime = (value) => value ? value.replace("T", " ") : "不限";
      startLabel.textContent = shortTime(row.from.value); endLabel.textContent = shortTime(row.to.value);
      axis.classList.toggle("time-handles-close", Math.abs(positions[1] - positions[0]) * axis.clientWidth / 100 < 28);
      fill.style.left = `${positions[0]}%`; fill.style.width = `${Math.max(0, positions[1] - positions[0])}%`;
      for (const [i, handle] of handles.entries()) {
        handle.style.left = `${positions[i]}%`;
        handle.textContent = values[i] < minTime ? "‹" : values[i] > maxTime ? "›" : "";
        handle.classList.toggle("time-handle-outside", Number.isFinite(values[i]) && (values[i] < minTime || values[i] > maxTime));
        handle.title = `${i === 0 ? "开始" : "结束"}：${shortTime([row.from, row.to][i].value)}${handle.textContent ? "（位于视野外，双击跳转）" : ""}`;
        handle.setAttribute("aria-valuemin", String(Number.isFinite(values[0]) ? Math.min(minTime, values[0]) : minTime));
        handle.setAttribute("aria-valuemax", String(i === 0 && Number.isFinite(values[1]) ? values[1] : latestAvailableTime()));
        handle.setAttribute("aria-valuenow", String(Number.isFinite(values[i]) ? values[i] : i === 0 ? minTime : maxTime));
        handle.setAttribute("aria-valuetext", [row.from, row.to][i].value?.replace("T", " ") || "未设置");
      }
    }

    function move(index, time) {
      if (state.querying || !row.element.isConnected) return;
      let start = beijingTimestamp(row.from.value), end = beijingTimestamp(row.to.value);
      if (!Number.isFinite(start)) start = minTime;
      if (!Number.isFinite(end)) end = maxTime;
      time = Math.min(latestAvailableTime(), time);
      if (index === 0) start = Math.min(time, end);
      else end = Math.max(time, start);
      row.from.value = beijingValue(start); row.to.value = beijingValue(end);
      sync(); markChanged();
    }
    for (const [i, handle] of handles.entries()) {
      function jumpToEndpoint() {
        const time = beijingTimestamp([row.from, row.to][i].value);
        if (!state.querying && Number.isFinite(time) && (time < minTime || time > maxTime)) {
          const span = maxTime - minTime;
          setView(time - span / 2, span);
        }
      }
      handle.addEventListener("dblclick", (event) => { event.stopPropagation(); jumpToEndpoint(); });
      let pointer = null;
      handle.addEventListener("pointerdown", (event) => {
        if (state.querying || (event.pointerType === "mouse" && event.button !== 0)) return;
        event.stopPropagation(); event.preventDefault(); handle.focus(); pointer = event.pointerId; handle.setPointerCapture(pointer);
      });
      handle.addEventListener("pointermove", (event) => {
        if (event.pointerId !== pointer) return;
        const rect = axis.getBoundingClientRect();
        if (!rect.width) return;
        const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        const raw = minTime + fraction * (maxTime - minTime);
        const origin = -8 * 3600000;
        move(i, fraction === 1 ? maxTime : origin + Math.round((raw - origin) / snap) * snap);
      });
      for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) handle.addEventListener(event, () => { pointer = null; });
      handle.addEventListener("keydown", (event) => {
        if (event.ctrlKey || event.metaKey) return;
        if (event.key === "Enter") { event.stopPropagation(); event.preventDefault(); jumpToEndpoint(); return; }
        const current = beijingTimestamp([row.from, row.to][i].value);
        const value = Number.isFinite(current) ? current : i === 0 ? minTime : maxTime;
        const step = event.altKey ? 1000 : snap * (event.shiftKey ? 10 : 1);
        const targets = {ArrowLeft: value - step, ArrowDown: value - step, ArrowRight: value + step, ArrowUp: value + step};
        if (Object.hasOwn(targets, event.key)) { event.stopPropagation(); event.preventDefault(); move(i, targets[event.key]); }
      });
    }
    axis.addEventListener("wheel", (event) => {
      if (state.querying) return;
      event.preventDefault();
      const rect = axis.getBoundingClientRect();
      if (event.shiftKey) setView(minTime + (event.deltaY || event.deltaX) / rect.width * (maxTime - minTime), maxTime - minTime);
      else zoom(Math.exp(Math.max(-500, Math.min(500, event.deltaY)) * .002), Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)));
    }, {passive: false});
    const pointers = new Map();
    let gesture = null;
    function saveGesture() {
      const points = [...pointers.values()];
      const rect = axis.getBoundingClientRect();
      const center = points.reduce((sum, point) => sum + point, 0) / points.length;
      gesture = {start: minTime, span: maxTime - minTime, center, width: rect.width, left: rect.left, distance: points.length > 1 ? Math.abs(points[1] - points[0]) : null};
    }
    axis.addEventListener("pointerdown", (event) => {
      if (state.querying || event.target.closest("button") || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault(); axis.focus();
      pointers.set(event.pointerId, event.clientX); axis.setPointerCapture(event.pointerId); saveGesture();
      axis.classList.add("time-panning");
    });
    axis.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId) || !gesture) return;
      pointers.set(event.pointerId, event.clientX);
      const points = [...pointers.values()];
      const center = points.reduce((sum, point) => sum + point, 0) / points.length;
      if (points.length > 1 && gesture.distance > 4) {
        const distance = Math.max(4, Math.abs(points[1] - points[0]));
        const span = boundSpan(gesture.span * gesture.distance / distance);
        const anchor = gesture.start + (gesture.center - gesture.left) / gesture.width * gesture.span;
        setView(anchor - (center - gesture.left) / gesture.width * span, span);
      } else setView(gesture.start - (center - gesture.center) / gesture.width * gesture.span, gesture.span);
    });
    for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) axis.addEventListener(event, (event) => {
      if (!pointers.delete(event.pointerId)) return;
      if (pointers.size) saveGesture();
      else { gesture = null; axis.classList.remove("time-panning"); }
    });
    axis.addEventListener("dblclick", () => { if (!state.querying) fitSelection(); });
    axis.addEventListener("keydown", (event) => {
      if (state.querying || event.ctrlKey || event.metaKey) return;
      const key = event.key.toLowerCase();
      if (["+", "=", "-", "_", "pageup", "pagedown"].includes(key)) zoom(["+", "=", "pageup"].includes(key) ? .8 : 1.25);
      else if (["arrowleft", "arrowright"].includes(key)) setView(minTime + (key === "arrowleft" ? -1 : 1) * (maxTime - minTime) * (event.shiftKey ? .05 : .2), maxTime - minTime);
      else if (key === "home") setView(latestAvailableTime() - 3 * DAY + 1000, 3 * DAY - 1000);
      else if (key === "f") fitSelection();
      else if (key === "r") {
        row.from.value = beijingValue(latestAvailableTime() - DAY + 1000); row.to.value = beijingValue(latestAvailableTime());
        setView(latestAvailableTime() - 3 * DAY + 1000, 3 * DAY - 1000); markChanged();
      } else if (key === "escape") return;
      else if (key === "?") { help.hidden = !help.hidden; helpToggle.setAttribute("aria-expanded", String(!help.hidden)); }
      else return;
      event.preventDefault();
    });
    timeline.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (dialog.open && !precise.open && help.hidden) closeDialog();
        else { precise.open = false; help.hidden = true; helpToggle.setAttribute("aria-expanded", "false"); axis.focus(); }
        event.preventDefault();
      }
    });
    for (const input of [row.from, row.to]) input.addEventListener("change", () => { fitSelection(); sync(); markChanged(); });
    row.setTimelineDisabled = (disabled) => { for (const control of timeline.querySelectorAll("button,input")) control.disabled = disabled; };
    let observer;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(sync); observer.observe(axis);
    }
    row.disposeTimeline = () => {
      observer?.disconnect(); closeDialog(false); dialog.remove();
      row.disposeTimeline = null; row.setTimelineDisabled = null;
    };
    sync();
  }

  function renderFixedPicker(row) {
    const options = choices[row.key];
    const details = el("details", "picker");
    const summary = el("summary");
    summary.setAttribute("aria-label", fields.find((field) => field.key === row.key).label);
    const menu = el("div", "picker-menu");
    const search = el("input", "picker-search");
    search.type = "search"; search.placeholder = "搜索选项"; search.setAttribute("aria-label", "搜索选项");
    const list = el("div", "picker-options");
    const tags = el("div", "selected-tags");
    function render() {
      summary.textContent = row.values.size ? `已选择 ${row.values.size} 项` : "请选择（可多选）";
      summary.classList.toggle("has-selection", row.values.size > 0);
      tags.replaceChildren(); list.replaceChildren();
      for (const [value, labelText] of options) {
        if (row.values.has(value)) {
          const tag = el("span", "value-tag", labelText);
          const remove = button("×", "", () => { row.values.delete(value); render(); markChanged(); });
          remove.setAttribute("aria-label", `移除${labelText}`); tag.append(remove); tags.append(tag);
        }
        if (!labelText.toLowerCase().includes(search.value.trim().toLowerCase())) continue;
        const label = el("label", "option-label");
        const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.value = value; checkbox.checked = row.values.has(value);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) row.values.add(value); else row.values.delete(value);
          // Keep the checkbox focused for keyboard multi-selection.
          summary.textContent = row.values.size ? `已选择 ${row.values.size} 项` : "请选择（可多选）";
          render(); markChanged();
          Array.from(list.querySelectorAll("input")).find((input) => input.value === value)?.focus();
        });
        label.append(checkbox, el("span", "option-text", labelText)); list.append(label);
      }
      if (!list.children.length) list.append(el("div", "picker-empty", "没有匹配的选项"));
    }
    const bottom = el("div", "picker-bottom");
    bottom.append(el("span", "", "支持多选"), button("清空", "mini-button", () => { row.values.clear(); render(); markChanged(); }));
    search.addEventListener("input", render);
    menu.append(search, list, bottom); details.append(summary, menu);
    row.valueElement.append(details, tags);
    render();
  }

  function renderManual(row) {
    const field = fields.find((item) => item.key === row.key);
    const input = el("textarea", "manual-values");
    input.rows = 2; input.setAttribute("aria-label", `填写${field.label}`);
    input.placeholder = row.key === "created_by" ? "填写任务归属账号，例如 zhangsan；多个账号用逗号、空格或换行分隔" : "填写 ID，多个 ID 用逗号、空格或换行分隔";
    const tags = el("div", "selected-tags");
    function renderTags() {
      tags.replaceChildren();
      for (const value of row.values) {
        const tag = el("span", "value-tag", value);
        const remove = button("×", "", () => {
          row.values.delete(value); input.value = [...row.values].join("\n"); renderTags(); markChanged();
        });
        remove.setAttribute("aria-label", `移除${value}`); tag.append(remove); tags.append(tag);
      }
    }
    input.addEventListener("input", () => {
      row.values = new Set(input.value.split(/[,，;；\s]+/u).map((value) => value.trim()).filter(Boolean));
      renderTags(); markChanged();
    });
    const clear = button("清空", "mini-button", () => { row.values.clear(); input.value = ""; renderTags(); markChanged(); });
    const note = row.key === "created_by"
      ? "按任务归属账号匹配，不提供姓名搜索。最多 100 个账号，自动去重；包含任一账号，或切换为排除。与时间等其他条件同时生效。"
      : "可填写多个值，自动去重；填写后即可查询。";
    row.valueElement.append(input, tags, clear, el("p", "value-note", note));
  }

  let exportJob = null;
  let exportBusy = false;
  let exportSubmitting = false;
  let exportKey = "";
  let exportSnapshot = null;
  let exportSelection = [];
  let exportComponents = [];
  let exportPage = 0;
  let exportTimer;
  let exportPolling = false;
  let exportGeneration = 0, exportPollController, exportRowsController, exportRowsTotal = 0, exportSearchTimer;
  const exportRowCache = new Map();
  const componentLabels = {task: "任务详情", trace: "Trace", metrics: "指标", artifacts: "产物文件", versions: "已采集版本", gui: "GUI 报告", conversation: "对话原文", "artifacts/manifest": "产物清单", "conversation/inventory": "对话索引", export: "导出文件"};
  const exportStatusLabels = {ok: "已获取", partial: "部分可用", error: "获取失败", failed: "拉取失败", running: "正在拉取", queued: "等待开始", cancelled: "已取消", interrupted: "已中断", not_started: "未拉取"};
  const terminalExportStatuses = new Set(["completed", "partial", "failed", "cancelled", "interrupted"]);

  function updateExportButtons() {
    const enabled = !state.querying && state.ids.length > 0 && state.resultRevision === state.revision;
    $("copy").disabled = !enabled; $("download").disabled = !enabled;
    const scope = $("data-export-scope").value;
    const manual = scope === "manual" && Boolean($("manual-task-ids")?.value.trim());
    const selected = scope === "selected" && enabled && Boolean(window.FluxWorkspace?.selectedIds().length);
    const all = scope === "all" && !state.querying && pagination && state.resultRevision === state.revision;
    $("data-export-start").disabled = !(scope === "manual" ? manual : scope === "selected" ? selected : scope === "all" ? all : enabled) || exportBusy || exportSubmitting;
    if ($("collect-all")) $("collect-all").disabled = state.querying || !pagination || state.resultRevision !== state.revision || !pagination.pages.at(-1).has_more;
    document.dispatchEvent(new CustomEvent("flux-query-state", {detail: {enabled}}));
    $("data-export-scope").disabled = exportBusy;
    for (const input of $("data-export-components").querySelectorAll("input")) input.disabled = exportBusy;
    const stopped = exportSnapshot && terminalExportStatuses.has(exportSnapshot.status);
    $("data-export-refetch").hidden = !stopped;
    $("data-export-refetch").disabled = exportBusy || exportSubmitting || !stopped;
    $("data-export-refetch-note").hidden = !stopped;
    for (const button of document.querySelectorAll(".export-task-refetch")) {
      button.hidden = !stopped; button.disabled = exportBusy || !stopped;
    }
  }

  function updatePagination() {
    const current = pagination && state.resultRevision === state.revision;
    $("pagination").hidden = !pagination;
    $("previous-page").disabled = state.querying || !current || pagination.index === 0;
    $("next-page").disabled = state.querying || !current || !pagination.pages[pagination.index].has_more;
    if (pagination) {
      const page = pagination.pages[pagination.index];
      $("page-info").textContent = `第 ${pagination.index + 1} 页 · 本页 ${page.task_ids.length} 个任务 · 已加载 ${pagination.pages.length} 页`;
    }
  }

  function setQuerying(value) {
    state.querying = value;
    for (const row of state.rows) row.setTimelineDisabled?.(value);
    $("filter-controls").disabled = value; $("reset").disabled = value;
    $("page-size").disabled = value;
    $("query").disabled = value; $("query").classList.toggle("loading", value);
    $("query-label").textContent = value ? "正在查询…" : "查询任务";
    $("cancel").hidden = !value; updateExportButtons(); updatePagination();
  }

  function renderPage() {
    const page = pagination.pages[pagination.index];
    $("result-count").textContent = state.ids.length.toLocaleString();
    const complete = !pagination.pages.at(-1).has_more;
    $("result-badge").textContent = complete ? "已加载全部" : "已加载部分";
    $("result-badge").className = complete ? "badge success" : "badge";
    $("result-stats").hidden = false;
    $("scanned-stat").textContent = `本页扫描 ${page.scanned_count.toLocaleString()} 条记录`;
    $("excluded-stat").textContent = `本页排除 ${page.excluded_count || 0} 个任务`;
    $("empty-state").hidden = page.task_ids.length > 0;
    $("result-content").hidden = !page.task_ids.length;
    $("task-ids").value = page.task_ids.join("\n");
    document.dispatchEvent(new CustomEvent("flux-task-page", {detail: {ids: page.task_ids, allIds: state.ids}}));
    if (!page.task_ids.length) {
      $("empty-state").querySelector("h3").textContent = "本页没有匹配的任务";
      $("empty-state").querySelector("p").textContent = page.has_more ? "可继续查看下一页。" : "";
    }
    $("result-message").textContent = complete ? "" : "仅复制或下载已加载的任务。";
    updateExportButtons(); updatePagination();
  }

  function validatePage(data, cursor, seen) {
    if (!Array.isArray(data.task_ids) || data.task_ids.some((id) => typeof id !== "string" || !/^[0-9]{1,64}$/.test(id) || /^0+$/.test(id)) || typeof data.has_more !== "boolean" || !Number.isInteger(data.scanned_count) || data.scanned_count < 0) {
      throw new Error("服务返回的任务分页格式不正确。");
    }
    if (data.has_more && (typeof data.next_cursor !== "string" || !data.next_cursor || data.next_cursor === cursor || seen.has(data.next_cursor))) {
      throw new Error("分页游标缺失或重复，请重新查询。");
    }
  }

  async function loadPage(first = false) {
    if (state.querying) return;
    let filters, limit, cursor = null;
    const revision = state.revision;
    if (first) {
      try { filters = buildFilters(); }
      catch (error) { $("query-error").textContent = error.message; $("query-error").hidden = false; return; }
      limit = Number($("page-size").value);
    } else {
      if (!pagination || state.resultRevision !== revision) return;
      if (pagination.index < pagination.pages.length - 1) {
        pagination.index += 1; $("query-error").hidden = true; renderPage(); return;
      }
      const last = pagination.pages.at(-1);
      if (!last.has_more) return;
      filters = pagination.filters; limit = pagination.limit; cursor = last.next_cursor;
    }
    const controller = new AbortController(); state.queryController = controller;
    $("query-error").hidden = true; setQuerying(true);
    try {
      const data = await apiRequest("v1/task-ids/query", {filters, limit, cursor}, controller.signal);
      if (controller.signal.aborted || revision !== state.revision) return;
      validatePage(data, cursor, first ? new Set() : pagination.seenCursors);
      if (first) {
        pagination = {filters, limit, pages: [], index: 0, seenCursors: new Set(), ids: new Set(), startedAt: beijingNow().replace("T", " ")};
      }
      if (data.has_more) pagination.seenCursors.add(data.next_cursor);
      // Deduplicate within each displayed page and across all loaded exports.
      data.task_ids = [...new Set(data.task_ids)];
      pagination.pages.push(data); pagination.index = pagination.pages.length - 1;
      for (const id of data.task_ids) pagination.ids.add(id);
      state.ids = [...pagination.ids]; state.resultRevision = revision;
      renderPage();
    } catch (error) {
      if (error.name === "AbortError") toast("已停止等待，已加载的页面保留，可继续查询。");
      else { $("query-error").textContent = error.message; $("query-error").hidden = false; }
    } finally { state.queryController = null; setQuerying(false); }
  }

  async function queryTasks(event) {
    event.preventDefault();
    await loadPage(true);
  }

  function previousPage() {
    if (state.querying || !pagination || state.resultRevision !== state.revision || pagination.index === 0) return;
    pagination.index -= 1; $("query-error").hidden = true; renderPage();
  }

  function reset() {
    for (const row of state.rows) row.disposeTimeline?.();
    state.rows = []; state.ids = []; state.resultRevision = -1; pagination = null;
    $("condition-list").replaceChildren();
    $("result-count").textContent = "—"; $("result-stats").hidden = true;
    $("result-content").hidden = true; $("empty-state").hidden = false;
    $("empty-state").querySelector("h3").textContent = "暂无结果";
    $("empty-state").querySelector("p").textContent = "";
    $("result-badge").textContent = ""; $("result-badge").className = "badge";
    $("result-message").textContent = "";
    $("task-ids").value = "";
    document.dispatchEvent(new CustomEvent("flux-task-page", {detail: {ids: [], allIds: []}}));
    addCondition("task_created_at", true);
  }

  async function copyIds() {
    if ($("copy").disabled) return;
    try { await navigator.clipboard.writeText(state.ids.join("\n")); toast(`已复制 ${state.ids.length} 个 Task ID。`); }
    catch { toast("复制失败，请使用「下载已加载」导出所有已加载 ID。"); }
  }

  function downloadIds() {
    if ($("download").disabled) return;
    const format = $("export-format").value;
    const content = format === "json" ? JSON.stringify(state.ids, null, 2) : format === "csv" ? `\uFEFFtask_id\n${state.ids.join("\n")}\n` : `${state.ids.join("\n")}\n`;
    const type = format === "json" ? "application/json" : format === "csv" ? "text/csv" : "text/plain";
    const url = URL.createObjectURL(new Blob([content], {type: `${type};charset=utf-8`}));
    const link = el("a"); link.href = url; link.download = `flux_task_ids_${pagination.pages.at(-1).has_more ? "partial" : "all"}_${beijingNow().slice(0, 10)}.${format}`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已下载 ${state.ids.length} 个 Task ID。`);
  }

  async function exportRequest(path, body, binary = false, options = {}) {
    if (window.FluxAPI) return window.FluxAPI.request(path, body, {binary, ...options});
    const headers = {};
    if (exportKey) headers.Authorization = `Bearer ${exportKey}`;
    const init = {...options, headers};
    if (body !== undefined) { init.method = "POST"; headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const response = await fetch(path, init);
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error?.message || `请求失败 (${response.status})`);
    }
    return binary ? response.blob() : response.json();
  }

  function exportBadge(status) {
    const known = Object.hasOwn(exportStatusLabels, status) ? status : "unknown";
    return el("span", `export-status export-status-${known}`, exportStatusLabels[status] || status || "未知状态");
  }

  function componentIssues(value) {
    const issues = (value.warnings || []).map((message) => ({kind: "warning", message}));
    const errorText = (error) => typeof error === "string" ? error : `${error.message || "获取失败"}${error.code ? ` (${error.code})` : ""}`;
    if (value.error) issues.push({kind: "error", message: errorText(value.error)});
    for (const [target, error] of Object.entries(value.errors || {})) {
      if (error) issues.push({kind: "error", message: `${target}：${errorText(error)}`});
    }
    for (const [source, error] of Object.entries(value.fallback_errors || {})) {
      if (error) issues.push({kind: "warning", message: `来源 ${source} 获取失败，已使用可用来源：${errorText(error)}`});
    }
    if (!issues.length && ["partial", "error", "failed"].includes(value.status)) {
      issues.push({kind: value.status === "partial" ? "warning" : "error", message: value.status === "partial" ? "数据不完整，上游未返回具体原因。" : "获取失败，上游未返回具体原因。"});
    }
    return issues;
  }

  function hasExportIssues(item) {
    return ["partial", "error", "failed", "cancelled", "interrupted", "not_started"].includes(item.status) || Object.values(item.components || {}).some((value) => componentIssues(value).length || ["partial", "error"].includes(value.status));
  }

  function exportEntries(job) {
    const tasks = job.tasks || {};
    const stopped = terminalExportStatuses.has(job.status);
    const missingStatus = stopped ? (job.status === "cancelled" ? "cancelled" : job.status === "interrupted" ? "interrupted" : "not_started") : "queued";
    return [...new Set([...(job.task_counts ? [] : exportSelection), ...Object.keys(tasks)])].map((id) => {
      const item = tasks[id] || {status: missingStatus, components: {}};
      // A persisted running record may remain after the service is interrupted.
      return [id, stopped && item.status === "running" ? {...item, status: job.status === "cancelled" ? "cancelled" : "interrupted"} : item];
    });
  }

  function renderExportRow(taskId, item) {
    const signature = JSON.stringify(item);
    const cached = exportRowCache.get(taskId);
    if (cached?.signature === signature) return cached.node;
    const row = el("tr"); row.dataset.taskId = taskId;
    const id = el("th", "export-task-id"); id.scope = "row";
    const savedBatch = exportJob;
    id.append(button(taskId, "text-button", () => window.FluxWorkspace?.openTask(taskId, savedBatch)));
    const status = el("td"); status.append(exportBadge(item.status));
    const refetch = button("重新获取", "mini-button export-task-refetch", () => retryExport(taskId));
    refetch.setAttribute("aria-label", `重新获取任务 ${taskId}`);
    refetch.hidden = !terminalExportStatuses.has(exportSnapshot?.status);
    refetch.disabled = exportBusy; status.append(refetch);
    const data = el("td", "export-component-cell");
    const components = item.components || {};
    const resolved = exportComponents.filter((name) => ["ok", "partial", "error", "cancelled"].includes(components[name]?.status)).length;
    data.append(el("div", "export-component-count", `${resolved} / ${exportComponents.length} 项已返回`));
    if (item.status === "running" && item.progress?.stage === "artifacts" && Number.isInteger(item.progress.file_count)) {
      const progress = item.progress;
      data.append(el("div", "export-file-progress", `文件 ${progress.completed_count} / ${progress.file_count}${progress.failed_count ? ` · ${progress.failed_count} 个失败` : ""}`));
    } else if (item.status === "running" && item.progress) {
      data.append(el("div", "export-file-progress", stageLabel(item.progress.stage)));
    }
    const available = exportComponents.filter((name) => components[name]?.status === "ok");
    const unavailable = exportComponents.filter((name) => ["error", "partial"].includes(components[name]?.status) || components[`${name}/manifest`]?.status === "error");
    if (unavailable.length) data.append(el("div", "export-outcome", `${available.length ? `${available.map((name) => componentLabels[name]).join("、")}已获取；` : ""}${unavailable.map((name) => componentLabels[name]).join("、")}需检查`));
    const chips = el("div", "export-component-chips");
    for (const name of exportComponents) {
      const value = components[name];
      const prerequisite = components[`${name}/manifest`];
      const chipStatus = value?.status || (prerequisite?.status === "error" ? "error" : ["queued", "running"].includes(item.status) ? "queued" : "not_started");
      const chip = exportBadge(chipStatus);
      chip.textContent = `${componentLabels[name] || name} · ${exportStatusLabels[chipStatus] || chipStatus}`;
      chips.append(chip);
    }
    data.append(chips);
    const detailCell = el("td", "export-detail-cell");
    const previews = el("div", "export-issue-previews");
    for (const [name, value] of Object.entries(components)) {
      const issues = componentIssues(value);
      if (!issues.length) continue;
      const preview = el("div", "export-issue-preview");
      preview.append(el("strong", "", componentLabels[name] || name));
      // Show the first reason for each component directly; all reasons remain in details.
      preview.append(el("p", `export-issue export-issue-${issues[0].kind}`, issues[0].message));
      if (issues.length > 1) preview.append(el("span", "export-source", `另有 ${issues.length - 1} 条原因，展开查看`));
      previews.append(preview);
    }
    detailCell.append(previews);
    const details = el("details", "export-task-details");
    details.open = Boolean(cached?.node.querySelector("details")?.open);
    const values = Object.values(components);
    const issueCount = values.reduce((sum, value) => sum + componentIssues(value).length, 0);
    details.append(el("summary", hasExportIssues(item) ? "export-has-issues" : "", issueCount ? `${issueCount} 条异常 / 告警` : hasExportIssues(item) ? "查看异常详情" : "查看详情"));
    for (const [name, value] of Object.entries(components)) {
      const section = el("div", "export-component-detail");
      const heading = el("div", "export-detail-heading");
      heading.append(el("strong", "", componentLabels[name] || name), exportBadge(value.status));
      section.append(heading);
      if (value.source) section.append(el("p", "export-source", `来源：${({flux_insight_openapi: "OpenAPI", fornax: "Fornax"})[value.source] || value.source}`));
      if (Number.isInteger(value.file_count)) section.append(el("p", "export-source", `文件：已下载 ${value.downloaded_count || 0} / ${value.file_count} 个`));
      for (const issue of componentIssues(value)) section.append(el("p", `export-issue export-issue-${issue.kind}`, issue.message));
      details.append(section);
    }
    if (!values.length) details.append(el("p", "export-source", ["queued", "running"].includes(item.status) ? "等待后台返回数据。" : "拉取已停止，此任务没有返回数据。"));
    detailCell.append(details); row.append(id, status, data, detailCell);
    exportRowCache.set(taskId, {signature, node: row});
    return row;
  }

  function renderExportTable() {
    if (!exportSnapshot) return;
    const search = $("export-task-search").value.trim();
    const filter = $("export-task-filter").value;
    const entries = exportEntries(exportSnapshot).filter(([id, item]) => id.includes(search) && (filter === "all" || (filter === "issues" && hasExportIssues(item)) || (filter === "active" && ["queued", "running"].includes(item.status)) || (filter === "ok" && item.status === "ok" && !hasExportIssues(item))));
    const serverPaging = Boolean(exportSnapshot.task_counts);
    const total = serverPaging ? exportRowsTotal : entries.length;
    const pages = Math.max(1, Math.ceil(total / 50));
    exportPage = Math.min(exportPage, pages - 1);
    const rows = (serverPaging ? entries : entries.slice(exportPage * 50, (exportPage + 1) * 50)).map(([id, item]) => renderExportRow(id, item));
    if (!rows.length) {
      const row = el("tr"); const cell = el("td", "export-table-empty", search || filter !== "all" ? "没有符合条件的任务。" : "正在确定任务范围，任务开始拉取后会逐行显示。" );
      cell.colSpan = 4; row.append(cell); rows.push(row);
    }
    // Reuse unchanged rows so expanded details stay open across polling updates.
    const tbody = $("export-task-rows");
    if (rows.length !== tbody.children.length || rows.some((row, i) => row !== tbody.children[i])) tbody.replaceChildren(...rows);
    $("export-visible-count").textContent = `${total} 个匹配任务`;
    $("export-page-info").textContent = `第 ${exportPage + 1} / ${pages} 页 · 每页 50 个任务`;
    $("export-previous").disabled = exportPage === 0; $("export-next").disabled = exportPage >= pages - 1;
  }

  function stageLabel(stage) {
    return ({waiting: "等待执行", task: "读取任务详情", trace: "读取会话与执行记录", metrics: "计算统计", conversation: "读取会话", artifacts: "读取文件清单", versions: "读取版本", diffs: "读取版本变更", gui: "读取测试报告"})[stage] || "正在获取资料";
  }

  async function loadExportRows() {
    if (!exportSnapshot?.task_counts) { renderExportTable(); return; }
    exportRowsController?.abort(); const controller = new AbortController(); exportRowsController = controller;
    const jobId = exportJob, generation = exportGeneration;
    const params = new URLSearchParams({offset: exportPage * 50, limit: 50, search: $("export-task-search").value.trim(), status: $("export-task-filter").value});
    try {
      const page = await exportRequest(`v1/exports/${jobId}/tasks?${params}`, undefined, false, {signal: controller.signal, timeout: 10000});
      if (controller.signal.aborted || generation !== exportGeneration || jobId !== exportJob) return;
      exportRowsTotal = page.total; exportSnapshot.tasks = page.tasks;
      for (const id of exportRowCache.keys()) if (!Object.hasOwn(page.tasks, id)) exportRowCache.delete(id);
      if (exportPage > 0 && exportPage * 50 >= page.total) { exportPage = Math.max(0, Math.ceil(page.total / 50) - 1); return loadExportRows(); }
      renderExportTable();
    } catch (error) { if (error.name !== "AbortError" && generation === exportGeneration) { $("export-visible-count").textContent = `列表读取失败：${error.message}`; $("data-export-retry").hidden = false; } }
  }

  function renderExport(job) {
    exportSnapshot = job;
    if (job.components) exportComponents = job.components;
    $("export-setup").hidden = true; $("data-export-start").hidden = true;
    $("data-export-title").textContent = "批次资料";
    $("export-content-label").hidden = false;
    $("export-content-label").textContent = exportComponents.map((name) => componentLabels[name] || name).join(" · ");
    document.dispatchEvent(new CustomEvent("flux-export-update", {detail: job}));
    const labels = {queued: "等待开始", running: "正在拉取", packaging: "正在打包", completed: "拉取完成", partial: "部分信息可用", cancelled: "已取消", failed: "拉取失败", interrupted: "服务重启，拉取中断"};
    const total = job.task_count || 0, completed = job.completed_count || 0;
    const percent = total ? Math.min(100, Math.round(completed / total * 100)) : 0;
    $("data-export-status").textContent = `${labels[job.status] || job.status}${job.error ? ` · ${job.error.message}${job.error.code ? ` (${job.error.code})` : ""}` : ""}${terminalExportStatuses.has(job.status) && !job.archive_ready && job.status !== "interrupted" ? " · 正在准备 ZIP…" : ""}`;
    if (job.retry_of) $("data-export-status").textContent += job.retry_of.job_id === job.job_id ? " · 原批次重新获取" : ` · 重新获取自批次 ${job.retry_of.job_id}`;
    if (job.warnings?.length) $("data-export-status").textContent += ` · ${job.warnings.join("；")}`;
    $("export-progress-label").textContent = total ? `${completed} / ${total} 个任务已处理 · ${percent}%` : terminalExportStatuses.has(job.status) ? "没有已确定的待拉取任务" : "正在确定待拉取任务数量…";
    $("export-job-id").textContent = `任务编号 ${job.job_id}`;
    $("export-progress").max = Math.max(1, total);
    if (!total && !terminalExportStatuses.has(job.status)) $("export-progress").removeAttribute("value");
    else $("export-progress").value = completed;
    const entries = exportEntries(job);
    const fileProgress = entries.filter(([, item]) => item.status === "running" && item.progress?.stage === "artifacts" && Number.isInteger(item.progress.file_count)).map(([, item]) => item.progress);
    if (job.file_progress?.file_count) fileProgress.splice(0, fileProgress.length, job.file_progress);
    if (job.status === "running" && fileProgress.length) {
      const done = fileProgress.reduce((sum, item) => sum + item.completed_count, 0);
      const totalFiles = fileProgress.reduce((sum, item) => sum + item.file_count, 0);
      $("data-export-status").textContent += ` · 正在下载文件 ${done} / ${totalFiles}`;
    }
    if (job.status === "running" && !fileProgress.length && job.stages) $("data-export-status").textContent += ` · ${Object.keys(job.stages).map(stageLabel).join("、") || "正在确定任务范围"}`;
    const counts = {ok: 0, running: 0, queued: 0, partial: 0, error: 0, stopped: 0};
    for (const [, item] of entries) {
      if (Object.hasOwn(counts, item.status)) counts[item.status] += 1;
      else counts.stopped += 1;
    }
    counts[terminalExportStatuses.has(job.status) ? "stopped" : "queued"] += Math.max(0, total - entries.length);
    if (job.task_counts) {
      for (const key of Object.keys(counts)) counts[key] = job.task_counts[key] || 0;
      counts.stopped += (job.task_counts.cancelled || 0) + (job.task_counts.interrupted || 0) + (job.task_counts.not_started || 0);
    }
    const summary = [["已获取", counts.ok, "ok"], ["拉取中", counts.running, "running"], [terminalExportStatuses.has(job.status) ? "未拉取" : "等待中", counts.queued, "queued"], ["部分可用", counts.partial, "partial"], ["失败", counts.error, "error"], ["已停止", counts.stopped, "cancelled"]];
    $("export-summary").replaceChildren(...summary.map(([label, count, status]) => {
      const card = el("div", `export-stat export-stat-${status}`); card.append(el("strong", "", String(count)), el("span", "", label)); return card;
    }));
    $("data-export-results").hidden = false;
    renderExportTable();
    $("data-export-download").disabled = !job.archive_ready;
    if (job.archive_ready) document.dispatchEvent(new CustomEvent("flux-export-ready", {detail: {job_id: job.job_id}}));
    $("data-export-cancel").hidden = !["queued", "running"].includes(job.status);
    updateExportButtons();
  }

  async function pollExport() {
    if (exportPolling || !exportJob) return;
    clearTimeout(exportTimer);
    const jobId = exportJob;
    const generation = exportGeneration, controller = new AbortController(); exportPollController = controller;
    exportPolling = true; $("data-export-retry").hidden = true;
    try {
      const job = await exportRequest(`v1/exports/${jobId}/overview`, undefined, false, {signal: controller.signal, timeout: 10000});
      if (jobId !== exportJob || generation !== exportGeneration) return;
      exportBusy = !terminalExportStatuses.has(job.status) || (!job.archive_ready && job.status !== "interrupted");
      renderExport(job.task_counts ? {...job, tasks: exportSnapshot?.tasks || {}} : job);
      await loadExportRows();
      if (generation !== exportGeneration) return;
      if (job.archive_ready || job.status === "interrupted" || (job.status === "failed" && job.error?.code === "EXPORT_WRITE_FAILED")) {
        exportBusy = false; updateExportButtons(); return;
      }
      exportTimer = setTimeout(pollExport, 1500);
    } catch (error) {
      if (generation !== exportGeneration || error.name === "AbortError") return;
      $("data-export-status").textContent = `进度查询失败：${error.message} · 后台拉取可能仍在继续，请重新查询进度。`;
      $("data-export-retry").hidden = false;
      if (error.retryable || error.status >= 500) exportTimer = setTimeout(pollExport, 5000);
    } finally {
      if (generation === exportGeneration) exportPolling = false;
    }
  }

  async function startExport() {
    if ($("data-export-start").disabled) return;
    const components = [...$("data-export-components").querySelectorAll("input:checked")].map((input) => input.value);
    if (!components.length) { toast("请选择至少一种信息。"); return; }
    const scope = $("data-export-scope").value;
    let ids = [...state.ids];
    if (scope === "selected") ids = window.FluxWorkspace?.selectedIds() || [];
    if (scope === "manual") ids = [...new Set($("manual-task-ids").value.trim().split(/[\s,，;；]+/).filter(Boolean))];
    if (scope !== "all" && (!ids.length || ids.length > (config.max_task_ids || 10000) || ids.some((id) => !/^[1-9][0-9]*$/.test(id) || BigInt(id) > 9223372036854775807n))) { toast("请输入有效的任务编号，数量不能超过服务上限。"); return; }
    const body = scope === "all" ? {filters: pagination.filters, components} : {task_ids: ids, components};
    if ($("conversation-mode")) body.conversation_mode = $("conversation-mode").value;
    body.include_raw_spans = Boolean($("include-raw")?.checked);
    body.recover_raw_spans = body.include_raw_spans && Boolean($("recover-raw")?.checked);
    clearTimeout(exportTimer); exportPollController?.abort(); exportRowsController?.abort(); exportGeneration += 1; exportPolling = false; exportJob = null; exportSnapshot = null;
    exportSelection = body.task_ids || []; exportComponents = components; exportPage = 0; exportRowCache.clear();
    $("export-task-search").value = ""; $("export-task-filter").value = "all";
    $("data-export-retry").hidden = true; $("data-export-cancel").hidden = true;
    exportKey = $("api-key").value.trim(); exportBusy = true; updateExportButtons();
    $("data-export-download").disabled = true; $("data-export-results").hidden = true;
    $("data-export-status").textContent = "正在创建后台拉取任务…";
    const generation = exportGeneration; exportSubmitting = true;
    try {
      const job = await exportRequest("v1/exports", body);
      if (generation !== exportGeneration) { window.FluxWorkspace?.refreshHistory(); return; }
      exportJob = job.job_id; renderExport(job); await pollExport();
    } catch (error) {
      if (generation !== exportGeneration) return;
      exportBusy = false; updateExportButtons(); $("data-export-status").textContent = `创建失败：${error.message}`;
    } finally { exportSubmitting = false; updateExportButtons(); }
  }

  async function cancelExport() {
    if (!exportJob) return;
    $("data-export-cancel").disabled = true;
    try { await exportRequest(`v1/exports/${exportJob}/cancel?include_tasks=false`, {}); await pollExport(); }
    catch (error) { toast(`取消失败：${error.message}`); }
    finally { $("data-export-cancel").disabled = false; }
  }

  async function retryExport(taskId = null) {
    if (exportBusy || !exportJob || !terminalExportStatuses.has(exportSnapshot?.status)) return;
    const sourceJob = exportJob, generation = exportGeneration;
    exportBusy = true; updateExportButtons();
    exportKey = $("api-key").value.trim();
    const suffix = taskId === null ? "retry" : `tasks/${encodeURIComponent(taskId)}/retry`;
    try {
      const job = await exportRequest(`v1/exports/${sourceJob}/${suffix}`, {});
      if (generation !== exportGeneration) { window.FluxWorkspace?.refreshHistory(); return; }
      clearTimeout(exportTimer);
      exportJob = job.job_id;
      exportSelection = job.request?.task_ids || exportEntries(exportSnapshot).map(([id]) => id);
      exportComponents = job.components || exportComponents;
      exportPage = 0; exportRowCache.clear();
      $("export-task-search").value = ""; $("export-task-filter").value = "all";
      renderExport(job); await pollExport();
    } catch (error) {
      if (generation !== exportGeneration) return;
      // Keep the current batch visible when the retry request is rejected.
      exportBusy = false; updateExportButtons();
      toast(`重新获取失败：${error.message}`);
    }
  }

  async function downloadExport() {
    if (!exportJob || $("data-export-download").disabled) return;
    const jobId = exportJob;
    $("data-export-download").disabled = true;
    try {
      const content = await exportRequest(`v1/exports/${jobId}/download`, undefined, true);
      const url = URL.createObjectURL(content);
      const link = el("a"); link.href = url; link.download = `flux_export_${jobId}.zip`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { toast(`下载失败：${error.message}`); }
    finally { $("data-export-download").disabled = !exportSnapshot?.archive_ready; }
  }

  async function checkHealth() {
    try {
      const response = await fetch("health/ready");
      if (response.ok) { $("health").textContent = "服务已连接"; $("health").className = "health ready"; }
      else { $("health").textContent = "凭据待配置"; $("health").className = "health warning"; $("connection-note").textContent = "服务已启动，但尚未配置可读取的服务账号凭据。请设置服务的 secret 文件或环境变量。"; $("connection-note").hidden = false; }
    } catch { $("health").textContent = "服务未连接"; $("health").className = "health error"; $("connection-note").textContent = "请先启动服务，再通过 http://127.0.0.1:18080/ 打开页面。"; $("connection-note").hidden = false; }
  }

  $("auth-section").hidden = !config.api_key_required;
  $("api-key").addEventListener("input", markChanged);
  window.FluxUI = {
    refresh: updateExportButtons,
    filters: buildFilters,
    async refreshOptions() {
      const filters = buildFilters(), values = new Map(choices.level_1_category);
      let cursor, pages = 0; const seen = new Set();
      do {
        const result = await apiRequest("v1/filter-options", {field: "category", filters, ...(cursor ? {cursor} : {})});
        for (const option of result.options || []) values.set(option.value, option.label || option.value);
        cursor = result.next_cursor;
        if (cursor && seen.has(cursor)) throw new Error("分类读取未能继续，请重试。");
        seen.add(cursor); pages += 1;
        if (cursor && pages >= 30) throw new Error("分类范围过大，请缩小时间范围后更新。");
      } while (cursor);
      choices.level_1_category = [...values];
      for (const row of state.rows) if (row.key === "level_1_category") renderValue(row);
    },
    async collectAll() {
      if ($("collect-all").disabled) return;
      const revision = state.revision, filters = pagination.filters;
      const controller = new AbortController(); state.queryController = controller; setQuerying(true);
      try {
        const data = await apiRequest("v1/task-ids/collect", {filters, limit: pagination.limit}, controller.signal);
        if (controller.signal.aborted || state.revision !== revision) return;
        if (!Array.isArray(data.task_ids) || data.task_ids.some((id) => typeof id !== "string" || !/^[0-9]{1,64}$/.test(id) || /^0+$/.test(id))) throw new Error("没有收到有效的任务列表。");
        state.ids = [...new Set(data.task_ids)]; pagination.ids = new Set(state.ids);
        pagination.pages = Array.from({length: Math.max(1, Math.ceil(state.ids.length / 50))}, (_, index) => ({task_ids: state.ids.slice(index * 50, index * 50 + 50), scanned_count: index === 0 ? data.scanned_count || 0 : 0, excluded_count: index === 0 ? data.excluded_count || 0 : 0, has_more: (index + 1) * 50 < state.ids.length}));
        pagination.index = 0; renderPage();
      } catch (error) { if (error.name !== "AbortError") toast(error.message); }
      finally { state.queryController = null; setQuerying(false); }
    },
    async resumeExport(id) {
      if (!/^[a-f0-9]{32}$/.test(id)) { toast("请输入有效的批次编号。"); return; }
      clearTimeout(exportTimer); exportPollController?.abort(); exportRowsController?.abort(); exportGeneration += 1; exportPolling = false;
      exportJob = id; exportSnapshot = null; exportSelection = []; exportRowsTotal = 0; exportPage = 0; exportRowCache.clear();
      $("export-task-search").value = ""; $("export-task-filter").value = "all";
      $("data-export-download").disabled = true; $("data-export-results").hidden = true;
      $("data-export-status").textContent = "正在打开批次…";
      exportKey = $("api-key").value.trim();
      await pollExport();
    },
    newExport() {
      if (location.hash.startsWith("#exports")) history.replaceState(null, "", "#exports");
      document.dispatchEvent(new Event("flux-export-new"));
      clearTimeout(exportTimer); exportPollController?.abort(); exportRowsController?.abort(); exportGeneration += 1; exportPolling = false;
      exportJob = null; exportSnapshot = null; exportBusy = false; exportSelection = []; exportRowCache.clear();
      $("data-export-results").hidden = true; $("data-export-status").textContent = "";
      $("export-setup").hidden = false; $("data-export-start").hidden = false; $("export-content-label").hidden = true; $("data-export-title").textContent = "准备任务资料";
      $("data-export-download").disabled = true; $("data-export-cancel").hidden = true; $("data-export-retry").hidden = true;
      updateExportButtons();
    },
  };
  for (const id of ["data-export-scope", "manual-task-ids"]) $(id)?.addEventListener("input", updateExportButtons);
  $("add-condition").addEventListener("click", () => addCondition());
  $("add-user-condition").addEventListener("click", addUserCondition);
  $("reset").addEventListener("click", reset);
  $("filter-form").addEventListener("submit", queryTasks);
  $("previous-page").addEventListener("click", previousPage);
  $("next-page").addEventListener("click", () => loadPage());
  $("page-size").addEventListener("change", markChanged);
  $("cancel").addEventListener("click", () => state.queryController?.abort());
  $("copy").addEventListener("click", copyIds);
  $("download").addEventListener("click", downloadIds);
  $("data-export-start").addEventListener("click", startExport);
  $("data-export-cancel").addEventListener("click", cancelExport);
  $("data-export-download").addEventListener("click", downloadExport);
  $("data-export-retry").addEventListener("click", pollExport);
  $("data-export-refetch").addEventListener("click", () => retryExport());
  for (const [id, event] of [["export-task-search", "input"], ["export-task-filter", "change"]]) $(id).addEventListener(event, () => { exportRowsController?.abort(); exportPage = 0; clearTimeout(exportSearchTimer); if (!exportSnapshot?.task_counts) renderExportTable(); else exportSearchTimer = setTimeout(loadExportRows, event === "input" ? 250 : 0); });
  $("export-previous").addEventListener("click", () => { exportPage = Math.max(0, exportPage - 1); loadExportRows(); });
  $("export-next").addEventListener("click", () => { exportPage += 1; loadExportRows(); });
  document.addEventListener("click", (event) => {
    for (const picker of document.querySelectorAll("details.picker[open],details.time-precise[open]")) if (!picker.contains(event.target)) picker.open = false;
    for (const timeline of document.querySelectorAll(".time-range")) if (!timeline.contains(event.target)) {
      timeline.querySelector(".time-help").hidden = true;
      timeline.querySelector(".time-help-toggle").setAttribute("aria-expanded", "false");
    }
  });
  reset(); checkHealth();
})();
