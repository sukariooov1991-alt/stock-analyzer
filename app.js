/* ============================================================
   app.js — Smooth Updates (بدون وميض)
   ============================================================ */
const API_BASE = window.location.origin;
let cards = [];
const socketMap = new Map();
const reconnectTimers = new Map();
const optionsTimers = new Map();
let pollTimer = null;

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

/* ===== OI Block — بنية ثابتة بـ 5 صفوف ===== */
function buildOIBlockStatic(title, kind, color) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `
      <div class="oi-row" data-oi-row="${kind}-${i}">
        <div class="oi-bar-wrap"><div class="oi-bar" data-oi-bar="${kind}-${i}" style="width:0%;background:${color};"></div></div>
        <div class="oi-strike" data-oi-strike="${kind}-${i}">—</div>
      </div>`;
  }
  return `
    <div class="oi-box">
      <div class="oi-title">${title}</div>
      <div class="oi-rows">${rows}</div>
    </div>
  `;
}

/* ===== تحديث OI Block (بدون إعادة بناء) ===== */
function updateOIBlockData(root, kind, data) {
  if (!root) return;
  data = data || [];
  const maxOI = Math.max(...data.map(x => x.oi || 0), 1);
  for (let i = 0; i < 5; i++) {
    const bar = root.querySelector(`[data-oi-bar="${kind}-${i}"]`);
    const strike = root.querySelector(`[data-oi-strike="${kind}-${i}"]`);
    if (i < data.length) {
      const item = data[i];
      const pct = ((item.oi || 0) / maxOI) * 100;
      if (bar) bar.style.width = `${pct}%`;
      if (strike) strike.textContent = item.strike;
    } else {
      if (bar) bar.style.width = "0%";
      if (strike) strike.textContent = "—";
    }
  }
}

/* ===== الحيتان — 5 صفوف ثابتة ===== */
function buildWhalesStatic(sym) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `
      <div class="whale-row" data-whale-row="${sym}-${i}" style="display:none;">
        <div class="whale-strike" data-whale-strike="${sym}-${i}">—</div>
        <div class="whale-vol" data-whale-vol="${sym}-${i}">Vol: <b>—</b></div>
        <div class="whale-oi" data-whale-oi="${sym}-${i}">OI: <b>—</b></div>
        <div class="whale-dir" data-whale-dir="${sym}-${i}">—</div>
      </div>`;
  }
  return `
    <div class="whales-section" id="whales-${sym}" style="display:none;">
      <div class="whales-title">🐋 الحيتان المكتشفة</div>
      <div class="whales-list">${rows}</div>
    </div>
  `;
}

function updateWhalesData(root, sym, whales) {
  if (!root) return;
  const wrap = root.querySelector(`#whales-${sym}`);
  if (!wrap) return;

  whales = whales || [];

  if (whales.length === 0) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";

  for (let i = 0; i < 5; i++) {
    const row = root.querySelector(`[data-whale-row="${sym}-${i}"]`);
    if (!row) continue;

    if (i < whales.length) {
      const w = whales[i];
      row.style.display = "grid";
      row.className = "whale-row " + (w.type === "CALL" ? "whale-call" : "whale-put");

      const strike = row.querySelector(`[data-whale-strike="${sym}-${i}"]`);
      const vol = row.querySelector(`[data-whale-vol="${sym}-${i}"]`);
      const oi = row.querySelector(`[data-whale-oi="${sym}-${i}"]`);
      const dir = row.querySelector(`[data-whale-dir="${sym}-${i}"]`);

      if (strike) strike.textContent = `${w.type} ${w.strike}`;
      const volFmt = w.volume >= 1000 ? (w.volume / 1000).toFixed(1) + "K" : w.volume;
      const oiFmt  = w.oi >= 1000 ? (w.oi / 1000).toFixed(1) + "K" : w.oi;
      if (vol) vol.innerHTML = `Vol: <b>${volFmt}</b>`;
      if (oi)  oi.innerHTML  = `OI: <b>${oiFmt}</b>`;
      if (dir) dir.textContent = w.direction === "buy" ? "🟢 يشتري"
                              : w.direction === "sell" ? "🔴 يبيع"
                              : "⚪ محايد";
    } else {
      row.style.display = "none";
    }
  }
}

