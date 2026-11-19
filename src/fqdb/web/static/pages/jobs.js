import {
  $,
  $$,
  esc,
  pill,
  table,
  pager,
  bindPager,
  jsonDetails,
  shortTime,
} from "../ui.js";

export async function jobs(ctx) {
  $("#view").innerHTML =
    `<section class="panel"><div class="panel-header"><h2>后台作业</h2></div><div id="jobs-list"></div></section>`;
  async function paint() {
    const data = await ctx.act("jobs");
    if (!ctx.live()) return;
    const offset = ctx.state.jobOffset || 0,
      page = { total: data.jobs.length, offset, limit: 10 };
    $("#jobs-list").innerHTML =
      table(
        [
          {
            title: "类型",
            render: (r) =>
              esc(
                {
                  check: "质量校验",
                  publish: "本地发布",
                  scan: "扫描登记",
                  source_fetch: "上游获取",
                  catalog_refresh: "目录检查",
                  subscription_check: "订阅检查",
                  transfer_prepare: "准备本地分片",
                  download_validate: "验证下载文件",
                }[r.kind] || r.kind,
              ),
          },
          { title: "状态", render: (r) => pill(r.status) },
          {
            title: "进度与说明",
            render: (r) =>
              r.status === "complete"
                ? "完成"
                : r.status === "paused"
                  ? r.kind === "subscription_check" ? "检查已中断" : "等待手动重新提交"
                  : esc(r.detail || "等待执行"),
          },
          { title: "最近更新", render: (r) => shortTime(r.updated_at) },
          {
            title: "结果 / 原因",
            render: (r) =>
              `${r.error ? `<span class="bad-text">${esc(r.error)}</span>` : ""}${r.result ? jsonDetails(r.result, "查看结果") : ""}${["queued", "running"].includes(r.status) ? `<button class="text-link" data-cancel-job="${r.id}">请求取消</button>` : ""}${jsonDetails(r.args, "范围与参数")}`,
          },
        ],
        data.jobs.slice(offset, offset + 10),
        "暂无后台作业。",
      ) + pager(page, "jobs");
    bindPager("jobs", page, (value) => {
      ctx.state.jobOffset = value;
      ctx.run(paint);
    });
    $$("[data-cancel-job]").forEach(
      (b) =>
        (b.onclick = () =>
          ctx.run(async () => {
            await ctx.act("job_cancel", { id: b.dataset.cancelJob });
            await paint();
          })),
    );
  }
  await paint();
  ctx.poll = paint;
}
