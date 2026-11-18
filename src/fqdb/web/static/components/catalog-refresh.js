import { $, esc, num } from "../ui.js";
import { waitJob } from "./jobs.js";

export function bindCatalogRefresh(ctx, button = $("#refresh-magnets")) {
  const output = $("#catalog-refresh-result");
  button.onclick = async () => {
    button.disabled = true;
    output.hidden = false;
    output.classList.remove("error-callout");
    output.textContent = "正在同步目录…";
    try {
      const job = await ctx.act("job_submit", { kind: "catalog_refresh", args: {} });
      const result = await waitJob(ctx, job.job_id);
      await ctx.reloadSummary();
      await ctx.render();
      const count = (key) => Array.isArray(result[key]) ? result[key].length : result[key];
      const parts = [
        ["added", "新增"], ["updated", "更新"], ["unchanged", "未变更"],
        ["hidden", "手工隐藏"], ["removed", "撤下"],
      ].filter(([key]) => count(key) != null).map(([key, label]) => `${label} ${num(count(key))}`);
      output.innerHTML = `目录已同步${parts.length ? " · " + esc(parts.join("，")) : ""}`;
    } catch (error) {
      output.classList.add("error-callout");
      output.textContent = `同步失败，继续使用本地目录。${error.message}`;
    } finally {
      button.disabled = false;
    }
  };
}
