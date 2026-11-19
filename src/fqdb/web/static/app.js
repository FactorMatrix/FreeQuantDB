// FreeQuantDB routing and actions. UI shell patterns follow CNEquity's console;
// every mutation below still uses the existing FreeQuantDB Owner action API.
import {
  $,
  $$,
  label,
  human,
  datasetOptions,
  options,
  empty,
} from "./ui.js";
import { bindCatalogRefresh } from "./components/catalog-refresh.js";
import { routes, sectionTabs } from "./routes.js";

const state = {
  summary: null,
  selection: { dataset: "", frequency: "" },
  start: "",
  end: "",
  page: "overview",
  seq: 0,
  busy: 0,
};
let current = null,
  toastTimer = null,
  controller = null,
  polling = false;
let gatewaySession = new URLSearchParams(location.search).get("session"),
  gatewayClosed = false,
  gatewayConnection = null;

$(".skip-link").onclick = (event) => {
  event.preventDefault();
  $("#title").focus();
};

function setConnection(message, online = false) {
  const connection = $("#connection");
  if (connection.textContent !== message) {
    connection.textContent = message;
    connection.title = message;
  }
  connection.classList.toggle("online", online);
}

function gatewayEnded() {
  gatewayClosed = true;
  gatewayConnection?.close();
  controller?.abort();
  state.seq++;
  current = null;
  setConnection("管理界面已退出");
  $("#exit-gateway").disabled = true;
  const view = $("#view");
  view.removeAttribute("aria-busy");
  view.inert = false;
  view.innerHTML = empty(
    "管理界面已退出",
    "再次进入请运行 fqdb gateway。",
  );
}

function updateGateway(session) {
  if (gatewayClosed) return;
  if (session.active) setConnection("本地服务在线", true);
  else gatewayEnded();
}

async function heartbeatGateway() {
  if (gatewayClosed) return;
  const session = await request("/api/action/gateway_heartbeat", {
    session_id: gatewaySession,
    browser: true,
  });
  updateGateway(session);
}

$("#exit-gateway").onclick = () =>
  run(async () => {
    await request("/api/action/gateway_close", { session_id: gatewaySession });
    gatewayEnded();
  });

window.addEventListener("pagehide", () => {
  if (
    gatewaySession &&
    !gatewayClosed &&
    gatewayConnection?.readyState !== EventSource.OPEN
  )
    navigator.sendBeacon(
      "/api/action/gateway_close",
      new Blob([JSON.stringify({ session_id: gatewaySession, grace: 3 })], {
        type: "application/json",
      }),
    );
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted && gatewaySession && gatewayConnection?.readyState !== EventSource.OPEN)
    heartbeatGateway().catch(showError);
});

