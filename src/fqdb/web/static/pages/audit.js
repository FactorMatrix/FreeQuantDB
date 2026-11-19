import { $, esc, options, pager, bindPager, table } from "../ui.js";
import { bindPackages, packageColumns } from "../components/packages.js";

const availability = {
  available: ["good", "本地已有"],
  missing: ["warn", "本地缺失"],
  invalid: ["bad", "文件异常"],
  new_version: ["warn", "本地有旧版本"],
};
const coverage = {
  verified: ["good", "已核对"],
  mismatch: ["bad", "有差异"],
  error: ["bad", "核对失败"],
};
const evidenceState = (state = ["neutral", "待核对"]) =>
  `<span class="ops-state ${state[0]}"><i class="dot" aria-hidden="true"></i>${state[1]}</span>`;

export async function audit(ctx) {
  const args = { ...ctx.scope(), offset: ctx.state.auditOffset || 0, limit: 30 };
  if (ctx.state.auditStatus) args.status = ctx.state.auditStatus;
  const data = await ctx.api("audit", args);
  if (!ctx.live()) return;
  $("#view").innerHTML = `
    <section class="panel ops-audit">
      <div class="panel-header">
        <h2>数据包核对</h2>
        <select id="audit-status" aria-label="包状态筛选">${options(
          ["anomaly", "pending", "missing", "revision", "no_peers", "local"].map((s) => [s, ctx.label(s)]),
          ctx.state.auditStatus, "全部状态",
        )}</select>
      </div>
      ${table([
        ...packageColumns.slice(0, 2),
        { title: "本地文件", render: (r) => evidenceState(availability[r.local_status]) },
        { title: "覆盖核对", render: (r) => evidenceState(coverage[r.coverage_status]) },
        { title: "发现的问题", render: (r) => r.issues?.length ? esc(r.issues.map((i) => i.message || i.code).join("、")) : '<span class="muted">—</span>' },
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
