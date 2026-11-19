import { scopeControls, bindScope } from "../components/scope.js";
import { bindPackages, packageColumns } from "../components/packages.js";
import {
  $,
  $$,
  esc,
  num,
  bytes,
  rate,
  label,
  pill,
  metric,
  table,
  options,
  pager,
  bindPager,
  empty,
  packageKind,
  packagePeriod,
  reasonText,
  tone,
} from "../ui.js";

const workingStates = ["queued", "preparing", "fetching_metadata", "validating", "downloading", "checking", "moving", "retry_wait"];
const waitingStates = ["waiting_metadata_peers", "waiting_peers"];
const taskFilters = [
  ...workingStates,
  ...waitingStates,
  "complete",
  "failed",
  "paused",
  "cancelled",
];
const progress = (value) =>
  `<div class="progress-cell"><progress max="1" value="${value}" aria-label="下载进度"></progress><span>${(value * 100).toFixed(0)}%</span></div>`;
const taskLabel = (state) =>
  ({ waiting_metadata_peers: "等待种子节点", waiting_peers: "等待数据节点" })[state] || label(state);
const statusText = (state, text = taskLabel(state)) =>
  `<span class="ops-state ${tone(state)}"><i class="dot" aria-hidden="true"></i>${esc(text)}</span>`;

function taskActions(task) {
  const allowed = ["failed", "cancelled"].includes(task.status)
    ? [["retry", "重试"]]
    : task.status === "paused"
      ? [
          ["resume", "恢复"],
          ["cancel", "取消"],
        ]
      : ["complete"].includes(task.status)
        ? []
        : [
            ["pause", "暂停"],
            ["cancel", "取消"],
          ];
  return `<div class="row-actions">${allowed.map(([op, text]) => `<button class="text-link" data-task="${task.id}" data-operation="${op}">${text}</button>`).join("") || '<span class="muted">—</span>'}</div>`;
}
const taskTable = (tasks) =>
  table(
    [
      {
        title: "交易日 / 数据包",
        render: (r) => {
          const name = esc(packagePeriod(r)) + " · " + packageKind(r);
          return r.catalog_path
            ? `<button class="text-link" data-package="${esc(r.catalog_path)}">${name}</button>`
            : name;
        }
      },
      { title: "状态", render: (r) => statusText(r.status) },
      { title: "进度", render: (r) => progress(r.progress) },
      {
        title: "下载 / 上传",
        render: (r) =>
          `${r.metrics.download_rate == null ? "—" : rate(r.metrics.download_rate)}<span class="cell-sub">↑ ${r.metrics.upload_rate == null ? "—" : rate(r.metrics.upload_rate)}</span>`,
      },
      {
        title: "节点",
        render: (r) => (r.metrics.peers == null ? "—" : num(r.metrics.peers)),
        n: true,
      },
      {
        title: "原因",
        render: (r) =>
          r.reason
            ? `<span class="truncate" title="${esc(r.reason)}">${esc(reasonText(r.reason)[0])}</span>`
            : "—",
      },
      { title: "操作", render: taskActions },
    ],
    tasks,
    "当前分组暂无任务",
  );
function bindTasks(ctx, root = document) {
  $$("[data-task]", root).forEach(
    (b) =>
      (b.onclick = () =>
        ctx.run(async () => {
          await ctx.act("task", {
            id: b.dataset.task,
            operation: b.dataset.operation,
          });
          await ctx.updateTasks();
        })),
  );
  bindPackages(ctx, root);
}