/* ===== الشرائط الكبيرة (OI + Volume) ===== */
function updatePutCallBars(root, sym, card) {
  const totalCallOI  = card.total_call_oi  || 0;
  const totalPutOI   = card.total_put_oi   || 0;
  const totalCallVol = card.total_call_vol || 0;
  const totalPutVol  = card.total_put_vol  || 0;

  const totalOI = totalCallOI + totalPutOI;
  let putOIPct = 50, callOIPct = 50;
  if (totalOI > 0) {
    putOIPct = Math.round(totalPutOI / totalOI * 100);
    callOIPct = 100 - putOIPct;
  }

  const totalVol = totalCallVol + totalPutVol;
  let putVolPct = 50, callVolPct = 50;
  if (totalVol > 0) {
    putVolPct = Math.round(totalPutVol / totalVol * 100);
    callVolPct = 100 - putVolPct;
  }

  const putOISpan = root.querySelector(`#put-oi-pct-${sym}`);
  const callOISpan = root.querySelector(`#call-oi-pct-${sym}`);
  const callOIFill = root.querySelector(`#call-oi-fill-${sym}`);
  if (putOISpan)  putOISpan.textContent  = `PUT ${putOIPct}%`;
  if (callOISpan) callOISpan.textContent = `${callOIPct}% CALL`;
  if (callOIFill) callOIFill.style.width = `${callOIPct}%`;

  const putVolSpan = root.querySelector(`#put-vol-pct-${sym}`);
  const callVolSpan = root.querySelector(`#call-vol-pct-${sym}`);
  const callVolFill = root.querySelector(`#call-vol-fill-${sym}`);
  if (putVolSpan)  putVolSpan.textContent  = `PUT ${putVolPct}%`;
  if (callVolSpan) callVolSpan.textContent = `${callVolPct}% CALL`;
  if (callVolFill) callVolFill.style.width = `${callVolPct}%`;
}

/* ===== تحديث جميع العناصر الديناميكية في root ===== */
function updateAllDynamic(root, sym, card) {
  if (!root || !card) return;
  const callOI = card.call_oi || [];
  const putOI  = card.put_oi  || [];

  updateOIBlockData(root, "put-oi",   putOI);
  updateOIBlockData(root, "put-liq",  putOI.map(x => ({strike: x.strike, oi: x.volume})));
  updateOIBlockData(root, "call-oi",  callOI);
  updateOIBlockData(root, "call-liq", callOI.map(x => ({strike: x.strike, oi: x.volume})));

  updatePutCallBars(root, sym, card);
  updateWhalesData(root, sym, card.whales || []);
}

