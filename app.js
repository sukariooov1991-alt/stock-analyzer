/* ============================================================
   app.js — Final (دمج ذكي — لا يفقد البيانات عند التحديث)
   ============================================================ */
const API_BASE = window.location.origin;
let cards = [];
const socketMap = new Map();
const reconnectTimers = new Map();
const stateTimers = new Map();
let pollTimer = null;

const lastAlertedState = {};

function saveCards() {
  try { localStorage.setItem("stock_cards", JSON.stringify(cards)); } catch (e) {}
}
function loadCards() {
  try { const r = localStorage.getItem("stock_cards"); return r ? JSON.parse(r) : []; }
  catch (e) { return []; }
}

const cardsArea = document.getElementById("cardsArea");
const emptyState = document.getElementById("emptyState");
const searchForm = document.getElementById("searchForm");
const symbolInput = document.getElementById("symbolInput");
const themeBtn = document.getElementById("themeBtn");
const themeIcon = document.getElementById("themeIcon");
const marketStatus = document.getElementById("marketStatus");
const connectionStatus = document.getElementById("connectionStatus");

function playAlertSound(type) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const notes = type === "green" ? [523.25, 783.99] : [783.99, 523.25];
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine"; osc.frequency.value = freq;
      osc.connect(gain); gain.connect(ctx.destination);
      const start = now + i * 0.2;
      gain.gain.setValueAtTime(0.3, start);
      gain.gain.exponentialRampToValueAtTime(0.01, start + 0.25);
      osc.start(start); osc.stop(start + 0.3);
    });
  } catch (e) {}
}

function checkSound(symbol, color) {
  const prev = lastAlertedState[symbol];
  if ((color === "green" || color === "red") && prev !== color) playAlertSound(color);
  lastAlertedState[symbol] = color;
}

function applyTheme(light) {
  document.body.classList.toggle("light", light);
  themeIcon.textContent = light ? "☀️" : "🌙";
  localStorage.setItem("theme", light ? "light" : "dark");
}
themeBtn.addEventListener("click", () => applyTheme(!document.body.classList.contains("light")));
applyTheme(localStorage.getItem("theme") === "light");

function updateMarketStatus() {
  const now = new Date();
  const nyH = (now.getUTCHours() - 4 + 24) % 24;
  const total = nyH * 60 + now.getUTCMinutes();
  let label = "", cls = "status";
  if (total >= 570 && total < 960)       { label = "السوق مفتوح";    cls += " online"; }
  else if (total >= 240 && total < 570)  { label = "قبل الافتتاح";   cls += " pre"; }
  else if (total >= 960 && total < 1200) { label = "بعد الإغلاق";    cls += " after"; }
  else                                    { label = "التداول الليلي"; cls += " overnight"; }
  marketStatus.className = cls;
  marketStatus.querySelector(".label").textContent = label;
}
updateMarketStatus();
setInterval(updateMarketStatus, 60000);

function setConnection(online) {
  connectionStatus.className = "status " + (online ? "online" : "offline");
  connectionStatus.querySelector(".label").textContent = online ? "متصل" : "غير متصل";
}
async function checkBackendStatus() {
  try {
    const r = await fetch(`${API_BASE}/api/status`);
    const d = await r.json();
    setConnection(!!d.connected);
  } catch (e) { setConnection(false); }
}

/* ===== OI Block ===== */
function buildOIBlockStatic(title, kind, color) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `<div class="oi-row">
      <div class="oi-bar-wrap"><div class="oi-bar" data-oi-bar="${kind}-${i}" style="width:0%;background:${color};"></div></div>
      <div class="oi-strike" data-oi-strike="${kind}-${i}">—</div>
    </div>`;
  }
  return `<div class="oi-box"><div class="oi-title">${title}</div><div class="oi-rows">${rows}</div></div>`;
}

function updateOIBlockData(root, kind, data) {
  if (!root) return;
  data = data || [];
  const maxOI = Math.max(...data.map(x => x.oi || 0), 1);
  for (let i = 0; i < 5; i++) {
    const bar = root.querySelector(`[data-oi-bar="${kind}-${i}"]`);
    const strike = root.querySelector(`[data-oi-strike="${kind}-${i}"]`);
    if (!bar || !strike) continue;
    if (i < data.length) {
      bar.style.width = `${((data[i].oi || 0) / maxOI) * 100}%`;
      strike.textContent = data[i].strike;
    } else {
      bar.style.width = "0%";
      strike.textContent = "—";
    }
  }
}

