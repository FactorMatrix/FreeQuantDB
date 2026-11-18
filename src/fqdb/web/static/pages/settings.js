import { scopeControls, bindScope } from "../components/scope.js";
import {
  $,
  $$,
  esc,
  num,
  label,
  pill,
  table,
  jsonDetails,
  datasetName,
  human,
  shortTime,
} from "../ui.js";

export async function settings(ctx) {
  const r = await ctx.api("runtime");
  if (!ctx.live()) return;
  const cfg = r.settings;
  $("#view").innerHTML = `
    <nav class="detail-tabs tabs" id="settings-tabs" aria-label="设置分类">${[
      ["transfer", "传输"],
      ["nodes", "节点"],
      ["subscriptions", "订阅"],
      ["sources", "目录源"],
      ["storage", "存储"],
      ["advanced", "扩展与日志"],
    ]
      .map(
        ([k, t]) =>
          `<button type="button" data-settings-tab="${k}" class="tab">${t}</button>`,
      )
      .join("")}</nav>
    <section class="panel" data-settings-section="transfer"><div class="panel-header"><h2>传输设置</h2></div><div class="panel-body"><form id="settings-form" class="form-grid">${[
      ["max_active", "并发下载数", 1],
      ["connections_limit", "连接上限", 1],
      ["download_limit", "下载上限（B/s，0 不限）", 0],
      ["upload_limit", "上传上限（B/s，0 不限）", 0],
      ["peer_timeout", "停滞超时（秒）", 1],
      ["max_retries", "失败重试次数", 0],
    ]
      .map(
        ([k, t, min]) =>
          `<label>${t}<input name="${k}" type="number" value="${cfg[k]}" min="${min}"></label>`,
      )
      .join(
        "",
      )}<div class="full action-row"><button class="button button-primary">保存设置</button></div></form></div></section>
    <section class="panel" data-settings-section="nodes"><div class="panel-header"><h2>BT 节点</h2></div><div id="peer-nodes"></div><div class="panel-body"><form id="peer-node-form" class="form-grid"><label>节点 IP<input name="ip" required placeholder="IPv4 / IPv6"></label><label>端口<input name="port" type="number" min="1" max="65535" required></label><label>备注<input name="note" maxlength="240"></label><label>状态<select name="enabled"><option value="true">启用</option><option value="false">停用</option></select></label><div class="full action-row"><button class="button" id="peer-node-save">添加节点</button><button class="button" type="button" id="peer-node-reset" hidden>取消编辑</button></div></form></div></section>
    <section class="panel" data-settings-section="subscriptions"><div class="panel-header"><h2>定时订阅</h2></div><div id="subscriptions"></div><div class="panel-body">${scopeControls(ctx, "subscription", true)}<form id="subscribe-form" class="form-grid"><label>触发方式<select name="schedule"><option value="interval">按间隔检查</option><option value="daily">每日定时检查</option></select></label><label id="subscribe-interval">间隔（秒）<input name="interval" type="number" min="10" value="3600"></label><label id="subscribe-at" hidden>每天时间<input name="at" type="time" step="60"></label><label id="subscribe-timezone" hidden>时区<input name="timezone" placeholder="IANA 时区，如 UTC"></label><label class="check full"><input name="download" type="checkbox">自动下载新日数据包</label><div class="full action-row"><button class="button button-primary" ${ctx.selection().dataset ? "" : "disabled"}>创建订阅</button><span class="muted">未勾选时仅检查目录</span></div></form></div></section>
    <section class="panel" data-settings-section="sources">
      <div class="panel-header"><h2>磁力目录源</h2></div>
      <div class="panel-body">
        ${cfg.sources.length ? table([
          { title: "镜像地址", render: (r) => esc(r.source) },
          { title: "优先顺序", render: (r) => r.first ? "当前目录源" : "已配置" },
          { title: "操作", render: (r) => `<button class="text-link" data-remove-source="${esc(r.source)}">移除</button>` },
        ], cfg.sources.map((source, index) => ({ source, first: index === 0 }))) : '<p class="panel-note">当前目录源为 GitHub 官方目录。</p>'}
        <form id="source-form" class="form-row">
          <label>本地目录或 HTTP(S) 地址<input name="source" required placeholder="镜像 catalog 根目录或 index.json"></label>
          <button class="button">验证并设为目录源</button>
        </form>
        <details class="advanced">
          <summary>导入本地目录说明</summary>
          <form id="catalog-import-form" class="form-row">
            <label>本地路径<input name="source" required placeholder="catalog 目录或本地 JSON 文件"></label>
            <label>目录内相对路径<input name="relative" placeholder="仅单个包说明需要填写"></label>
            <button class="button">导入说明</button>
          </form>
        </details>
      </div>
    </section>
    <section class="panel" data-settings-section="storage"><div class="panel-header"><h2>数据位置</h2></div><div class="panel-body"><dl class="facts"><div class="fact"><dt>数据根目录</dt><dd><code>${esc(r.data_dir)}</code></dd></div></dl><p class="panel-note">更换目录：停止服务后运行 <code>fqdb init --data-dir</code>。</p><button type="button" class="button" id="layout-preview">刷新目录信息</button><div id="layout-result"></div></div></section>
    <section class="panel" data-settings-section="advanced"><div class="panel-header"><h2>扩展与日志</h2><button id="logs" class="button">读取日志</button></div><div class="panel-body"><details class="advanced"><summary>注册数据集</summary><form id="register-form"><label>数据描述 JSON<textarea name="descriptor" rows="6" required placeholder="字段、主键、时区、来源、授权和规则"></textarea></label><div class="action-row"><button class="button">校验并注册</button></div></form></details><pre id="log-output" hidden></pre>${jsonDetails(cfg, "服务配置详情")}</div></section>`;
  bindScope(ctx, "subscription", true);
  function selectSettings(tab) {
    ctx.state.settingsTab = tab;
    $$("[data-settings-tab]").forEach((b) => {
      b.classList.toggle("active", b.dataset.settingsTab === tab);
      b.setAttribute("aria-selected", String(b.dataset.settingsTab === tab));
    });
    $$("[data-settings-section]").forEach((panel) => {
      panel.hidden = panel.dataset.settingsSection !== tab;
    });
  }
  $$("[data-settings-tab]").forEach((b) => {
    b.onclick = () => selectSettings(b.dataset.settingsTab);
  });
  selectSettings(ctx.state.settingsTab || "transfer");
  async function layoutInfo() {
    const info = await ctx.api("layout");
    if (!ctx.live()) return;
    const labels = {
      data: "行情数据",
      catalog: "磁力目录",
      state: "运行状态",
      staging: "临时文件",
      imports: "导入工作区",
      logs: "日志",
    };
    $("#layout-result").innerHTML = table(
      [
        { title: "用途", render: (r) => esc(labels[r.name] || r.name) },
        { title: "目录", render: (r) => `<code>${esc(r.path)}</code>` },
        { title: "状态", render: (r) => (r.exists ? "已创建" : "尚未创建") },
      ],
      Object.entries(info.paths).map(([name, value]) => ({ name, ...value })),
    );
  }
  $("#layout-preview").onclick = () => ctx.run(layoutInfo);
  await layoutInfo();
  async function subscriptions() {
    const data = await ctx.api("runtime");
    if (!ctx.live()) return;
    $("#subscriptions").innerHTML = table(
      [
        {
          title: "数据范围",
          render: (s) =>
            `${esc(datasetName(s.body.dataset))}<span class="cell-sub">${esc(human(s.body.frequency))} · ${esc(s.body.start_date || "全部日期")} — ${esc(s.body.end_date || "最新已发布")}</span>`,
        },
        {
          title: "触发与授权",
          render: (s) =>
            `${s.body.at ? `${esc(s.body.at)} · ${esc(s.body.timezone || "UTC")}` : num(s.body.interval || 3600) + " 秒"}<span class="cell-sub">${s.body.download ? "允许自动下载" : "仅刷新目录"}</span>`,
        },
        {
          title: "状态",
          render: (s) =>
            pill(
              s.body.running_job
                ? "checking"
                : s.body.enabled === false
                  ? "paused"
                  : "queued",
              s.body.running_job
                ? "后台检查中"
                : s.body.enabled === false
                  ? "已停用"
                  : "等待计划",
            ),
        },
        { title: "最近检查", render: (s) => shortTime(s.body.last_check) },
        {
          title: "最近异常",
          render: (s) =>
            s.body.last_error
              ? `<span class="truncate" title="${esc(s.body.last_error)}">${esc(s.body.last_error)}</span>`
              : "暂无异常记录",
        },
        {
          title: "下次检查",
          render: (s) =>
            s.body.enabled === false
              ? "已停用"
              : s.next_run > 0
                ? shortTime(s.next_run * 1000)
                : "等待首次检查",
        },
        {
          title: "操作",
          render: (s) =>
            `<div class="row-actions"><button class="text-link" data-subscription-toggle="${esc(s.id)}">${s.body.enabled === false ? "启用" : "停用"}</button><button class="text-link" data-unsubscribe="${esc(s.id)}">取消订阅</button></div>`,
        },
      ],
      data.subscriptions,
      "暂无订阅",
    );
    $$("[data-unsubscribe]").forEach(
      (b) =>
        (b.onclick = () =>
          ctx.run(async () => {
            await ctx.act("unsubscribe", { id: b.dataset.unsubscribe });
            await subscriptions();
          })),
    );
    $$("[data-subscription-toggle]").forEach(
      (b) =>
        (b.onclick = () =>
          ctx.run(async () => {
            const selected = data.subscriptions.find(
              (s) => s.id === b.dataset.subscriptionToggle,
            );
            await ctx.act("subscribe", {
              ...selected.body,
              enabled: selected.body.enabled === false,
            });
            await subscriptions();
          })),
    );
  }
  const nodeForm = $("#peer-node-form");
  let nodeRows = [];
  function resetNode() {
    nodeForm.reset();
    nodeForm.elements.ip.readOnly = false;
    nodeForm.elements.port.readOnly = false;
    $("#peer-node-save").textContent = "添加节点";
    $("#peer-node-reset").hidden = true;
  }
  async function nodes() {
    const data = await ctx.act("peers_list");
    if (!ctx.live()) return;
    nodeRows = data.nodes;
    $("#peer-nodes").innerHTML = table(
      [
        {
          title: "节点",
          render: (n) =>
            `<code>${esc(n.ip.includes(":") ? "[" + n.ip + "]" : n.ip)}:${esc(n.port)}</code>`,
        },
        {
          title: "状态",
          render: (n) =>
            pill(
              n.enabled ? "queued" : "paused",
              n.enabled ? "后续连接已启用" : "已停用",
            ),
        },
        { title: "备注", key: "note" },
        {
          title: "操作",
          render: (n) =>
            `<div class="row-actions"><button type="button" class="text-link" data-node-op="edit" data-node-index="${nodeRows.indexOf(n)}">编辑</button><button type="button" class="text-link" data-node-op="toggle" data-node-index="${nodeRows.indexOf(n)}">${n.enabled ? "停用" : "启用"}</button><button type="button" class="text-link" data-node-op="remove" data-node-index="${nodeRows.indexOf(n)}">删除</button></div>`,
        },
      ],
      nodeRows,
      "暂无手动节点",
    );
  }
  $("#peer-nodes").onclick = (e) => {
    const button = e.target.closest("[data-node-op]");
    if (!button) return;
    const node = nodeRows[Number(button.dataset.nodeIndex)];
    if (!node) return;
    if (button.dataset.nodeOp === "edit") {
      for (const field of ["ip", "port", "enabled", "note"])
        nodeForm.elements[field].value = String(node[field] ?? "");
      nodeForm.elements.ip.readOnly = true;
      nodeForm.elements.port.readOnly = true;
      $("#peer-node-save").textContent = "保存节点修改";
      $("#peer-node-reset").hidden = false;
      nodeForm.elements.note.focus();
      return;
    }
    ctx.run(async () => {
      await ctx.act(
        button.dataset.nodeOp === "remove" ? "peers_remove" : "peers_enable",
        { ip: node.ip, port: node.port, enabled: !node.enabled },
      );
      resetNode();
      await nodes();
      ctx.toast("节点配置已更新。");
    });
  };
  nodeForm.onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      const v = new FormData(nodeForm);
      await ctx.act("peers_add", {
        ip: String(v.get("ip")).trim(),
        port: Number(v.get("port")),
        enabled: v.get("enabled") === "true",
        note: v.get("note") || "",
      });
      resetNode();
      await nodes();
      ctx.toast("节点已保存。");
    });
  };
  $("#peer-node-reset").onclick = resetNode;
  const subscriptionForm = $("#subscribe-form");
  subscriptionForm.elements.timezone.value =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  function subscriptionSchedule() {
    const daily = subscriptionForm.elements.schedule.value === "daily";
    $("#subscribe-interval").hidden = daily;
    $("#subscribe-at").hidden = !daily;
    $("#subscribe-timezone").hidden = !daily;
    subscriptionForm.elements.interval.required = !daily;
    subscriptionForm.elements.at.required = daily;
    subscriptionForm.elements.timezone.required = daily;
  }
  subscriptionForm.elements.schedule.onchange = subscriptionSchedule;
  subscriptionSchedule();
  $("#source-form").onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      await ctx.act("source", Object.fromEntries(new FormData(event.target)));
      await ctx.reloadSummary();
      await ctx.render();
      ctx.toast("镜像目录已启用");
    });
  };
  $$("[data-remove-source]").forEach((button) => {
    button.onclick = () => ctx.run(async () => {
      await ctx.act("source_remove", { source: button.dataset.removeSource });
      await ctx.reloadSummary();
      await ctx.render();
    });
  });
  $("#catalog-import-form").onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      const args = Object.fromEntries(new FormData(event.target));
      if (!args.relative) delete args.relative;
      const result = await ctx.act("catalog_import", args);
      await ctx.reloadSummary();
      ctx.toast(`已导入 ${num(result.imported.length)} 个目录项`);
    });
  };
  $("#settings-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      await ctx.act(
        "settings",
        Object.fromEntries(
          [...new FormData(e.target)].map(([k, v]) => [k, Number(v)]),
        ),
      );
      ctx.toast("传输设置已保存。");
    });
  };
  $("#subscribe-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      const values = new FormData(e.target),
        daily = values.get("schedule") === "daily";
      const args = {
        ...ctx.scope(),
        enabled: true,
        download: values.get("download") === "on",
      };
      if (daily) {
        args.at = values.get("at");
        args.timezone = String(values.get("timezone")).trim();
      } else args.interval = Number(values.get("interval"));
      await ctx.act("subscribe", args);
      await subscriptions();
      ctx.toast(
        args.download
          ? "已保存订阅，并允许按计划自动下载。"
          : "已保存目录订阅，不会自动下载行情。",
      );
    });
  };
  $("#register-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      await ctx.act(
        "register",
        JSON.parse(new FormData(e.target).get("descriptor")),
      );
      await ctx.reloadSummary();
      ctx.toast("数据描述已注册。");
    });
  };
  $("#logs").onclick = () =>
    ctx.run(async () => {
      const data = await ctx.request("/api/logs");
      $("#log-output").hidden = false;
      $("#log-output").textContent = data.text;
    });
  await subscriptions();
  await nodes();
  ctx.poll = async () => {
    await subscriptions();
    await nodes();
  };
}
