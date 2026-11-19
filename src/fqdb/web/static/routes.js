const page = (module, name = module) => async (ctx) => {
  const render = (await import(`./pages/${module}.js`))[name];
  if (ctx.live()) await render(ctx);
};

export const routes = {
  overview: { title: "概览", render: page("overview"), catalogAction: true },
  datasets: { title: "数据集", render: page("datasets"), tab: "目录", catalogAction: true },
  detail: {
    title: "数据集详情",
    render: page("dataset-detail", "detail"),
    parent: "datasets",
    hideSectionTabs: true,
    scope: "identity",
    catalogAction: true,
    headingAction: "← 返回数据集",
  },
  downloads: { title: "传输", render: page("downloads"), tab: "任务", catalogAction: true },
  jobs: {
    title: "后台作业",
    render: page("jobs"),
    parent: "downloads",
    tab: "后台作业",
  },
  validation: { title: "质量", render: page("audit"), scope: "range" },
  publish: {
    title: "本地发布",
    render: page("publish"),
    parent: "datasets",
    tab: "本地发布",
  },
  upstream: {
    title: "上游接入",
    render: page("upstream"),
    parent: "datasets",
    tab: "数据导入",
  },
  settings: { title: "设置", render: page("settings") },
};

export function sectionTabs(section) {
  return Object.entries(routes).filter(
    ([name, route]) => route.tab && (route.parent || name) === section,
  );
}