/* ===== الحيتان ===== */
function buildWhalesStatic(sym) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `<div class="whale-pill" data-whale-row="${sym}-${i}" style="display:none;">
      <span class="whale-type" data-whale-type="${sym}-${i}">—</span>
      <span class="whale-sep">·</span>
      <span class="whale-strike" data-whale-strike="${sym}-${i}">—</span>
      <span class="whale-sep">·</span>
      <span class="whale-vol">حجم <b data-whale-vol="${sym}-${i}">—</b></span>
      <span class="whale-sep">·</span>
      <span class="whale-oi">مفتوحة <b data-whale-oi="${sym}-${i}">—</b></span>
      <span class="whale-sep">·</span>
      <span class="whale-dir" data-whale-dir="${sym}-${i}">—</span>
    </div>`;
  }
  return `<div class="whales-section" id="whales-${sym}" style="display:none;">
    <div class="whales-title">🐋 الحيتان المكتشفة</div>
    <div class="whales-list">${rows}</div>
  </div>`;
}

function updateWhalesData(root, sym, whales) {
  if (!root) return;
  const wrap = root.querySelector(`#whales-${sym}`);
  if (!wrap) return;
  whales = whales || [];
  if (whales.length === 0) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";

  for (let i = 0; i < 5; i++) {
    const row = root.querySelector(`[data-whale-row="${sym}-${i}"]`);
    if (!row) continue;

    if (i < whales.length) {
      const w = whales[i];
      row.style.display = "inline-flex";
      row.className = "whale-pill " + (w.type === "CALL" ? "whale-call" : "whale-put");

      const type = row.querySelector(`[data-whale-type="${sym}-${i}"]`);
      const strike = row.querySelector(`[data-whale-strike="${sym}-${i}"]`);
      const vol = row.querySelector(`[data-whale-vol="${sym}-${i}"]`);
      const oi = row.querySelector(`[data-whale-oi="${sym}-${i}"]`);
      const dir = row.querySelector(`[data-whale-dir="${sym}-${i}"]`);

      if (type) type.textContent = w.type === "CALL" ? "شراء" : "بيع";
      if (strike) strike.textContent = w.strike;

      const volFmt = w.volume >= 1000 ? (w.volume / 1000).toFixed(1) + "K" : w.volume;
      const oiFmt  = w.oi >= 1000 ? (w.oi / 1000).toFixed(1) + "K" : w.oi;
      if (vol) vol.textContent = volFmt;
      if (oi)  oi.textContent  = oiFmt;
      if (dir) dir.textContent = w.direction === "buy" ? "🟢 يشتري"
                              : w.direction === "sell" ? "🔴 يبيع"
                              : "⚪ محايد";
    } else {
      row.style.display = "none";
    }
  }
}

/* ===== مربعات CALL/PUT ===== */
function updatePutCallBoxes(root, sym, card) {
  const callOI = card.call_oi || [];
  const putOI  = card.put_oi  || [];
  const tCOI = callOI.reduce((s, x) => s + (x.oi || 0), 0);
  const tPOI = putOI.reduce((s, x) => s + (x.oi || 0), 0);
  const tCV  = callOI.reduce((s, x) => s + (x.volume || 0), 0);
  const tPV  = putOI.reduce((s, x) => s + (x.volume || 0), 0);

  const totOI = tCOI + tPOI;
  const totV  = tCV + tPV;

  // إذا لا بيانات — نعرض "—" بدل 50/50
  const callOITxt  = totOI > 0 ? Math.round(tCOI / totOI * 100) + "%" : "—";
  const putOITxt   = totOI > 0 ? Math.round(tPOI / totOI * 100) + "%" : "—";
  const callVolTxt = totV  > 0 ? Math.round(tCV  / totV  * 100) + "%" : "—";
  const putVolTxt  = totV  > 0 ? Math.round(tPV  / totV  * 100) + "%" : "—";

  const set = (id, val) => {
    const el = root.querySelector(`#${id}-${sym}`);
    if (el) el.textContent = val;
  };
  set("pcbox-call-oi",  callOITxt);
  set("pcbox-put-oi",   putOITxt);
  set("pcbox-call-vol", callVolTxt);
  set("pcbox-put-vol",  putVolTxt);
}

