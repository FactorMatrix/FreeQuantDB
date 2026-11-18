import { $, esc, empty, human, num, options, pill, table, packageKind, packagePeriod } from "../ui.js";
import { waitJob } from "../components/jobs.js";

export async function publish(ctx) {
  const { items } = await ctx.api("publisher_options");
  if (!ctx.live()) return;
  if (!items.length) {
    $("#view").innerHTML = empty("暂无发布配置", "可在设置中注册数据描述。");
    return;
  }
  const selected = ctx.selection();
  const scopes = items.map(({ dataset, frequency }) => {
    const scope = dataset + "/" + frequency;
    return [scope, dataset + " · " + human(frequency)];
  });
  $("#view").innerHTML = `
    <section class="panel">
      <div class="panel-header"><h2>生成数据包与磁力链接</h2></div>
      <div class="panel-body">
        <form id="publish-form" class="form-grid">
          <label>数据范围<select name="scope">${options(scopes, selected.dataset + "/" + selected.frequency)}</select></label>
          <label>打包方式<select name="granularity">${options([
            ["day", "按交易日"], ["month", "完整月"], ["year", "完整年"],
          ], "day")}</select></label>
          <label class="full">本地输入文件<input name="input_path" required placeholder="Parquet 或 CSV 文件的绝对路径"></label>
          <label>包标识<input name="period" pattern="[0-9]{4}([0-9]{2}){0,2}" placeholder="YYYY、YYYYMM 或 YYYYMMDD"></label>
          <label>内容版本<input type="number" min="1" name="version" value="1"></label>
          <div class="full action-row"><button class="button button-primary">生成本地数据包</button></div>
        </form>
      </div>
    </section>
    <div id="publish-result"></div>
  `;
  const form = $("#publish-form");
  form.onsubmit = (event) => {
    event.preventDefault();
    return ctx.run(async () => {
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const values = new FormData(form);
        const parts = String(values.get("scope")).split("/");
        const frequency = parts.pop();
        const period = values.get("period");
        const granularity = values.get("granularity");
        if (granularity !== "day" && !period) throw Error("请填写年份或月份标识。");
        const response = await ctx.act("job_submit", {
          kind: "publish",
          args: {
            dataset: parts.join("/"),
            frequency,
            granularity,
            input_path: values.get("input_path"),
            version: Number(values.get("version")),
            ...(period ? { period } : {}),
          },
        });
        const result = await waitJob(ctx, response.job_id, (job) => {
          if (ctx.live()) $("#publish-result").innerHTML =
            `<div class="callout">${pill(job.status)} ${esc(job.detail || "")}</div>`;
        });
        if (!ctx.live()) return;
        await ctx.reloadSummary();
        $("#publish-result").innerHTML = `
          <section class="panel">
            <div class="panel-header"><h2>已生成数据包</h2><span class="panel-meta">${num(result.entries?.length)} 个包</span></div>
            ${table([
              { title: "时期", render: (r) => esc(packagePeriod(r)) + " · " + packageKind(r) },
              { title: "路径", render: (r) => esc(r.file) },
            ], result.entries || [], "本次没有新增数据包。")}
          </section>`;
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
  };
}
