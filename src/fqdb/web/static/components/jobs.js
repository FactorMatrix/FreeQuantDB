import { $ } from "../ui.js";

export async function waitJob(ctx, id, paint = null) {
  for (;;) {
    const job = await ctx.act("job_status", { id });
    if (paint) paint(job);
    const activity = $("#activity");
    if (activity)
      activity.textContent = `后台作业：${job.detail || job.status}`;
    if (job.status === "complete") return job.result;
    if (job.status === "paused")
      throw Error("此作业已中断，等待手动重新提交。");
    if (["failed", "cancelled"].includes(job.status))
      throw Error(job.error || "作业已取消；已完成的不可变文件保留。");
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
}
