import { bindDatasetLinks } from "../components/datasets.js";
import { $, $$, bindPager, bytes, datasetLabel, datasetName, esc, human, num, options, pager, pill } from "../ui.js";

const statuses = [
  ["", "全部"],
  ["local", "本地就绪"],
  ["missing", "本地缺失"],
  ["revision", "待更新"],
  ["anomaly", "异常"],
  ["downloading", "下载中"],
  ["no_peers", "等待节点"],
  ["pending", "待确认"],
];
const columns = [
  ["dataset", "数据集"],
  ["frequency", "频率"],
  ["", "状态"],
  ["local", "本地 / 目录"],
  ["latest_local", "本地实测至"],
  ["bytes", "本地体积"],
];
const pageSize = 30;
const choices = (rows, key) => [...new Set(rows.map((r) => r[key]))].sort();

function datasetRows(rows) {
  return rows.map((row) => `
    <tr>
      <td><button type="button" class="text-link catalog-dataset-name" data-open-dataset="${esc(row.dataset)}" data-frequency="${esc(row.frequency)}">${esc(datasetLabel(row))}</button><span class="cell-sub">${esc(row.dataset)}</span></td>
      <td class="catalog-frequency">${esc(human(row.frequency))}</td>
      <td>${pill(row.status, row.status === "no_peers" ? "等待节点" : undefined)}</td>
      <td><div class="catalog-row-coverage">${row.published > 0 ? `<progress max="${row.published}" value="${row.local}" aria-label="本地数据包占目录数据包"></progress>` : ""}<span>${num(row.local)} / ${num(row.published)}</span></div></td>
      <td class="catalog-date">${esc(row.latest_local || (row.local ? "待核对" : "—"))}</td>
      <td class="numeric">${bytes(row.bytes)}</td>
    </tr>`).join("");
}

function sortValue(row, key) {
  if (key === "dataset") return datasetLabel(row);
  if (key === "frequency") return human(row.frequency);
  return row[key];
}

function compare(a, b, { sort, direction }) {
  const first = sortValue(a, sort);
  const second = sortValue(b, sort);
  if (first == null && second != null) return 1;
  if (second == null && first != null) return -1;
  const order = typeof first === "number" ? first - second : (first || "").localeCompare(second || "", "zh-CN");
  return direction * order || datasetLabel(a).localeCompare(datasetLabel(b), "zh-CN");
}

