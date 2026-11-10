(() => {
  "use strict";
  function create(jobId, taskId) {
    const node = (tag, text) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
    const root = node("section"), controls = node("div"), mode = node("select"), status = node("p"), body = node("pre");
    root.className = "review-transcript"; controls.className = "transcript-controls"; body.className = "transcript-text"; status.className = "detail-note"; status.setAttribute("role", "status");
    mode.setAttribute("aria-label", "对话预览模式");
    for (const [key, label] of [["assistant", "助手回复"], ["user", "用户输入"], ["all", "全部消息"]]) mode.add(new Option(label, key));
    const copy = node("button", "复制"), download = node("button", "下载 TXT"), retry = node("button", "重试");
    for (const button of [copy, download, retry]) { button.type = "button"; button.className = "text-button"; }
    retry.hidden = true; copy.disabled = download.disabled = true;
    controls.append(node("strong", "已保存对话"), mode, copy, download, retry); root.append(controls, status, body);
    let controller, text = "", loadedMode, revision = 0;
    async function load() {
      controller?.abort(); controller = new AbortController(); const signal = controller.signal, version = ++revision, selectedMode = mode.value;
      text = ""; loadedMode = null; body.textContent = ""; copy.disabled = download.disabled = true; retry.hidden = true;
      if (!jobId) { status.textContent = "无法确定原资料批次，请重新打开筛选结果。"; return; }
      status.textContent = "读取对话…";
      try {
        const value = await window.FluxAPI.request(`v1/chatml/${encodeURIComponent(jobId)}/${encodeURIComponent(taskId)}?${new URLSearchParams({mode: selectedMode})}`, undefined, {text: true, signal});
        if (signal.aborted || version !== revision) return;
        text = value; loadedMode = selectedMode; body.textContent = text;
        status.textContent = text.trim() ? "" : "此模式下没有可显示的消息。";
        copy.disabled = download.disabled = !text.trim();
      } catch (error) { if (!signal.aborted && version === revision) { status.textContent = error.message; retry.hidden = false; } }
    }
    mode.addEventListener("change", load); retry.addEventListener("click", load);
    copy.addEventListener("click", async () => {
      const version = revision;
      try { await navigator.clipboard.writeText(text); if (version === revision) status.textContent = "已复制。"; }
      catch { if (version === revision) status.textContent = "复制失败，可下载 TXT。"; }
    });
    download.addEventListener("click", () => { if (loadedMode && text) window.FluxAPI.save(new Blob([text], {type: "text/plain;charset=utf-8"}), `task_${taskId}_${loadedMode}.txt`); });
    return {element: root, load, close: () => { revision += 1; controller?.abort(); }};
  }
  window.FluxTranscript = {create};
})();
