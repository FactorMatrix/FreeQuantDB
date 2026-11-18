import { $, esc, num, options, pager, bindPager, table } from "../ui.js";
import { bindPackages, packageColumns } from "../components/packages.js";

export async function audit(ctx) {
  const args = { ...ctx.scope(), offset: ctx.state.auditOffset || 0, limit: 30 };
  if (ctx.state.auditStatus) args.status = ctx.state.auditStatus;
  const data = await ctx.api("audit", args);
  if (!ctx.live()) return;
  $("#view").innerHTML = `
    <section class="panel">
      <div class="panel-header"><h2>数据包核对</h2><span class="panel-meta">${num(data.total)} 个匹配包</span></div>
      <div class="filter-bar">
        <select id="audit-status" aria-label="包状态筛选">${options(
          ["anomaly", "pending", "missing", "revision", "no_peers", "local"].map((s) => [s, ctx.label(s)]),
          ctx.state.auditStatus, "全部状态",
        )}</select>
      </div>
      ${table([
        ...packageColumns.slice(0, 2),
        { title: "文件", render: (r) => esc({ available: "本地已有", missing: "本地缺失", invalid: "文件异常", new_version: "本地有旧版本" }[r.local_status] || "待核对") },
        { title: "覆盖", render: (r) => esc({ verified: "已核对", mismatch: "有差异", error: "核对失败" }[r.coverage_status] || "待核对") },
        { title: "问题", render: (r) => r.issues?.length ? esc(r.issues.map((i) => i.message || i.code).join("；")) : "—" },
        packageColumns.at(-1),
      ], data.items, "当前范围没有可核对的数据包。", "check-table")}
      ${pager(data, "audit")}
    </section>`;
  $("#audit-status").onchange = (event) => {
    ctx.state.auditStatus = event.target.value;
    ctx.state.auditOffset = 0;
    ctx.render();
  };
  bindPager("audit", data, (offset) => {
    ctx.state.auditOffset = offset;
    ctx.render();
  });
  bindPackages(ctx);
}