function updateAllDynamic(root, sym, card) {
  if (!root || !card) return;
  const callOI = card.call_oi || [];
  const putOI  = card.put_oi  || [];
  updateOIBlockData(root, "put-oi",   putOI);
  updateOIBlockData(root, "put-liq",  putOI.map(x => ({strike: x.strike, oi: x.volume})));
  updateOIBlockData(root, "call-oi",  callOI);
  updateOIBlockData(root, "call-liq", callOI.map(x => ({strike: x.strike, oi: x.volume})));
  updatePutCallBoxes(root, sym, card);
  updateWhalesData(root, sym, card.whales || []);
}

/* ===== الشريط السفلي ===== */
function buildSummaryBar(cardData) {
  const c = cardData.card || {};
  const color = c.color || "gray";
  const label = c.label || "—";

  const macd = c.weekly_macd ?? "—";
  const rsi  = c.daily_rsi   ?? "—";
  const adx  = c.adx         ?? "—";
  const adxOk = !!c.adx_ok;
  const volOk = !!c.volume_ok;

  const macdIcon = (typeof macd === "number")
    ? (macd > 0 ? "🟢" : macd < 0 ? "🔴" : "⚪") : "⚪";

  let rsiIcon = "⏸️";
  if (c.status === "call") rsiIcon = "🟢";
  else if (c.status === "put") rsiIcon = "🔴";

  return `
    <div class="summary-bar">
      <span class="summary-badge badge-${color}">${label}</span>
      <span class="summary-item">MACD <b>${macd}</b> ${macdIcon}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">RSI <b>${rsi}</b> ${rsiIcon}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">ADX <b>${adx}</b> ${adxOk ? "✅" : "❌"}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">الحجم ${volOk ? "✅" : "❌"}</span>
    </div>
  `;
}

