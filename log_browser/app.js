"use strict";

const byId = id => document.getElementById(id);
const tree = byId("runs");
const feed = byId("feed");
const connection = byId("connection");
const filterList = byId("filters");
const resetFilters = byId("clear-filters");
const types = new Map([
  ["user", "User"], ["assistant", "Assistant"], ["reasoning", "Reasoning"],
  ["tool", "Tool"], ["file", "File change"], ["usage", "Usage"],
  ["system", "System"], ["error", "Error"],
]);
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });
const numberFormat = new Intl.NumberFormat();
const pageSize = 60;
const expandedRuns = new Set();
const expandedMessages = new Set();
const excludedTypes = new Set();
let runs = [];
let activeRun = null;
let activeLog = null;
let followLatest = true;
let selection = 0;
let revision = "";
let messages = [];
let limit = pageSize;
let treeKey = "";
let filterKey = "";
let loading = true;
let failure = "";
let busy = false;
let queued = false;
let timer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function dateLabel(milliseconds) {
  const date = new Date(milliseconds);
  return Number.isNaN(date.getTime()) ? "Unknown date" : dateFormat.format(date);
}

function emptyState(title, description) {
  const node = element("div", "empty");
  node.append(element("p", "empty-title", title), element("p", "", description));
  return node;
}

function setConnection(state, label) {
  connection.parentElement.dataset.state = state;
  if (connection.textContent !== label) connection.textContent = label;
}

function select(run, log, follow) {
  const nextRun = run?.name ?? null;
  const nextLog = log?.id ?? null;
  followLatest = follow;
  if (nextRun === activeRun && nextLog === activeLog) return;
  activeRun = nextRun;
  activeLog = nextLog;
  if (run) expandedRuns.add(run.name);
  selection++;
  revision = "";
  messages = [];
  limit = pageSize;
  failure = "";
  expandedMessages.clear();
  const runLabel = byId("viewer-run");
  runLabel.hidden = !run;
  runLabel.textContent = run?.name ?? "";
  byId("viewer-title").textContent = log?.name ?? (run ? "Run logs" : "Log stream");
  feed.scrollTop = 0;
}

function selectAndRefresh(run, log, follow) {
  select(run, log, follow);
  drawSelection();
  drawFilters();
  drawFeed();
  schedule(0);
}

function drawSelection() {
  for (const button of tree.querySelectorAll("button[data-log]")) {
    if (button.dataset.log === activeLog) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  }
}

function drawTree() {
  // Changes to file contents do not require rebuilding the tree.
  const key = JSON.stringify(runs.map(run => [run.name, run.logs.map(log => log.id)]));
  if (key !== treeKey) {
    treeKey = key;
    byId("run-count").textContent = numberFormat.format(runs.length);
    const focusKey = tree.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    const position = tree.scrollTop;
    const content = document.createDocumentFragment();
    if (!runs.length) content.append(element("p", "sidebar-empty", "No article runs found."));
    for (const run of runs) {
      const branch = element("details", "run");
      branch.open = expandedRuns.has(run.name);
      const summary = element("summary");
      summary.dataset.key = run.name;
      summary.append(
        element("span", "run-name", run.name),
        element("span", "run-time", dateLabel(run.timestamp * 1000)),
      );
      summary.addEventListener("click", event => {
        // Details handles keyboard activation and its own open state.
        if (branch.open && activeRun !== run.name) {
          event.preventDefault();
          selectAndRefresh(run, run.logs[0], true);
        } else if (branch.open) {
          expandedRuns.delete(run.name);
        } else {
          expandedRuns.add(run.name);
          selectAndRefresh(run, run.logs[0], true);
        }
      });
      const children = element("div", "run-logs");
      if (!run.logs.length) children.append(element("p", "sidebar-empty", "Waiting for logs…"));
      for (const log of run.logs) {
        const button = element("button", "log-button log-name", log.name);
        button.type = "button";
        button.title = log.source === "file" ? log.name : "Codex CLI rollout: " + log.name;
        button.dataset.key = log.id;
        button.dataset.log = log.id;
        button.addEventListener("click", () => selectAndRefresh(run, log, false));
        children.append(button);
      }
      branch.append(summary, children);
      content.append(branch);
    }
    tree.replaceChildren(content);
    if (focusKey) {
      const target = Array.from(tree.querySelectorAll("[data-key]")).find(node => node.dataset.key === focusKey);
      target?.focus({ preventScroll: true });
    }
    tree.scrollTop = position;
  }
  drawSelection();
}

