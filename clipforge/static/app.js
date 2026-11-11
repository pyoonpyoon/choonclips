"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
let STATUS = null;
let pollTimer = null;
const PLATFORM_LABEL = { tiktok: "TikTok", instagram: "Instagram", youtube: "YouTube", x: "X" };
const CAPTION_TABS = [
  ["tiktok", "TikTok"], ["instagram", "Instagram"], ["youtube_title", "YT title"],
  ["youtube_description", "YT desc"], ["x", "X"],
];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = (t % 60).toFixed(1); return `${m}:${s.padStart(4, "0")}`; };
const when = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), err ? 8000 : 3500);
}

function setNav(name) {
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
}

async function loadStatus() { STATUS = await api("/api/status"); return STATUS; }

/* ---------------- Library ---------------- */

async function renderLibrary() {
  setNav("library");
  view.innerHTML = $("#tpl-library").innerHTML;
  const st = STATUS || (await loadStatus());
  const d = st.defaults;
  $("#o-min").value = d.min_seconds; $("#o-target").value = d.target_seconds;
  $("#o-max").value = d.max_seconds; $("#o-count").value = d.max_clips;
  const c = st.capabilities;
  $("#o-ai").checked = c.claude; $("#o-ai").disabled = !c.claude;
  const caps = [
    ["ffmpeg", "FFmpeg"], ["transcribe", "Transcription"], ["captions", "Captions"],
    ["face_reframe", "Speaker tracking"], ["claude", "Claude clip picker"], ["url_import", "URL import"],
  ];
  $("#caps").innerHTML = caps.map(([k, l]) => `<span class="pill ${c[k] ? "on" : "off"}" title="${c[k] ? "ready" : "not installed / configured, see README"}">${c[k] ? "✓" : "○"} ${l}</span>`).join("");

  const opts = () => ({
    min_seconds: $("#o-min").value, target_seconds: $("#o-target").value, max_seconds: $("#o-max").value,
    max_clips: $("#o-count").value, layout: $("#o-layout").value, captions: $("#o-captions").checked,
    hook_overlay: $("#o-hook").checked, use_ai: $("#o-ai").checked,
  });

  const drop = $("#drop"), file = $("#file");
  $("#pick").onclick = (e) => { e.preventDefault(); file.click(); };
  file.onchange = () => file.files[0] && upload(file.files[0], opts());
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0], opts()));

  $("#urlform").onsubmit = async (e) => {
    e.preventDefault();
    const url = $("#url").value.trim(); if (!url) return;
    try { const r = await api("/api/videos/url", { method: "POST", body: JSON.stringify({ url, options: opts() }) }); location.hash = `#/video/${r.id}`; }
    catch (err) { toast(err.message, true); }
  };
  await refreshVideos();
  pollTimer = setInterval(refreshVideos, 3000);
}

function upload(f, options) {
  const bar = $("#upbar"), span = bar.querySelector("span");
  bar.hidden = false;
  const fd = new FormData(); fd.append("file", f); fd.append("options", JSON.stringify(options));
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/videos");
  xhr.upload.onprogress = (e) => e.lengthComputable && (span.style.width = `${(e.loaded / e.total) * 100}%`);
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status >= 300) return toast(JSON.parse(xhr.responseText || "{}").detail || "Upload failed", true);
    location.hash = `#/video/${JSON.parse(xhr.responseText).id}`;
  };
  xhr.onerror = () => { bar.hidden = true; toast("Upload failed", true); };
  xhr.send(fd);
}

