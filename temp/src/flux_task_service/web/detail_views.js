(() => {
  "use strict";
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
  const button = (text, action) => { const node = el("button", text, "text-button"); node.type = "button"; node.addEventListener("click", action); return node; };
  const size = (bytes) => typeof bytes !== "number" ? "" : bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
  function files(items, request, signal, imageURL) {
    const root = el("div"), search = el("input"), crumbs = el("nav", null, "file-breadcrumbs"), list = el("div"), count = el("p", null, "detail-note");
    search.type = "search"; search.placeholder = "搜索文件"; search.setAttribute("aria-label", "搜索文件"); crumbs.setAttribute("aria-label", "文件目录");
    let prefix = "", limit = 100;
    const more = button("显示更多文件", () => { limit += 100; render(); });
    function change(next) { prefix = next; limit = 100; search.value = ""; render(); }
    function row(file, searching) {
      const node = el("div", null, "detail-file"), info = el("span", searching ? file.path : file.path.slice(prefix.length));
      info.append(el("small", `${size(file.size_bytes ?? file.size)}${file.available === false ? " · 未保存" : ""}`)); node.append(info);
      const read = () => request("artifacts/raw", {path: file.path}, {binary: true});
      const download = button("下载", async () => {
        download.disabled = true;
        try { const blob = await read(); if (!signal.aborted) window.FluxAPI.save(blob, file.path.split("/").at(-1)); }
        catch (error) { if (!signal.aborted) node.append(el("p", error.message, "detail-warning")); }
        finally { download.disabled = file.available === false; }
      });
      download.disabled = file.available === false;
      const extension = file.path.split(".").at(-1).toLowerCase();
      const raster = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp"}[extension];
      if ((raster || /^(txt|md|json|csv|log|py|js|ts|tsx|jsx|html|css|yaml|yml|toml|sh|go|rs|java|xml|diff|patch)$/.test(extension)) && (file.size ?? file.size_bytes ?? 0) <= 1048576) {
        const preview = button("预览", async () => {
          preview.disabled = true;
          try {
            const blob = await read(); if (signal.aborted) return;
            if (blob.size > 1048576) throw new Error("文件较大，请下载后查看。");
            const detail = el("details", null, "file-preview"); detail.open = true; detail.append(el("summary", "文件预览"));
            if (raster) { const image = el("img"); image.alt = file.path; image.src = imageURL(new Blob([blob], {type: raster})); detail.append(image); }
            else detail.append(el("pre", await blob.text(), "detail-code"));
            if (!signal.aborted) { node.append(detail); preview.hidden = true; }
          } catch (error) { if (!signal.aborted) node.append(el("p", error.message, "detail-warning")); }
          finally { preview.disabled = file.available === false; }
        }); preview.disabled = file.available === false; node.append(preview);
      }
      node.append(download); return node;
    }
    function render() {
      crumbs.replaceChildren(button("全部文件", () => change("")));
      const parts = prefix.split("/").filter(Boolean);
      parts.forEach((part, index) => crumbs.append(el("span", "/"), button(part, () => change(parts.slice(0, index + 1).join("/") + "/"))));
      const term = search.value.trim().toLowerCase(), dirs = new Map(), selected = [];
      for (const file of items) {
        if (term) { if (file.path.toLowerCase().includes(term)) selected.push(file); continue; }
        if (!file.path.startsWith(prefix)) continue;
        const rest = file.path.slice(prefix.length);
        if (rest.includes("/")) { const dir = rest.split("/")[0]; dirs.set(dir, (dirs.get(dir) || 0) + 1); }
        else selected.push(file);
      }
      const entries = [...[...dirs].sort(([a], [b]) => a.localeCompare(b)).map(([name, count]) => ({name, count})), ...selected.sort((a, b) => a.path.localeCompare(b.path))];
      list.replaceChildren(...entries.slice(0, limit).map((entry) => entry.path ? row(entry, Boolean(term)) : button(`▸ ${entry.name} · ${entry.count} 个文件`, () => change(prefix + entry.name + "/"))));
      count.textContent = term ? `${selected.length} 个匹配文件` : `${items.length} 个文件 · 当前目录 ${dirs.size} 个文件夹、${selected.length} 个文件`;
      if (!entries.length) list.append(el("p", term ? "没有匹配的文件。" : "此目录没有文件。", "detail-note"));
      more.hidden = entries.length <= limit;
    }
    search.addEventListener("input", () => { limit = 100; render(); });
    list.className = "file-browser-list"; root.append(search, count, crumbs, list, more); render(); return root;
  }
  function execution(data) {
    const root = el("div", null, "execution-tree"), body = data.data || data;
    const label = (value) => value.title || value.name || value.span_name || value.displayName || ({tool_call: "调用工具", model_call: "模型调用", reasoning: "思考", agent_narration: "过程说明"})[value.kind] || "执行步骤";
    const text = (value) => typeof value === "string" ? value : value && typeof value === "object" ? text(value.text || value.content || value.summary) : "";
    function step(value, depth = 0) {
      const detail = el("details", null, "execution-step"), summary = el("summary", label(value));
      const elapsed = value.timing?.duration_ms ?? value.duration_ms;
      if (typeof elapsed === "number") summary.append(el("small", ` · ${(elapsed / 1000).toFixed(1)} 秒`));
      detail.append(summary);
      const content = text(value.summary) || text(value.payload?.data) || text(value.result?.output);
      if (content) detail.append(el("pre", content, "detail-code"));
      const children = value.children || [];
      let rendered = false;
      detail.addEventListener("toggle", () => {
        if (!detail.open || rendered || depth >= 20) return; rendered = true;
        paged(detail, children, (child) => step(child, depth + 1));
        if (!content && !children.length) detail.append(el("p", "此步骤未提供正文。", "detail-note"));
      });
      return detail;
    }
    function paged(parent, values, render) {
      let offset = 0; const more = button("更多步骤", append);
      function append() { for (const value of values.slice(offset, offset + 100)) parent.insertBefore(render(value), more); offset += 100; more.hidden = offset >= values.length; }
      parent.append(more); append();
    }
    if (Array.isArray(body.sessions) && body.sessions.length) {
      for (const [index, session] of body.sessions.entries()) {
        const group = el("details", null, "execution-session"); group.open = index === 0;
        group.append(el("summary", `${session.role === "primary" ? "主会话" : "子会话"}${session.title ? ` · ${session.title}` : ""}`));
        for (const [turnIndex, turn] of (session.turns || []).entries()) {
          const round = el("details", null, "execution-turn"); round.open = turnIndex === 0 && index === 0;
          round.append(el("summary", `第 ${turnIndex + 1} 轮${turn.title ? ` · ${turn.title}` : ""}`));
          paged(round, turn.steps || [], step);
          const calls = el("details"); calls.append(el("summary", `${(turn.model_calls || []).length} 次模型调用`));
          let loaded = false; calls.addEventListener("toggle", () => { if (calls.open && !loaded) { loaded = true; paged(calls, turn.model_calls || [], (call) => step({...call, title: call.model || "模型调用"})); } });
          round.append(calls); group.append(round);
        }
        root.append(group);
      }
    } else {
      const spans = data.spans || (Array.isArray(body) ? body : []);
      const byId = new Map(spans.map((span) => [span.span_id || span.spanId || span.id, {...span, children: []}]));
      const roots = [];
      for (const span of byId.values()) {
        const parent = byId.get(span.parent_span_id || span.parentSpanId || span.parent_id);
        if (parent && parent !== span) parent.children.push(span); else roots.push(span);
      }
      paged(root, roots.length ? roots : [...byId.values()], step);
      if (!spans.length) root.append(el("p", "当前记录没有可展示的步骤。", "detail-note"));
    }
    return root;
  }
  window.FluxViews = {files, execution};
})();
