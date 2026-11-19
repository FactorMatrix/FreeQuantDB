import { datasetCols, bindDatasetLinks } from "../components/datasets.js";
import { $, $$, bytes, esc, human, metric, num, packagePeriod, pill, shortTime, table } from "../ui.js";

const idleTaskStates = new Set(["complete", "cancelled", "failed", "paused"]);
const previewLength = 8;
const assetGroup = (dataset) => `${dataset.market}/${dataset.asset}`;

function coverageGroups(datasets) {
  const groups = new Map();
  for (const dataset of datasets) {
    if (!dataset.published) continue;
    const key = assetGroup(dataset);
    if (!groups.has(key)) {
      groups.set(key, {
        name: `${human(dataset.market)} · ${human(dataset.asset)}`,
        published: 0,
        ready: 0,
      });
    }
    const group = groups.get(key);
    group.published += dataset.published;
    group.ready += dataset.counts.local || 0;
  }
  return [...groups].sort(([, a], [, b]) => b.published - a.published);
}

export async function overview(ctx) {
  const { datasets, issue_count, issues, latest_task, runtime, task_counts, totals } = ctx.summary;
  const active = Object.entries(task_counts)
    .filter(([status]) => !idleTaskStates.has(status))
    .reduce((sum, [, count]) => sum + count, 0);
  const groups = coverageGroups(datasets);
  const ready = groups.reduce((sum, [, group]) => sum + group.ready, 0);
  const alerts = [
    ["anomaly", totals.anomaly + issue_count, "目录或文件异常", "validation"],
    ["revision", totals.revision, "有新版本", "validation"],
    ["missing", totals.missing, "本地缺失", "validation"],
    ["pending", totals.coverage_pending, "覆盖待核对", "validation"],
    ["no_peers", totals.no_peers, "等待节点", "downloads"],
    ["failed", task_counts.failed, "下载失败", "downloads"],
  ].filter(([, count]) => count > 0);

  const view = $("#view");
  view.innerHTML = `
    <section class="metric-grid metrics-four overview-metrics" id="stats" aria-label="目录与下载指标">
      ${metric(num(totals.registered), "目录数据集", `${num(totals.published)} 个数据包`)}
      ${metric(bytes(totals.bytes), "本地存储", `${num(totals.local_files)} 个文件`)}
      ${metric(totals.latest_local || (totals.local_files ? "待核对" : "—"), "本地最新交易日")}
      ${metric(num(active), "活动下载")}
    </section>
    <div class="overview-grid">
      <section class="panel overview-coverage" aria-labelledby="overview-coverage-heading">
        <div class="panel-header">
          <h2 id="overview-coverage-heading">数据包覆盖</h2>
          <span class="panel-meta">${num(ready)} / ${num(totals.published)} 个就绪</span>
        </div>
        ${groups.length ? `
          <div class="overview-coverage-list">
            ${groups.map(([key, group]) => `
              <button class="overview-coverage-row" type="button" data-overview-group="${esc(key)}" aria-pressed="false" aria-controls="overview-table">
                <span>${esc(group.name)}</span>
                <progress aria-hidden="true" value="${group.ready}" max="${group.published}"></progress>
                <span class="overview-coverage-count">${num(group.ready)} / ${num(group.published)}</span>
              </button>
            `).join("")}
          </div>
          <div class="overview-legend"><i aria-hidden="true"></i>本地就绪 <span> / </span> 目录收录</div>
        ` : '<p class="overview-empty">暂无数据包，更新磁力链接后可查看覆盖。</p>'}
      </section>
      <section class="panel overview-transfers" aria-labelledby="overview-transfers-heading">
        <div class="panel-header">
          <h2 id="overview-transfers-heading">传输</h2>
          <button class="text-link" data-go="downloads" type="button">下载任务 →</button>
        </div>
        <div id="overview-runtime"></div>
        <div class="overview-latest-task">
          <span class="panel-meta">最近下载</span>
          ${latest_task ? `
            <div>${pill(latest_task.status)} <strong>${esc(packagePeriod(latest_task) || "数据包")}</strong></div>
            ${latest_task.updated_at ? `<time datetime="${esc(latest_task.updated_at)}">${esc(shortTime(latest_task.updated_at))}</time>` : ""}
          ` : '<span class="muted">暂无下载记录</span>'}
        </div>
      </section>
    </div>
    ${alerts.length ? `
      <section class="overview-attention" aria-labelledby="overview-attention-heading">
        <h2 id="overview-attention-heading">需要处理</h2>
        <div class="chips">
          ${alerts.map(([state, count, title, page]) => `
            <button class="overview-alert" data-go="${page}" type="button">${pill(state, `${title} ${num(count)}`)}<span aria-hidden="true">→</span></button>
          `).join("")}
        </div>
      </section>
    ` : ""}
    <section class="panel">
      <div class="panel-header">
        <h2>数据集</h2>
        <div class="action-row">
          <button class="text-link" id="clear-overview-group" type="button" hidden>清除筛选</button>
          <button class="text-link" data-go="datasets" type="button">全部数据集 →</button>
        </div>
      </div>
      <div id="overview-table"></div>
    </section>
    ${issues.length ? `<section class="panel"><div class="panel-header"><h2>目录异常</h2></div>${table([
      { title: "路径", render: (r) => esc(r.path || "catalog") },
      { title: "原因", render: (r) => esc(r.message || r.error || r.code || "目录无效") },
    ], issues)}</section>` : ""}
  `;

  const clearGroup = $("#clear-overview-group", view);
  const groupButtons = $$("[data-overview-group]", view);
  let activeGroup = "";
  function showDatasets(group = "") {
    activeGroup = group;
    clearGroup.hidden = !group;
    for (const button of groupButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.overviewGroup === group));
    }
    const items = group ? datasets.filter((row) => assetGroup(row) === group) : datasets;
    const area = $("#overview-table", view);
    area.innerHTML = table(
      datasetCols, items.slice(0, previewLength), "本地目录暂无数据集。", "overview-datasets",
    );
    if (items.length > previewLength) {
      area.insertAdjacentHTML(
        "beforeend", `<p class="overview-table-note">显示 ${num(previewLength)} / ${num(items.length)} 个数据集</p>`,
      );
    }
    bindDatasetLinks(ctx, area);
  }
  for (const button of groupButtons) {
    button.onclick = () => showDatasets(activeGroup === button.dataset.overviewGroup ? "" : button.dataset.overviewGroup);
  }
  clearGroup.onclick = () => showDatasets();
  $$("[data-go]", view).forEach((button) => (button.onclick = () => ctx.navigate(button.dataset.go)));
  showDatasets();
  ctx.paintRuntime(runtime);
}