function drawFilters() {
  const counts = new Map();
  for (const message of messages) counts.set(message.type, (counts.get(message.type) ?? 0) + 1);
  const key = JSON.stringify([Array.from(counts), Array.from(excludedTypes)]);
  if (key === filterKey) return;
  filterKey = key;
  const focusType = filterList.contains(document.activeElement) ? document.activeElement.value : null;
  resetFilters.hidden = !excludedTypes.size;
  const content = document.createDocumentFragment();
  if (!counts.size) content.append(element("p", "sidebar-empty", "Message types appear when the log has entries."));
  for (const [type, label] of types) {
    if (!counts.has(type)) continue;
    const option = element("label", "filter");
    const input = element("input");
    input.type = "checkbox";
    input.value = type;
    input.checked = !excludedTypes.has(type);
    input.addEventListener("change", () => {
      if (input.checked) excludedTypes.delete(type);
      else excludedTypes.add(type);
      limit = pageSize;
      drawFilters();
      drawFeed();
      feed.scrollTop = 0;
    });
    option.append(input, element("span", "", label), element("span", "count", numberFormat.format(counts.get(type))));
    content.append(option);
  }
  filterList.replaceChildren(content);
  if (focusType) filterList.querySelector('input[value="' + focusType + '"]')?.focus({ preventScroll: true });
}

function messageCard(message) {
  const card = element("article", "message");
  card.dataset.line = message.line;
  const header = element("header", "message-heading");
  const badge = element("span", "badge", types.get(message.type) ?? message.type);
  badge.dataset.type = message.type;
  header.append(badge);
  if (message.detail) header.append(element("span", "event", message.detail));
  const reference = "Line " + message.line;
  if (message.time) {
    const time = element("time", "message-reference", dateLabel(Date.parse(message.time)));
    time.dateTime = message.time;
    time.title = reference;
    header.append(time);
  } else {
    header.append(element("span", "message-reference", reference));
  }
  card.append(header);

  const lines = message.text.split("\n", 13);
  if (message.text.length > 1400 || lines.length > 12) {
    const body = element("details", "message-body");
    body.open = expandedMessages.has(message.line);
    const summary = element("summary");
    const preview = element("pre", "preview", lines.slice(0, 12).join("\n").slice(0, 1400) + "…");
    const action = element("span", "message-action", body.open ? "Collapse message" : "Expand message");
    summary.append(preview, action);
    body.append(summary);
    if (body.open) body.append(element("pre", "", message.text));
    body.addEventListener("toggle", () => {
      if (body.open) {
        expandedMessages.add(message.line);
        if (!body.querySelector(":scope > pre")) body.append(element("pre", "", message.text));
      } else {
        expandedMessages.delete(message.line);
      }
      action.textContent = body.open ? "Collapse message" : "Expand message";
    });
    card.append(body);
  } else {
    card.append(element("pre", "", message.text));
  }
  return card;
}