async function refreshVideos() {
  const el = $("#videos"); if (!el) return;
  const vids = await api("/api/videos");
  if (!vids.length) { el.innerHTML = `<p class="muted">No projects yet. Drop in a video to get started.</p>`; return; }
  el.innerHTML = vids.map((v) => `
    <div class="vcard" data-id="${v.id}">
      <div class="vthumb" style="${v.thumb_url ? `background-image:url(${v.thumb_url})` : ""}">${v.thumb_url ? "" : "⏳"}</div>
      <div class="vbody">
        <div class="vtitle" title="${esc(v.title)}">${esc(v.title)}</div>
        ${statusLine(v)}
        <div class="muted" style="font-size:12px">${v.duration ? fmt(v.duration) + " · " : ""}${v.clip_count} clips</div>
      </div>
    </div>`).join("");
  el.querySelectorAll(".vcard").forEach((c) => (c.onclick = () => (location.hash = `#/video/${c.dataset.id}`)));
}

function statusLine(v) {
  if (v.status === "error") return `<div class="status error">Failed: ${esc((v.error || "").split("\n")[0])}</div>`;
  if (v.status === "ready") return `<div class="status ready">✓ ${esc(v.message || "Ready")}</div>`;
  return `<div class="status">${esc(v.message || v.status)}</div><div class="bar"><span style="width:${Math.round((v.progress || 0) * 100)}%"></span></div>`;
}

/* ---------------- Video / clips ---------------- */

const selected = new Set();
let currentVideo = null;

async function renderVideo(id) {
  setNav("library");
  selected.clear();
  view.innerHTML = `<p class="muted">Loading…</p>`;
  await refreshVideo(id, true);
  pollTimer = setInterval(() => refreshVideo(id, false), 3000);
}

