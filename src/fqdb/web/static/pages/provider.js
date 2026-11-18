import { mountPriority } from "../components/priority.js";
import {
  $,
  esc,
  num,
  bytes,
  rate,
  label,
  pill,
  metric,
  table,
  options,
  datasetName,
  human,
  reasonText,
} from "../ui.js";

export async function provider(ctx) {
  const data = await ctx.api("runtime");
  if (!ctx.live()) return;
  const cfg = data.settings,
    scopes = ctx.summary.datasets.filter((r) => r.local || r.published);
  let starting = false;
  $("#view").innerHTML = `
    <section id="provider-status" class="status-hero ${data.provider ? "good" : "neutral"}"><i class="dot"></i><div><strong id="provider-state">提供模式：${data.provider ? "开启" : "关闭"}</strong><p id="provider-message"></p></div><div class="action-row"><button class="button button-primary" id="provider-start">开始做种</button><button class="button" id="provider-stop" ${data.provider ? "" : "disabled"}>停止做种</button><button class="button button-ghost" id="provider-config">高级设置</button></div></section>
    <div class="metric-grid metrics-four" id="provider-metrics"></div>
    <section class="panel"><div class="panel-header"><h2>做种与传输</h2></div><div id="provider-transfers"></div><div id="provider-issues"></div></section>
    <details class="panel" id="provider-options"><summary class="panel-header"><h2>提供范围与限速</h2></summary><div class="panel-body"><form id="provider-form"><div class="form-grid"><label class="full">数据集（不选则提供全部本地数据）<div class="field-checks">${scopes.map((r) => `<label><input type="checkbox" name="datasets" value="${esc(r.dataset + "/" + r.frequency)}" ${!cfg.provider_scopes.length || cfg.provider_scopes.includes(r.dataset + "/" + r.frequency) ? "checked" : ""}>${esc(datasetName(r.dataset))} · ${esc(human(r.frequency))}</label>`).join("") || '<span class="muted">暂无可提供的数据包</span>'}</div></label><label>开始日期<input name="start_date" type="date" value="${esc(cfg.provider_start || "")}"></label><label>结束日期<input name="end_date" type="date" value="${esc(cfg.provider_end || "")}"></label><label>上传上限（B/s，0 不限）<input name="upload_limit" type="number" min="0" value="${cfg.upload_limit}"></label><label class="check full"><input name="seed_old_versions" type="checkbox" ${cfg.seed_old_versions ? "checked" : ""}>提供已校验的历史版本</label></div><div class="action-row"><button class="button button-primary">${data.provider ? "保存并应用" : "应用并开始做种"}</button></div></form></div></details>`;
  $("#provider-config").onclick = () => {
    $("#provider-options").open = true;
    $("#provider-options").scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };
  async function paint() {
    const current = await ctx.api("runtime");
    if (!ctx.live()) return;
    const provision = current.provision;
    $("#provider-state").textContent =
      `提供模式：${current.provider ? "开启" : "关闭"}`;
    $("#provider-stop").disabled = !current.provider;
    $("#provider-start").disabled = current.provider || starting;
    $("#provider-start").textContent = starting
      ? "准备中…"
      : current.provider
        ? "做种已开启"
        : "开始做种";
    $("#provider-message").textContent = starting
      ? "正在校验本地数据和种子…"
      : provision.message;
    $("#provider-status").className =
      `status-hero ${current.provider ? "good" : ["missing_files", "invalid_data", "pending_metadata"].includes(provision.status) ? "warn" : "neutral"}`;
    $("#provider-form button").textContent = current.provider
      ? "保存并应用"
      : "应用并开始做种";
    $("#provider-metrics").innerHTML =
      metric(num(provision.eligible), "可提供数据包") +
      metric(rate(current.upload_rate), "当前上传速度") +
      metric(
        bytes(current.transfers.reduce((n, t) => n + t.uploaded, 0)),
        "累计上传量",
      ) +
      metric(num(current.peer_connections), "节点连接");
    $("#provider-transfers").innerHTML = table(
      [
        {
          title: "数据包",
          render: (r) =>
            `${esc(datasetName(r.dataset))}<span class="cell-sub">${esc(r.date || "数据包")} · ${esc(human(r.frequency))}</span>`,
        },
        {
          title: "状态",
          render: (r) =>
            pill(
              r.seeding ? "local" : "checking",
              r.seeding
                ? r.upload_rate > 0
                  ? "上传中"
                  : r.peers
                    ? "等待请求"
                    : "等待连接"
                : r.download_rate > 0
                  ? "下载中"
                  : "准备中",
            ),
        },
        { title: "节点", key: "peers", n: true },
        { title: "上传速度", render: (r) => rate(r.upload_rate), n: true },
        { title: "已上传", render: (r) => bytes(r.uploaded), n: true },
      ],
      current.transfers,
      "暂无做种任务",
    );
    const issues = [...provision.issues, ...current.provider_issues].filter(
      (issue, index, all) =>
        all.findIndex(
          (row) =>
            (row.entry?.info_hash || row.file) ===
              (issue.entry?.info_hash || issue.file) &&
            row.reason === issue.reason,
        ) === index,
    );
    $("#provider-issues").innerHTML = issues.length
      ? `<div class="callout warn">以下文件尚不能提供。</div>${table(
          [
            {
              title: "数据包 / 文件",
              render: (r) =>
                esc(
                  r.entry
                    ? `${r.entry.trading_date} · v${r.entry.version}`
                    : r.file || "本地数据包",
                ),
            },
            {
              title: "原因",
              render: (r) =>
                esc(
                  {
                    published_but_local_missing: "本地文件缺失",
                    missing_file: "本地文件缺失",
                    missing_metadata: "缺少必要元数据",
                    pending_metadata: "必要元数据待补充",
                    pending_schema: "缺少数据集说明",
                    pending_units: "字段单位待确认",
                    missing_trading_day_metadata: "缺少可靠的交易日信息",
                    missing_torrent_metadata: "缺少种子元数据",
                    invalid_torrent_metadata: "种子校验未通过",
                    unsupported_local_layout: "目录结构不符合当前格式",
                    file_changed_during_check: "文件在校验期间发生变化",
                    catalog_invalid: "本地清单校验未通过",
                    active_download: "同一数据包正在下载",
                    data_invalid: "数据校验未通过",
                    invalid_file: "文件校验未通过",
                    invalid_data: "数据校验未通过",
                    local_file_changed_or_unverified:
                      "文件已变化或缺少有效校验记录",
                  }[r.reason] ||
                    r.detail ||
                    reasonText(r.reason)[0],
                ),
            },
          ],
          issues.slice(0, 50),
        )}`
      : "";
    ctx.paintRuntime(current);
  }
  $("#provider-start").onclick = () =>
    ctx.run(async () => {
      if (starting) return;
      starting = true;
      $("#provider-start").disabled = true;
      $("#provider-start").textContent = "准备中…";
      $("#provider-message").textContent = "正在校验本地数据和种子…";
      try {
        const result = await ctx.act("provider_start");
        ctx.toast(result.provision.message);
      } finally {
        starting = false;
        await paint();
      }
    });
  $("#provider-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      const form = new FormData(e.target),
        datasets = form.getAll("datasets");
      const start = form.get("start_date"),
        end = form.get("end_date");
      if (start && end && start > end)
        throw Error("开始交易日不能晚于结束交易日。");
      const result = await ctx.act("provider_start", {
        datasets,
        upload_limit: Number(form.get("upload_limit")),
        start_date: start || null,
        end_date: end || null,
        seed_old_versions: form.has("seed_old_versions"),
      });
      await paint();
      ctx.toast(result.provision.message);
    });
  };
  $("#provider-stop").onclick = () =>
    ctx.run(async () => {
      await ctx.act("provider_stop");
      await paint();
      ctx.toast("提供模式已关闭。");
    });
  const priorityPanel = document.createElement("details");
  priorityPanel.className = "panel";
  priorityPanel.id = "provider-priority";
  priorityPanel.innerHTML =
    '<summary class="panel-header"><h2>IP 上传优先级</h2></summary><div class="panel-body"></div>';
  $("#view").append(priorityPanel);
  const priorityHost = $(".panel-body", priorityPanel);
  const refreshPriority = await mountPriority(ctx, priorityHost);
  await paint();
  ctx.poll = async () => {
    await paint();
    await refreshPriority();
  };
}