/* ===== بناء البطاقة ===== */
function buildCard(cardData) {
  const c = cardData.card || {};
  const lv = cardData.levels || {};
  const cls = c.color || "gray";
  const price = cardData.price ?? 0;
  const sym = cardData.symbol;

  checkSound(sym, cls);

  const div = document.createElement("div");
  div.className = "card " + cls;
  div.dataset.symbol = sym;

  const row1 = `<div class="card-row row-1">
    <div class="cell symbol-cell">${sym}</div>
    <div class="cell price-cell">
      <div class="val" data-price>$${price.toFixed(2)}</div>
      <div class="sub">السعر الحالي</div>
    </div>
    <div class="cell score-cell">
      <div class="val" data-score>${c.score ?? 0}%</div>
      <div class="sub">قوة الإشارة</div>
    </div>
    <div class="cell badge-cell">
      <div class="badge" data-badge>🔥 ${c.label || "—"}</div>
      <button class="card-trash">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
        </svg>
      </button>
    </div>
  </div>`;

  const row2 = `<div class="card-row row-2">
    <div class="cell"><div class="label">الأيام</div><div class="val">${lv.dte || "—"}</div></div>
    <div class="cell"><div class="label">سعر العقد</div><div class="val">${lv.premium && lv.premium !== "—" ? "$" + lv.premium : "—"}</div></div>
    <div class="cell"><div class="label">الانتهاء</div><div class="val">${lv.expiry || "—"}</div></div>
    <div class="cell"><div class="label">التنفيذ</div><div class="val">${lv.strike || "—"}</div></div>
  </div>`;

  const row3 = `<div class="card-row row-3">
    <div class="cell"><div class="label">الوقف</div><div class="val">${lv.stop ? "$" + lv.stop : "—"}</div></div>
    <div class="cell"><div class="label">الهدف 2</div><div class="val">${lv.target2 ? "$" + lv.target2 : "—"}</div></div>
    <div class="cell"><div class="label">الهدف 1</div><div class="val">${lv.target1 ? "$" + lv.target1 : "—"}</div></div>
    <div class="cell"><div class="label">الدخول</div><div class="val">${lv.entry ? "$" + lv.entry : "—"}</div></div>
  </div>`;

  const tfs = cardData.timeframes || [];
  const tfsHtml = tfs.map((t, idx) => `
    <div class="tf-cell ${t.trend}" data-tf-cell="${idx}">
      <div class="tf-label"><span>${t.label}</span><span data-tf-trend>${t.trend === "up" ? "صاعد ↑" : "هابط ↓"}</span></div>
      <div class="tf-row"><span>EMA</span><span class="v" data-tf-ema>${t.ema20}/${t.ema50}</span></div>
      <div class="tf-row"><span>RSI</span><span class="v" data-tf-rsi>${t.rsi}</span></div>
      <div class="tf-row"><span>ADX</span><span class="v" data-tf-adx>${t.adx}</span></div>
      <div class="tf-row"><span>RVOL</span><span class="v" data-tf-rvol>${t.rvol}x</span></div>
    </div>`).join("");

  const oiHtml = `<div class="oi-grid">
    ${buildOIBlockStatic("مفتوحة - بيع", "put-oi", "#8b5cf6")}
    ${buildOIBlockStatic("سيولة - بيع", "put-liq", "#ef4444")}
    ${buildOIBlockStatic("مفتوحة - شراء", "call-oi", "#3b82f6")}
    ${buildOIBlockStatic("سيولة - شراء", "call-liq", "#22c55e")}
  </div>`;

  const boxesHtml = `
    <div class="putcall-boxes">
      <div class="pcbox pcbox-call">
        <div class="pcbox-label">مفتوحة - شراء</div>
        <div class="pcbox-pct" id="pcbox-call-oi-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-put">
        <div class="pcbox-label">مفتوحة - بيع</div>
        <div class="pcbox-pct" id="pcbox-put-oi-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-call">
        <div class="pcbox-label">حجم - شراء</div>
        <div class="pcbox-pct" id="pcbox-call-vol-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-put">
        <div class="pcbox-label">حجم - بيع</div>
        <div class="pcbox-pct" id="pcbox-put-vol-${sym}">—</div>
      </div>
    </div>`;

  const whalesHtml = buildWhalesStatic(sym);

  const expanded = `<div class="card-expanded">
    <div class="tf-grid">${tfsHtml}</div>
    ${oiHtml}
    ${boxesHtml}
    ${whalesHtml}
    <div class="bottom-grid">
      <div class="cell"><div class="label">VWAP</div><div class="val" data-btm-vwap>$${cardData.vwap ?? "—"}</div></div>
      <div class="cell"><div class="label">مقاومات</div><div class="val" data-btm-res>${(cardData.resistances||[]).join(" / ") || "—"}</div></div>
      <div class="cell"><div class="label">دعوم</div><div class="val" data-btm-sup>${(cardData.supports||[]).join(" / ") || "—"}</div></div>
      <div class="cell"><div class="label">السعر</div><div class="val" data-btm-price>$${price.toFixed(2)}</div></div>
    </div>
    <div data-summary-wrap>${buildSummaryBar(cardData)}</div>
  </div>`;

  div.innerHTML = row1 + row2 + row3 + expanded;
  updateAllDynamic(div, sym, cardData);

  div.addEventListener("click", (e) => {
    if (e.target.closest(".card-trash")) return;
    div.classList.toggle("open");
  });
  div.querySelector(".card-trash").addEventListener("click", (e) => {
    e.stopPropagation(); deleteCard(sym);
  });

  return div;
}

