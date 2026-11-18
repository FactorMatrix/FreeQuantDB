import {
  $, $$, empty, esc, human, num, bytes, pill, metric, table, options, pager, bindPager,
} from "../ui.js";
import { bindPackages, packageColumns } from "../components/packages.js";
import { query } from "./query.js";

function treeMarkup(ctx, browse) {
  const tree = new Map();
  const selected = ctx.selection();
  for (const row of ctx.summary.datasets) {
    const levels = ["market", "asset", "data_type"];
    const parts = [row.market, row.asset, row.data_type];
    let branch = tree;
    let node;
    parts.forEach((part, i) => {
      if (!branch.has(part)) branch.set(part, {
        part, level: levels[i], path: parts.slice(0, i + 1).join("/"),
        children: new Map(), frequencies: [],
      });
      node = branch.get(part);
      branch = node.children;
    });
    node.frequencies.push(row);
  }
  const years = browse.years.map((year) => `
    <details data-tree-level="year" ${String(ctx.state.year) === year ? "open" : ""}>
      <summary data-year="${esc(year)}">${esc(year)} 年</summary>
      ${browse.year_months[year].map((month) => `<button data-month="${esc(month)}" data-year="${esc(year)}" class="${ctx.state.month === month && ctx.state.year === year ? "selected" : ""}">${num(Number(month))} 月</button>`).join("")}
    </details>
  `).join("");
  function render(branch) {
    return [...branch.values()].map((node) => {
      const open = selected.dataset === node.path || selected.dataset.startsWith(node.path + "/");
      return `<details data-tree-level="${node.level}" ${open ? "open" : ""}>
        <summary>${esc(human(node.part))}</summary>
        ${render(node.children)}
        ${node.frequencies.map((row) => {
          const match = selected.dataset === row.dataset && selected.frequency === row.frequency;
          return `<button data-tree-frequency="${esc(row.frequency)}" data-dataset="${esc(row.dataset)}" class="${match ? "selected" : ""}">${esc(human(row.frequency))}</button>${match && years ? `<div class="tree-years">${years}</div>` : ""}`;
        }).join("")}
      </details>`;
    }).join("");
  }
  return `<div class="tree-title">数据目录</div>${render(tree)}`;
}

async function distribution(ctx, target) {
  const { year, month, catalogStatus, catalogOffset } = ctx.state;
  const browsing = await ctx.api("browse", {
    ...ctx.selection(), year, month, status: catalogStatus, limit: 30, offset: catalogOffset || 0,
  });
  if (!ctx.live()) return;
  target.innerHTML = `
    <div class="catalog-layout">
      <aside class="catalog-tree" aria-label="数据目录层级">${treeMarkup(ctx, browsing)}</aside>
      <section class="panel">
        <div class="panel-header">
          <h2>数据包${year ? ` · ${esc(year)} 年` : ""}${month ? ` ${num(Number(month))} 月` : ""}</h2>
          <button class="button" id="catalog-range-download">按范围下载</button>
        </div>
        <div class="filter-bar">
          <button class="button button-small" id="catalog-all">全部时期</button>
          <select id="catalog-status" aria-label="包状态筛选">${options(
            ["local", "missing", "revision", "downloading", "no_peers", "anomaly", "pending"].map((s) => [s, ctx.label(s)]),
            catalogStatus, "全部状态",
          )}</select>
          <span class="panel-meta">${num(browsing.total)} 个包</span>
        </div>
        ${table(packageColumns, browsing.items, "当前目录没有匹配的数据包。", "distribution-table")}
        ${pager(browsing, "catalog")}
      </section>
    </div>
  `;
  function selectPeriod(nextYear, nextMonth) {
    ctx.state.year = nextYear || null;
    ctx.state.month = nextMonth || null;
    ctx.state.catalogOffset = 0;
    ctx.render();
  }
  $$("[data-tree-frequency]", target).forEach((button) =>
    (button.onclick = () => ctx.navigate("detail", {
      dataset: button.dataset.dataset, frequency: button.dataset.treeFrequency, tab: "coverage",
    })),
  );
  $$("summary[data-year]", target).forEach((heading) =>
    (heading.onclick = (event) => {
      event.preventDefault();
      selectPeriod(heading.dataset.year);
    }),
  );
  $$("button[data-month]", target).forEach((button) =>
    (button.onclick = () => selectPeriod(button.dataset.year, button.dataset.month)),
  );
  $("#catalog-all").onclick = () => selectPeriod(null, null);
  $("#catalog-status").onchange = (event) => {
    ctx.state.catalogStatus = event.target.value;
    ctx.state.catalogOffset = 0;
    ctx.render();
  };
  $("#catalog-range-download").onclick = () => {
    if (year) ctx.state.downloadPreset = {
      ...ctx.selection(), year: Number(year), ...(month ? { month: Number(month) } : {}),
    };
    ctx.state.downloadPlannerOpen = true;
    ctx.navigate("downloads");
  };
  bindPager("catalog", browsing, (offset) => {
    ctx.state.catalogOffset = offset;
    ctx.render();
  });
  bindPackages(ctx, target);
}

export async function detail(ctx) {
  const data = await ctx.api("dataset", ctx.selection());
  if (!ctx.live()) return;
  const { schema, summary } = data;
  const tab = ctx.state.detailTab || "coverage";
  $("#title").textContent = summary.name || summary.dataset;
  $("#view").innerHTML = `
    <div class="dataset-identity">
      <span class="code-id">${esc(summary.scope)}</span>${pill(summary.status)}
      <span class="panel-meta">${esc(summary.start_date || "暂无日期")} — ${esc(summary.end_date || "暂无日期")}</span>
    </div>
    <section class="metric-grid metrics-four">
      ${metric(num(summary.published), "目录数据包", `${num(summary.local)} 个本地文件`)}
      ${metric(num(summary.published_rows), "目录行情行数")}
      ${metric(num(summary.published_instruments), "标的数", summary.published_instruments == null ? "单包统计见详情" : "")}
      ${metric(bytes(summary.bytes), "本地体积")}
    </section>
    <section class="panel dataset-workbench">
      <div class="tabs" role="tablist" aria-label="数据集工作区">${[
        ["coverage", "数据目录"], ["data", "行情查询"], ["fields", "字段"],
      ].map(([id, title]) => `<button class="tab ${tab === id ? "active" : ""}" data-detail-tab="${id}" role="tab" aria-selected="${tab === id}">${title}</button>`).join("")}</div>
      <div id="detail-body" class="tab-body"></div>
    </section>`;
  $$("[data-detail-tab]").forEach((button) =>
    (button.onclick = () => ctx.navigate("detail", { tab: button.dataset.detailTab })),
  );
  const target = $("#detail-body");
  if (tab === "data") {
    if (!schema.query_supported) target.innerHTML = empty("此数据集尚未接入行情查询");
    else if (!summary.local) target.innerHTML = empty("暂无本地行情", "在数据目录下载对应数据包。");
    else await query(ctx, target, data);
  } else if (tab === "fields") {
    target.innerHTML = `
      <dl class="facts">
        <div class="fact"><dt>标的列</dt><dd>${esc(schema.instrument_column)}</dd></div>
        <div class="fact"><dt>时间列</dt><dd>${esc(schema.time_column)}</dd></div>
      </dl>
      ${table([
        { title: "字段", key: "field" },
        { title: "类型", render: (row) => esc(row.type || "待核对") },
      ], Object.entries(schema.fields).map(([field, type]) => ({ field, type })))}
    `;
  } else {
    await distribution(ctx, target);
  }
}
