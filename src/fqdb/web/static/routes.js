import { overview } from "./pages/overview.js";
import { datasets } from "./pages/datasets.js";
import { detail } from "./pages/dataset-detail.js";
import { downloads } from "./pages/downloads.js";
import { jobs } from "./pages/jobs.js";
import { audit } from "./pages/audit.js";
import { provider } from "./pages/provider.js";
import { publish } from "./pages/publish.js";
import { upstream } from "./pages/upstream.js";
import { settings } from "./pages/settings.js";

export const routes = {
  overview: { title: "概览", render: overview, headingAction: "浏览数据集" },
  datasets: { title: "数据集", render: datasets },
  detail: {
    title: "数据集详情",
    render: detail,
    parent: "datasets",
    scope: "identity",
    headingAction: "← 返回数据集",
  },
  downloads: { title: "下载", render: downloads, tab: "下载任务" },
  jobs: {
    title: "后台作业",
    render: jobs,
    parent: "downloads",
    tab: "后台作业",
  },
  validation: { title: "质量", render: audit, scope: "range" },
  provider: { title: "提供", render: provider, tab: "做种" },
  publish: {
    title: "本地发布",
    render: publish,
    parent: "provider",
    tab: "本地发布",
  },
  upstream: {
    title: "上游接入",
    render: upstream,
    parent: "provider",
    tab: "数据导入",
  },
  settings: { title: "设置", render: settings },
};

export function sectionTabs(section) {
  return Object.entries(routes).filter(
    ([name, route]) => route.tab && (route.parent || name) === section,
  );
}
