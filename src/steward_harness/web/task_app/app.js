"use strict";

const app = window.Telegram && window.Telegram.WebApp;
if (app) { app.ready(); app.expand(); }
const auth = { Authorization: "tma " + ((app && app.initData) || "") };
const el = (id) => document.getElementById(id);
const icons = { proposed: "💡", queued: "⏳", running: "⚙️", waiting: "💬", blocked: "⚠️", done: "✅", cancelled: "🛑" };
const attention = new Set(["waiting", "blocked", "proposed"]);
let snapshot = null;
let version = null;
let filter = "active";
let selected = null;
let detailVersion = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function fail(message) { el("error").hidden = false; el("error").textContent = message; }
async function read(query = "") {
  const response = await fetch("/tasks/api" + query, { headers: auth });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  return { etag: response.headers.get("ETag"), body: await response.json() };
}
function status(task) {
  return (icons[task.status] || "") + " " + task.status[0].toUpperCase() + task.status.slice(1);
}
function row(task) {
  const node = element("button", "", "task");
  node.append(element("h2", task.title));
  const meta = element("div", "", "meta");
  meta.append(element("span", status(task), "badge"), element("span", task.repository), element("span", task.reference));
  if (task.priority) meta.append(element("span", "Priority " + task.priority));
  node.append(meta);
  if (task.reason) node.append(element("p", task.reason, "reason"));
  node.onclick = () => showTask(task.task_id);
  return node;
}
function render() {
  if (!snapshot) return;
  const query = el("search").value.trim().toLowerCase();
  const tasks = snapshot.tasks.filter((task) =>
    (filter === "all" || (filter === "attention" ? attention.has(task.status) : !["done", "cancelled"].includes(task.status))) &&
    [task.title, task.repository, task.task_id, task.reference].some((value) => value.toLowerCase().includes(query)));
  el("tasks").replaceChildren(...tasks.map(row));
  el("empty").hidden = tasks.length > 0;
  el("empty").textContent = query ? "No matching tasks. Try another title or reference." :
    filter === "attention" ? "Nothing needs your input." : "No tasks here. Choose All tasks to see completed work.";
}
function section(parent, title, text) {
  if (!text) return;
  parent.append(element("h3", title), element("div", text, "prose"));
}
function copyCommand(parent, command) {
  const button = element("button", command, "command");
  button.title = "Copy command";
  button.onclick = async () => {
    try { await navigator.clipboard.writeText(command); button.textContent = "Copied — paste in the task’s Telegram chat"; }
    catch (_) { button.textContent = command; fail("Could not copy. Select and copy the command below."); }
  };
  parent.append(button);
}
async function showTask(reference, focus = true, updating = false) {
  if (!updating) detailVersion = null;
  selected = reference;
  el("board").hidden = true;
  el("detail").hidden = false;
  if (!updating) el("content").textContent = "Loading task…";
  if (app && app.BackButton) app.BackButton.show();
  if (focus) el("back").focus();
  try {
    const { etag, body: task } = await read("?task=" + encodeURIComponent(reference));
    if (selected !== reference) return;
    el("error").hidden = true;
    if (etag === detailVersion) return;
    detailVersion = etag;
    const content = el("content");
    content.replaceChildren(element("h2", task.title), element("p", status(task) + " · " + task.repository + " · " + task.reference, "meta"));
    if (task.withdrawn) content.append(element("p", "Withdrawal requested"));
    section(content, attention.has(task.status) ? "Needs your attention" : "Status", task.reason);
    section(content, "Brief", task.brief);
    section(content, "Findings", task.findings);
    if (task.checkpoints.length) section(content, "Recent checkpoints", task.checkpoints.map((p) => p.disposition + (p.question ? ": " + p.question : "")).join("\n"));
    if (task.pending_inputs) content.append(element("p", task.pending_inputs + " pending input(s)", "help"));
    content.append(element("h3", "Continue in Telegram"), element("p", "Tap to copy a command. Paste it in your steward’s chat and replace the placeholder text.", "help"));
    if (task.discussion_url) {
      const discussion = element("a", "Open discussion", "discussion");
      discussion.href = task.discussion_url;
      if (app && app.openTelegramLink) discussion.onclick = (event) => {
        event.preventDefault();
        app.openTelegramLink(task.discussion_url);
      };
      content.append(discussion);
    }
    const ref = task.reference;
    copyCommand(content, "/task show " + ref);
    if (task.status === "waiting") copyCommand(content, "/task answer " + ref + " <your answer>");
    if (task.status === "proposed") copyCommand(content, "/task confirm " + ref);
    if (["blocked", "cancelled"].includes(task.status)) copyCommand(content, "/task retry " + ref);
    if (!["done", "cancelled"].includes(task.status)) copyCommand(content, "/task note " + ref + " <your note>");
    const technical = element("details", "");
    technical.append(element("summary", "Git details"), element("p", task.task_id));
    if (task.tip) technical.append(element("p", "Accepted commit: " + task.tip));
    content.append(technical);
  } catch (error) { if (selected === reference) { el("content").textContent = "Task unavailable."; fail(error.message); } }
}
function back() {
  selected = null;
  el("board").hidden = false;
  el("detail").hidden = true;
  el("error").hidden = true;
  if (app && app.BackButton) app.BackButton.hide();
  el("search").focus();
}
async function refresh() {
  try {
    const { etag, body } = await read();
    el("error").hidden = true;
    if (selected) await showTask(selected, false, true);
    if (etag === version) return;
    version = etag;
    snapshot = body;
    const total = Object.values(body.counts).reduce((sum, n) => sum + n, 0);
    const needs = [...attention].reduce((sum, status) => sum + (body.counts[status] || 0), 0);
    const listed = body.tasks.length < total ? " (" + body.tasks.length + " listed; all open tasks included)" : "";
    el("counts").textContent = total + " tasks" + listed + " · " + needs + (needs === 1 ? " needs your attention" : " need your attention") + (body.paused ? " · Scheduling paused" : "");
    render();
  } catch (error) { fail(error.message); }
}
el("search").oninput = render;
for (const button of document.querySelectorAll("[data-filter]")) button.onclick = () => {
  filter = button.dataset.filter;
  for (const peer of document.querySelectorAll("[data-filter]")) peer.setAttribute("aria-pressed", String(peer === button));
  render();
};
el("back").onclick = back;
if (app && app.BackButton) app.BackButton.onClick(back);
// The parameter only selects a read. Every API call still requires signed initData.
const start = (app && app.initDataUnsafe && app.initDataUnsafe.start_param) || new URLSearchParams(location.search).get("tgWebAppStartParam") || "";
if (/^task_[0-9a-f]{8}$/.test(start)) showTask("#" + start.slice(5), false);
refresh();
setInterval(refresh, 15000);