async function request(path, body, signal) {
  const response = await fetch(
    path,
    body === undefined
      ? { signal }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal,
        },
  );
  let data;
  try {
    data = await response.json();
  } catch {
    throw Error(`服务未返回有效结果（HTTP ${response.status}）。`);
  }
  if (!response.ok)
    throw Error(
      data.error || data.detail || `请求失败（HTTP ${response.status}）。`,
    );
  return data;
}
const params = (object) =>
  new URLSearchParams(
    Object.entries(object || {}).filter(
      ([, v]) => v !== undefined && v !== null && v !== "",
    ),
  );
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 4500);
}
function showError(error) {
  if (gatewayClosed || error.name === "AbortError") return;
  let message = error.message || String(error);
  if (
    message.includes("Missing=") ||
    message.includes("needs verification") ||
    message.includes("Record gaps=")
  )
    message =
      "所选范围有缺失、待更新或未经确认的数据。请在目录或质量页查看后，显式同步 / 修复。\n" +
      message;
  if (message.includes("No local records"))
    message =
      "所选标的没有可确认的本地记录，请核对代码、日期和覆盖信息。\n" + message;
  $("#error").hidden = false;
  $("#error").textContent = message;
}
async function run(fn) {
  state.busy++;
  $("#error").hidden = true;
  try {
    await fn();
  } catch (e) {
    showError(e);
  } finally {
    state.busy--;
  }
}
function selection() {
  return { ...state.selection };
}
function scope() {
  return {
    ...selection(),
    start_date: $("#start").value || null,
    end_date: $("#end").value || null,
  };
}
function defaultRange(row) {
  const end = row.latest_local || row.latest_published || row.end_date || "";
  let start = row.start_date || "";
  if (end && start) {
    const recent = new Date(end + "T12:00:00Z");
    recent.setUTCDate(recent.getUTCDate() - 30);
    const from = recent.toISOString().slice(0, 10);
    start = start > from ? start : from;
  }
  state.start = start;
  state.end = end;
}
function setSelection(dataset, frequency) {
  const row = state.summary?.datasets.find(
    (r) => r.dataset === dataset && (!frequency || r.frequency === frequency),
  );
  if (!row) return;
  const changed =
    row.dataset !== state.selection.dataset ||
    row.frequency !== state.selection.frequency;
  state.selection = { dataset: row.dataset, frequency: row.frequency };
  if (changed) {
    defaultRange(row);
      state.plan = null;
    state.queryIds = null;
    state.queryFields = null;
    state.year = null;
    state.month = null;
    state.catalogOffset = 0;
    state.auditOffset = 0;
    state.taskOffset = 0;
    state.openGroups = [];
  }
  fillContext();
}
function setRange(start, end) {
  state.start = start || "";
  state.end = end || "";
  fillContext();
}
function fillContext() {
  if (!state.summary) return;
  $("#dataset").innerHTML = datasetOptions(
    state.summary.datasets,
    state.selection.dataset,
  );
  const freqs = state.summary.datasets
    .filter((r) => r.dataset === state.selection.dataset)
    .map((r) => r.frequency);
  $("#frequency").innerHTML = options(
    freqs.map((f) => [f, human(f)]),
    state.selection.frequency,
  );
  $("#start").value = state.start || "";
  $("#end").value = state.end || "";
}
function acceptSummary(data) {
  state.summary = data;
  if (
    !data.datasets.some(
      (r) =>
        r.dataset === state.selection.dataset &&
        r.frequency === state.selection.frequency,
    )
  ) {
    const row =
      data.datasets.find((r) => r.local) ||
      data.datasets.find((r) => r.published) ||
      data.datasets[0];
    if (row) setSelection(row.dataset, row.frequency);
    else {
      state.selection = { dataset: "", frequency: "" };
      setRange("", "");
    }
  } else fillContext();
  const running = Object.entries(data.task_counts)
    .filter(([s]) => !["complete", "failed", "cancelled", "paused"].includes(s))
    .reduce((n, [, v]) => n + v, 0);
  $("#nav-task-count").hidden = !running;
  $("#nav-task-count").textContent = running;
}
async function reloadSummary() {
  const data = await request("/api/console/summary?include_runtime=false");
  acceptSummary(data);
  return data;
}
function navigate(page, values = {}) {
  if (values.dataset) setSelection(values.dataset, values.frequency);
  const query = params({ ...selection(), ...values }).toString();
  const hash = "#/" + page + (query ? "?" + query : "");
  if (location.hash === hash) render();
  else location.hash = hash;
}
async function render() {
  if (!state.summary || gatewayClosed) return;
  controller?.abort();
  controller = new AbortController();
  const { signal } = controller;
  const seq = ++state.seq;
  const parsed = new URL(
    location.hash.replace(/^#/, "") || "/overview",
    "http://local",
  );
  const requested = parsed.pathname.slice(1) || "overview";
  state.page = Object.hasOwn(routes, requested) ? requested : "overview";
  const route = routes[state.page];
  const changedRoute = state.lastHash !== location.hash;
  if (changedRoute) window.scrollTo(0, 0);
  state.lastHash = location.hash;
  if (changedRoute && state.page === "detail")
    state.detailTab = parsed.searchParams.get("tab") || "coverage";
  if (changedRoute && parsed.searchParams.get("dataset"))
    setSelection(
      parsed.searchParams.get("dataset"),
      parsed.searchParams.get("frequency"),
    );
  if (changedRoute && parsed.searchParams.get("year"))
    state.year = parsed.searchParams.get("year");
  if (changedRoute && parsed.searchParams.get("month"))
    state.month = parsed.searchParams.get("month");
  const active = route.parent || state.page;
  $$(".sidebar [data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === active);
    a.setAttribute(
      "aria-current",
      a.dataset.page === active ? "page" : "false",
    );
  });
  const section = $("#section-nav");
  const tabs = route.hideSectionTabs ? [] : sectionTabs(active);
  section.hidden = tabs.length === 0;
  section.innerHTML = tabs
    .map(
      ([page, { tab }]) =>
        `<a href="#/${page}" data-page="${page}" class="${page === state.page ? "active" : ""}" ${page === state.page ? 'aria-current="page"' : ""}>${tab}</a>`,
    )
    .join("");
  $("#title").textContent = route.title;
  $("#refresh-magnets").hidden = !route.catalogAction;
  $(".page-heading").classList.toggle(
    "dataset-heading",
    state.page === "detail",
  );
  document.title = `${route.title} · FreeQuantDB`;
  $("#context").hidden = !route.scope || (state.page === "detail" && state.detailTab === "coverage");
  $("#context").classList.toggle("scope-identity", route.scope === "identity");
  $("#heading-actions").innerHTML = route.headingAction
    ? `<button class="button" data-heading="datasets">${route.headingAction}</button>`
    : "";
  $$("[data-heading]").forEach(
    (b) => (b.onclick = () => navigate(b.dataset.heading)),
  );
  $("#error").hidden = true;
  const view = $("#view");
  if (changedRoute) view.replaceChildren();
  view.setAttribute("aria-busy", "true");
  view.inert = true;
  const ctx = {
    state,
    get summary() {
      return state.summary;
    },
    label,
    selection,
    scope,
    setSelection,
    setRange,
    navigate,
    render,
    run,
    toast,
    acceptSummary,
    reloadSummary,
    live: () => seq === state.seq,
    request: (path, body) => request(path, body),
    api: (view, args) =>
      request(
        "/api/console/" + view + (args ? "?" + params(args) : ""),
        undefined,
        signal,
      ),
    act: (action, args = {}) => request("/api/action/" + action, args),
    poll: null,
  };
  current = ctx;
  bindCatalogRefresh(ctx);
  try {
    if (!state.selection.dataset && route.scope)
      view.innerHTML = empty(
        "本地目录没有数据集",
        "更新磁力链接，或将数据集索引和包说明放入本地 catalog。",
      );
    else await route.render(ctx);
  } catch (e) {
    if (e.name !== "AbortError" && ctx.live()) {
      showError(e);
      view.innerHTML = empty(
        "当前页面尚未加载完成",
        "修正范围或检查服务后，可点击刷新重试。",
      );
    }
  } finally {
    if (ctx.live()) {
      view.removeAttribute("aria-busy");
      view.inert = false;
    }
  }
}
$("#refresh").onclick = () =>
  run(async () => {
    await reloadSummary();
    await render();
  });
