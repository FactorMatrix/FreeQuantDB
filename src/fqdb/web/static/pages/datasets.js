import { datasetCols, bindDatasetLinks } from "../components/datasets.js";
import {
  $,
  esc,
  num,
  bytes,
  label,
  table,
  options,
  pager,
  bindPager,
  datasetName,
} from "../ui.js";

export async function datasets(ctx) {
  const rows = ctx.summary.datasets;
  const filters = (ctx.state.datasetFilters ||= {
    search: "",
    market: "",
    asset: "",
    status: "",
    sort: "dataset",
    dir: 1,
    offset: 0,
  });
  const assets = () => [
    ...new Set(
      rows
        .filter((r) => !filters.market || r.market === filters.market)
        .map((r) => r.asset),
    ),
  ];
  $("#view").innerHTML =
    `<section class="panel"><div class="filter-bar"><input id="dataset-search" type="search" placeholder="搜索数据集" value="${esc(filters.search)}" aria-label="搜索数据集"><select id="market-filter" aria-label="市场筛选">${options(
      [...new Set(rows.map((r) => r.market))],
      filters.market,
      "全部市场",
    )}</select><select id="asset-filter" aria-label="资产筛选">${options(assets(), filters.asset, "全部资产")}</select><select id="dataset-status" aria-label="状态筛选">${options(
      [
        "verified",
        "local",
        "missing",
        "revision",
        "source_unpublished",
        "no_peers",
        "anomaly",
        "pending",
        "downloading",
      ].map((s) => [s, ctx.label(s)]),
      filters.status,
      "全部状态",
    )}</select><span class="panel-meta" id="dataset-count"></span><button class="text-link filter-end" id="sort-datasets">按${filters.sort === "bytes" ? "体积" : "名称"}排序 ↕</button></div><div id="dataset-list"></div></section>`;
  function paint() {
    const q = filters.search.trim().toLowerCase();
    const filtered = rows
      .filter(
        (r) =>
          (!filters.market || r.market === filters.market) &&
          (!filters.asset || r.asset === filters.asset) &&
          (!filters.status || r.status === filters.status) &&
          (!q ||
            `${r.dataset} ${r.name || ""} ${datasetName(r.dataset)}`.toLowerCase().includes(q)),
      )
      .sort(
        (a, b) =>
          filters.dir *
          (filters.sort === "bytes"
            ? a.bytes - b.bytes
            : a.dataset.localeCompare(b.dataset)),
      );
    const result = {
      items: filtered.slice(filters.offset, filters.offset + 50),
      total: filtered.length,
      offset: filters.offset,
      limit: 50,
    };
    $("#dataset-count").textContent = `${num(filtered.length)} 个数据范围`;
    $("#dataset-list").innerHTML =
      table(
        datasetCols,
        result.items,
        "没有匹配的数据集。请调整筛选条件。",
        "dataset-table",
      ) + pager(result, "datasets");
    bindPager("datasets", result, (offset) => {
      filters.offset = offset;
      paint();
    });
    bindDatasetLinks(ctx, $("#dataset-list"));
  }
  $("#dataset-search").oninput = (e) => {
    filters.search = e.target.value;
    filters.offset = 0;
    paint();
  };
  $("#market-filter").onchange = (e) => {
    filters.market = e.target.value;
    const choices = assets();
    if (!choices.includes(filters.asset)) filters.asset = "";
    $("#asset-filter").innerHTML = options(choices, filters.asset, "全部资产");
    filters.offset = 0;
    paint();
  };
  for (const [id, key] of [
    ["asset-filter", "asset"],
    ["dataset-status", "status"],
  ])
    $("#" + id).onchange = (e) => {
      filters[key] = e.target.value;
      filters.offset = 0;
      paint();
    };
  $("#sort-datasets").onclick = () => {
    filters.sort = filters.sort === "dataset" ? "bytes" : "dataset";
    filters.dir = filters.sort === "bytes" ? -1 : 1;
    $("#sort-datasets").textContent =
      `按${filters.sort === "bytes" ? "体积" : "名称"}排序 ↕`;
    paint();
  };
  paint();
}
