import { $, $$, esc, empty, num, options, pill, table, jsonDetails } from "../ui.js";

export async function query(ctx, target = $("#view"), supplied = null) {
  const data = supplied || (await ctx.api("dataset", ctx.selection()));
  if (!ctx.live()) return;
  const fields = Object.keys(data.schema.fields),
    chosen = ctx.state.queryFields || fields,
    key = `${data.summary.dataset}@${data.summary.frequency}`,
    saved = ctx.state.queryRange?.key === key ? ctx.state.queryRange : null,
    initial = data.summary.latest_local || "",
    start = saved?.start ?? (initial || ctx.scope().start_date || ""),
    end = saved?.end ?? (initial || ctx.scope().end_date || "");
  target.innerHTML = `<section class="${supplied ? "query-workspace" : "panel"}"><form id="query-form"><div class="filter-bar query-controls"><label>标的<input name="ids" aria-label="查询标的" value="${esc(ctx.state.queryIds ?? data.symbols.slice(0, 1).join(","))}" placeholder="代码，逗号分隔" required list="symbol-options"><datalist id="symbol-options">${data.symbols.map((s) => `<option value="${esc(s)}">`).join("")}</datalist></label><label>开始<input id="query-start" name="start" type="date" value="${esc(start)}" required></label><label>结束<input id="query-end" name="end" type="date" value="${esc(end)}" required></label><button class="button button-primary" type="submit">查询</button><button id="csv" type="button" class="button" disabled>导出 CSV</button></div><div class="filter-bar query-options"><details class="query-fields"><summary>字段 <span id="query-field-count">${chosen.length}</span></summary><div class="field-checks">${fields.map((f) => `<label><input type="checkbox" name="fields" value="${esc(f)}" ${chosen.includes(f) ? "checked" : ""}>${esc(f)}</label>`).join("")}</div></details>${data.summary.frequency === "1m" ? '<details class="query-time-filter"><summary>分钟时间</summary><div class="form-row"><label>开始时间（含时区）<input name="start_time" placeholder="ISO8601，含时区偏移"></label><label>结束时间（含时区）<input name="end_time" placeholder="ISO8601，含时区偏移"></label></div></details>' : ""}<label>每页<select name="limit">${options(
    [5, 10, 25, 50, 100, 200].map((n) => [n, n + " 条"]),
    ctx.state.queryLimit || 50,
  )}</select></label><span class="panel-meta filter-end">${esc(data.schema.timezone)} · 日期包含两端</span></div></form><div id="query-result">${empty(data.summary.local ? "选择条件后查询" : "暂无本地行情", data.summary.local ? "" : "可在覆盖页查看可下载日期")}</div></section>`;
  let last = null,
    next = null;
  async function execute(offset = 0) {
    const form = new FormData($("#query-form")),
      ids = String(form.get("ids"))
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      selectedFields = form.getAll("fields"),
      startDate = String(form.get("start") || ""),
      endDate = String(form.get("end") || "");
    if (!ids.length) throw Error("请输入至少一个标的代码。");
    if (!selectedFields.length) throw Error("请至少选择一个查询字段。");
    if (!startDate || !endDate) throw Error("请选择开始和结束日期。");
    if (startDate > endDate) throw Error("开始日期不能晚于结束日期。");
    ctx.state.queryIds = ids.join(",");
    ctx.state.queryFields = selectedFields;
    ctx.state.queryLimit = Number(form.get("limit"));
    ctx.state.queryRange = { key, start: startDate, end: endDate };
    const request = {
      dataset: data.summary.dataset,
      instruments: ids,
      frequency: data.summary.frequency,
      start_date: startDate,
      end_date: endDate,
      fields: selectedFields,
      limit: ctx.state.queryLimit,
      offset,
      ...(form.get("start_time")
        ? { start_time: String(form.get("start_time")) }
        : {}),
      ...(form.get("end_time")
        ? { end_time: String(form.get("end_time")) }
        : {}),
    };
    $("#csv").disabled = true;
    $("#query-result").innerHTML = empty("查询中…");
    let result;
    try {
      result = await ctx.request("/api/query", request);
    } catch (error) {
      if (ctx.live())
        $("#query-result").innerHTML =
          `<div class="callout error-callout">${esc(error.message)}</div>`;
      throw error;
    }
    if (!ctx.live()) return;
    last = request;
    next = result.next_offset;
    const rows = result.records.map((row) => {
      const value = { ...row };
      for (const key of ["trading_time"])
        if (value[key] != null)
          value[key] = new Date(value[key]).toLocaleString("sv-SE", {
            timeZone: data.schema.timezone || "UTC",
            hour12: false,
          });
      return value;
    });
    $("#query-result").innerHTML =
      `<div class="notice-line" id="query-meta"><span>${result.metadata.synthetic ? '<span class="tag synthetic">合成测试数据</span>' : ""} ${esc(startDate)} — ${esc(endDate)} · 本页 ${num(rows.length)} 条</span>${pill("pending", result.metadata.unconfirmed_records?.length ? `${num(result.metadata.unconfirmed_records.length)} 个标的日期待确认` : result.metadata.coverage === "calendar_known" ? "标的覆盖见审计" : "覆盖待确认")}</div>${table(
        Object.keys(
          result.records[0] ||
            Object.fromEntries(selectedFields.map((f) => [f, ""])),
        ).map((k) => ({ title: k, key: k })),
        rows,
        "所选范围暂无记录，覆盖原因见质量审计",
        "query-table",
      )}<div class="pagination"><span>第 ${Math.floor(offset / last.limit) + 1} 页 · 本页 ${rows.length} 条</span><div><button id="query-prev" class="button" ${offset === 0 ? "disabled" : ""}>上一页</button><button id="next" class="button" ${next === null ? "disabled" : ""}>下一页</button></div></div>${jsonDetails(result.metadata, "查询详情")}`;
    $("#csv").disabled = false;
    $("#query-prev").onclick = () =>
      ctx.run(() => execute(Math.max(0, offset - last.limit)));
    $("#next").onclick = () => ctx.run(() => execute(next));
  }
  $("#query-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(() => execute());
  };
  $$("[name=fields]").forEach(
    (input) =>
      (input.onchange = () => {
        $("#query-field-count").textContent = $$(
          "[name=fields]:checked",
        ).length;
      }),
  );
  $("#csv").onclick = () =>
    ctx.run(async () => {
      if (!last) return;
      const response = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...last, export: true }),
      });
      if (!response.ok) throw Error((await response.json()).error);
      const url = URL.createObjectURL(await response.blob()),
        a = document.createElement("a");
      a.href = url;
      a.download = "prices.csv";
      a.click();
      URL.revokeObjectURL(url);
    });
  if (data.summary.local && data.symbols.length && start && end) {
    try {
      await execute();
    } catch {
      /* A failed local query is visible beside its controls. */
    }
  }
}