async function refreshVideo(id, full) {
  let v;
  try { v = await api(`/api/videos/${id}`); } catch (e) { view.innerHTML = `<p class="err-text">${esc(e.message)}</p>`; return; }
  const busy = !["ready", "error"].includes(v.status) || v.clips.some((c) => ["pending", "rendering"].includes(c.status));
  const sig = JSON.stringify([v.status, v.progress, v.message, v.clips.map((c) => [c.id, c.status, c.posts.map((p) => p.status)])]);
  if (!full && currentVideo && currentVideo._sig === sig) return;
  // Don't clobber the page while the user is typing
  if (!full && document.activeElement && view.contains(document.activeElement) && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  v._sig = sig; currentVideo = v;

  view.innerHTML = `
    <section class="head">
      <div class="left"><a href="#/" class="btn ghost sm">← Library</a><h2 title="${esc(v.title)}">${esc(v.title)}</h2></div>
      <div class="toolbar">
        <button class="btn ghost sm" id="selall">Select all</button>
        <button class="btn sm" id="postsel" disabled>Post / schedule selected</button>
        <button class="btn ghost sm" id="reproc">Re-analyse</button>
        <button class="btn danger sm" id="delvid">Delete</button>
      </div>
    </section>
    <section class="card" ${v.status === "ready" ? "hidden" : ""}>
      ${statusLine(v)}
      ${v.status === "error" ? `<pre class="err-text">${esc(v.error)}</pre>` : ""}
    </section>
    <section class="clips">${v.clips.map(clipCard).join("")}</section>
    ${!v.clips.length && v.status === "ready" ? `<p class="muted">No clips found.</p>` : ""}`;

  $("#delvid").onclick = async () => { if (confirm("Delete this project and all its clips?")) { await api(`/api/videos/${id}`, { method: "DELETE" }); location.hash = "#/"; } };
  $("#reproc").onclick = async () => { await api(`/api/videos/${id}/reprocess`, { method: "POST", body: "{}" }); refreshVideo(id, true); };
  $("#selall").onclick = () => { v.clips.filter((c) => c.status === "ready").forEach((c) => selected.add(c.id)); syncSel(); };
  $("#postsel").onclick = () => openPostDialog([...selected]);
  v.clips.forEach((c) => wireClip(c));
  syncSel();
  if (!busy && full === false) return;
}

function scoreClass(s) { return s >= 75 ? "score hi" : "score"; }

function clipCard(c) {
  const caps = (c.meta && c.meta.captions) || {};
  const tags = ((c.meta && c.meta.hashtags) || []).join(" ");
  const posts = c.posts.map((p) => `<span class="pill ${p.status === "posted" ? "on" : p.status === "error" ? "off" : ""}" title="${esc(p.error || "")}">${PLATFORM_LABEL[p.platform]}: ${p.status}${p.url ? ` <a href="${esc(p.url)}" target="_blank">↗</a>` : ""}</span>`).join("");
  return `
  <article class="clip ${selected.has(c.id) ? "sel" : ""}" id="c-${c.id}">
    ${c.video_url && c.status === "ready" ? `<video src="${c.video_url}?v=${Math.round(c.end * 100)}" poster="${c.thumb_url || ""}" controls preload="none" playsinline></video>`
      : `<div class="ph">${c.status === "error" ? `<span class="err-text">${esc(c.error)}</span>` : "Rendering…"}</div>`}
    <div class="cbody">
      <div class="cmeta">
        <label class="chk"><input type="checkbox" class="sel" ${selected.has(c.id) ? "checked" : ""} ${c.status !== "ready" ? "disabled" : ""}> #${c.rank}</label>
        <span class="muted">${fmt(c.start)} → ${fmt(c.end)} · ${(c.end - c.start).toFixed(0)}s</span>
        <span class="${scoreClass(c.score)}" title="Predicted strength vs other clips from this video">${Math.round(c.score)}</span>
      </div>
      <label>Title<input class="f-title" value="${esc(c.title)}"></label>
      <label>On-screen hook<input class="f-hook" value="${esc(c.hook)}"></label>
      <div class="reason">${esc(c.reason)}</div>
      <div class="tabs">${CAPTION_TABS.map(([k, l], i) => `<button data-k="${k}" class="${i === 0 ? "on" : ""}">${l}</button>`).join("")}</div>
      <textarea class="f-cap" data-k="tiktok">${esc(caps.tiktok || "")}</textarea>
      <label>Hashtags<input class="f-tags" value="${esc(tags)}"></label>
      <details><summary class="muted">Trim</summary>
        <div class="trim" style="margin-top:8px">
          <label>Start (s)<input class="f-start" type="number" step="0.1" value="${c.start.toFixed(2)}"></label>
          <label>End (s)<input class="f-end" type="number" step="0.1" value="${c.end.toFixed(2)}"></label>
          <button class="btn ghost sm f-retrim">Re-render</button>
        </div>
      </details>
      <div class="toolbar">
        <button class="btn ghost sm f-save">Save text</button>
        <button class="btn sm f-post" ${c.status !== "ready" ? "disabled" : ""}>Post…</button>
        <a class="btn ghost sm" href="/api/clips/${c.id}/package">Download</a>
        <button class="btn danger sm f-del">Remove</button>
      </div>
      ${posts ? `<div class="posts">${posts}</div>` : ""}
    </div>
  </article>`;
}

function wireClip(c) {
  const el = $(`#c-${c.id}`); if (!el) return;
  const caps = { ...((c.meta && c.meta.captions) || {}) };
  const ta = $(".f-cap", el);
  el.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
    caps[ta.dataset.k] = ta.value;
    el.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    ta.dataset.k = b.dataset.k; ta.value = caps[b.dataset.k] || "";
  }));
  $(".sel", el).onchange = (e) => { e.target.checked ? selected.add(c.id) : selected.delete(c.id); syncSel(); };
  const body = () => {
    caps[ta.dataset.k] = ta.value;
    return {
      title: $(".f-title", el).value, hook: $(".f-hook", el).value, captions: caps,
      hashtags: $(".f-tags", el).value.split(/[\s,]+/).filter(Boolean),
    };
  };
  $(".f-save", el).onclick = async () => {
    try { await api(`/api/clips/${c.id}`, { method: "PATCH", body: JSON.stringify(body()) }); toast("Saved" + (body().hook !== c.hook ? " (re-rendering hook)" : "")); refreshVideo(c.video_id, true); }
    catch (e) { toast(e.message, true); }
  };
  $(".f-retrim", el).onclick = async () => {
    try {
      await api(`/api/clips/${c.id}`, { method: "PATCH", body: JSON.stringify({ ...body(), start: +$(".f-start", el).value, end: +$(".f-end", el).value }) });
      toast("Re-rendering…"); refreshVideo(c.video_id, true);
    } catch (e) { toast(e.message, true); }
  };
  $(".f-del", el).onclick = async () => { if (confirm("Remove this clip?")) { await api(`/api/clips/${c.id}`, { method: "DELETE" }); refreshVideo(c.video_id, true); } };
  $(".f-post", el).onclick = async () => {
    try { await api(`/api/clips/${c.id}`, { method: "PATCH", body: JSON.stringify(body()) }); } catch (_) {}
    openPostDialog([c.id]);
  };
}

