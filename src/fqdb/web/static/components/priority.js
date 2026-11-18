import {
  $,
  esc,
  num,
  bytes,
  rate,
  pill,
  metric,
  table,
  jsonDetails,
} from "../ui.js";

const states = {
  active: ["local", "已应用于当前连接"],
  pending_reconnect: ["revision", "已有连接待重连"],
  waiting_rate_limit: ["pending", "等待设置上传限速"],
  waiting_connection: ["waiting_peers", "等待目标连接"],
  local_limit_exception: ["pending", "本地网络限速例外"],
  disabled: ["paused", "已停用"],
  normal: ["pending", "普通优先级"],
};
const reasons = {
  native_peer_class_for_new_connection:
    "已为新连接应用原生上传调度优先级；实际带宽取决于争用情况。",
  existing_connection_keeps_previous_class:
    "已有连接保留原调度级别；等节点重新连接后采用新规则。",
  global_upload_limit_disabled:
    "当前全局上传上限为 0（不限速），无法宣称优先级有效。",
  local_network_limit_exception_enabled:
    "会话启用了本地网络限速例外，目标的调度效果待确认。",
  target_not_connected: "目前没有目标 IP 的真实连接；规则等待后续连接。",
  rule_disabled: "规则已保存但停用。",
  normal_priority: "目标按普通上传优先级调度。",
};
const liveRate = (value) => (value == null ? "暂无统计" : rate(value));
const liveBytes = (value) => (value == null ? "暂无统计" : bytes(value));

