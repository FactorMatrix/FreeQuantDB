// Table/status/shell patterns adapted from rootSunc/CNEquity frontend/src/main.js
// at 05370ab722acaaf1cf4cebc8b4489aaa81d5083b (Apache-2.0). FreeQuantDB adaptations.
export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [
  ...root.querySelectorAll(selector),
];
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const num = (value) =>
  value == null ? "待确认" : Number(value).toLocaleString("zh-CN");
export function bytes(value) {
  if (value == null) return "暂无数据";
  if (value < 1024) return `${num(value)} B`;
  const unit = value < 1048576 ? "KiB" : value < 1073741824 ? "MiB" : "GiB";
  return `${(value / (unit === "KiB" ? 1024 : unit === "MiB" ? 1048576 : 1073741824)).toFixed(1)} ${unit}`;
}
export const rate = (value) => `${bytes(value ?? 0)}/s`;
export const shortTime = (value) =>
  value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "暂无记录";
export const names = {
  cn: "中国",
  us: "美国",
  hk: "中国香港",
  stock: "股票",
  etf: "ETF",
  lof: "LOF",
  future: "期货",
  option: "期权",
  fund: "基金",
  index: "指数",
  constituents: "成分",
  base: "基础行情",
  financial: "财务",
  instrument: "证券资料",
  "1d": "日线",
  "1m": "1 分钟",
};
export const human = (value) => names[value] || value || "—";
export const datasetName = (id) =>
  id ? id.split("/").map(human).join(" / ") : "—";
export const datasetLabel = (row) =>
  row.name && row.name !== row.dataset ? row.name : datasetName(row.dataset);
export const packageKind = (entry) =>
  ({ day: "日包", month: "月包", year: "年包" })[entry?.period_kind || "day"] ||
  "数据包";
export function packagePeriod(entry) {
  const value = String(entry?.period || "");
  if (entry?.period_kind === "year") return `${value} 年`;
  if (entry?.period_kind === "month")
    return `${value.slice(0, 4)}-${value.slice(4, 6)}`;
  return entry?.trading_date || entry?.date || value;
}
export const labels = {
  local: "本地就绪",
  verified: "已确认",
  missing: "本地缺失",
  source_unpublished: "源端未发布",
  anomaly: "异常",
  pending: "待确认",
  closed: "闭市",
  revision: "待更新",
  no_peers: "暂未发现可用节点",
  downloading: "下载中",
  fetching_metadata: "获取种子元数据",
  waiting_metadata_peers: "暂未发现可用节点",
  running: "执行中",
  validating: "验证下载文件",
  preparing: "准备本地分片",
  queued: "排队中",
  checking: "分片校验",
  moving: "启用版本",
  waiting_peers: "暂未发现可用节点",
  retry_wait: "等待重试",
  complete: "已完成",
  failed: "失败",
  paused: "已暂停",
  cancelled: "已取消",
  verified_cached: "已验证（缓存）",
  damaged: "损坏",
  invalid: "异常",
  recheck_required: "需要复验",
  error: "错误",
  pending_calendar: "交易日历待确认",
  source: "发布源",
  file: "文件 / BT",
  coverage: "覆盖元数据",
  records: "标的记录",
  quality: "格式 / 行情质量",
  version: "历史版本",
  network: "节点连接",
};
export const label = (value) => labels[value] || value || "待确认";
export const tone = (value) =>
  ["local", "verified", "complete", "verified_cached"].includes(value)
    ? "good"
    : ["anomaly", "failed", "error", "damaged", "invalid"].includes(value)
      ? "bad"
      : ["missing", "revision", "source_unpublished", "no_peers"].includes(
            value,
          )
        ? "warn"
        : [
              "preparing",
              "validating",
              "running",
              "fetching_metadata",
              "waiting_metadata_peers",
              "downloading",
              "checking",
              "queued",
              "moving",
              "waiting_peers",
              "retry_wait",
            ].includes(value)
          ? "blue"
          : "neutral";
