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
        ["added", "新增"], ["updated", "更新"],
        ["hidden", "手工隐藏"], ["removed", "撤下"],
      ].filter(([key]) => count(key) > 0).map(([key, label]) => `${label} ${num(count(key))}`);
      showResult(parts.length ? `目录已同步 · ${parts.join("，")}` : "目录已同步，无变更");
    } catch (error) {
      output.classList.add("error-callout");
      showResult("同步失败，继续使用本地目录。", error.message);
    } finally {
      button.disabled = false;
    }
  };

  function showResult(message, detail) {
    output.innerHTML = `
      <div class="catalog-result-line">
        <span>${esc(message)}</span>
        <button type="button" class="icon-button" aria-label="关闭同步提示">×</button>
      </div>
      ${detail ? `<details><summary>错误详情</summary><p>${esc(detail)}</p></details>` : ""}`;
    $("button", output).onclick = () => {
      output.hidden = true;
      button.focus();
    };
  }
}