/** Mount into an existing page. Return refresh for the caller to compose into ctx.poll. */
export async function mountPriority(ctx, host) {
  if (typeof host === "string") host = $(host);
  if (!host) throw Error("IP 优先级容器不存在。");
  let status = null,
    editing = null,
    offset = 0;
  const limit = 25;
  host.innerHTML = `<section class="panel"><div class="panel-header"><h2>添加规则</h2></div><div class="panel-body"><div data-priority-metrics class="metric-grid metrics-four"></div><div data-priority-notice></div><form data-priority-form class="form-grid"><label>目标 IP<input name="ip" required autocomplete="off" placeholder="输入 IPv4 或 IPv6 地址，不含端口"></label><label>优先级<select name="level"><option value="high">高优先级</option><option value="normal">普通优先级</option></select></label><label>备注<input name="note" maxlength="240" autocomplete="off" placeholder="可选"></label><label>规则状态<select name="enabled"><option value="true">启用</option><option value="false">停用</option></select></label><div class="full action-row"><button class="button button-primary" data-priority-save>添加规则</button><button type="button" class="button" data-priority-clear hidden>取消编辑</button></div></form><p class="panel-note">规则变更在节点重连后生效。</p></div></section><section class="panel"><div class="panel-header"><h2>规则与实际连接</h2><div class="action-row"><label>筛选<input data-priority-search aria-label="按 IP 或备注筛选优先级规则" placeholder="IP / 备注"></label><button type="button" class="button" data-priority-refresh>刷新状态</button></div></div><div data-priority-table></div><div class="panel-body"><div data-priority-pending></div><div data-priority-details></div></div></section>`;
  const form = $("[data-priority-form]", host);
  const board = $("[data-priority-table]", host);

  function resetForm() {
    editing = null;
    form.reset();
    form.elements.ip.readOnly = false;
    $("[data-priority-save]", host).textContent = "添加规则";
    $("[data-priority-clear]", host).hidden = true;
  }

  function paint() {
    if (!status) return;
    const rules = status.rules || [];
    const connections = status.connections || [];
    const pending = status.pending_reconnect || [];
    $("[data-priority-metrics]", host).innerHTML =
      metric(
        status.global_upload_limit
          ? rate(status.global_upload_limit)
          : "不限速",
        "当前全局上传上限",
      ) +
      metric(
        num(rules.filter((r) => r.enabled).length),
        "已启用规则",
        `${rules.length} / ${status.rule_limit ?? "—"} 条已保存`,
      ) +
      metric(
        num(rules.filter((r) => r.effective).length),
        "已应用的高优先级规则",
      ) +
      metric(
        num(pending.length),
        "待重连的连接",
        `${connections.length} 个实际连接`,
      );
    $("[data-priority-notice]", host).innerHTML =
      status.global_upload_limit === 0 &&
      rules.some((r) => r.enabled && r.level === "high")
        ? '<div class="callout warn">当前不限速。设置上传上限后，高优先级规则才能参与限速调度。</div>'
        : "";
    const search = $("[data-priority-search]", host).value.trim().toLowerCase();
    const filtered = rules.filter((r) =>
      `${r.ip} ${r.note}`.toLowerCase().includes(search),
    );
    offset = Math.min(
      offset,
      Math.max(0, Math.ceil(filtered.length / limit) - 1) * limit,
    );
    board.innerHTML =
      table(
        [
          {
            title: "目标 IP / 备注",
            render: (r) =>
              `<code>${esc(r.ip)}</code>${r.note ? `<span class="cell-sub">${esc(r.note)}</span>` : ""}`,
          },
          {
            title: "规则",
            render: (r) =>
              `${r.level === "high" ? "高优先级" : "普通优先级"}<span class="cell-sub">${r.enabled ? "启用" : "停用"}</span>`,
          },
          {
            title: "实际状态",
            render: (r) => {
              const [tone, text] = states[r.state] || ["pending", "待确认"];
              return `<span title="${esc(reasons[r.reason] || r.reason || "状态待确认")}">${pill(tone, text)}</span>`;
            },
          },
          { title: "当前连接", render: (r) => num(r.connections), n: true },
          {
            title: "上传速度",
            render: (r) => liveRate(r.upload_rate),
            n: true,
          },
          {
            title: "活跃连接已上传",
            render: (r) => liveBytes(r.uploaded),
            n: true,
          },
          {
            title: "操作",
            render: (r) =>
              `<div class="row-actions"><button type="button" class="text-link" data-priority-op="edit" data-ip="${esc(r.ip)}">编辑</button><button type="button" class="text-link" data-priority-op="toggle" data-ip="${esc(r.ip)}">${r.enabled ? "停用" : "启用"}</button><button type="button" class="text-link" data-priority-op="remove" data-ip="${esc(r.ip)}">删除</button></div>`,
          },
        ],
        filtered.slice(offset, offset + limit),
        search ? "没有匹配的 IP 规则。" : "暂无 IP 规则",
      ) +
      `<div class="pagination"><span>共 ${num(filtered.length)} 条${filtered.length ? ` · ${offset + 1}–${Math.min(offset + limit, filtered.length)}` : ""}</span><div><button type="button" class="button" data-priority-page="prev" ${offset === 0 ? "disabled" : ""}>上一页</button><button type="button" class="button" data-priority-page="next" ${offset + limit >= filtered.length ? "disabled" : ""}>下一页</button></div></div>`;
    // Include connections left behind by deleted rules, so removal never hides
    // their still-effective native class until the next real reconnect.
    $("[data-priority-pending]", host).innerHTML = pending.length
      ? `<div class="callout warn">${pending.length} 个已有连接仍保留变更前的级别，等待重新连接。</div>${table(
          [
            {
              title: "连接地址",
              render: (p) =>
                `<code>${esc(p.ip)}</code><span class="cell-sub">端口 ${esc(p.endpoint?.[1] ?? "待确认")}</span>`,
            },
            {
              title: "当前级别",
              render: (p) =>
                p.assigned_level === "high" ? "高优先级" : "普通优先级",
            },
            {
              title: "重连后级别",
              render: (p) =>
                p.desired_level === "high" ? "高优先级" : "普通优先级",
            },
          ],
          pending,
        )}`
      : "";
    $("[data-priority-details]", host).innerHTML = jsonDetails(
      status,
      "连接与调度详情",
    );
  }

  async function refresh() {
    const result = await ctx.act("priority_list");
    if (!ctx.live() || !host.isConnected) return;
    status = result;
    paint();
  }

  form.onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      const values = new FormData(form);
      const args = {
        ip: String(values.get("ip")).trim(),
        level: values.get("level"),
        enabled: values.get("enabled") === "true",
        note: values.get("note") || "",
      };
      await ctx.act("priority_set", args);
      resetForm();
      await refresh();
      ctx.toast("规则已保存。");
    });
  };
  $("[data-priority-clear]", host).onclick = resetForm;
  $("[data-priority-refresh]", host).onclick = () => ctx.run(refresh);
  $("[data-priority-search]", host).oninput = () => {
    offset = 0;
    paint();
  };
  board.onclick = (event) => {
    const page = event.target.closest("[data-priority-page]");
    if (page) {
      offset += page.dataset.priorityPage === "next" ? limit : -limit;
      paint();
      return;
    }
    const button = event.target.closest("[data-priority-op]");
    if (!button) return;
    const rule = status?.rules.find((r) => r.ip === button.dataset.ip);
    if (!rule) return;
    if (button.dataset.priorityOp === "edit") {
      editing = rule.ip;
      for (const field of ["ip", "level", "enabled", "note"])
        form.elements[field].value = String(rule[field] ?? "");
      form.elements.ip.readOnly = true;
      $("[data-priority-save]", host).textContent = "保存修改";
      $("[data-priority-clear]", host).hidden = false;
      form.elements.level.focus();
      return;
    }
    ctx.run(async () => {
      await ctx.act(
        button.dataset.priorityOp === "remove"
          ? "priority_remove"
          : "priority_enable",
        { ip: rule.ip, enabled: !rule.enabled },
      );
      if (editing === rule.ip) resetForm();
      await refresh();
      ctx.toast(
        button.dataset.priorityOp === "remove"
          ? "规则已删除；已有连接状态仍可见。"
          : "规则状态已更新。",
      );
    });
  };
  await refresh();
  return refresh;
}
