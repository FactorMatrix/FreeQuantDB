import { $, datasetName, datasetOptions, esc, human, num, options, shortTime, table } from "../ui.js";

const SECONDS_PER_HOUR = 3600;
const DEFAULT_INTERVAL = 3 * SECONDS_PER_HOUR;
const advancedOptions = ["source", "year", "month", "granularity", "allow_daily_fallback"];
const subscriptionOptions = [
  "dataset", "frequency", "start_date", "end_date", "interval", "at", "timezone", "download", ...advancedOptions,
];

export const subscriptionSettings = `
  <section class="panel" id="subscription-settings" aria-labelledby="subscription-settings-title">
    <div class="panel-header"><h2 id="subscription-settings-title" tabindex="-1">自动更新与订阅</h2></div>
    <div id="subscriptions"></div>
    <div class="panel-body">
      <form id="subscribe-form">
        <div class="form-row">
          <label>数据集<select name="dataset" id="subscription-dataset"></select></label>
          <label>频率<select name="frequency" id="subscription-frequency"></select></label>
          <label>触发方式<select name="schedule"><option value="interval">按间隔检查</option><option value="daily">每日定时检查</option></select></label>
          <label id="subscribe-interval">间隔（小时）<input name="interval" type="number" min="${10 / SECONDS_PER_HOUR}" step="any"></label>
          <label id="subscribe-at" hidden>每天时间<input name="at" type="time" step="1"></label>
          <label id="subscribe-timezone" hidden>时区<input name="timezone" placeholder="如 Asia/Shanghai"></label>
        </div>
        <div class="form-row subscription-range" hidden>
          <label>开始日期<input name="start_date" id="subscription-start" type="date"></label>
          <label>结束日期<input name="end_date" id="subscription-end" type="date"></label>
          <small class="muted" id="subscribe-range-note">日期留空时跟进新发布</small>
        </div>
        <div class="action-row">
          <label class="check"><input name="download" type="checkbox">自动下载新数据包</label>
          <button class="button button-primary" id="subscribe-submit">保存订阅</button>
          <button class="button" type="button" id="subscribe-reset" hidden>取消编辑</button>
        </div>
      </form>
    </div>
  </section>`;

function checkResult({ last_result, last_error }) {
  if (last_error) return `<span class="cell-sub subscription-error">${esc(last_error)}</span>`;
  if (!last_result) return "";
  const { added, updated, task_ids } = last_result;
  const notes = ["检查成功"];
  if (Number.isInteger(added) && Number.isInteger(updated)) {
    const changes = added + updated;
    notes.push(changes ? `${num(changes)} 项新增或更新` : "无新增或更新");
  }
  if (task_ids?.length) notes.push(`${num(task_ids.length)} 个下载任务`);
  return `<span class="cell-sub">${notes.join(" · ")}</span>`;
}

function scheduleLabel(body) {
  if (body.at) return `每天 ${esc(body.at)} · ${esc(body.timezone || "UTC")}`;
  const seconds = body.interval ?? DEFAULT_INTERVAL;
  if (seconds % SECONDS_PER_HOUR === 0) return `每 ${num(seconds / SECONDS_PER_HOUR)} 小时`;
  if (seconds % 60 === 0) return `每 ${num(seconds / 60)} 分钟`;
  return `每 ${num(seconds)} 秒`;
}

function periodLabel(body) {
  return body.year ? `${body.year} 年${body.month ? ` ${body.month} 月` : ""}` : "";
}

function subscriptionScope(body) {
  if (!body.dataset) return "仅更新磁力目录";
  let detail = "仅更新磁力目录";
  if (body.download) {
    detail = body.start_date || body.end_date
      ? `${body.start_date || "不限开始"} — ${body.end_date || "后续发布"}`
      : periodLabel(body) || "跟进新发布";
  }
  return `${esc(datasetName(body.dataset))} · ${esc(human(body.frequency))}<span class="cell-sub">${esc(detail)}</span>`;
}

function subscriptionSchedule({ body, next_run }) {
  let status = next_run > 0 ? `下次 ${shortTime(next_run * 1000)}` : "等待首次检查";
  if (body.enabled === false) status = "已停用";
  else if (body.running_job) status = "后台检查中";
  return `${scheduleLabel(body)}<span class="cell-sub">${status}</span>`;
}