/* ===== التحديث داخل المكان ===== */
function updateCardInPlace(cardEl, data) {
  const c = data.card || {};
  const sym = data.symbol;
  const price = data.price ?? 0;

  const wasOpen = cardEl.classList.contains("open");
  cardEl.className = "card " + (c.color || "gray");
  if (wasOpen) cardEl.classList.add("open");

  const badge = cardEl.querySelector("[data-badge]");
  if (badge) badge.textContent = "🔥 " + (c.label || "—");

  const score = cardEl.querySelector("[data-score]");
  if (score) score.textContent = (c.score ?? 0) + "%";

  const priceEl = cardEl.querySelector("[data-price]");
  if (priceEl) priceEl.textContent = "$" + price.toFixed(2);
  const btmPrice = cardEl.querySelector("[data-btm-price]");
  if (btmPrice) btmPrice.textContent = "$" + price.toFixed(2);

  const tfs = data.timeframes || [];
  tfs.forEach((t, idx) => {
    const cell = cardEl.querySelector(`[data-tf-cell="${idx}"]`);
    if (!cell) return;
    cell.className = "tf-cell " + t.trend;
    const trendEl = cell.querySelector("[data-tf-trend]");
    if (trendEl) trendEl.textContent = t.trend === "up" ? "صاعد ↑" : "هابط ↓";
    const emaEl = cell.querySelector("[data-tf-ema]");
    if (emaEl) emaEl.textContent = `${t.ema20}/${t.ema50}`;
    const rsiEl = cell.querySelector("[data-tf-rsi]");
    if (rsiEl) rsiEl.textContent = t.rsi;
    const adxEl = cell.querySelector("[data-tf-adx]");
    if (adxEl) adxEl.textContent = t.adx;
    const rvolEl = cell.querySelector("[data-tf-rvol]");
    if (rvolEl) rvolEl.textContent = t.rvol + "x";
  });

  const vwapEl = cardEl.querySelector("[data-btm-vwap]");
  if (vwapEl) vwapEl.textContent = "$" + (data.vwap ?? "—");
  const resEl = cardEl.querySelector("[data-btm-res]");
  if (resEl) resEl.textContent = (data.resistances || []).join(" / ") || "—";
  const supEl = cardEl.querySelector("[data-btm-sup]");
  if (supEl) supEl.textContent = (data.supports || []).join(" / ") || "—";

  updateAllDynamic(cardEl, sym, data);

  const sumWrap = cardEl.querySelector("[data-summary-wrap]");
  if (sumWrap) sumWrap.innerHTML = buildSummaryBar(data);

  checkSound(sym, c.color);
}

/* ===== ✅ الدمج الذكي — لا يمسح البيانات عند التحديث ===== */
function rebuildCard(symbol, newData) {
  const idx = cards.findIndex(c => c.symbol === symbol);
  if (idx === -1) return;

  const oldCard = cards[idx];
  const merged = { ...newData };

  // ✅ احتفظ ببيانات OI القديمة إذا الجديدة فارغة
  if (!newData.call_oi || newData.call_oi.length === 0) {
    merged.call_oi = oldCard.call_oi || [];
  }
  if (!newData.put_oi || newData.put_oi.length === 0) {
    merged.put_oi = oldCard.put_oi || [];
  }
  if (!newData.whales || newData.whales.length === 0) {
    merged.whales = oldCard.whales || [];
  }

  // ✅ احتفظ بالإجماليات إذا الجديدة صفر
  if ((newData.total_call_oi || 0) === 0 && (oldCard.total_call_oi || 0) > 0) {
    merged.total_call_oi = oldCard.total_call_oi;
  }
  if ((newData.total_put_oi || 0) === 0 && (oldCard.total_put_oi || 0) > 0) {
    merged.total_put_oi = oldCard.total_put_oi;
  }
  if ((newData.total_call_vol || 0) === 0 && (oldCard.total_call_vol || 0) > 0) {
    merged.total_call_vol = oldCard.total_call_vol;
  }
  if ((newData.total_put_vol || 0) === 0 && (oldCard.total_put_vol || 0) > 0) {
    merged.total_put_vol = oldCard.total_put_vol;
  }

  cards[idx] = merged;
  const cardEl = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
  if (!cardEl) return;
  updateCardInPlace(cardEl, merged);
}

function renderCards() {
  cardsArea.innerHTML = "";
  emptyState.style.display = cards.length ? "none" : "block";
  const sorted = [...cards].sort((a, b) => {
    const sa = a.card?.score ?? 0, sb = b.card?.score ?? 0;
    const ga = a.card?.color === "gray" ? -1 : 0;
    const gb = b.card?.color === "gray" ? -1 : 0;
    if (ga !== gb) return gb - ga;
    return sb - sa;
  });
  sorted.forEach(c => cardsArea.appendChild(buildCard(c)));
}

