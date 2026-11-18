import { $$, esc, num, bytes, pill, human } from "../ui.js";

const dsLink = (row) =>
  `<button class="text-link" data-open-dataset="${esc(row.dataset)}" data-frequency="${esc(row.frequency)}">${esc(row.name || row.dataset)}</button><span class="cell-sub">${esc(row.dataset)} · ${esc(human(row.frequency))}</span>`;
export const datasetCols = [
  { title: "数据集", render: dsLink },
  { title: "状态", render: (r) => pill(r.status) },
  { title: "本地 / 目录", render: (r) => `${num(r.local)} / ${num(r.published)}`, n: true },
  { title: "目录日期范围", render: (r) => `${esc(r.start_date || "—")} — ${esc(r.end_date || "—")}` },
  { title: "目录行数", render: (r) => num(r.published_rows), n: true },
  { title: "标的数", render: (r) => r.published_instruments == null && r.published > 1 ? "见单包" : num(r.published_instruments), n: true },
  { title: "本地体积", render: (r) => bytes(r.bytes), n: true },
];

export function bindDatasetLinks(ctx, root = document) {
  $$("[data-open-dataset]", root).forEach(
    (button) => (button.onclick = () => ctx.navigate("detail", {
      dataset: button.dataset.openDataset, frequency: button.dataset.frequency, tab: "coverage",
    })),
  );
}