function buildCard(cardData) {
  const c = cardData.card || {};
  const lv = cardData.levels || {};
  const cls = c.color || "gray";
  const price = cardData.price ?? 0;
  const sym = cardData.symbol;

  const div = document.createElement("div");
  div.className = "card " + cls;
  div.dataset.symbol = sym;

  const row1 = `
    <div class="card-row row-1">
      <div class="cell symbol-cell">${sym}</div>
      <div class="cell price-cell">
        <div class="val">$${price.toFixed(2)}</div>
        <div class="sub">السعر الحالي</div>
      </div>
      <div class="cell score-cell">
        <div class="val">${c.score ?? 0}%</div>
        <div class="sub">قوة الإشارة</div>
      </div>
      <div class="cell badge-cell">
        <div class="badge">🔥 ${c.label || "—"}</div>
        <button class="card-trash">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
          </svg>
        </button>
      </div>
    </div>
  `;

  const row2 = `
    <div class="card-row row-2">
      <div class="cell"><div class="label">DTE</div><div class="val">${lv.dte || "—"}</div></div>
      <div class="cell"><div class="label">PRICE</div><div class="val">${lv.premium && lv.premium !== "—" ? "$" + lv.premium : "—"}</div></div>
      <div class="cell"><div class="label">EXPIRY</div><div class="val">${lv.expiry || "—"}</div></div>
      <div class="cell"><div class="label">STRIKE</div><div class="val">${lv.strike || "—"}</div></div>
    </div>
  `;

  const row3 = `
    <div class="card-row row-3">
      <div class="cell"><div class="label">STOP</div><div class="val">${lv.stop ? "$" + lv.stop : "—"}</div></div>
      <div class="cell"><div class="label">TARGET 2</div><div class="val">${lv.target2 ? "$" + lv.target2 : "—"}</div></div>
      <div class="cell"><div class="label">TARGET 1</div><div class="val">${lv.target1 ? "$" + lv.target1 : "—"}</div></div>
      <div class="cell"><div class="label">ENTRY</div><div class="val">${lv.entry ? "$" + lv.entry : "—"}</div></div>
    </div>
  `;

  const tfs = cardData.timeframes || [];
  const tfsHtml = tfs.map(t => `
    <div class="tf-cell ${t.trend}">
      <div class="tf-label"><span>${t.label}</span><span>${t.trend === "up" ? "صاعد ↑" : "هابط ↓"}</span></div>
      <div class="tf-row"><span>EMA</span><span class="v">${t.ema20}/${t.ema50}</span></div>
      <div class="tf-row"><span>RSI</span><span class="v">${t.rsi}</span></div>
      <div class="tf-row"><span>ADX</span><span class="v">${t.adx}</span></div>
      <div class="tf-row"><span>RVOL</span><span class="v">${t.rvol}x</span></div>
    </div>
  `).join("");

  const oiHtml = `
    <div class="oi-grid">
      ${buildOIBlockStatic("PUT OI", "put-oi", "#8b5cf6")}
      ${buildOIBlockStatic("PUT LIQUIDITY", "put-liq", "#ef4444")}
      ${buildOIBlockStatic("CALL OI", "call-oi", "#3b82f6")}
      ${buildOIBlockStatic("CALL LIQUIDITY", "call-liq", "#22c55e")}
    </div>
  `;

  const barHtml = `
    <div class="putcall-wrapper">
      <div class="putcall-label">Open Interest</div>
      <div class="putcall-bar">
        <span class="put" id="put-oi-pct-${sym}">PUT 50%</span>
        <div class="track"><div class="fill" id="call-oi-fill-${sym}" style="width:50%"></div></div>
        <span class="call" id="call-oi-pct-${sym}">50% CALL</span>
      </div>
      <div class="putcall-label">Liquidity (Volume)</div>
      <div class="putcall-bar">
        <span class="put" id="put-vol-pct-${sym}">PUT 50%</span>
        <div class="track"><div class="fill" id="call-vol-fill-${sym}" style="width:50%"></div></div>
        <span class="call" id="call-vol-pct-${sym}">50% CALL</span>
      </div>
    </div>
  `;

  const whalesHtml = buildWhalesStatic(sym);

  const expanded = `
    <div class="card-expanded">
      <div class="tf-grid">${tfsHtml}</div>
      ${oiHtml}
      ${barHtml}
      ${whalesHtml}
      <div class="bottom-grid">
        <div class="cell"><div class="label">VWAP</div><div class="val">$${cardData.vwap ?? "—"}</div></div>
        <div class="cell"><div class="label">مقاومات</div><div class="val">${(cardData.resistances||[]).join(" / ") || "—"}</div></div>
        <div class="cell"><div class="label">دعوم</div><div class="val">${(cardData.supports||[]).join(" / ") || "—"}</div></div>
        <div class="cell"><div class="label">السعر</div><div class="val">$${price.toFixed(2)}</div></div>
      </div>
      <div class="summary">${buildSummary(cardData)}</div>
    </div>
  `;

  div.innerHTML = row1 + row2 + row3 + expanded;

  // ✅ ملء البيانات الديناميكية الآن (بدون وميض لاحقاً)
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

function buildSummary(c) {
  const p = [];
  if (c.sweep) p.push("سحب سيولة ✓");
  if (c.ifvg) p.push("IFVG ✓");
  if (c.mss) p.push("MSS ✓");
  const dir = c.card?.color === "green" ? "CALL" : c.card?.color === "red" ? "PUT" : "انتظار";
  return `📌 إشارة ${dir} — ${p.join(" | ") || "لا توجد شروط محققة"}.`;
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
    startOptionsPolling(symbol);
  } catch (e) {
    setConnection(false);
    alert("تعذّر الاتصال:\n" + e.message);
  }
}

