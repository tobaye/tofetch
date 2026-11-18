"use strict";
// ToFetch Connect — send browser downloads / links to a ToFetch instance.
// Talks to the instance's HTTP API: POST /api/login (session cookie),
// then POST /api/rpc addUri with the target's Cookie + Referer attached
// as aria2 options, so login-walled and hotlink-protected files work.

const DEFAULTS = { server: "http://127.0.0.1:8077", password: "", intercept: false };

async function settings() {
  return Object.assign({}, DEFAULTS, await chrome.storage.local.get(["server", "password", "intercept"]));
}

function badge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
  if (text) setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
}

async function sendToToFetch(url, referer) {
  if (!/^((https?|ftp):\/\/|magnet:|ed2k:|thunder:)/i.test(url || "")) {
    badge("!", "#c0392b");
    return;
  }
  const s = await settings();
  const base = s.server.replace(/\/+$/, "");
  try {
    if (s.password) {
      await fetch(base + "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ password: s.password }),
      });
    }
    const opts = {};
    if (referer) opts.referer = referer;
    if (/^https?:\/\//i.test(url)) {
      try {
        const cookies = await chrome.cookies.getAll({ url });
        if (cookies.length)
          opts.header = ["Cookie: " + cookies.map(c => c.name + "=" + c.value).join("; ")];
      } catch (e) { /* no cookies: send without */ }
    }
    const r = await fetch(base + "/api/rpc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ method: "addUri", params: [[url], opts] }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.result) badge("✓", "#2ea043");
    else badge("!", "#c0392b");
  } catch (e) {
    badge("!", "#c0392b");
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "tf-link", title: "发送链接到 ToFetch", contexts: ["link"] });
  chrome.contextMenus.create({ id: "tf-page", title: "发送本页到 ToFetch", contexts: ["page"] });
  chrome.contextMenus.create({ id: "tf-sel", title: "把选中的链接发到 ToFetch", contexts: ["selection"] });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const pageUrl = (tab && tab.url) || info.pageUrl || "";
  if (info.menuItemId === "tf-link" && info.linkUrl) sendToToFetch(info.linkUrl, pageUrl);
  else if (info.menuItemId === "tf-page") sendToToFetch(pageUrl, "");
  else if (info.menuItemId === "tf-sel" && info.selectionText) {
    const m = info.selectionText.match(/((https?|ftp):\/\/[^\s<>"']+|magnet:\?[^\s<>"']+)/i);
    if (m) sendToToFetch(m[1], pageUrl);
    else badge("!", "#c0392b");
  }
});

chrome.action.onClicked.addListener(tab => {
  if (tab && tab.url) sendToToFetch(tab.url, "");
});

// Optional: take over browser downloads (off by default; enable in options).
chrome.downloads.onCreated.addListener(async item => {
  const s = await settings();
  if (!s.intercept) return;
  if (!/^https?:\/\//i.test(item.url || "")) return;
  try { await chrome.downloads.cancel(item.id); } catch (e) {}
  try { await chrome.downloads.erase({ id: item.id }); } catch (e) {}
  sendToToFetch(item.url, item.referrer || "");
});