$("#dataset").onchange = (e) => {
  setSelection(e.target.value);
  navigate(state.page, state.page === "detail" ? { tab: state.detailTab } : {});
};
$("#frequency").onchange = (e) => {
  setSelection(state.selection.dataset, e.target.value);
  navigate(state.page, state.page === "detail" ? { tab: state.detailTab } : {});
};
$("#apply-scope").onclick = () => {
  state.start = $("#start").value;
  state.end = $("#end").value;
  state.catalogOffset = 0;
  state.auditOffset = 0;
  run(render);
};
$("#close-package").onclick = () => $("#package-detail").close();
$("#package-detail").onclick = (e) => {
  if (
    e.target === $("#package-detail") &&
    e.clientX < $("#package-detail").getBoundingClientRect().left
  )
    $("#package-detail").close();
};
window.addEventListener("hashchange", () => run(render));
setInterval(async () => {
  if (gatewayClosed || polling) return;
  polling = true;
  try {
    if (gatewaySession && gatewayConnection?.readyState !== EventSource.OPEN) {
      try {
        await heartbeatGateway();
      } catch {
        if (!gatewayClosed) setConnection("服务连接中断");
        return;
      }
    }
    const ctx = current;
    if (gatewayClosed || state.busy || document.hidden || !ctx?.poll) return;
    try {
      await ctx.poll();
    } catch (e) {
      if (ctx.live() && e.name !== "AbortError") showError(e);
    }
  } finally {
    polling = false;
  }
}, 5000);
run(async () => {
  if (!gatewaySession) {
    gatewaySession = (await request("/api/action/gateway_open", {})).session_id;
    const address = new URL(location.href);
    address.searchParams.set("session", gatewaySession);
    history.replaceState(null, "", address);
  }
  gatewayConnection = new EventSource("/api/gateway/" + gatewaySession);
  gatewayConnection.onmessage = (event) => updateGateway(JSON.parse(event.data));
  gatewayConnection.onerror = () => {
    if (!gatewayClosed) setConnection("服务连接中断");
  };
  await Promise.all([heartbeatGateway(), reloadSummary()]);
  await render();
});