export const pill = (value, text = label(value)) =>
  `<span class="status-pill ${tone(value)}"><i class="dot"></i>${esc(text)}</span>`;
export const button = (text, action, cls = "") =>
  `<button type="button" class="button ${cls}" data-action="${esc(action)}">${esc(text)}</button>`;
export const empty = (title, note = "") =>
  `<div class="empty-state"><strong>${esc(title)}</strong>${note ? `<p>${esc(note)}</p>` : ""}</div>`;
export const metric = (value, title, note = "") =>
  `<article class="metric-card"><span>${esc(title)}</span><strong>${esc(value)}</strong>${note ? `<small>${esc(note)}</small>` : ""}</article>`;
export const jsonDetails = (value, title = "高级详情") =>
  `<details class="advanced"><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(value, null, 2))}</pre></details>`;
export function table(columns, rows, emptyText = "当前范围暂无记录", id = "") {
  return `<div class="table-scroll"><table ${id ? `id="${esc(id)}"` : ""}><thead><tr>${columns.map((c) => `<th ${c.n ? 'class="numeric"' : ""}>${esc(c.title)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.map((row) => `<tr>${columns.map((c) => `<td ${c.n ? 'class="numeric"' : ""}>${c.render ? c.render(row) : esc(row[c.key] ?? "—")}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${columns.length}" class="empty-cell">${esc(emptyText)}</td></tr>`}</tbody></table></div>`;
}
export const pager = (data, id) =>
  `<div class="pagination" data-pager="${esc(id)}"><span>共 ${num(data.total)} 条${data.total ? ` · ${data.offset + 1}–${Math.min(data.total, data.offset + data.limit)}` : ""}</span><div><button class="button" type="button" data-step="prev" ${data.offset === 0 ? "disabled" : ""}>上一页</button><button class="button" type="button" data-step="next" ${data.offset + data.limit >= data.total ? "disabled" : ""}>下一页</button></div></div>`;
export function bindPager(id, data, onPage) {
  $$(`[data-pager="${id}"] button`).forEach(
    (b) =>
      (b.onclick = () =>
        onPage(
          Math.max(
            0,
            data.offset +
              (b.dataset.step === "next" ? data.limit : -data.limit),
          ),
        )),
  );
}
export function options(values, selected, placeholder = "") {
  return (
    (placeholder ? `<option value="">${esc(placeholder)}</option>` : "") +
    values
      .map((value) => {
        const [key, text] = Array.isArray(value)
          ? value
          : [value, human(value)];
        return `<option value="${esc(key)}" ${String(key) === String(selected) ? "selected" : ""}>${esc(text)}</option>`;
      })
      .join("")
  );
}
export function datasetOptions(rows, selected) {
  const markets = new Map();
  for (const { market, asset, data_type, dataset } of rows) {
    if (!markets.has(market)) markets.set(market, new Map());
    markets.get(market).set(dataset, `${human(asset)} / ${human(data_type)}`);
  }
  return [...markets]
    .map(
      ([market, datasets]) =>
        `<optgroup label="${esc(human(market))}">${options([...datasets], selected)}</optgroup>`,
    )
    .join("");
}
const reasons = {
  manual_resume_required: [
    "等待手动恢复",
    "此任务尚未在本次运行中激活，可点击恢复继续下载。",
  ],
  metadata_not_cached: [
    "尚未取得种子元数据",
    "连接配置的 BT 节点取得元数据后，再检查分片。",
  ],
  no_metadata_peers: [
    "没有可用的元数据节点",
    "清单已登记，需在线 BT 节点提供种子元数据；检查节点后重试。",
  ],
  metadata_timeout: [
    "取得种子元数据超时",
    "节点已连接但未提供完整元数据；检查节点状态后重试。",
  ],
  local_file_changed_or_unverified: [
    "本地文件变化或尚未验证",
    "深度校验通过后才能持续提供该版本。",
  ],
  file_changed_during_check: [
    "检查期间文件发生变化",
    "检查结果未启用，确认文件稳定后重新校验。",
  ],
  published_but_local_missing: [
    "可信目录已发布，本地文件缺失",
    "可从清单对应种子下载或修复。",
  ],
  size_mismatch: [
    "本地文件大小与清单不符",
    "预览修复计划，重新校验并复用有效分片。",
  ],
  sha256_mismatch: [
    "文件 SHA-256 校验失败",
    "通过对应种子恢复，再校验后启用。",
  ],
  historical_revision: [
    "发布方已提供新的历史版本",
    "下载新版本并校验后切换，旧文件与种子保留。",
  ],
  source_unpublished: [
    "源端尚未发布所需日期",
    "等待可信目录发布；不能通过猜测行情补全。",
  ],
  source_record_gap: [
    "来源文件的标的记录不完整",
    "等待发布方修正，重新下载同一版本不能补出记录。",
  ],
  metadata_conflict: [
    "来源记录与标的元数据冲突",
    "核对证券状态、交易日和来源规则。",
  ],
  pending_calendar: [
    "缺少范围内可信交易日历",
    "覆盖分母待确认，不把空白直接当作缺失。",
  ],
  pending_trading_dates: [
    "已发布数据包的交易日覆盖待确认",
    "查看数据包或运行深度检查，确认文件中的交易日。",
  ],
  pending_units: [
    "成交量和成交额单位待确认",
    "已检查行情字段和数值，单位需由发布方确认。",
  ],
  pending_trading_day_index: [
    "分钟数据的交易日索引待确认",
    "需要可信交易日索引以检查跨日交易时段。",
  ],
  pending_coverage_metadata: [
    "文件已就绪，行情覆盖依据不足",
    "补充可信日历与历史标的信息后再检查。",
  ],
  pending_record_check: [
    "文件已验证，记录覆盖尚未深检",
    "运行深度检查，独立核验标的和记录数。",
  ],
  pending_instrument_metadata: [
    "历史标的信息不足",
    "补充上市、停牌或合约资料后判断。",
  ],
  pending_record_metadata: [
    "记录数预期不足",
    "依据数据集规则补充可信记录数量或时段。",
  ],
  pending_bt_check: [
    "BT 分片尚未深度核验",
    "运行深度检查，不用文件存在替代分片结果。",
  ],
  pending_quality_check: [
    "格式与行情质量尚未深度核验",
    "运行深度检查，检查实际记录与字段。",
  ],
  pending_file_check: ["本地文件需重新验证", "运行深度检查，确认大小与哈希。"],
  verified: ["检查已通过", ""],
  aggregate_outdated: [
    "月/年包待更新",
    "日数据已修订，重新生成并验证相应月/年包。",
  ],
  coverage_content_conflict: [
    "重叠数据包内容不一致",
    "核对日来源摘要并重新发布，查询不会静默合并。",
  ],
};
export function reasonText(reason) {
  if (reasons[reason]) return reasons[reason];
  if (String(reason).includes("no_available_peers"))
    return [
      "没有可用节点或传输停滞",
      "等待节点上线，或检查显式节点配置后重试。",
    ];
  if (String(reason).includes("metadata_invalid"))
    return [
      "种子元数据不符合可信清单",
      "文件身份、路径或大小验证未通过，已停止下载；核对可信来源。 ",
    ];
  if (String(reason).includes("source_content_invalid"))
    return [
      "来源文件存在格式或行情质量错误",
      "请发布方创建修正版；不能通过重下载同一文件修复内容。",
    ];
  return [reason || "尚未确认", "查看高级检查详情，按文件或来源原因处理。"];
}
export function bindActions(actions, root = document) {
  $$("[data-action]", root).forEach((b) => {
    if (actions[b.dataset.action]) b.onclick = actions[b.dataset.action];
  });
}
