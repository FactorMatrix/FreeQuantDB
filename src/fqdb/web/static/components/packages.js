import { $, $$, bytes, esc, num, packageKind, packagePeriod, pill, table, empty } from "../ui.js";

const values = {
  start_date: ["开始日期", (value) => value],
  end_date: ["结束日期", (value) => value],
  rows: ["行情行数", num],
  instruments: ["标的数", num],
  trading_days: ["交易日数", num],
  size_bytes: ["文件大小", bytes],
  fields: ["字段", (value) => Array.isArray(value) ? value.join("、") : value],
};

const magnetLabels = {
  available: "有磁力链接",
  missing: "无磁力链接",
  invalid: "磁力链接无效",
};
const localLabels = {
  available: "本地已有",
  missing: "本地缺失",
  invalid: "文件异常",
  new_version: "本地有旧版本",
};
const reviewLabels = {
  verified: "已核对", available: "已核对", valid: "已核对",
  pending: "待核对", invalid: "有差异", mismatch: "有差异",
};

const formatValue = (field, value) => {
  if (value == null) return "待核对";
  return (values[field]?.[1] || String)(value);
};
const noDownload = (r) =>
  r.magnet_status !== "available" || r.version_status === "current" || r.status === "downloading" ||
  ["waiting_peers", "waiting_metadata_peers"].includes(r.task?.status);
const fileLabel = (r) => packagePeriod({ ...r, trading_date: r.period_kind === "day" ? r.start_date : null });
const packageDateRange = ({ start_date: start, end_date: end }) => {
  if (!start || !end) return "待核对";
  return start === end ? start : `${start} — ${end}`;
};
const missingDates = (dates) => Array.isArray(dates) && dates.length
  ? `<p class="panel-note">缺失日期 · ${dates.map(esc).join("、")}</p>` : "";

export const packageColumns = [
  {
    title: "数据包",
    render: (r) => `<button class="text-link" data-package="${esc(r.path)}">${esc(fileLabel(r))}</button><span class="cell-sub">${packageKind(r)}</span>`,
  },
  {
    title: "日期范围",
    render: (r) => esc(packageDateRange(r)),
  },
  { title: "行数", render: (r) => formatValue("rows", r.rows), n: true },
  { title: "标的", render: (r) => formatValue("instruments", r.instruments), n: true },
  { title: "大小", render: (r) => formatValue("size_bytes", r.size_bytes), n: true },
  {
    title: "磁力",
    render: (r) => esc(magnetLabels[r.magnet_status] || magnetLabels.invalid),
  },
  {
    title: "状态",
    render: (r) => pill(r.status, r.status === "no_peers" ? "暂未发现可用节点" : undefined),
  },
  {
    title: "操作",
    render: (r) => `<button class="button button-small" data-package-download="${esc(r.path)}" ${noDownload(r) ? "disabled" : ""}>${r.status === "revision" ? "下载新版本" : "下载"}</button>`,
  },
];

export function bindPackages(ctx, root = document) {
  $$("[data-package]", root).forEach((button) =>
    (button.onclick = () => openPackage(ctx, button.dataset.package)),
  );
  $$("[data-package-download]", root).forEach((button) =>
    (button.onclick = () => downloadPackage(ctx, button.dataset.packageDownload, button)),
  );
}
export function downloadPackage(ctx, path, button) {
  button.disabled = true;
  return ctx.run(async () => {
    try {
      const result = await ctx.act("download_package", { catalog_path: path });
      const ids = result.task_ids || [];
      ctx.toast(ids.length ? "数据包已加入下载任务" : "本地已就绪");
      await ctx.reloadSummary();
      await ctx.render();
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
}

let activePath = null;

export async function openPackage(ctx, path) {
  const dialog = $("#package-detail");
  const target = $("#package-body");
  activePath = path;
  target.innerHTML = empty("正在读取数据包…");
  if (!dialog.open) dialog.showModal();
  try {
    const data = await ctx.request("/api/console/package?" + new URLSearchParams({ catalog_path: path }));
    if (!dialog.open || activePath !== path) return;
    const expected = data.expected || data;
    const observed = data.observed || {};
    const checks = Object.keys(values).map((field) => ({
      field,
      expected: expected[field],
      actual: observed[field],
    }));
    const messages = Array.isArray(data.issues) ? data.issues : [];
    target.innerHTML = `
      <div class="section-heading"><div><h2>${esc(fileLabel(data))} · ${packageKind(data)}</h2><p>${esc(data.scope)}</p></div>${pill(data.status, data.status === "no_peers" ? "暂未发现可用节点" : undefined)}</div>
      <dl class="facts">${[
        ["磁力状态", magnetLabels[data.magnet_status]],
        ["本地文件", localLabels[data.local_status]],
        ["版本", data.version_status === "new_version" ? "有新版本" : data.version_status === "current" ? "当前版本" : "待核对"],
        ["覆盖核对", reviewLabels[data.coverage_status] || "待核对"],
        ["目录路径", "catalog/" + data.path],
        ["数据路径", data.file || data.data_file],
      ].map(([title, value]) => `<div class="fact"><dt>${title}</dt><dd>${esc(value || "待核对")}</dd></div>`).join("")}</dl>
      ${table([
        { title: "核对项", render: (r) => esc(values[r.field][0]) },
        { title: "目录预期", render: (r) => esc(formatValue(r.field, r.expected)) },
        { title: "本地实测", render: (r) => esc(formatValue(r.field, r.actual)) },
      ], checks)}
      ${messages.map((r) => `<div class="callout warn">${esc(values[r.code]?.[0] || r.message || r.code)}${values[r.code] ? ` · 预期 ${esc(formatValue(r.code, r.expected))} · 实测 ${esc(formatValue(r.code, r.observed))}` : ""}</div>`).join("")}
      ${missingDates(data.missing_dates || observed.missing_dates)}
      ${data.task?.reason ? `<p class="panel-note">${esc(data.task.reason === "no_available_peers" ? "暂未发现可用节点" : data.task.reason)}</p>` : ""}
      <div class="action-row"><button class="button button-primary" id="package-download" ${noDownload(data) ? "disabled" : ""}>${data.status === "revision" ? "下载新版本" : "下载"}</button><button class="button" id="package-check" ${data.local_status === "missing" ? "disabled" : ""}>核对覆盖</button>${data.query_supported ? `<button class="button" id="package-query">查询本地行情</button>` : ""}</div>
      ${data.magnet_status === "available" ? `<details class="advanced"><summary>磁力链接</summary><label>磁力链接<input readonly value="${esc(data.magnet)}"></label></details>` : ""}
    `;
    const download = $("#package-download");
    download.onclick = async () => {
      await downloadPackage(ctx, path, download);
      if (dialog.open && activePath === path) await openPackage(ctx, path);
    };
    $("#package-check").onclick = (event) => ctx.run(async () => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await ctx.act("catalog_check", { catalog_path: path });
        await ctx.reloadSummary();
        await ctx.render();
        if (dialog.open && activePath === path) await openPackage(ctx, path);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
    if ($("#package-query")) $("#package-query").onclick = () => {
      dialog.close();
      const start = observed.start_date || data.start_date;
      const end = observed.end_date || data.end_date;
      ctx.state.queryRange = { key: data.dataset + "@" + data.frequency, start, end };
      ctx.navigate("detail", { dataset: data.dataset, frequency: data.frequency, tab: "data" });
    };
  } catch (error) {
    if (dialog.open && activePath === path)
      target.innerHTML = `<div class="callout error-callout">${esc(error.message)}</div>`;
  }
}
