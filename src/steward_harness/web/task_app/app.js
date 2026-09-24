// The board, rendered. Every request carries the Telegram sign-in; nothing
// here writes, because every action is a `/task` command in the chat this was
// opened from.
"use strict";

const app = window.Telegram && window.Telegram.WebApp;
if (app) { app.ready(); app.expand(); }

const auth = { Authorization: "tma " + ((app && app.initData) || "") };
const el = (id) => document.getElementById(id);
const open = new Set();
let version = null;

function fail(message) {
  el("error").hidden = false;
  el("error").textContent = message;
}

async function read(query) {
  const response = await fetch("api" + query, { headers: auth });
  if (!response.ok) {
    throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  }
  return { etag: response.headers.get("ETag"), body: await response.json() };
}

function row(task) {
  const node = document.createElement("div");
  node.className = "task " + task.status;
  const head = document.createElement("h2");
  head.textContent = task.title;
  const meta = document.createElement("div");
  meta.className = "meta";
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = task.status;
  meta.append(badge, " " + task.repository + " · " + task.task_id +
    (task.priority ? " · priority " + task.priority : "") +
    (task.withdrawn ? " · withdrawal requested" : "") +
    (task.tip ? " · " + task.tip.slice(0, 12) : ""));
  node.append(head, meta);
  if (task.reason) {
    const reason = document.createElement("div");
    reason.className = "reason";
    reason.textContent = task.reason;
    node.append(reason);
  }
  if (open.has(task.task_id)) { node.append(detail(task.task_id)); }
  node.onclick = () => {
    open.has(task.task_id) ? open.delete(task.task_id) : open.add(task.task_id);
    refresh(true);
  };
  return node;
}

function detail(taskId) {
  const node = document.createElement("div");
  node.className = "detail";
  node.textContent = "…";
  read("?task=" + encodeURIComponent(taskId)).then(({ body }) => {
    const lines = [body.brief];
    if (body.findings) { lines.push("", "Findings", body.findings); }
    if (body.checkpoints.length) {
      lines.push("", "Checkpoints");
      for (const point of body.checkpoints) {
        lines.push("  " + point.disposition + (point.question ? " — " + point.question : ""));
      }
    }
    if (body.pending_inputs) { lines.push("", body.pending_inputs + " pending input(s)"); }
    node.textContent = lines.join("\n");
  }).catch((error) => { node.textContent = error.message; });
  return node;
}

async function refresh(force) {
  try {
    const { etag, body } = await read("");
    if (!force && etag === version) { return; }
    version = etag;
    el("error").hidden = true;
    el("heading").textContent = body.paused ? "Tasks (scheduler paused)" : "Tasks";
    el("counts").textContent = Object.entries(body.counts)
      .map(([status, count]) => count + " " + status).join(" · ") || "no tasks";
    const list = el("tasks");
    list.replaceChildren(...body.tasks.map(row));
  } catch (error) {
    fail(error.message);
  }
}

refresh(true);
setInterval(refresh, 15000);
