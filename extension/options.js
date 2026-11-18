"use strict";
const $ = s => document.querySelector(s);

async function load() {
  const s = await chrome.storage.local.get(["server", "password", "intercept"]);
  $("#server").value = s.server || "http://127.0.0.1:8077";
  $("#password").value = s.password || "";
  $("#intercept").checked = !!s.intercept;
}

async function save() {
  await chrome.storage.local.set({
    server: $("#server").value.trim().replace(/\/+$/, "") || "http://127.0.0.1:8077",
    password: $("#password").value,
    intercept: $("#intercept").checked,
  });
  $("#msg").textContent = "已保存";
}

async function test() {
  await save();
  const s = await chrome.storage.local.get(["server", "password"]);
  const base = s.server;
  const msg = $("#msg");
  msg.textContent = "连接中…";
  try {
    if (s.password) {
      const lr = await fetch(base + "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ password: s.password }),
      });
      if (!lr.ok) { msg.textContent = "登录失败：密码不对"; return; }
    }
    const r = await fetch(base + "/api/state", { credentials: "include" });
    const j = await r.json();
    msg.textContent = j.engine ? "连接成功，引擎运行中" : "连接成功，但下载引擎未启动（看 ToFetch 页面提示）";
  } catch (e) {
    msg.textContent = "连不上：" + e.message + "（确认 ToFetch 正在运行、地址没填错）";
  }
}

$("#save").addEventListener("click", save);
$("#test").addEventListener("click", test);
load();