export function bindSubscriptions(ctx, view, refresh) {
  const form = $("#subscribe-form", view);
  const fields = form.elements;
  const list = $("#subscriptions", view);
  const reset = $("#subscribe-reset", view);
  const range = $(".subscription-range", form);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  let editing = null;
  let subscriptions = [];
  let lastContent;

  function showRange() {
    range.hidden = !fields.download.checked;
    fields.start_date.disabled = range.hidden;
    fields.end_date.disabled = range.hidden;
  }

  function setFrequency(selected) {
    const frequencies = ctx.summary.datasets
      .filter((row) => row.dataset === fields.dataset.value)
      .map((row) => row.frequency);
    fields.frequency.innerHTML = options(frequencies, selected);
    fields.frequency.disabled = !!editing || !frequencies.length;
    fields.download.disabled = !fields.dataset.value || !fields.frequency.value;
    if (fields.download.disabled) fields.download.checked = false;
    showRange();
  }

  function scheduleFields() {
    const daily = fields.schedule.value === "daily";
    $("#subscribe-interval", form).hidden = daily;
    $("#subscribe-at", form).hidden = !daily;
    $("#subscribe-timezone", form).hidden = !daily;
    fields.interval.required = !daily;
    fields.interval.disabled = daily;
    fields.at.required = daily;
    fields.at.disabled = !daily;
    fields.timezone.required = daily;
    fields.timezone.disabled = !daily;
  }

  function fillForm(row = null) {
    editing = row;
    const body = row?.body ?? {};
    fields.dataset.innerHTML = '<option value="">仅更新磁力目录</option>' +
      datasetOptions(ctx.summary.datasets, body.dataset);
    fields.dataset.disabled = !!row;
    fields.download.checked = !!body.download;
    setFrequency(body.frequency);
    fields.start_date.value = body.start_date || "";
    fields.end_date.value = body.end_date || "";
    const period = periodLabel(body);
    $("#subscribe-range-note", form).textContent = period ? `限定 ${period}` : "日期留空时跟进新发布";
    fields.interval.value = (body.interval ?? DEFAULT_INTERVAL) / SECONDS_PER_HOUR;
    fields.schedule.value = body.at ? "daily" : "interval";
    fields.at.value = body.at || "";
    fields.timezone.value = body.timezone || timezone;
    scheduleFields();
    reset.hidden = !row;
    $("#subscribe-submit", form).textContent = row ? "保存修改" : "保存订阅";
  }

  function config(body, options) {
    return Object.fromEntries(
      options.filter((field) => body?.[field] != null).map((field) => [field, body[field]]),
    );
  }

  form.onsubmit = (event) => {
    event.preventDefault();
    ctx.run(async () => {
      const args = {
        ...config(editing?.body, advancedOptions),
        enabled: editing?.body.enabled !== false,
        download: fields.download.checked,
      };
      if (fields.dataset.value && fields.frequency.value) {
        args.dataset = fields.dataset.value;
        args.frequency = fields.frequency.value;
        if (fields.start_date.value) args.start_date = fields.start_date.value;
        if (fields.end_date.value) args.end_date = fields.end_date.value;
      }
      if (fields.schedule.value === "daily") {
        args.at = fields.at.value;
        args.timezone = fields.timezone.value.trim();
      } else args.interval = Math.round(fields.interval.valueAsNumber * SECONDS_PER_HOUR);
      await ctx.act("subscribe", args);
      fillForm();
      await refresh();
      ctx.toast("订阅已保存。");
    });
  };
  fields.dataset.onchange = () => setFrequency();
  fields.download.onchange = showRange;
  fields.schedule.onchange = scheduleFields;
  reset.onclick = () => fillForm();
  list.onclick = (event) => {
    const button = event.target.closest("[data-subscription]");
    if (!button) return;
    const row = subscriptions.find((item) => item.id === button.dataset.subscription);
    if (!row) return;
    if (button.dataset.operation === "edit") {
      fillForm(row);
      fields.schedule.focus();
      return;
    }
    ctx.run(async () => {
      if (button.dataset.operation === "remove") {
        await ctx.act("unsubscribe", { id: row.id });
        if (editing?.id === row.id) fillForm();
      } else {
        const enabled = row.body.enabled === false;
        await ctx.act("subscribe", { ...config(row.body, subscriptionOptions), enabled });
        if (editing?.id === row.id) editing = { ...editing, body: { ...editing.body, enabled } };
      }
      await refresh();
    });
  };
  fillForm();

  return (rows) => {
    subscriptions = rows;
    const content = table(
      [
        { title: "同步范围", render: ({ body }) => subscriptionScope(body) },
        { title: "检查计划", render: subscriptionSchedule },
        {
          title: "最近检查",
          render: ({ body }) => `${body.last_check ? shortTime(body.last_check) : "暂无记录"}${checkResult(body)}`,
        },
        {
          title: "操作",
          render: ({ id, body }) => `<div class="row-actions"><button type="button" class="text-link" data-subscription="${esc(id)}" data-operation="edit">编辑</button><button type="button" class="text-link" data-subscription="${esc(id)}" data-operation="toggle">${body.enabled === false ? "启用" : "停用"}</button><button type="button" class="text-link" data-subscription="${esc(id)}" data-operation="remove">取消订阅</button></div>`,
        },
      ],
      rows,
      "暂无订阅",
    );
    if (content !== lastContent) {
      list.innerHTML = content;
      lastContent = content;
    }
  };
}