function drawFeed({ preserve = false } = {}) {
  const top = feed.getBoundingClientRect().top;
  const scrolled = feed.scrollTop > 0 || (window.scrollY > 0 && top < 0);
  const anchor = preserve && scrolled
    ? Array.from(feed.querySelectorAll(".message")).find(card => card.getBoundingClientRect().bottom > Math.max(top, 0))
    : null;
  const offset = anchor?.getBoundingClientRect().top;
  const focusLine = feed.contains(document.activeElement) ? document.activeElement.closest(".message")?.dataset.line : null;
  const visible = messages.filter(message => !excludedTypes.has(message.type));

  if (!visible.length) {
    let state;
    if (failure) state = emptyState("Unable to load the log", failure + " Retrying automatically.");
    else if (loading) state = emptyState("Looking for article runs", "The run list refreshes automatically.");
    else if (!activeRun) state = emptyState("No article runs yet", "Run folders named work_article_{idx}_{unixtimestamp} will appear here as they are created.");
    else if (!activeLog) state = emptyState("Waiting for logs", "New log files will appear under this run.");
    else if (!revision) state = emptyState("Loading log", "Reading the current log file.");
    else if (!messages.length) state = emptyState("Waiting for messages", "New entries will appear here.");
    else state = emptyState("No messages match", "Use the message type filters to show more entries.");
    feed.replaceChildren(state);
    return;
  }

  const content = document.createDocumentFragment();
  for (const message of visible.slice(0, limit)) content.append(messageCard(message));
  if (visible.length > limit) {
    const older = element("button", "", "Show older messages");
    older.id = "older";
    older.type = "button";
    older.addEventListener("click", () => {
      const firstNew = visible[limit].line;
      limit += pageSize;
      drawFeed({ preserve: true });
      const target = feed.querySelector('[data-line="' + firstNew + '"]');
      target.tabIndex = -1;
      target.focus();
    });
    content.append(older);
  }
  feed.replaceChildren(content);
  if (focusLine) {
    feed.querySelector('[data-line="' + focusLine + '"] summary')?.focus({ preventScroll: true });
  }
  if (anchor) {
    const replacement = feed.querySelector('[data-line="' + anchor.dataset.line + '"]');
    if (replacement) {
      const difference = replacement.getBoundingClientRect().top - offset;
      if (feed.scrollTop || top >= 0) feed.scrollTop += difference;
      else window.scrollBy(0, difference);
    }
  }
}

resetFilters.addEventListener("click", () => {
  excludedTypes.clear();
  limit = pageSize;
  drawFilters();
  drawFeed();
  filterList.querySelector("input")?.focus();
});

async function getJson(path, query = {}) {
  const response = await fetch(path + "?" + new URLSearchParams(query), {
    cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error((await response.json()).error);
  return response.json();
}

function schedule(delay = 1000) {
  clearTimeout(timer);
  if (busy) {
    queued = true;
    return;
  }
  timer = setTimeout(refresh, delay);
}

async function refresh() {
  busy = true;
  const wasLoading = loading;
  const hadFailure = Boolean(failure);
  try {
    ({ runs } = await getJson("/api/runs"));
    loading = false;
    const run = runs.find(entry => entry.name === activeRun) ?? runs[0];
    const pinned = run?.logs.find(log => log.id === activeLog);
    const previous = selection;
    const log = followLatest ? run?.logs[0] : pinned ?? run?.logs[0];
    select(run, log, followLatest || !pinned);
    drawTree();
    const requestSelection = selection;
    let changed = false;
    if (activeLog) {
      const result = await getJson("/api/messages", { log: activeLog, revision });
      if (requestSelection !== selection) return;
      revision = result.revision;
      if (result.messages) {
        messages = result.messages;
        changed = true;
      }
    }
    failure = "";
    if (previous !== selection || wasLoading || changed || hadFailure) {
      drawFilters();
      drawFeed({ preserve: previous === selection && !wasLoading });
    }
    setConnection("live", "Live");
  } catch (error) {
    loading = false;
    failure = error.message || "The local server is unavailable.";
    setConnection("error", "Retrying connection");
    if (!messages.length) drawFeed();
  } finally {
    busy = false;
    const delay = queued ? 0 : 1000;
    queued = false;
    schedule(delay);
  }
}

schedule(0);
