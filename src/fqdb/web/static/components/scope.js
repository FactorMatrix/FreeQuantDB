import { $, esc, options, datasetOptions, human } from "../ui.js";

// Page-local scope controls keep filters next to the action they affect.
export function scopeControls(ctx, id, dates = false) {
  const selected = ctx.selection(),
    range = ctx.scope();
  const freqs = ctx.summary.datasets
    .filter((r) => r.dataset === selected.dataset)
    .map((r) => r.frequency);
  return `<div class="form-row local-scope" data-scope="${id}"><label>数据集<select id="${id}-dataset" aria-label="数据集">${datasetOptions(
    ctx.summary.datasets,
    selected.dataset,
  )}</select></label><label>频率<select id="${id}-frequency" aria-label="频率">${options(
    freqs.map((v) => [v, human(v)]),
    selected.frequency,
  )}</select></label>${dates ? `<label>开始日期<input id="${id}-start" type="date" value="${esc(range.start_date || "")}"></label><label>结束日期<input id="${id}-end" type="date" value="${esc(range.end_date || "")}"></label>` : ""}</div>`;
}
export function bindScope(ctx, id, dates = false) {
  $(`#${id}-dataset`).onchange = (e) => {
    ctx.setSelection(e.target.value);
    ctx.render();
  };
  $(`#${id}-frequency`).onchange = (e) => {
    ctx.setSelection(ctx.selection().dataset, e.target.value);
    ctx.render();
  };
  if (dates)
    for (const side of ["start", "end"])
      $(`#${id}-${side}`).onchange = () => {
        ctx.setRange($(`#${id}-start`).value, $(`#${id}-end`).value);
        ctx.state.plan = null;
      };
}