export async function downloads(ctx) {
  const preset = ctx.state.downloadPreset;
  if (preset) {
    ctx.state.downloadMode = preset.month
      ? "month"
      : preset.year
        ? "year"
        : preset.start_date && preset.start_date === preset.end_date
          ? "day"
          : "range";
    ctx.state.downloadPreset = null;
  }
  const mode = ctx.state.downloadMode || "range",
    range = ctx.scope();
  const end = range.end_date || "",
    year = preset?.year || end.slice(0, 4),
    month = preset?.month
      ? String(preset.month).padStart(2, "0")
      : end.slice(5, 7);
  const plannerOpen = !!preset || (ctx.state.downloadPlannerOpen ?? true);
  const filters = `<select id="task-status" aria-label="任务状态筛选">${options(
    taskFilters.map((k) => [k, taskLabel(k)]),
    ctx.state.taskStatus,
    "全部状态",
  )}</select><select id="task-group" aria-label="任务汇总方式">${options(
    [
      ["month", "按月汇总"],
      ["year", "按年汇总"],
      ["day", "文件任务列表"],
    ],
    ctx.state.taskGroup || "month",
  )}</select>`;
  $("#view").innerHTML = `
    <div class="ops-download-scope">${scopeControls(ctx, "download")}</div>
    <section class="panel ops-download-preview" id="download-plan" aria-label="下载预览" hidden></section>
    <div class="ops-workspace">
      <section class="panel ops-task-panel" aria-labelledby="task-queue-title">
        <div class="panel-header"><h2 id="task-queue-title" tabindex="-1">任务队列</h2><button type="button" id="new-download" class="button button-primary">新建下载</button></div>
        <div class="metric-grid ops-task-metrics" id="task-metrics" aria-label="筛选范围内的任务统计"></div>
        <div class="filter-bar ops-task-filters">${filters}</div>
        <div id="task-board"></div>
      </section>
      <details class="panel download-planner ops-planner" id="download-planner" ${plannerOpen ? "open" : ""}>
        <summary class="panel-header"><h2>下载范围</h2></summary>
        <div class="panel-body">
    <div class="segmented" id="download-modes" aria-label="下载范围">${[
      ["day", "单日"],
      ["month", "整月"],
      ["year", "整年"],
      ["range", "日期范围"],
    ]
      .map(
        ([k, t]) =>
          `<button type="button" class="${mode === k ? "active" : ""}" data-mode="${k}">${t}</button>`,
      )
      .join("")}</div>
    <form id="download-form"><div class="form-row" id="download-period" data-mode="${mode}">
    ${
      mode === "day"
        ? `<label>交易日<input name="date" type="date" value="${esc(end)}" required></label>`
        : mode === "month"
          ? `<label>年份<input name="year" type="number" min="1" max="9999" value="${esc(year)}" required></label><label>月份<select name="month" required>${options(
              Array.from({ length: 12 }, (_, i) => [i + 1, i + 1 + " 月"]),
              Number(month),
              "选择月份",
            )}</select></label>`
          : mode === "year"
            ? `<label>年份<input name="year" type="number" min="1" max="9999" value="${esc(year)}" required></label>`
            : `<label>开始日期<input name="start_date" type="date" value="${esc(range.start_date || "")}" required></label><label>结束日期<input name="end_date" type="date" value="${esc(end)}" required></label>`
    }
    <button class="button button-primary" id="plan" type="submit">预览计划</button></div></form>
        </div>
      </details>
    </div>`;
  bindScope(ctx, "download");
  $("#plan").disabled = !ctx.selection().dataset;
  $("#download-planner").ontoggle = (e) => {
    ctx.state.downloadPlannerOpen = e.target.open;
  };
  $("#new-download").onclick = () => {
    $("#download-plan").hidden = true;
    $("#download-planner").open = true;
    $("#download-form input, #download-form select").focus();
  };
  $$("[data-mode]")
    .filter((e) => e.tagName === "BUTTON")
    .forEach(
      (b) =>
        (b.onclick = () => {
          const previous = selectedScope();
          if (previous.start_date && previous.end_date)
            ctx.setRange(previous.start_date, previous.end_date);
          else if (previous.year >= 1 && previous.year <= 9999) {
            const year = String(previous.year).padStart(4, "0");
            if (previous.month >= 1 && previous.month <= 12) {
              const month = String(previous.month).padStart(2, "0");
              const last = new Date(`${year}-${month}-01T00:00:00Z`);
              last.setUTCMonth(last.getUTCMonth() + 1);
              last.setUTCDate(0);
              ctx.setRange(
                `${year}-${month}-01`,
                last.toISOString().slice(0, 10),
              );
            } else ctx.setRange(`${year}-01-01`, `${year}-12-31`);
          }
          ctx.state.downloadMode = b.dataset.mode;
          ctx.state.downloadPlannerOpen = true;
          ctx.state.plan = null;
          ctx.render();
        }),
    );
  function selectedScope() {
    const form = new FormData($("#download-form")),
      s = ctx.selection();
    if (mode === "day")
      return { ...s, start_date: form.get("date"), end_date: form.get("date") };
    if (mode === "month")
      return {
        ...s,
        year: Number(form.get("year")),
        month: Number(form.get("month")),
      };
    if (mode === "year") return { ...s, year: Number(form.get("year")) };
    return {
      ...s,
      start_date: form.get("start_date"),
      end_date: form.get("end_date"),
    };
  }
  async function preview(offset = 0) {
    const scope = selectedScope(),
      data = await ctx.request("/api/console/plan", {
        ...scope,
        offset,
        limit: 25,
      });
    if (!ctx.live()) return;
    ctx.state.plan = { scope, data };
    const previewPanel = $("#download-plan");
    previewPanel.hidden = false;
    previewPanel.innerHTML = `
        <div class="panel-header"><h2>下载预览</h2><span class="panel-meta">${num(data.total)} 个数据包</span></div>
        <div class="metric-grid metrics-four">
          ${metric(num(data.ready), "本地已就绪")}
          ${metric(num(data.to_download), "待下载", data.revision ? `${num(data.revision)} 个新版本` : "")}
          ${metric(bytes(data.bytes), "预计下载大小")}
          ${metric(num(data.no_magnet), "无磁力链接")}
        </div>
        ${table([
          ...packageColumns.slice(0, 3).map((column) => ({ ...column, render: (r) => column.render(r.package) })),
          { title: "计划", render: (r) => ({
            ready: pill("local", "复用本地"),
            revision: pill("revision", "下载新版本"),
            verify_or_download: pill("missing", "下载"),
            no_magnet: pill("pending", "无磁力链接"),
          })[r.action] },
          { title: "大小", render: (r) => bytes(r.package.size_bytes) },
        ], data.items, "本地目录中没有匹配的数据包。", "plan-table")}
        ${pager(data, "plan")}
        <div class="plan-footer"><button class="button button-primary" id="sync" ${data.to_download ? "" : "disabled"}>执行下载 · ${num(data.to_download)} 个数据包</button></div>`;
    if (offset === 0) previewPanel.scrollIntoView({ block: "nearest" });
    $("#sync").onclick = (event) =>
      ctx.run(async () => {
        const button = event.currentTarget;
        if (JSON.stringify(scope) !== JSON.stringify(selectedScope()))
          throw Error("范围已改变，请重新预览下载计划。");
        button.disabled = true;
        let result;
        try {
          result = await ctx.act("sync", scope);
        } catch (error) {
          button.disabled = false;
          throw error;
        }
        if (!ctx.live()) return;
        ctx.toast(
          result.task_ids.length
            ? `已安排 ${result.task_ids.length} 个文件任务`
            : "已有数据已就绪，无需重复下载",
        );
        previewPanel.hidden = true;
        await ctx.updateTasks();
        if (ctx.live()) $("#task-queue-title").focus();
      });
    bindPackages(ctx, $("#download-plan"));
    bindPager("plan", data, (offset) => ctx.run(() => preview(offset)));
  }
  $("#download-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(() => preview());
  };
  $("#download-form").oninput = () => {
    if ($("#sync")) $("#sync").disabled = true;
  };
  $("#task-status").onchange = (e) => {
    ctx.state.taskStatus = e.target.value;
    ctx.state.taskOffset = 0;
    ctx.state.taskGroupOffset = 0;
    ctx.run(() => ctx.updateTasks());
  };
  $("#task-group").onchange = (e) => {
    ctx.state.taskGroup = e.target.value;
    ctx.state.taskOffset = 0;
    ctx.state.taskGroupOffset = 0;
    ctx.run(() => ctx.updateTasks());
  };
  ctx.updateTasks = async () => {
    const args = {
      ...ctx.selection(),
      offset: ctx.state.taskOffset || 0,
      limit: 25,
    };
    if (ctx.state.taskStatus) args.status = ctx.state.taskStatus;
    const data = await ctx.api("tasks", args);
    if (!ctx.live()) return;
    const tally = (states) => states.reduce((total, state) => total + (data.counts[state] || 0), 0);
    $("#task-metrics").innerHTML =
      metric(num(tally(workingStates)), "进行中") +
      metric(num(tally(waitingStates)), "等待节点") +
      metric(num(data.counts.complete || 0), "已完成") +
      metric(num(tally(["failed", "paused"])), "失败或暂停");
    const grouping = ctx.state.taskGroup || "month";
    if (grouping === "day") {
      $("#task-board").innerHTML = taskTable(data.items) + pager(data, "tasks");
      bindPager("tasks", data, (offset) => {
        ctx.state.taskOffset = offset;
        ctx.run(() => ctx.updateTasks());
      });
      bindTasks(ctx, $("#task-board"));
      return;
    }
    let groups = data.groups;
    if (grouping === "year") {
      const grouped = {};
      for (const g of groups) {
        const key =
            g.month.slice(0, 4),
          x = (grouped[key] ||= {
            ...g,
            month: key,
            total: 0,
            size: 0,
            weighted: 0,
            weight: 0,
            unweighted: 0,
            counts: {},
          });
        x.total += g.total;
        x.size = g.size == null || x.size == null ? null : x.size + g.size;
        const weight = Math.max(g.size || 0, 1);
        x.weighted += g.progress * weight;
        x.weight += weight;
        x.unweighted += g.progress * g.total;
        for (const [k, v] of Object.entries(g.counts))
          x.counts[k] = (x.counts[k] || 0) + v;
      }
      groups = Object.values(grouped).map((g) => ({
        ...g,
        progress: g.size == null ? g.unweighted / g.total : g.weighted / g.weight,
      }));
    }
    const groupOffset = ctx.state.taskGroupOffset || 0,
      shown = groups.slice(groupOffset, groupOffset + 12);
    const pageData = { total: groups.length, offset: groupOffset, limit: 12 };
    $("#task-board").innerHTML = shown.length
      ? shown
          .map(
            (g, i) =>
              `<details class="task-group" data-task-group="${i}" ${ctx.state.openGroups?.includes(g.month) ? "open" : ""}><summary><span class="ops-group-period"><strong>${esc(g.month.replace("-", " 年 ")) + (g.month.includes("-") ? " 月" : " 年")}</strong><small>${num(g.total)} 个文件 · ${bytes(g.size)}</small></span>${progress(g.progress)}<span class="ops-group-states">${Object.entries(
                g.counts,
              )
                .map(([s, n]) => statusText(s, `${taskLabel(s)} ${n}`))
                .join(
                  "",
                )}</span></summary><div class="group-days" data-group-days="${i}"></div></details>`,
          )
          .join("") + pager(pageData, "task-groups")
      : empty("暂无下载任务");
    bindPager("task-groups", pageData, (offset) => {
      ctx.state.taskGroupOffset = offset;
      ctx.run(() => ctx.updateTasks());
    });
    async function loadGroup(i, offset = 0) {
      const g = shown[i],
        body = $(`[data-group-days="${i}"]`),
        days = await ctx.api("tasks", {
          ...ctx.selection(),
          month: g.month,
          offset,
          limit: 20,
          ...(ctx.state.taskStatus ? { status: ctx.state.taskStatus } : {}),
        });
      if (!ctx.live() || !body.isConnected) return;
      body.innerHTML = taskTable(days.items) + pager(days, "group-" + i);
      bindPager("group-" + i, days, (offset) =>
        ctx.run(() => loadGroup(i, offset)),
      );
      bindTasks(ctx, body);
    }
    $$("[data-task-group]").forEach((el) => {
      const i = Number(el.dataset.taskGroup),
        g = shown[i];
      el.ontoggle = () => {
        ctx.state.openGroups = ctx.state.openGroups || [];
        if (el.open) {
          if (!ctx.state.openGroups.includes(g.month))
            ctx.state.openGroups.push(g.month);
          ctx.run(() => loadGroup(i));
        } else
          ctx.state.openGroups = ctx.state.openGroups.filter(
            (m) => m !== g.month,
          );
      };
    });
  };
  await ctx.updateTasks();
  ctx.poll = () => ctx.updateTasks();
}