async function searchSymbol(symbol) {
  symbol = symbol.toUpperCase().trim();
  if (!symbol) return;
  const ex = cards.find(c => c.symbol === symbol);
  if (ex) {
    const el = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
    if (el) el.classList.add("open");
    return;
  }
  try {
    const r = await fetch(`${API_BASE}/api/analyze/${symbol}`);
    const d = await r.json();
    if (!r.ok) {
      alert(`خطأ ${r.status}:\n${d.error || "غير معروف"}`);
      setConnection(false); return;
    }
    setConnection(true);
    cards.push(d);
    saveCards();
    renderCards();
    connectLive(symbol);
    startStatePolling(symbol);
  } catch (e) {
    setConnection(false);
    alert("تعذّر الاتصال:\n" + e.message);
  }
}

function deleteCard(symbol) {
  cards = cards.filter(c => c.symbol !== symbol);
  saveCards(); renderCards();
  if (socketMap.has(symbol)) { try { socketMap.get(symbol).close(); } catch (e) {} socketMap.delete(symbol); }
  if (reconnectTimers.has(symbol)) { clearTimeout(reconnectTimers.get(symbol)); reconnectTimers.delete(symbol); }
  if (stateTimers.has(symbol)) { clearInterval(stateTimers.get(symbol)); stateTimers.delete(symbol); }
  fetch(`${API_BASE}/api/remove/${symbol}`).catch(() => {});
}

function updateCardPrice(symbol, price) {
  const card = cards.find(c => c.symbol === symbol);
  if (!card) return;
  if (price && price > 0 && price !== card.price) {
    card.price = price;
    const cardEl = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
    if (!cardEl) return;
    const priceEl = cardEl.querySelector("[data-price]");
    if (priceEl) priceEl.textContent = "$" + parseFloat(price).toFixed(2);
    const btmPrice = cardEl.querySelector("[data-btm-price]");
    if (btmPrice) btmPrice.textContent = "$" + parseFloat(price).toFixed(2);
  }
}

async function fetchState(symbol) {
  try {
    const r = await fetch(`${API_BASE}/api/analyze/${symbol}`);
    if (!r.ok) return;
    const d = await r.json();
    rebuildCard(symbol, d);
  } catch (e) {}
}

function startStatePolling(symbol) {
  if (stateTimers.has(symbol)) clearInterval(stateTimers.get(symbol));
  const timer = setInterval(() => fetchState(symbol), 60000);
  stateTimers.set(symbol, timer);
}

function connectLive(symbol) {
  if (socketMap.has(symbol)) { try { socketMap.get(symbol).close(); } catch (e) {} }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/${symbol}`);
  ws.onopen = () => {
    setConnection(true);
    ws._ping = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) { try { ws.send("ping"); } catch (e) {} }
    }, 30000);
  };
  ws.onclose = () => {
    if (ws._ping) clearInterval(ws._ping);
    socketMap.delete(symbol);
    if (cards.find(c => c.symbol === symbol)) {
      const timer = setTimeout(() => connectLive(symbol), 5000);
      reconnectTimers.set(symbol, timer);
    }
  };
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      updateCardPrice(m.symbol, m.price);
    } catch (e) {}
  };
  socketMap.set(symbol, ws);
}

async function pollPrices() {
  for (const card of cards) {
    try {
      const r = await fetch(`${API_BASE}/api/price/${card.symbol}`);
      if (!r.ok) continue;
      const d = await r.json();
      if (d.price) updateCardPrice(card.symbol, d.price);
    } catch (e) {}
  }
}

searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  searchSymbol(symbolInput.value);
  symbolInput.value = "";
  symbolInput.blur();
});

(function init() {
  cards = loadCards();
  renderCards();
  cards.forEach(c => {
    connectLive(c.symbol);
    startStatePolling(c.symbol);
  });
  checkBackendStatus();
  setInterval(checkBackendStatus, 60000);
  setTimeout(pollPrices, 3000);
  pollTimer = setInterval(pollPrices, 5000);
})();
