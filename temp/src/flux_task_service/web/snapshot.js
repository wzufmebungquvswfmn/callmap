(() => {
  "use strict";
  // Adapt saved, manifest-owned documents to the same views as live data.
  // No saved-data failure silently falls back to an upstream request.
  function create(batch, task, signal) {
    const base = `v1/exports/${batch}/tasks/${task}`, cache = new Map();
    const read = (name, offset = 0, limit = 100) => {
      const key = `${name}:${offset}:${limit}`;
      if (!cache.has(key)) cache.set(key, window.FluxAPI.request(`${base}/datasets/${name}?offset=${offset}&limit=${limit}`, undefined, {signal}));
      return cache.get(key);
    };
    async function dataset(name) {
      const value = await read(name);
      if (["not_requested", "unavailable"].includes(value.status)) throw new Error(value.status === "not_requested" ? "此批次未选择这部分资料。" : "这部分资料未成功保存。可切换到实时资料重试。");
      return value;
    }
    async function request(path, body = {}, options = {}) {
      const name = ({detail: "task", metrics: "metrics", trace: "execution", conversation: "conversation", "artifacts/list": "artifacts", "artifacts/raw": "artifacts", "artifacts/versions": "versions", "artifacts/version-diff": "versions", "artifacts/gui-test/case-result": "gui", "artifacts/gui-test/screenshots": "gui"})[path];
      if (!name) throw new Error("这项操作不支持已保存资料。");
      const value = await dataset(name), docs = value.documents;
      const data = docs.find((doc) => doc.reference.role === "data")?.content;
      if (path === "trace") return {execution_complete: value.complete, data, ...(Array.isArray(data) ? {spans: data} : {})};
      if (path === "conversation") {
        const refs = value.files.filter((ref) => ref.format !== "bytes.v1");
        if (body.action === "inventory") return {sessions: refs.flatMap((ref, index) => ref.role === "summary" ? [] : [{ticket: `saved:${index}`}])};
        const offset = Number(String(body.ticket).split(":")[1]);
        if (!Number.isInteger(offset) || offset < 0 || offset >= refs.length) throw new Error("无效的已保存会话页。");
        const page = await read(name, offset, 1);
        return {data: page.documents[0]?.content, meta: {complete: value.complete === true, has_more: false}};
      }
      if (path === "artifacts/list") {
        const manifest = docs.find((doc) => ["manifest", "initial-manifest"].includes(doc.reference.role))?.content || docs[0]?.content || {};
        const refs = new Set(value.files.filter((ref) => ref.role === "artifact").map((ref) => ref.path));
        return {...manifest, files: (manifest.files || []).map((file) => ({...file, saved_path: `artifacts/files/${file.path}`, available: refs.has(`artifacts/files/${file.path}`)}))};
      }
      if (path.endsWith("/raw") || path.endsWith("/screenshots")) {
        const refs = value.files.filter((ref) => ref.format === "bytes.v1");
        const matches = refs.filter((ref) => path.endsWith("/raw") ? ref.path === `artifacts/files/${body.path}` : ref.role === "screenshot" && (value.file_names?.[ref.path] || ref.path.split("/").at(-1)) === body.path);
        if (matches.length !== 1) throw new Error("此文件未保存，或无法唯一匹配。");
        return window.FluxAPI.request(`${base}/file?${new URLSearchParams({path: matches[0].path})}`, undefined, {...options, signal});
      }
      if (path === "artifacts/version-diff") {
        const index = (data?.comparisons || []).findIndex((item) => item.from_version_id === body.from_version_id && item.to_version_id === body.to_version_id && body.from_version_id != null);
        const refs = value.files.filter((ref) => ref.format !== "bytes.v1");
        const offset = refs.findIndex((ref) => ref.path === `diffs/${index}.json`);
        if (index < 0 || offset < 0) throw new Error("此比较区间未保存在批次中，可切换到实时资料查看。");
        return (await read(name, offset, 1)).documents[0].content;
      }
      if (data === undefined) throw new Error("此批次没有可展示的正文。");
      return data;
    }
    return request;
  }
  window.FluxSnapshot = {create};
})();
