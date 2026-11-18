import { datasetCols, bindDatasetLinks } from "../components/datasets.js";
import { $, $$, esc, num, bytes, pill, metric, table } from "../ui.js";

export async function overview(ctx) {
  const data = ctx.summary;
  const totals = data.totals;
  const active = Object.entries(data.task_counts)
    .filter(([status]) => !["complete", "cancelled", "failed", "paused"].includes(status))
    .reduce((sum, [, count]) => sum + count, 0);
  const attention = [
    ["anomaly", totals.anomaly + data.issue_count, "目录或文件异常"],
    ["revision", totals.revision, "有新版本"],
    ["missing", totals.missing, "本地缺失"],
    ["no_peers", totals.no_peers, "暂未发现可用节点"],
    ["pending", totals.coverage_pending, "覆盖待核对"],
  ].filter(([, count]) => count > 0);
  $("#view").innerHTML = `
    <section class="metric-grid" id="stats">
      ${metric(num(totals.registered), "目录数据集", `${num(totals.published)} 个数据包`)}
      ${metric(bytes(totals.bytes), "本地数据", `${num(totals.local_files)} 个文件`)}
      ${metric(totals.latest_local || "待核对", "最新本地交易日")}
      ${metric(num(totals.missing), "本地缺失", `${num(totals.revision)} 个有新版本`)}
      ${metric(num(totals.anomaly + data.issue_count), "异常")}
      ${metric(num(active), "活动下载")}
    </section>
    ${attention.length ? `<div class="status-hero warn"><div class="chips">${attention.map(([tone, count, title]) => pill(tone, `${title} ${num(count)}`)).join("")}</div><button class="text-link filter-end" data-go="validation">质量审计 →</button></div>` : ""}
    <section class="panel">
      <div class="panel-header"><h2>数据集</h2><button class="text-link" data-go="datasets">全部数据集 →</button></div>
      ${table(datasetCols, data.datasets.slice(0, 8), "本地目录为空。点击「更新磁力链接」获取发布目录。", "overview-datasets")}
    </section>
    ${data.issues.length ? `<section class="panel"><div class="panel-header"><h2>目录异常</h2></div>${table([
      { title: "路径", render: (r) => esc(r.path || "catalog") },
      { title: "原因", render: (r) => esc(r.message || r.error || r.code || "目录无效") },
    ], data.issues)}</section>` : ""}
    <section class="panel">
      <div class="panel-header"><h2>运行状态</h2><button class="text-link" data-go="downloads">下载管理 →</button></div>
      <div class="runtime-strip"><div id="overview-runtime" class="chips"></div><span class="panel-meta">最近任务：${data.latest_task ? `${pill(data.latest_task.status)} ${esc(data.latest_task.period || "")}` : "暂无"}</span></div>
    </section>
  `;
  $$("[data-go]").forEach((button) => (button.onclick = () => ctx.navigate(button.dataset.go)));
  bindDatasetLinks(ctx);
  ctx.paintRuntime(data.runtime);
}
