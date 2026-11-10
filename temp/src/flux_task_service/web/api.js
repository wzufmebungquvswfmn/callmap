(() => {
  "use strict";
  const messages = {
    UNAUTHORIZED: "访问密钥不正确，请在连接设置中更新。",
    AUTH_NOT_CONFIGURED: "服务账号尚未配置。",
    UPSTREAM_FORBIDDEN: "当前账号没有读取这份资料的权限。",
    CONVERSATION_FORBIDDEN: "当前账号没有读取会话的权限。",
    QUERY_TIMEOUT: "读取超时，请缩小范围后重试。",
    QUERY_LIMIT_EXCEEDED: "任务过多，请缩小时间范围或增加筛选条件。",
    INVALID_QUERY: "请检查填写的编号、时间或选项。",
    TRACE_UNAVAILABLE: "暂时没有可读取的执行记录。",
    METRICS_UNAVAILABLE: "暂时没有可用的统计数据。",
    UPSTREAM_UNAVAILABLE: "数据服务暂时无法连接，请稍后重试。",
    SAVED_DATA_UNAVAILABLE: "这部分资料未保存或无法读取，可切换到实时资料。",
    SAVED_FILE_NOT_FOUND: "此文件未成功保存在批次中。",
    EXPORT_NOT_READY: "此任务仍在获取中，完成后可查看已保存资料。",
    CHATML_UNAVAILABLE: "此任务没有可转换的已保存对话，请检查原资料批次。",
    CHATML_READ_FAILED: "已保存对话读取失败，请重试。",
    INVALID_TASK_IDS: "所选任务编号无效，请重新选择。",
  };
  async function request(path, body, options = {}) {
    const key = options.key ?? document.getElementById("api-key")?.value.trim();
    const headers = {...options.headers};
    if (key) headers.Authorization = `Bearer ${key}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response, timedOut = false;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    else options.signal?.addEventListener("abort", abort, {once: true});
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, options.timeout ?? 120000);
    try {
      response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers,
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(messages[data.error?.code] || ({412: "此任务已被其他人修改，请重新打开后再保存。", 428: "请重新打开任务后再保存。", 404: "没有找到这份资料。", 409: "当前任务仍在处理中，请稍后重试。"})[response.status] || `暂时无法完成操作（${response.status}）。`);
      error.status = response.status; error.code = data.error?.code;
      if (response.status === 401) document.dispatchEvent(new Event("flux-auth-required"));
      throw error;
    }
    if (options.response) return response;
    return await (options.binary ? response.blob() : options.text ? response.text() : response.json());
    } catch (error) {
      if (timedOut) { const timeout = new Error("读取超时，可重试；后台任务仍会继续。"); timeout.retryable = true; throw timeout; }
      if (error.name === "AbortError" || error.status) throw error;
      const network = new Error("连接中断，请检查网络并重试。"); network.retryable = true; throw network;
    } finally {
      clearTimeout(timer); options.signal?.removeEventListener("abort", abort);
    }
  }
  function save(blob, filename) {
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  window.FluxAPI = {request, save};
})();