export async function datasets(ctx) {
  const rows = ctx.summary.datasets;
  const filters = (ctx.state.datasetFilters ||= {
    search: "",
    market: "",
    asset: "",
    frequency: "",
    status: "",
    sort: "dataset",
    direction: 1,
    offset: 0,
  });
  const assets = () => choices(rows.filter((r) => !filters.market || r.market === filters.market), "asset");
  const availableStatuses = statuses.filter(([key]) => !key || rows.some((r) => r.status === key));
  if (!availableStatuses.some(([key]) => key === filters.status)) filters.status = "";
  const view = $("#view");
  view.innerHTML = `
    <section class="panel catalog-list">
      <div class="catalog-toolbar">
        <label class="catalog-search">
          <input id="dataset-search" type="search" placeholder="搜索名称、市场、资产或频率" value="${esc(filters.search)}" aria-label="搜索数据集" autocomplete="off">
        </label>
        <div class="catalog-selects">
          <select id="market-filter" aria-label="市场筛选">${options(choices(rows, "market"), filters.market, "全部市场")}</select>
          <select id="asset-filter" aria-label="资产筛选">${options(assets(), filters.asset, "全部资产")}</select>
          <select id="frequency-filter" aria-label="频率筛选">${options(choices(rows, "frequency"), filters.frequency, "全部频率")}</select>
        </div>
        <span class="panel-meta" id="dataset-count"></span>
      </div>
      <div class="catalog-status-bar" id="dataset-status" role="group" aria-label="状态筛选" ${availableStatuses.length < 3 ? "hidden" : ""}>
        ${availableStatuses.map(([key, title]) => `<button class="catalog-chip" type="button" data-dataset-status="${key}">${title}<span></span></button>`).join("")}
      </div>
      <div class="table-scroll" id="dataset-list">
        <table id="dataset-table" aria-label="数据集">
          <thead><tr>${columns.map(([key, title]) =>
            key ? `<th scope="col" ${key === "bytes" ? 'class="numeric"' : ""}><button type="button" class="sort-button" data-dataset-sort="${key}">${title}<span aria-hidden="true"></span></button></th>`
              : `<th scope="col">${title}</th>`,
          ).join("")}</tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div id="dataset-pages"></div>
    </section>`;

  function paint() {
    const search = filters.search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matching = rows.filter((r) => {
      if (filters.market && r.market !== filters.market) return false;
      if (filters.asset && r.asset !== filters.asset) return false;
      if (filters.frequency && r.frequency !== filters.frequency) return false;
      const description = `${datasetLabel(r)} ${r.dataset} ${datasetName(r.dataset)} ${r.frequency} ${human(r.frequency)}`.toLowerCase();
      return search.every((term) => description.includes(term));
    });
    const filtered = matching.filter((r) => !filters.status || r.status === filters.status);
    filtered.sort((a, b) => compare(a, b, filters));
    filters.offset = Math.min(filters.offset, Math.max(0, Math.ceil(filtered.length / pageSize) - 1) * pageSize);
    const result = { total: filtered.length, offset: filters.offset, limit: pageSize };
    const subset = filtered.slice(filters.offset, filters.offset + pageSize);
    $("#dataset-count", view).textContent = `${num(filtered.length)} 个数据范围`;
    $("#dataset-table tbody", view).innerHTML = subset.length ? datasetRows(subset)
      : `<tr><td colspan="${columns.length}" class="empty-cell">${rows.length ? "没有匹配的数据集" : "目录为空。更新磁力链接后可浏览数据集。"}</td></tr>`;
    $("#dataset-pages", view).innerHTML = filtered.length > pageSize ? pager(result, "datasets") : "";
    $$("[data-dataset-status]", view).forEach((button) => {
      const key = button.dataset.datasetStatus;
      button.setAttribute("aria-pressed", String(filters.status === key));
      $("span", button).textContent = num(key ? matching.filter((r) => r.status === key).length : matching.length);
    });
    $$("[data-dataset-sort]", view).forEach((button) => {
      const active = filters.sort === button.dataset.datasetSort;
      button.parentElement.setAttribute("aria-sort", active ? filters.direction === 1 ? "ascending" : "descending" : "none");
      $("span", button).textContent = active ? filters.direction === 1 ? "↑" : "↓" : "";
    });
    bindPager("datasets", result, (offset) => {
      filters.offset = offset;
      paint();
    });
    bindDatasetLinks(ctx, $("#dataset-list", view));
  }
  function filter(key, value) {
    filters[key] = value;
    filters.offset = 0;
    paint();
  }
  $("#dataset-search", view).oninput = (event) => filter("search", event.target.value);
  $("#market-filter", view).onchange = (event) => {
    filters.market = event.target.value;
    const current = assets();
    if (!current.includes(filters.asset)) filters.asset = "";
    $("#asset-filter", view).innerHTML = options(current, filters.asset, "全部资产");
    filter("market", filters.market);
  };
  $("#asset-filter", view).onchange = (event) => filter("asset", event.target.value);
  $("#frequency-filter", view).onchange = (event) => filter("frequency", event.target.value);
  $$("[data-dataset-status]", view).forEach((button) => {
    button.onclick = () => filter("status", button.dataset.datasetStatus);
  });
  $$("[data-dataset-sort]", view).forEach((button) => {
    button.onclick = () => {
      const sort = button.dataset.datasetSort;
      filters.direction = sort === filters.sort ? -filters.direction : (["dataset", "frequency"].includes(sort) ? 1 : -1);
      filter("sort", sort);
    };
  });
  paint();
}
