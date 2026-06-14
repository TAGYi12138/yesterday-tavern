"use strict";
// 昨日酒馆 · 观察器前端:三栏只读视图。
// 中间用 SSE 增量接收群聊消息;左右两栏每 3 秒轮询 NPC/世界状态。
// debug/player 模式切换会重连(裁剪在服务端完成,前端只是请求不同 mode)。

const $ = (id) => document.getElementById(id);
let gameId = null;
let mode = "player";
let lastId = 0;
let es = null;
let lastDay = null;
let pollTimer = null;
let curGroupId = null;        // 当前正在拼装的群聊块 group_id
let curGroupEl = null;        // 当前群聊块的 DOM 容器
const npcNames = {};          // id → 名字(由侧栏轮询填充,用于群聊块头部)

function mode_() { return $("mode").checked ? "debug" : "player"; }

async function loadGames() {
  const r = await fetch("/api/games");
  const d = await r.json();
  const sel = $("game");
  sel.innerHTML = "";
  (d.games || []).forEach((g) => {
    const o = document.createElement("option");
    o.value = g; o.textContent = g; sel.appendChild(o);
  });
  if (d.games && d.games.length) {
    gameId = sel.value || d.games[0];
    start();
  } else {
    $("stream").innerHTML = '<div class="daysep"><span>暂无存档，先让守护进程跑起来</span></div>';
  }
}

function setConn(ok) {
  const el = $("conn");
  el.textContent = ok ? "已连接" : "重连中…";
  el.style.color = ok ? "#5fd39a" : "#e2b714";
}

function dayShown(day) {
  if (day === lastDay) return;
  lastDay = day;
  const s = $("stream");
  const div = document.createElement("div");
  div.className = "daysep";
  div.innerHTML = `<span>第 ${day} 天</span>`;
  s.appendChild(div);
}

// 同一 group_id 的消息归入同一个群聊块;返回该消息应插入的容器。
function groupContainer(m) {
  const s = $("stream");
  if (!m.group_id) { curGroupId = null; curGroupEl = null; return s; }
  if (m.group_id !== curGroupId) {
    curGroupId = m.group_id;
    curGroupEl = document.createElement("div");
    curGroupEl.className = "groupblock";
    const names = (m.participants || []).map((id) => npcNames[id] || id).join("、");
    const head = document.createElement("div");
    head.className = "ghead";
    head.innerHTML = `<span class="gtag">群聊</span><span>${esc(names)}</span>`;
    curGroupEl.appendChild(head);
    s.appendChild(curGroupEl);
  }
  return curGroupEl;
}

function renderMsg(m) {
  dayShown(m.day);
  const s = groupContainer(m);
  const wrap = document.createElement("div");
  let cls = m.type || "narration";
  wrap.className = "msg " + cls;
  if (cls === "dialogue") {
    // 发起方靠右(类似"我"），仅用于视觉区分说话人。
    if (m.speaker_id && m.target_id) wrap.classList.add("right");
    const who = m.target_name
      ? `${m.speaker_name || ""} → ${m.target_name}`
      : (m.speaker_name || "");
    wrap.innerHTML = `<div class="who">${esc(who)}</div><div class="bub">${esc(m.text)}</div>`;
  } else {
    wrap.innerHTML = `<div class="bub">${esc(m.text)}</div>`;
  }
  if (m.debug_payload) {
    const d = document.createElement("div");
    d.className = "dbg";
    d.textContent = JSON.stringify(m.debug_payload);
    wrap.appendChild(d);
  }
  s.appendChild(wrap);
  const scroller = $("stream").parentElement;
  scroller.scrollTop = scroller.scrollHeight;
}

function esc(t) {
  return (t == null ? "" : String(t)).replace(/[&<>]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

async function pollSide() {
  if (!gameId) return;
  try {
    const [nr, wr] = await Promise.all([
      fetch(`/api/npcs?game_id=${gameId}&mode=${mode}`),
      fetch(`/api/world-state?game_id=${gameId}&mode=${mode}`),
    ]);
    renderNpcs((await nr.json()).npcs || []);
    renderWorld(await wr.json());
  } catch (e) { /* 静默重试 */ }
}

function renderNpcs(npcs) {
  const box = $("npcs");
  box.innerHTML = "";
  npcs.forEach((n) => {
    npcNames[n.id] = n.name;
    const div = document.createElement("div");
    div.className = "npc";
    let tags = "";
    if (mode === "debug") {
      const st = n.status || (n.present ? "active" : "absent");
      tags += `<span class="tag ${esc(st)}">${esc(st)}</span>`;
      if (n.mental_state) tags += `<span class="tag ms">${esc(n.mental_state)}</span>`;
    } else {
      tags += `<span class="tag ${n.present ? "active" : "absent"}">${esc(n.state_label || (n.present ? "在场" : "今天没来"))}</span>`;
    }
    let meta = `<div class="meta">${esc(n.job || "")}</div>`;
    if (mode === "debug") {
      meta = `<div class="meta">${esc(n.job || "")} · 压力 ${n.stress}</div>` +
             (n.current_goal ? `<div class="meta">目标：${esc(n.current_goal)}</div>` : "");
    }
    div.innerHTML = `<div class="nm">${esc(n.name)}</div>${tags}${meta}`;
    box.appendChild(div);
  });
}

function renderWorld(w) {
  const box = $("world");
  if (w.mode === "debug") {
    const rows = [
      ["当前天数", w.current_day],
      ["世界阶段", w.world_phase],
      ["全局紧张度", w.global_tension],
      ["曝光阶段", `${w.exposure_stage} (${w.police_exposure_risk})`],
      ["债务阶段", `${w.debt_stage} (${w.boss_debt})`],
      ["封店倒计时", w.debt_seize_countdown < 0 ? "—" : w.debt_seize_countdown],
      ["债务终局", w.debt_resolution || "—"],
      ["真相压力", `${w.truth_pressure} (${w.truth_stage})`],
      ["玩家真相进度", w.athou_truth_progress],
      ["危机阶段", `${w.crisis_phase} (${w.crisis_days})`],
    ];
    box.innerHTML = rows.map(([k, v]) =>
      `<div class="row"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  } else {
    box.innerHTML =
      `<div class="feel">第 ${esc(w.current_day)} 天<br>` +
      `酒馆气氛：${esc(w.atmosphere)}<br>` +
      `债务压力：${esc(w.debt_feel)}<br>` +
      `老陈动向：${esc(w.exposure_feel)}<br>` +
      `真相氛围：${esc(w.truth_feel)}</div>`;
  }
}

function start() {
  mode = mode_();
  lastId = 0; lastDay = null;
  curGroupId = null; curGroupEl = null;
  $("stream").innerHTML = "";
  if (es) es.close();
  if (pollTimer) clearInterval(pollTimer);

  es = new EventSource(`/api/stream?game_id=${gameId}&after_id=0&mode=${mode}`);
  es.addEventListener("ready", () => setConn(true));
  es.addEventListener("message", (ev) => {
    setConn(true);
    try {
      const m = JSON.parse(ev.data);
      if (m.id) lastId = Math.max(lastId, m.id);
      renderMsg(m);
    } catch (e) {}
  });
  es.onerror = () => setConn(false);

  pollSide();
  pollTimer = setInterval(pollSide, 3000);
}

$("game").addEventListener("change", (e) => { gameId = e.target.value; start(); });
$("mode").addEventListener("change", () => start());
loadGames();
