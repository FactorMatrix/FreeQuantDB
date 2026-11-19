import { $, esc, metric, num, reasonText, table } from "../ui.js";

const BYTES_PER_MB = 1_000_000;
const provisionStates = {
  no_data: "暂无可共享数据",
  waiting_peers: "等待节点",
  ready: "节点已连接，等待请求",
  sharing: "正在上传",
  preparing: "正在准备本地数据",
  checking: "正在校验分片",
  missing_files: "本地文件缺失",
  invalid_data: "本地文件异常",
  pending_metadata: "缺少种子元数据",
};
const issueLabels = {
  published_but_local_missing: "本地文件缺失",
  missing_torrent_metadata: "缺少种子元数据",
  invalid_torrent_metadata: "种子元数据校验未通过",
  file_changed_during_check: "文件在校验期间发生变化",
  catalog_invalid: "本地清单校验未通过",
  data_invalid: "分片或文件校验未通过",
  active_download: "同一数据包正在下载",
};

const mb = (bytes) => bytes / BYTES_PER_MB;

export async function mountTransferSettings(ctx, view) {
  const initial = await ctx.api("runtime");
  if (!ctx.live()) return;
  view.innerHTML = `
    <div class="ops-workspace ops-provision">
      <section class="panel" id="provider-status" aria-label="共享设置">
        <div class="panel-header">
          <div class="ops-provider-state"><h2><i class="dot" aria-hidden="true"></i><span id="provider-state"></span></h2><p id="provider-message" class="muted"></p></div>
          <button class="button" type="button" id="provider-toggle"></button>
        </div>
        <div class="metric-grid" id="provider-metrics"></div>
        <div class="panel-body ops-provider-limit">
          <form id="provider-limit" class="form-row">
            <label>总下载上限（MB/s）<input name="download_limit" type="number" min="0" max="2147.483647" step="0.000001" required aria-describedby="provider-limit-note"></label>
            <label>总上传上限（MB/s）<input name="limit" type="number" min="0" max="2147.483647" step="0.000001" required aria-describedby="provider-limit-note"></label>
            <button class="button button-primary">保存限速</button>
          </form>
          <small id="provider-limit-note" class="muted">0 表示不限速</small>
        </div>
      </section>
      <section class="panel ops-ip-rules">
        <div class="panel-header"><h2>IP 上传优先</h2></div>
        <div class="panel-body">
          <form id="provider-priority" class="form-row">
            <label>节点 IP<input name="ip" required autocomplete="off" placeholder="IPv4 或 IPv6"></label>
            <button class="button button-primary">添加优先 IP</button>
          </form>
        </div>
        <div id="provider-rules"></div>
        <p class="panel-note" id="priority-note">规则变更在节点重连后生效。</p>
      </section>
    </div>
    <section class="panel" id="provider-issues" hidden>
      <div class="panel-header"><h2>文件异常</h2><span id="provider-issue-count" class="panel-meta"></span></div>
      <div id="provider-issue-list"></div>
    </section>`;

  const toggle = $("#provider-toggle", view);
  const limitForm = $("#provider-limit", view);
  const limitInput = limitForm.elements.limit;
  const downloadInput = limitForm.elements.download_limit;
  const priorityForm = $("#provider-priority", view);
  const rules = $("#provider-rules", view);
  const issuePanel = $("#provider-issues", view);
  let enabled = false;
  let toggling = false;
  let editingLimit = false;
  let lastRules;

  function paint(current) {
    if (!ctx.live()) return;
    const { provision, settings } = current;
    enabled = current.provider;
    const paused = !enabled && settings.auto_seed === false;
    $("#provider-state", view).textContent = enabled
      ? "后台共享已开启"
      : paused
        ? "后台共享已暂停"
        : "后台共享未开启";
    const message = $("#provider-message", view);
    message.textContent = paused
      ? current.upload_rate > 0 ? "下载任务仍可分享已下载的分片。" : ""
      : provisionStates[provision.status] || provision.message;
    message.hidden = !message.textContent;
    const indicator = $("#provider-status .dot", view);
    indicator.className = `dot ${enabled ? "good" : "neutral"}`;
    $("#provider-metrics", view).innerHTML =
      metric(`${mb(current.download_rate)} MB/s`, "下载速度") +
      metric(`${mb(current.upload_rate)} MB/s`, "上传速度") +
      metric(num(provision.eligible), "可共享数据包");
    toggle.textContent = enabled ? "暂停共享" : "开启共享";
    toggle.classList.toggle("button-primary", !enabled);
    toggle.disabled = toggling;
    if (!editingLimit) {
      limitInput.value = mb(settings.upload_limit);
      downloadInput.value = mb(settings.download_limit);
    }

    const rulesContent = table(
      [
        { title: "IP 地址", render: (r) => `<code>${esc(r.ip)}</code>` },
        { title: "优先级", render: (r) => !r.enabled ? "已停用" : r.level === "high" ? "高" : "普通" },
        {
          title: "操作",
          render: (r) =>
            `<button class="text-link" type="button" data-remove-ip="${esc(r.ip)}" aria-label="删除 ${esc(r.ip)}">删除</button>`,
        },
      ],
      settings.priority_rules,
      "暂无优先 IP",
    );
    if (rulesContent !== lastRules) {
      rules.innerHTML = rulesContent;
      lastRules = rulesContent;
    }
    $("#priority-note", view).textContent = settings.upload_limit === 0
      ? "设置上传总限速后，优先级参与带宽分配。规则变更在节点重连后生效。"
      : "规则变更在节点重连后生效。";

    const issues = [...provision.issues, ...current.provider_issues];
    const uniqueIssues = [...new Map(issues.map((issue) => [
      `${issue.entry?.info_hash || issue.file || ""}/${issue.reason}`,
      issue,
    ])).values()];
    issuePanel.hidden = uniqueIssues.length === 0;
    $("#provider-issue-count", issuePanel).textContent = uniqueIssues.length > 50
      ? `共 ${num(uniqueIssues.length)} 项，显示前 50 项`
      : "";
    $("#provider-issue-list", issuePanel).innerHTML = uniqueIssues.length
      ? table(
          [
            { title: "文件", render: (r) => esc(r.entry?.file || r.file || "本地数据包") },
            { title: "原因", render: (r) => esc(issueLabels[r.reason] || reasonText(r.reason)[0]) },
          ],
          uniqueIssues.slice(0, 50),
        )
      : "";
  }

  async function refresh() {
    const current = await ctx.api("runtime");
    if (!ctx.live()) return;
    paint(current);
  }

  toggle.onclick = () => ctx.run(async () => {
    const action = enabled ? "provider_stop" : "provider_start";
    toggling = toggle.disabled = true;
    try {
      await ctx.act(action, enabled ? {} : { auto: true });
    } finally {
      toggling = toggle.disabled = false;
    }
    await refresh();
  });
  limitForm.oninput = () => { editingLimit = true; };
  limitForm.onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      await ctx.act("settings", {
        upload_limit: Math.round(limitInput.valueAsNumber * BYTES_PER_MB),
        download_limit: Math.round(downloadInput.valueAsNumber * BYTES_PER_MB),
      });
      editingLimit = false;
      await refresh();
      ctx.toast("传输限速已保存。");
    });
  };
  priorityForm.onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      await ctx.act("priority_set", { ip: priorityForm.elements.ip.value.trim() });
      priorityForm.reset();
      await refresh();
    });
  };
  rules.onclick = (event) => {
    const button = event.target.closest("[data-remove-ip]");
    if (!button) return;
    ctx.run(async () => {
      await ctx.act("priority_remove", { ip: button.dataset.removeIp });
      await refresh();
    });
  };

  paint(initial);
  return refresh;
}