function deleteCard(symbol) {
  cards = cards.filter(c => c.symbol !== symbol);
  saveCards(); renderCards();
  if (socketMap.has(symbol)) {
    try { socketMap.get(symbol).close(); } catch (e) {}
    socketMap.delete(symbol);
  }
  if (reconnectTimers.has(symbol)) {
    clearTimeout(reconnectTimers.get(symbol));
    reconnectTimers.delete(symbol);
  }
  if (optionsTimers.has(symbol)) {
    clearInterval(optionsTimers.get(symbol));
    optionsTimers.delete(symbol);
  }
}

function updateCardPrice(symbol, price) {
  const card = cards.find(c => c.symbol === symbol);
  if (!card) return;
  if (price && price > 0 && price !== card.price) {
    card.price = price;
    const el = cardsArea.querySelector(`[data-symbol="${symbol}"] .price-cell .val`);
    if (el) el.textContent = "$" + parseFloat(price).toFixed(2);
    const elBottom = cardsArea.querySelector(`[data-symbol="${symbol}"] .bottom-grid .cell:last-child .val`);
    if (elBottom) elBottom.textContent = "$" + parseFloat(price).toFixed(2);
  }
}

/* ✅ تحديث الخيارات — بدون إعادة بناء */
function updateCardOptions(symbol, opt) {
  const card = cards.find(c => c.symbol === symbol);
  if (!card) return;

  if (opt.total_call_oi  != null) card.total_call_oi  = opt.total_call_oi;
  if (opt.total_put_oi   != null) card.total_put_oi   = opt.total_put_oi;
  if (opt.total_call_vol != null) card.total_call_vol = opt.total_call_vol;
  if (opt.total_put_vol  != null) card.total_put_vol  = opt.total_put_vol;
  if (opt.call_oi)  card.call_oi = opt.call_oi;
  if (opt.put_oi)   card.put_oi  = opt.put_oi;
  if (opt.whales)   card.whales  = opt.whales;

  const cardEl = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
  if (!cardEl) return;
  if (!cardEl.classList.contains("open")) return;

  updateAllDynamic(cardEl, symbol, card);
}

async function fetchCardOptions(symbol) {
  try {
    const r = await fetch(`${API_BASE}/api/options/${symbol}`);
    if (!r.ok) return;
    const d = await r.json();
    updateCardOptions(symbol, d);
  } catch (e) {}
}

function startOptionsPolling(symbol) {
  if (optionsTimers.has(symbol)) {
    clearInterval(optionsTimers.get(symbol));
  }
  setTimeout(() => fetchCardOptions(symbol), 10000);
  const timer = setInterval(() => fetchCardOptions(symbol), 30000);
  optionsTimers.set(symbol, timer);
}

function connectLive(symbol) {
  if (socketMap.has(symbol)) {
    try { socketMap.get(symbol).close(); } catch (e) {}
  }

  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/${symbol}`);

  ws.onopen = () => {
    setConnection(true);
    ws._ping = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        try { ws.send("ping"); } catch (e) {}
      }
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
      if (d.price) {
        updateCardPrice(card.symbol, d.price);
      }
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
    startOptionsPolling(c.symbol);
  });
  checkBackendStatus();
  setInterval(checkBackendStatus, 60000);
  setTimeout(pollPrices, 3000);
  pollTimer = setInterval(pollPrices, 5000);
})();