function syncSel() {
  document.querySelectorAll(".clip").forEach((el) => {
    const id = el.id.slice(2); el.classList.toggle("sel", selected.has(id));
    const cb = $(".sel", el); if (cb) cb.checked = selected.has(id);
  });
  const b = $("#postsel"); if (b) { b.disabled = !selected.size; b.textContent = selected.size ? `Post / schedule ${selected.size} selected` : "Post / schedule selected"; }
}

async function openPostDialog(clipIds) {
  const st = await loadStatus();
  const connected = st.platforms.filter((p) => p.connected);
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  const local = new Date(Date.now() + 10 * 60 * 1000); local.setSeconds(0, 0);
  const iso = new Date(local.getTime() - local.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  bg.innerHTML = `
    <div class="modal">
      <h2>Post ${clipIds.length} clip${clipIds.length > 1 ? "s" : ""}</h2>
      ${connected.length ? "" : `<p class="err-text">No accounts connected. <a href="#/accounts">Connect accounts</a> first.</p>`}
      <div class="grid4" style="margin:0">
        ${st.platforms.map((p) => `<label class="chk"><input type="checkbox" value="${p.name}" ${p.connected ? "checked" : "disabled"}> ${p.label}${p.connected ? "" : " (not connected)"}</label>`).join("")}
      </div>
      <label>When
        <select id="pd-when"><option value="now">Post now</option><option value="later">Schedule</option></select>
      </label>
      <div id="pd-later" hidden class="grid4" style="margin:0">
        <label>First post at<input id="pd-at" type="datetime-local" value="${iso}"></label>
        <label>Then one clip every (minutes)<input id="pd-int" type="number" min="0" value="${clipIds.length > 1 ? 240 : 0}"></label>
      </div>
      <p class="muted" style="margin:0">Captions come from each clip's per-platform text. ClipForge must be running at the scheduled time.</p>
      <div class="toolbar" style="justify-content:flex-end">
        <button class="btn ghost" id="pd-cancel">Cancel</button>
        <button class="btn" id="pd-go" ${connected.length ? "" : "disabled"}>Confirm</button>
      </div>
    </div>`;
  document.body.appendChild(bg);
  const close = () => bg.remove();
  $("#pd-cancel", bg).onclick = close;
  bg.onclick = (e) => e.target === bg && close();
  $("#pd-when", bg).onchange = (e) => ($("#pd-later", bg).hidden = e.target.value !== "later");
  $("#pd-go", bg).onclick = async () => {
    const platforms = [...bg.querySelectorAll("input[type=checkbox]:checked")].map((i) => i.value);
    if (!platforms.length) return toast("Pick at least one platform", true);
    const later = $("#pd-when", bg).value === "later";
    const body = { clip_ids: clipIds, platforms, interval_minutes: later ? +$("#pd-int", bg).value || 0 : 0,
      start_at: later ? new Date($("#pd-at", bg).value).getTime() / 1000 : null };
    try {
      const r = await api("/api/posts", { method: "POST", body: JSON.stringify(body) });
      toast(later ? `Scheduled ${r.created.length} posts` : `Posting ${r.created.length}… check the queue`);
      close(); selected.clear(); syncSel();
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- Queue ---------------- */

async function renderQueue() {
  setNav("queue");
  const draw = async () => {
    const posts = await api("/api/posts");
    view.innerHTML = `<section class="card"><h2>Posting queue</h2>${posts.length ? `
      <div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Platform</th><th>Clip</th><th>Status</th><th></th></tr></thead><tbody>
      ${posts.map((p) => `<tr>
        <td>${when(p.posted_at || p.scheduled_at)}</td>
        <td>${PLATFORM_LABEL[p.platform] || p.platform}</td>
        <td><a href="#/video/${p.video_id}">${esc(p.clip_title || p.clip_id)}</a></td>
        <td>${esc(p.status)}${p.url ? ` · <a href="${esc(p.url)}" target="_blank">view ↗</a>` : ""}${p.error ? `<div class="err-text">${esc(p.error)}</div>` : ""}</td>
        <td>${p.status === "scheduled" ? `<button class="btn ghost sm" data-cancel="${p.id}">Cancel</button>` : ""}
            ${["error", "canceled"].includes(p.status) ? `<button class="btn ghost sm" data-retry="${p.id}">Retry</button>` : ""}</td>
      </tr>`).join("")}</tbody></table></div>` : `<p class="muted">Nothing posted or scheduled yet.</p>`}</section>`;
    view.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = async () => { await api(`/api/posts/${b.dataset.cancel}/cancel`, { method: "POST" }); draw(); }));
    view.querySelectorAll("[data-retry]").forEach((b) => (b.onclick = async () => { await api(`/api/posts/${b.dataset.retry}/retry`, { method: "POST" }); draw(); }));
  };
  await draw();
  pollTimer = setInterval(draw, 5000);
}

/* ---------------- Accounts ---------------- */

async function renderAccounts() {
  setNav("accounts");
  const st = await loadStatus();
  view.innerHTML = `<section><h2>Accounts</h2><p class="muted">Sign in once per platform. Tokens stay in your local database.</p>
    <div class="accts">${st.platforms.map((p) => `
      <div class="card acct">
        <h3>${p.label}</h3>
        ${p.connected ? `<div class="status ready">✓ Connected as ${esc(p.account_name || "")}</div>`
          : p.configured ? `<div class="muted">Not connected</div>` : `<div class="status error">App keys not set in .env</div>`}
        <div class="toolbar">
          ${p.configured ? `<a class="btn ${p.connected ? "ghost" : ""} sm" href="/oauth/${p.name}/start">${p.connected ? "Reconnect" : "Connect"}</a>` : ""}
          ${p.connected ? `<button class="btn danger sm" data-dis="${p.name}">Disconnect</button>` : ""}
          <a class="btn ghost sm" href="${p.docs}" target="_blank">API docs ↗</a>
        </div>
        <div class="muted" style="font-size:12px">Redirect URI to register:<br><code>${esc(p.redirect_uri)}</code></div>
      </div>`).join("")}</div></section>`;
  view.querySelectorAll("[data-dis]").forEach((b) => (b.onclick = async () => { await api(`/api/accounts/${b.dataset.dis}/disconnect`, { method: "POST" }); STATUS = null; renderAccounts(); }));
}

/* ---------------- Router ---------------- */

async function route() {
  clearInterval(pollTimer);
  const h = location.hash.replace(/^#/, "") || "/";
  try {
    if (h.startsWith("/video/")) return await renderVideo(h.split("/")[2]);
    if (h === "/queue") return await renderQueue();
    if (h === "/accounts") return await renderAccounts();
    return await renderLibrary();
  } catch (e) { view.innerHTML = `<p class="err-text">${esc(e.message)}</p>`; }
}

(function init() {
  const q = new URLSearchParams(location.search);
  if (q.get("connected")) { toast(`${PLATFORM_LABEL[q.get("connected")] || q.get("connected")} connected`); history.replaceState(null, "", "/#/accounts"); }
  if (q.get("connect_error")) { toast("Connection failed: " + q.get("connect_error"), true); history.replaceState(null, "", "/#/accounts"); }
  window.addEventListener("hashchange", route);
  route();
})();
