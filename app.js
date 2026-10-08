/* ============================================================
   app.js — منطق الواجهة
   ============================================================ */
const API_BASE = window.location.origin;

let cards = [];
const socketMap = new Map();

function saveCards() {
  try { localStorage.setItem("stock_cards", JSON.stringify(cards)); } catch (e) {}
}
function loadCards() {
  try {
    const raw = localStorage.getItem("stock_cards");
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}

const cardsArea      = document.getElementById("cardsArea");
const emptyState     = document.getElementById("emptyState");
const searchForm     = document.getElementById("searchForm");
const symbolInput    = document.getElementById("symbolInput");
const themeBtn       = document.getElementById("themeBtn");
const themeIcon      = document.getElementById("themeIcon");
const marketStatus   = document.getElementById("marketStatus");
const connectionStatus = document.getElementById("connectionStatus");

/* ===== الثيم ===== */
function applyTheme(light) {
  document.body.classList.toggle("light", light);
  themeIcon.textContent = light ? "☀️" : "🌙";
  localStorage.setItem("theme", light ? "light" : "dark");
}
themeBtn.addEventListener("click", () => {
  applyTheme(!document.body.classList.contains("light"));
});
applyTheme(localStorage.getItem("theme") === "light");

/* ===== حالة السوق ===== */
function updateMarketStatus() {
  const now = new Date();
  const utcH = now.getUTCHours();
  const nyH = (utcH - 4 + 24) % 24;
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

/* ===== حالة الاتصال ===== */
function setConnection(online, extra) {
  connectionStatus.className = "status " + (online ? "online" : "offline");
  connectionStatus.querySelector(".label").textContent =
    online ? (extra ? "متصل ✓" : "متصل") : "غير متصل";
}

async function checkBackendStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/status`);
    const data = await res.json();
    if (data.connected) {
      setConnection(true);
      console.log("✅ Longbridge connected:", data.symbol, data.price);
    } else {
      setConnection(false);
      console.error("❌ Backend error:", data);
      alert("خطأ في الاتصال بـ Longbridge:\n" + (data.error || "غير معروف"));
    }
  } catch (e) {
    setConnection(false);
    console.error("❌ Network error:", e);
  }
}

/* ===== بناء البطاقة ===== */
function buildCard(cardData) {
  const c = cardData.card || {};
  const lv = cardData.levels || {};
  const cls = c.color || "gray";
  const price = cardData.price ?? 0;
  const chg = cardData.change ?? 0;
  const chgPct = cardData.changePercent ?? 0;
  const sign = chg >= 0 ? "+" : "";

  const div = document.createElement("div");
  div.className = "card " + cls;
  div.dataset.symbol = cardData.symbol;

  const head = `
    <div class="card-head">
      <div class="card-badge">🔥 ${c.label || "—"}</div>
      <div class="card-score">
        <div class="value">${c.score ?? 0}%</div>
        <div class="sub">قوة الإشارة</div>
      </div>
      <div class="card-price">
        <div class="value">$${price.toFixed(2)}</div>
        <div class="sub">${sign}${chg.toFixed(2)} (${sign}${chgPct.toFixed(2)}%)</div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;justify-self:end;">
        <div class="card-symbol">${cardData.symbol}</div>
        <button class="card-trash" title="حذف">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
            <path d="M10 11v6M14 11v6"></path>
          </svg>
        </button>
      </div>
    </div>
  `;

  const options = `
    <div class="card-options">
      <div class="cell"><div class="label">DTE</div><div class="val">${lv.dte ?? "—"}</div></div>
      <div class="cell"><div class="label">PRICE</div><div class="val">${lv.premium ? "$" + lv.premium : "—"}</div></div>
      <div class="cell"><div class="label">EXPIRY</div><div class="val">${lv.expiry ?? "—"}</div></div>
      <div class="cell"><div class="label">STRIKE</div><div class="val">${lv.strike ?? "—"}</div></div>
    </div>
  `;

  const levels = `
    <div class="card-levels">
      <div class="cell"><div class="label">STOP</div><div class="val">${lv.stop ? "$" + lv.stop : "—"}</div></div>
      <div class="cell"><div class="label">TARGET 2</div><div class="val">${lv.target2 ? "$" + lv.target2 : "—"}</div></div>
      <div class="cell"><div class="label">TARGET 1</div><div class="val">${lv.target1 ? "$" + lv.target1 : "—"}</div></div>
      <div class="cell"><div class="label">ENTRY</div><div class="val">${lv.entry ? "$" + lv.entry : "—"}</div></div>
    </div>
  `;

  const tfs = cardData.timeframes || [];
  const tfsHtml = tfs.map(t => `
    <div class="tf-cell ${t.trend}">
      <div class="tf-label"><span>${t.label}</span><span>${t.trend === "up" ? "↑ صاعد" : "↓ هابط"}</span></div>
      <div class="tf-row"><span>EMA</span><span class="v">${t.ema20}/${t.ema50}</span></div>
      <div class="tf-row"><span>RSI</span><span class="v">${t.rsi}</span></div>
      <div class="tf-row"><span>ADX</span><span class="v">${t.adx}</span></div>
      <div class="tf-row"><span>RVOL</span><span class="v">${t.rvol}x</span></div>
    </div>
  `).join("");

  const putPct = c.color === "red" ? 60 : c.color === "green" ? 40 : 50;
  const callPct = 100 - putPct;

  const expanded = `
    <div class="card-expanded">
      <div class="tf-grid">${tfsHtml}</div>
      <div class="putcall-bar">
        <span class="put">PUT ${putPct}%</span>
        <div class="track"><div class="fill" style="width:${callPct}%"></div></div>
        <span class="call">${callPct}% CALL</span>
      </div>
      <div class="bottom-grid">
        <div class="cell"><div class="label">السعر الحالي</div><div class="val">$${price.toFixed(2)}</div></div>
        <div class="cell"><div class="label">الدعوم</div><div class="val">${(cardData.supports||[]).join(" / ") || "—"}</div></div>
        <div class="cell"><div class="label">المقاومات</div><div class="val">${(cardData.resistances||[]).join(" / ") || "—"}</div></div>
        <div class="cell"><div class="label">VWAP</div><div class="val">$${cardData.vwap ?? "—"}</div></div>
      </div>
      <div class="summary">${buildSummary(cardData)}</div>
    </div>
  `;

  div.innerHTML = head + options + levels + expanded;

  div.addEventListener("click", (e) => {
    if (e.target.closest(".card-trash")) return;
    div.classList.toggle("open");
  });
  div.querySelector(".card-trash").addEventListener("click", (e) => {
    e.stopPropagation();
    deleteCard(cardData.symbol);
  });

  return div;
}

function buildSummary(c) {
  const parts = [];
  if (c.sweep) parts.push("سحب سيولة ✓");
  if (c.ifvg) parts.push("IFVG ✓");
  if (c.mss) parts.push("MSS ✓");
  const dir = c.card?.color === "green" ? "CALL"
            : c.card?.color === "red" ? "PUT" : "انتظار";
  return `📌 إشارة ${dir} — ${parts.join(" | ") || "لا توجد شروط محققة"}.`;
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
  const existing = cards.find(c => c.symbol === symbol);
  if (existing) {
    const el = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
    if (el) el.classList.add("open");
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/analyze/${symbol}`);
    const data = await res.json();
    if (!res.ok) {
      console.error("Backend error:", data);
      alert(`خطأ ${res.status}:\n${data.error || "غير معروف"}\n\nالنوع: ${data.type || "-"}\n\n${(data.traceback||[]).slice(-5).join("\n")}`);
      setConnection(false);
      return;
    }
    setConnection(true);
    cards.push(data);
    saveCards();
    renderCards();
    connectLive(symbol);
  } catch (e) {
    setConnection(false);
    alert("تعذّر الاتصال بالخادم:\n" + e.message);
  }
}

function deleteCard(symbol) {
  cards = cards.filter(c => c.symbol !== symbol);
  saveCards();
  renderCards();
  if (socketMap.has(symbol)) {
    try { socketMap.get(symbol).close(); } catch (e) {}
    socketMap.delete(symbol);
  }
}

function connectLive(symbol) {
  if (socketMap.has(symbol)) return;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/${symbol}`);
  ws.onopen  = () => setConnection(true);
  ws.onclose = () => setConnection(false);
  ws.onerror = () => setConnection(false);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      const el = cardsArea.querySelector(`[data-symbol="${msg.symbol}"] .card-price .value`);
      if (el) el.textContent = "$" + msg.price.toFixed(2);
    } catch (e) {}
  };
  socketMap.set(symbol, ws);
}

searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  searchSymbol(symbolInput.value);
  symbolInput.value = "";
  symbolInput.blur();
});

/* ===== التحميل ===== */
(function init() {
  cards = loadCards();
  renderCards();
  cards.forEach(c => connectLive(c.symbol));
  checkBackendStatus();      // ← فحص الاتصال فور فتح الصفحة
  setInterval(checkBackendStatus, 60000);  // ← إعادة الفحص كل دقيقة
})();
