import { waitJob } from "../components/jobs.js";
import { $, esc, bytes, human, table, options, jsonDetails } from "../ui.js";

export async function upstream(ctx) {
  const [feature, { items }] = await Promise.all([ctx.api("features"), ctx.api("publisher_options")]);
  const choice = ctx.selection();
  const ranges = items.map((row) => [
    row.dataset + "/" + row.frequency, row.dataset + " · " + human(row.frequency),
  ]);
  if (!ctx.live()) return;
  $("#view").innerHTML = `
    <section class="panel"><div class="panel-header"><h2>导入来源</h2><button type="button" class="button" id="new-source">添加来源</button></div><div id="adapter-list"></div></section>
    <section class="panel"><div class="panel-header"><h2>导入范围</h2></div><div class="panel-body"><form id="fetch-form" class="form-grid"><div class="full"><div class="form-row"><label>数据范围<select name="scope" required>${options(ranges, choice.dataset + "/" + choice.frequency)}</select></label><label>开始日期<input name="start_date" type="date" value="${esc(ctx.scope().start_date || "")}" required></label><label>结束日期<input name="end_date" type="date" value="${esc(ctx.scope().end_date || "")}" required></label></div></div><label>来源<select name="source" required></select></label><label>新版本号<input name="version" type="number" min="1" value="1" required></label><label class="full">标的<input name="symbols" placeholder="空格或逗号分隔；本地文件可留空"></label><details class="advanced full"><summary>网络来源授权</summary><label class="check"><input type="checkbox" name="authorize_real">允许本次所选范围访问真实上游</label></details><div class="full"><button class="button button-primary">预览导入</button></div></form><div id="fetch-result"></div></div></section>
    <details class="panel" id="source-config"><summary class="panel-header"><h2>添加来源</h2></summary><div class="panel-body"><form id="adapter-form" class="form-grid"><label>名称<input name="id" required placeholder="唯一名称"></label><label>适配器<select name="adapter">${options(feature.adapters.available.map((r) => [r.id || r.name, r.id || r.name]))}</select></label><label class="full">本地文件路径<input name="path" placeholder="CSV / Parquet 完整路径"></label><details class="advanced full"><summary>高级适配配置</summary><label>设置 JSON<textarea name="settings" rows="4">{}</textarea></label><label class="check"><input type="checkbox" name="authorize_real">允许此来源访问真实网络</label><p class="panel-note">凭证使用环境变量名称，勿输入密码或 token。</p></details><div class="full"><button class="button button-primary">保存来源</button></div></form></div></details>`;
  $("#fetch-form button").disabled = !items.length;
  $("#new-source").onclick = () => {
    $("#source-config").open = true;
    $("#source-config").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#adapter-form [name=id]").focus();
  };
  async function paint() {
    const data = await ctx.act("source_list");
    if (!ctx.live()) return;
    $("#adapter-list").innerHTML = table(
      [
        { title: "来源", key: "id" },
        { title: "类型", key: "adapter" },
        {
          title: "可用性",
          render: (r) =>
            esc(r.availability.status || r.availability.available || "待确认"),
        },
        {
          title: "说明",
          render: (r) => esc(r.availability.reason || "可使用"),
        },
      ],
      data.sources,
      "尚未配置上游来源。",
    );
    const selected = $("#fetch-form [name=source]").value;
    $("#fetch-form [name=source]").innerHTML = options(
      data.sources.map((r) => [r.id, r.id + " · " + r.adapter]),
      selected,
      "选择来源",
    );
  }
  $("#adapter-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      const form = new FormData(e.target),
        settings = JSON.parse(form.get("settings") || "{}");
      if (form.get("adapter") === "local") settings.path = form.get("path");
      await ctx.act("source_configure", {
        id: form.get("id"),
        adapter: form.get("adapter"),
        settings,
        authorize_real: form.has("authorize_real"),
      });
      await paint();
      ctx.toast("本机上游配置已保存，尚未获取数据。");
    });
  };
  const request = () => {
    const form = new FormData($("#fetch-form"));
    const parts = String(form.get("scope")).split("/");
    const frequency = parts.pop();
    return {
      dataset: parts.join("/"),
      frequency,
      start_date: form.get("start_date"),
      end_date: form.get("end_date"),
      source: form.get("source"),
      version: Number(form.get("version")),
      symbols: String(form.get("symbols"))
        .split(/[\s,]+/)
        .filter(Boolean),
      authorize_real: form.has("authorize_real"),
    };
  };
  $("#fetch-form").onsubmit = (e) => {
    e.preventDefault();
    ctx.run(async () => {
      const args = request();
      if (!args.dataset || !args.start_date || !args.end_date)
        throw Error("请选择数据集与开始、结束日期。");
      const plan = await ctx.act("source_fetch", { ...args, dry_run: true });
      $("#fetch-result").innerHTML =
        `<div class="callout">${esc(args.dataset)} · ${esc(args.start_date)} — ${esc(args.end_date)} · v${args.version}。预览没有发起网络获取。</div>${jsonDetails(plan, "可获取范围与配置状态")}<button id="fetch-execute" class="button button-primary">执行获取、校验与本地发布</button>`;
      $("#fetch-execute").onclick = () =>
        ctx.run(async () => {
          if (JSON.stringify(args) !== JSON.stringify(request()))
            throw Error("参数已改变，请重新预览。");
          const job = await ctx.act("job_submit", {
            kind: "source_fetch",
            args: { ...args, dry_run: false },
          });
          const result = await waitJob(ctx, job.job_id, (j) => {
            if (ctx.live())
              $("#fetch-result").textContent = j.detail || "后台正在执行…";
          });
          if (ctx.live())
            $("#fetch-result").innerHTML =
              `<div class="callout">${result.published?.length ? "已发布 " + result.published.length + " 个本地日版本。" : "本次没有发布新数据。"}${result.source_unavailable?.length ? " 源端未提供：" + result.source_unavailable.map(esc).join("、") + "。" : ""} 未对外发布。</div>${table(
                [
                  { title: "已发布交易日", key: "trading_date" },
                  { title: "版本", render: (r) => "v" + r.version },
                  { title: "大小", render: (r) => bytes(r.size) },
                ],
                result.published || [],
                "本次没有发布新的日数据。",
              )}${jsonDetails(result, "日版本与任务详情")}`;
          await ctx.reloadSummary();
        });
    });
  };
  await paint();
}
