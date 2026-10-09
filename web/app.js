/* Gold Trader phone app. Vanilla JS, no build step. Talks to /api on the same origin. */
(() => {
  "use strict";

  // ------------------------------------------------------------ state & api
  const S = {
    token: localStorage.getItem("gt_token") || "",
    status: null, presets: [], history: [], events: [],
    route: "home", param: null, timer: null, sse: null,
    form: null,          // new-trade form state
    detailEdit: {},      // edits in progress on the trade detail page
  };
  const $ = (sel, el = document) => el.querySelector(sel);
  const view = $("#view");

  async function api(path, opts = {}) {
    const headers = { Authorization: "Bearer " + S.token, ...(opts.body ? { "Content-Type": "application/json" } : {}) };
    let r;
    try {
      r = await fetch("/api" + path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
    } catch (e) {
      throw new Error("Can't reach the bot. Is the server running?");
    }
    if (r.status === 401) { showLogin(S.token ? "Token rejected" : ""); throw new Error("Not authorised"); }
    const data = r.status === 204 ? null : await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && (data.detail?.[0]?.msg || data.detail)) || r.statusText);
    return data;
  }

  // ------------------------------------------------------------------ utils
  const fmt = {
    price: (p, d = 2) => (p == null ? "—" : Number(p).toFixed(d)),
    usd: (v) => (v == null ? "—" : (v < 0 ? "-$" : "+$") + Math.abs(v).toFixed(2)),
    pips: (v) => (v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(v % 1 ? 1 : 0)),
    time: (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""),
    date: (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""),
    lot: (v) => (v == null ? "—" : Number(v).toFixed(2)),
    cls: (v) => (v > 0 ? "pos" : v < 0 ? "neg" : ""),
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => (v === "" || v == null ? null : Number(v));

  function toast(msg, kind = "info", ms = 5000) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), ms);
  }
  const fail = (e) => toast(e.message || String(e), "error", 7000);

  // ------------------------------------------------------------------ login
  function showLogin(msg = "") {
    $("#login").classList.remove("hidden");
    $("#login-error").textContent = msg;
    $("#login-token").focus();
  }
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    S.token = $("#login-token").value.trim();
    try {
      await api("/me");
      localStorage.setItem("gt_token", S.token);
      $("#login").classList.add("hidden");
      $("#login-token").value = "";
      boot();
    } catch (err) {
      $("#login-error").textContent = "That token was rejected.";
    }
  });

  // ------------------------------------------------------------- live data
  async function refreshStatus() {
    try {
      S.status = await api("/status");
      renderTopbar();
      if (S.route === "home" || S.route === "detail") render();
    } catch (e) {
      setConn(false);
    }
  }

  function setConn(on) {
    $("#conn-dot").className = "dot " + (on ? "on" : "off");
  }

  function renderTopbar() {
    const st = S.status;
    if (!st) return;
    setConn(st.connected && st.engine.last_tick > Date.now() / 1000 - 5);
    $("#sym").textContent = st.symbol;
    const q = st.quote;
    $("#quote").innerHTML = q ? `${fmt.price(q.bid, st.info.digits)}<small>${q.spread_pips} pip</small>` : "no quote";
  }

  function connectEvents() {
    if (S.sse) S.sse.close();
    const es = new EventSource("/api/events/stream?token=" + encodeURIComponent(S.token));
    S.sse = es;
    for (const kind of ["opened", "step_hit", "sl_moved", "closed", "error", "info", "signal"]) {
      es.addEventListener(kind, (e) => {
        const ev = JSON.parse(e.data);
        S.events.push(ev);
        if (S.events.length > 100) S.events.shift();
        toast(kind === "signal" ? `Signal · ${ev.message}` : `#${ev.ticket} ${ev.message}`, kind);
        if (navigator.vibrate && (kind === "closed" || kind === "error" || kind === "signal")) navigator.vibrate(200);
        notify(ev);
        refreshStatus();
        if (kind === "signal" && (S.route === "signals" || S.route === "channels")) render();
      });
    }
    es.onerror = () => setConn(false);
  }

  function notify(ev) {
    if (document.visibilityState === "visible" || !("Notification" in window) || Notification.permission !== "granted") return;
    try { new Notification("Gold Trader #" + ev.ticket, { body: ev.message, icon: "icon-192.png", tag: "gt-" + ev.id }); } catch (_) {}
  }

  // ----------------------------------------------------------------- router
  function navigate() {
    const hash = location.hash.replace(/^#\/?/, "");
    const [route, param] = hash.split("/");
    S.route = route || "home";
    S.param = param || null;
    if (S.route === "trade" && !S.form) S.form = null;
    const tab = { detail: "home", channels: "signals", presets: "settings" }[S.route] || S.route;
    document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
    S.detailEdit = {};
    render();
  }
  window.addEventListener("hashchange", navigate);

  async function render() {
    try {
      switch (S.route) {
        case "home": return renderHome();
        case "trade": return renderTradeForm();
        case "detail": return renderDetail();
        case "history": return renderHistory();
        case "presets": return renderPresets();
        case "signals": return renderSignals();
        case "channels": return renderChannels();
        case "settings": return renderSettings();
        default: location.hash = "#/";
      }
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------------- home
  function tradeCard(t) {
    const d = t.digits;
    const steps = t.settings.steps.map((s, i) => {
      const st = t.steps[i];
      const what = [s.close_pct ? `close ${s.close_pct}%` : "", s.move_sl_pips == null ? "" : s.move_sl_pips === 0 ? "SL→BE" : `SL→+${s.move_sl_pips}`].filter(Boolean).join(", ");
      return `<div class="step ${st.status}"><b>P${i + 1} +${s.pips}</b>${what || "no action"}</div>`;
    }).join("");
    return `<div class="card clickable" data-go="#/detail/${t.ticket}">
      <div class="row between">
        <div class="row"><span class="side ${t.side}">${t.side}</span><b>${fmt.lot(t.current_volume)}</b><span class="muted small">/ ${fmt.lot(t.volume)} lot</span></div>
        <div class="right"><div class="big ${fmt.cls(t.profit_usd)}">${fmt.usd(t.profit_usd)}</div><div class="small ${fmt.cls(t.profit_pips)}">${fmt.pips(t.profit_pips)} pips</div></div>
      </div>
      <div class="kv small" style="margin-top:8px">
        <div>Entry</div><div>${fmt.price(t.entry, d)}</div>
        <div>SL</div><div>${t.sl == null ? "none" : `${fmt.price(t.sl, d)} <span class="muted">(${fmt.pips(t.sl_pips)})</span>`}</div>
        <div>TP</div><div>${t.tp == null ? "none" : `${fmt.price(t.tp, d)} <span class="muted">(${fmt.pips(t.tp_pips)})</span>`}</div>
      </div>
      <div class="steps">${steps}</div>
      ${t.settings.trailing && t.steps[2].status === "hit" ? `<div class="small muted" style="margin-top:6px">Trailing every ${t.settings.trail_pips} pips · ${t.trail_k} move${t.trail_k === 1 ? "" : "s"}</div>` : ""}
    </div>`;
  }

  function renderHome() {
    const st = S.status;
    if (!st) { if (!view.innerHTML) view.innerHTML = `<div class="empty">Connecting…</div>`; return; }
    const a = st.account;
    const open = st.open_trades;
    view.innerHTML = `
      <div class="stats">
        <div class="stat"><div class="label">Balance</div><div class="value">${a.balance.toFixed(2)}</div></div>
        <div class="stat"><div class="label">Equity</div><div class="value">${a.equity.toFixed(2)}</div></div>
        <div class="stat"><div class="label">Today</div><div class="value ${fmt.cls(st.today_usd)}">${fmt.usd(st.today_usd)}</div></div>
        <div class="stat"><div class="label">Floating</div><div class="value ${fmt.cls(st.floating_usd)}">${fmt.usd(st.floating_usd)}</div></div>
      </div>
      ${!st.market_open ? `<p class="warn">Market closed: new trades are blocked.</p>` : ""}
      ${st.engine.last_error ? `<p class="error">Engine error: ${esc(st.engine.last_error)}</p>` : ""}
      ${st.engine.sim ? `<p class="warn">Simulator mode: no real money.</p>` : ""}
      <h2>Open trades (${open.length})</h2>
      ${open.length ? open.map(tradeCard).join("") : `<div class="card empty">No open trades.<br><a href="#/trade">Open one →</a></div>`}
      ${(st.unmanaged || []).length ? `<h2>Not managed yet</h2>
        <p class="warn small">These positions carry the bot's tag on MT5 but the bot isn't running steps on them. Adopt one to manage it with a preset, or close it in MT5.</p>
        ${st.unmanaged.map((p) => `<div class="card">
          <div class="row between"><div class="row"><span class="side ${p.side}">${p.side}</span><b>${fmt.lot(p.volume)}</b><span class="muted small">#${p.ticket} @ ${fmt.price(p.entry, st.info.digits)}</span></div>
            <span class="small muted">SL ${p.sl == null ? "none" : fmt.price(p.sl, st.info.digits)}</span></div>
          <div class="row" style="margin-top:8px"><select class="grow" data-adopt-preset="${p.ticket}" style="margin:0">${S.presets.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
            <button class="primary sm" data-adopt="${p.ticket}">Adopt</button></div>
        </div>`).join("")}` : ""}
      <h2>Recent events</h2>
      <div class="card" id="events">${eventsHtml()}</div>`;
    view.querySelectorAll("[data-go]").forEach((el) => el.addEventListener("click", () => (location.hash = el.dataset.go)));
    view.querySelectorAll("[data-adopt]").forEach((b) => b.addEventListener("click", async () => {
      const ticket = b.dataset.adopt;
      const p = S.presets.find((x) => x.id === Number($(`[data-adopt-preset="${ticket}"]`).value));
      if (!p || !confirm(`Manage #${ticket} with preset "${p.name}"? Steps that are already reached will fire right away.`)) return;
      try { await api(`/trades/${ticket}/adopt`, { method: "POST", body: p.settings }); toast(`#${ticket} adopted`); refreshStatus(); } catch (e) { fail(e); }
    }));
  }

  function eventsHtml() {
    const evs = S.events.slice(-20).reverse();
    if (!evs.length) return `<div class="empty small">Nothing yet.</div>`;
    return evs.map((e) => `<div class="event ${e.kind}"><span class="t">${fmt.time(e.ts)}</span><span class="m">#${e.ticket} ${esc(e.message)}</span></div>`).join("");
  }

  // ------------------------------------------------------------- trade form
  function defaultForm() {
    const p = S.presets[0];
    return { side: "buy", preset_id: p ? p.id : null, settings: p ? structuredClone(p.settings) : {
      lot: 0.04, stop_mode: "medium", sl_pips: null, tp_pips: null, trailing: true, trail_pips: 50,
      steps: [{ pips: 20, close_pct: 50, move_sl_pips: 0 }, { pips: 50, close_pct: 25, move_sl_pips: 20 }, { pips: 100, close_pct: 0, move_sl_pips: 50 }],
    } };
  }

  function settingsFields(s, prefix = "f") {
    const steps = s.steps.map((st, i) => `<tr>
      <td>P${i + 1}</td>
      <td><input type="number" step="1" min="1" data-step="${i}" data-k="pips" value="${st.pips}"></td>
      <td><input type="number" step="5" min="0" max="100" data-step="${i}" data-k="close_pct" value="${st.close_pct}"></td>
      <td><input type="number" step="1" min="0" data-step="${i}" data-k="move_sl_pips" value="${st.move_sl_pips ?? ""}" placeholder="keep"></td>
    </tr>`).join("");
    return `
      <div class="grid2">
        <label>Lot<input type="number" step="0.01" min="0.01" id="${prefix}-lot" value="${s.lot}"></label>
        <label>Take profit (pips)<input type="number" step="10" min="0" id="${prefix}-tp" value="${s.tp_pips ?? ""}" placeholder="none"></label>
      </div>
      <label>Stop loss</label>
      <div class="seg" id="${prefix}-mode">
        ${["tight", "medium", "far"].map((m) => `<button type="button" data-mode="${m}" class="${s.stop_mode === m ? "on" : ""}">${m[0].toUpperCase() + m.slice(1)}${m === "tight" ? " 20" : m === "medium" ? " 50" : ""}</button>`).join("")}
      </div>
      <label class="${s.stop_mode === "far" ? "" : "hidden"}" id="${prefix}-sl-wrap">Far SL (pips, blank = backup stop)<input type="number" step="10" min="0" id="${prefix}-sl" value="${s.sl_pips ?? ""}" placeholder="e.g. 150"></label>
      <h2>Profit steps</h2>
      <table><thead><tr><th>Step</th><th>Trigger +pips</th><th>Close %</th><th>Move SL to +pips</th></tr></thead><tbody>${steps}</tbody></table>
      <div class="small muted" style="margin-top:6px">Move SL: 0 = breakeven, blank = don't move.</div>
      <div class="inline"><input type="checkbox" id="${prefix}-trail" ${s.trailing ? "checked" : ""}><label for="${prefix}-trail" style="margin:0;color:var(--text)">Trail after P3 every</label>
        <input type="number" step="10" min="1" id="${prefix}-trailpips" value="${s.trail_pips}" style="width:90px;margin:0"><span>pips</span></div>`;
  }

  function readSettings(root, prefix = "f") {
    const mode = $(`#${prefix}-mode .on`, root).dataset.mode;
    return {
      lot: Number($(`#${prefix}-lot`, root).value),
      stop_mode: mode,
      sl_pips: mode === "far" ? num($(`#${prefix}-sl`, root).value) : null,
      tp_pips: num($(`#${prefix}-tp`, root).value),
      steps: [0, 1, 2].map((i) => ({
        pips: Number($(`[data-step="${i}"][data-k="pips"]`, root).value),
        close_pct: Number($(`[data-step="${i}"][data-k="close_pct"]`, root).value || 0),
        move_sl_pips: num($(`[data-step="${i}"][data-k="move_sl_pips"]`, root).value),
      })),
      trailing: $(`#${prefix}-trail`, root).checked,
      trail_pips: Number($(`#${prefix}-trailpips`, root).value),
    };
  }

  function wireSettings(root, prefix = "f") {
    $(`#${prefix}-mode`, root).addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      root.querySelectorAll(`#${prefix}-mode button`).forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      $(`#${prefix}-sl-wrap`, root).classList.toggle("hidden", b.dataset.mode !== "far");
    });
  }

  async function renderTradeForm() {
    if (!S.presets.length) S.presets = await api("/presets").catch(() => []);
    if (!S.form) S.form = defaultForm();
    const f = S.form;
    view.innerHTML = `
      <h1>New trade</h1>
      <div class="seg" id="side">
        <button type="button" data-side="buy" class="${f.side === "buy" ? "on buy" : ""}">BUY</button>
        <button type="button" data-side="sell" class="${f.side === "sell" ? "on sell" : ""}">SELL</button>
      </div>
      <label>Preset<select id="preset"><option value="">Custom</option>${S.presets.map((p) => `<option value="${p.id}" ${p.id === f.preset_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
      <div id="fields">${settingsFields(f.settings)}</div>
      <div class="btns"><button type="button" id="preview">Preview</button><button type="button" id="save-preset" class="ghost">Save as preset</button></div>
      <div id="preview-out"></div>`;
    wireSettings(view);
    $("#side").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      f.side = b.dataset.side;
      view.querySelectorAll("#side button").forEach((x) => (x.className = x.dataset.side === f.side ? "on " + f.side : ""));
      $("#preview-out").innerHTML = "";
    });
    $("#preset").addEventListener("change", (e) => {
      const p = S.presets.find((x) => x.id === Number(e.target.value));
      f.preset_id = p ? p.id : null;
      if (p) { f.settings = structuredClone(p.settings); $("#fields").innerHTML = settingsFields(f.settings); wireSettings(view); }
    });
    $("#preview").addEventListener("click", preview);
    $("#save-preset").addEventListener("click", async () => {
      const name = prompt("Preset name");
      if (!name) return;
      try {
        const p = await api("/presets", { method: "POST", body: { name, settings: readSettings(view) } });
        S.presets.push(p); f.preset_id = p.id; toast("Preset saved"); renderTradeForm();
      } catch (e) { fail(e); }
    });
  }

  async function preview() {
    const f = S.form;
    f.settings = readSettings(view);
    const out = $("#preview-out");
    out.innerHTML = `<div class="card muted">Checking…</div>`;
    try {
      const p = await api("/trades/preview", { method: "POST", body: { side: f.side, settings: f.settings } });
      const d = S.status?.info?.digits ?? 2;
      const ok = !p.errors.length;
      out.innerHTML = `<div class="card">
        <h3>${f.side.toUpperCase()} ${fmt.lot(f.settings.lot)} @ ${fmt.price(p.entry, d)}</h3>
        <div class="kv" style="margin-top:8px">
          <div>Stop loss</div><div>${p.sl ? `${fmt.price(p.sl.price, d)} <span class="neg">${fmt.usd(p.sl.usd)}</span>` : "none"}</div>
          <div>Take profit</div><div>${p.tp ? `${fmt.price(p.tp.price, d)} <span class="pos">${fmt.usd(p.tp.usd)}</span>` : "none"}</div>
          ${p.steps.map((s, i) => `<div>P${i + 1}</div><div>${fmt.price(s.price, d)} ${s.close_volume ? `· close ${fmt.lot(s.close_volume)} <span class="pos">${fmt.usd(s.usd)}</span>` : ""}</div>`).join("")}
        </div>
        ${p.warnings.map((w) => `<p class="warn">${esc(w)}</p>`).join("")}
        ${p.errors.map((e) => `<p class="error">${esc(e)}</p>`).join("")}
        <button type="button" class="wide ${f.side}" id="confirm" ${ok ? "" : "disabled"}>Send ${f.side.toUpperCase()} ${fmt.lot(f.settings.lot)}</button>
      </div>`;
      $("#confirm")?.addEventListener("click", async () => {
        $("#confirm").disabled = true;
        try {
          const t = await api("/trades", { method: "POST", body: { side: f.side, settings: f.settings } });
          S.form = null;
          location.hash = "#/detail/" + t.ticket;
        } catch (e) { fail(e); $("#confirm").disabled = false; }
      });
    } catch (e) { out.innerHTML = ""; fail(e); }
  }

  // ------------------------------------------------------------ trade detail
  async function renderDetail() {
    const ticket = Number(S.param);
    let t;
    try { t = await api("/trades/" + ticket); } catch (e) { view.innerHTML = `<a class="back" href="#/">← Back</a><div class="empty">${esc(e.message)}</div>`; return; }
    const d = t.digits;
    const edit = S.detailEdit;   // keep what the user is typing across live refreshes
    const stepRows = t.settings.steps.map((s, i) => {
      const st = t.steps[i];
      const locked = st.status !== "pending";
      const status = st.status === "hit" ? `<span class="pos">hit${st.closed_volume ? ` · ${fmt.lot(st.closed_volume)} @ ${fmt.price(st.close_price, d)}` : ""}</span>`
        : st.status === "failed" ? `<span class="neg">failed: ${esc(st.error)}</span> <button class="sm" data-retry="${i}">Retry</button>`
        : `<span class="muted">${fmt.price(t.step_prices[i], d)}</span>`;
      return `<tr>
        <td>P${i + 1}<br><span class="small">${status}</span></td>
        <td><input type="number" ${locked ? "disabled" : ""} data-step="${i}" data-k="pips" value="${edit[`s${i}pips`] ?? s.pips}"></td>
        <td><input type="number" ${locked ? "disabled" : ""} data-step="${i}" data-k="close_pct" value="${edit[`s${i}close_pct`] ?? s.close_pct}"></td>
        <td><input type="number" ${locked ? "disabled" : ""} data-step="${i}" data-k="move_sl_pips" value="${edit[`s${i}move_sl_pips`] ?? (s.move_sl_pips ?? "")}" placeholder="keep"></td>
      </tr>`;
    }).join("");

    view.innerHTML = `
      <a class="back" href="#/">← Back</a>
      <div class="card">
        <div class="row between">
          <div class="row"><span class="side ${t.side}">${t.side}</span><b>#${t.ticket}</b><span class="muted small">${fmt.date(t.opened_at)}</span></div>
          <div class="right"><div class="big ${fmt.cls(t.profit_usd)}">${fmt.usd(t.profit_usd)}</div><div class="small ${fmt.cls(t.profit_pips)}">${fmt.pips(t.profit_pips)} pips</div></div>
        </div>
        <div class="kv" style="margin-top:10px">
          <div>Volume</div><div>${fmt.lot(t.current_volume)} of ${fmt.lot(t.volume)}</div>
          <div>Entry</div><div>${fmt.price(t.entry, d)}</div>
          <div>Now</div><div>${fmt.price(t.current_price, d)}</div>
          <div>Stop loss</div><div>${t.sl == null ? "none" : `${fmt.price(t.sl, d)} (${fmt.pips(t.sl_pips)})`}</div>
          <div>Take profit</div><div>${t.tp == null ? "none" : `${fmt.price(t.tp, d)} (${fmt.pips(t.tp_pips)})`}</div>
          ${t.open ? "" : `<div>Closed</div><div>${fmt.date(t.closed_at)} · ${esc(t.close_reason)}</div>`}
        </div>
      </div>
      ${t.open ? `
      <h2>Stop loss & take profit <span class="muted">(pips from entry)</span></h2>
      <div class="card">
        <div class="grid2">
          <label>SL pips (− loss, + locked)<input type="number" step="5" id="e-sl" value="${edit.sl ?? (t.sl_pips ?? "")}" placeholder="${t.sl_pips ?? "none"}"></label>
          <label>TP pips<input type="number" step="10" id="e-tp" value="${edit.tp ?? (t.tp_pips ?? "")}" placeholder="none"></label>
        </div>
        <div class="small muted" id="e-prices" style="margin-top:6px"></div>
        <div class="btns"><button class="primary" id="save-sltp">Update SL / TP</button><button class="ghost" id="clear-tp">Remove TP</button></div>
      </div>
      <h2>Profit steps</h2>
      <div class="card">
        <table><thead><tr><th>Step</th><th>+pips</th><th>Close %</th><th>SL → +pips</th></tr></thead><tbody>${stepRows}</tbody></table>
        <div class="row between" style="margin-top:10px">
          <div class="small muted">Trailing: ${t.settings.trailing ? `on, every ${t.settings.trail_pips} pips` : "off"}</div>
          <button class="primary sm" id="save-steps">Update steps</button>
        </div>
      </div>
      <h2>Close</h2>
      <div class="card">
        <label>Partial volume (lot)<input type="number" step="0.01" min="0.01" max="${t.current_volume}" id="e-vol" value="${edit.vol ?? ""}" placeholder="${fmt.lot(t.current_volume)}"></label>
        <div class="btns">
          ${[25, 50, 75].map((p) => `<button class="sm" data-pct="${p}">${p}%</button>`).join("")}
          <button class="sm" id="close-part">Close part</button>
        </div>
        <button class="wide danger" id="close-all">Close entire trade (${fmt.lot(t.current_volume)} lot)</button>
      </div>` : `
      <div class="btns"><button class="primary" id="reenter">Re-enter: ${t.side.toUpperCase()} ${fmt.lot(t.volume)} with the same settings</button></div>`}
      <h2>Settings used</h2>
      <div class="card small kv">
        <div>Stop mode</div><div>${t.settings.stop_mode}${t.settings.sl_pips ? ` (${t.settings.sl_pips} pips)` : ""}</div>
        <div>Trailing</div><div>${t.settings.trailing ? `${t.settings.trail_pips} pips, ${t.trail_k} move${t.trail_k === 1 ? "" : "s"}` : "off"}</div>
      </div>`;

    if (!t.open) {
      $("#reenter")?.addEventListener("click", async () => {
        try { const n = await api(`/trades/${t.ticket}/reenter`, { method: "POST" }); location.hash = "#/detail/" + n.ticket; } catch (e) { fail(e); }
      });
      return;
    }

    const pricesHint = () => {
      const sl = num($("#e-sl").value), tp = num($("#e-tp").value);
      const sign = t.side === "buy" ? 1 : -1;
      const px = (p) => fmt.price(t.entry + sign * p * 0.1, d);
      $("#e-prices").textContent = `${sl == null ? "" : `SL at ${px(sl)}`}${sl != null && tp != null ? " · " : ""}${tp == null ? "" : `TP at ${px(tp)}`}`;
    };
    pricesHint();
    view.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => {
      if (inp.id === "e-sl") edit.sl = inp.value; else if (inp.id === "e-tp") edit.tp = inp.value; else if (inp.id === "e-vol") edit.vol = inp.value;
      else if (inp.dataset.step != null) edit[`s${inp.dataset.step}${inp.dataset.k}`] = inp.value;
      pricesHint();
    }));

    const act = async (fn, okMsg) => { try { await fn(); S.detailEdit = {}; if (okMsg) toast(okMsg); await refreshStatus(); renderDetail(); } catch (e) { fail(e); } };

    $("#save-sltp").addEventListener("click", () => act(async () => {
      const sl = num($("#e-sl").value), tp = num($("#e-tp").value);
      const body = {};
      if (sl != null && sl !== t.sl_pips) body.sl_pips = sl;
      if (tp != null && tp !== t.tp_pips) body.tp_pips = tp;
      if (!Object.keys(body).length) throw new Error("Nothing changed");
      await api(`/trades/${t.ticket}`, { method: "PATCH", body });
    }));
    $("#clear-tp").addEventListener("click", () => act(() => api(`/trades/${t.ticket}`, { method: "PATCH", body: { clear_tp: true } }), "TP removed"));
    $("#save-steps").addEventListener("click", () => act(async () => {
      const steps = {};
      t.settings.steps.forEach((s, i) => {
        if (t.steps[i].status !== "pending") return;
        const g = (k) => $(`[data-step="${i}"][data-k="${k}"]`).value;
        const patch = {};
        if (Number(g("pips")) !== s.pips) patch.pips = Number(g("pips"));
        if (Number(g("close_pct") || 0) !== s.close_pct) patch.close_pct = Number(g("close_pct") || 0);
        const mv = num(g("move_sl_pips"));
        if (mv !== (s.move_sl_pips ?? null)) { if (mv == null) patch.clear_move_sl = true; else patch.move_sl_pips = mv; }
        if (Object.keys(patch).length) steps[i] = patch;
      });
      if (!Object.keys(steps).length) throw new Error("Nothing changed");
      await api(`/trades/${t.ticket}`, { method: "PATCH", body: { steps } });
    }, "Steps updated"));
    view.querySelectorAll("[data-retry]").forEach((b) => b.addEventListener("click", () => act(() => api(`/trades/${t.ticket}/retry/${b.dataset.retry}`, { method: "POST" }), "Step queued again")));
    view.querySelectorAll("[data-pct]").forEach((b) => b.addEventListener("click", () => {
      const step = S.status?.info?.vol_step || 0.01;
      const v = Math.floor(t.current_volume * b.dataset.pct / 100 / step + 1e-9) * step;
      $("#e-vol").value = v.toFixed(2); edit.vol = $("#e-vol").value;
    }));
    $("#close-part").addEventListener("click", () => {
      const v = num($("#e-vol").value);
      if (!v || v <= 0) return fail(new Error("Enter a volume to close"));
      if (!confirm(`Close ${v.toFixed(2)} lot of #${t.ticket}?`)) return;
      act(() => api(`/trades/${t.ticket}/close`, { method: "POST", body: { volume: v } }), "Partial close sent");
    });
    $("#close-all").addEventListener("click", () => {
      if (!confirm(`Close the entire trade #${t.ticket} (${fmt.lot(t.current_volume)} lot) at market?`)) return;
      act(() => api(`/trades/${t.ticket}/close`, { method: "POST", body: {} }), "Close sent");
    });
  }

  // ---------------------------------------------------------------- history
  async function renderHistory() {
    const rows = await api("/history?limit=200");
    const d = S.status?.info?.digits ?? 2;
    const total = rows.reduce((a, t) => a + (t.result_usd || 0), 0);
    const wins = rows.filter((t) => (t.result_usd || 0) > 0).length;
    view.innerHTML = `<h1>History</h1>
      <div class="stats">
        <div class="stat"><div class="label">Trades</div><div class="value">${rows.length}</div></div>
        <div class="stat"><div class="label">Win rate</div><div class="value">${rows.length ? Math.round(wins / rows.length * 100) : 0}%</div></div>
        <div class="stat"><div class="label">Total</div><div class="value ${fmt.cls(total)}">${fmt.usd(total)}</div></div>
      </div>
      ${rows.length ? rows.map((t) => `<div class="card clickable" data-go="#/detail/${t.ticket}">
        <div class="row between">
          <div class="row"><span class="side ${t.side}">${t.side}</span><b>${fmt.lot(t.volume)}</b><span class="muted small">${fmt.date(t.closed_at)}</span></div>
          <div class="right"><b class="${fmt.cls(t.result_usd)}">${fmt.usd(t.result_usd)}</b><div class="small muted">${fmt.pips(t.result_pips)} pips · ${esc(t.close_reason)}</div></div>
        </div>
        <div class="small muted" style="margin-top:4px">Entry ${fmt.price(t.entry, d)} · steps ${t.steps.map((s, i) => s.status === "hit" ? `P${i + 1}` : "").filter(Boolean).join(" ") || "none"}</div>
      </div>`).join("") : `<div class="empty">No closed trades yet.</div>`}`;
    view.querySelectorAll("[data-go]").forEach((el) => el.addEventListener("click", () => (location.hash = el.dataset.go)));
  }

  // ---------------------------------------------------------------- presets
  async function renderPresets() {
    S.presets = await api("/presets");
    const editing = S.param ? S.presets.find((p) => p.id === Number(S.param)) : null;
    if (S.param === "new" || editing) {
      const p = editing || { name: "", settings: defaultForm().settings };
      view.innerHTML = `<a class="back" href="#/presets">← Presets</a><h1>${editing ? "Edit preset" : "New preset"}</h1>
        <label>Name<input id="p-name" value="${esc(p.name)}"></label>
        <div id="fields">${settingsFields(p.settings, "p")}</div>
        <div class="btns"><button class="primary" id="p-save">Save</button>${editing ? `<button class="danger" id="p-del">Delete</button>` : ""}</div>`;
      wireSettings(view, "p");
      $("#p-save").addEventListener("click", async () => {
        const body = { name: $("#p-name").value.trim(), settings: readSettings(view, "p") };
        if (!body.name) return fail(new Error("Give the preset a name"));
        try {
          await api(editing ? `/presets/${editing.id}` : "/presets", { method: editing ? "PUT" : "POST", body });
          toast("Preset saved"); location.hash = "#/presets";
        } catch (e) { fail(e); }
      });
      $("#p-del")?.addEventListener("click", async () => {
        if (!confirm(`Delete preset "${editing.name}"?`)) return;
        try { await api(`/presets/${editing.id}`, { method: "DELETE" }); location.hash = "#/presets"; } catch (e) { fail(e); }
      });
      return;
    }
    view.innerHTML = `<div class="row between"><h1>Presets</h1><a href="#/presets/new"><button class="sm primary">+ New</button></a></div>
      ${S.presets.map((p) => `<div class="card clickable" data-go="#/presets/${p.id}">
        <div class="row between"><h3>${esc(p.name)}</h3><span class="muted small">${fmt.lot(p.settings.lot)} lot · ${p.settings.stop_mode}${p.settings.sl_pips ? " " + p.settings.sl_pips : ""}</span></div>
        <div class="small muted" style="margin-top:4px">${p.settings.steps.map((s, i) => `P${i + 1} +${s.pips}${s.close_pct ? ` ×${s.close_pct}%` : ""}${s.move_sl_pips == null ? "" : s.move_sl_pips === 0 ? " BE" : ` →+${s.move_sl_pips}`}`).join(" · ")}${p.settings.tp_pips ? ` · TP +${p.settings.tp_pips}` : ""}${p.settings.trailing ? ` · trail ${p.settings.trail_pips}` : ""}</div>
      </div>`).join("")}`;
    view.querySelectorAll("[data-go]").forEach((el) => el.addEventListener("click", () => (location.hash = el.dataset.go)));
  }

  // ---------------------------------------------------------------- signals
  const STATUS_CLS = { pending: "warn", placed: "pos", applied: "pos", failed: "neg", ignored: "muted", dismissed: "muted", new: "muted", watch: "warn", result: "muted" };

  function planSummary(sig, digits) {
    const p = sig.parsed || {}, plan = sig.plan || {};
    if (plan.action) return `${plan.action.replace("_", " ")} on #${plan.ticket}${plan.close_pct ? ` (${plan.close_pct}%)` : ""}${plan.sl_pips != null ? ` → SL ${fmt.pips(plan.sl_pips)} pips` : ""}${plan.tp_pips != null ? ` → TP +${plan.tp_pips} pips` : ""}`;
    if (!plan.settings) return p.reason || "";
    const s = plan.settings;
    return `${plan.side.toUpperCase()} ${fmt.lot(plan.lot)} lot @ market ${fmt.price(plan.ref_price, digits)} · SL ${plan.sl_price} (${s.sl_pips} pips) · P ${s.steps.map((x) => "+" + x.pips).join(" / ")}${s.tp_pips ? ` · TP +${s.tp_pips}` : ""}`;
  }

  function signalCard(sig, digits) {
    const p = sig.parsed || {};
    const pending = sig.status === "pending";
    return `<div class="card">
      <div class="row between">
        <div><b>${esc(sig.channel_title)}</b> <span class="muted small">${fmt.date(sig.ts)}</span></div>
        <span class="small ${STATUS_CLS[sig.status] || ""}">${sig.status}${sig.ticket ? ` · #${sig.ticket}` : ""}</span>
      </div>
      <div class="small" style="margin:6px 0;white-space:pre-wrap;color:var(--muted)">${esc(sig.text.length > 300 ? sig.text.slice(0, 300) + "…" : sig.text)}</div>
      ${p.action === "result" ? `<div class="small muted"><b>Channel result:</b> ${p.result === "tp" ? "TP" + (p.result_tp || "") + " hit" : p.result === "sl" ? "stop hit" : p.result === "be" ? "breakeven" : "result"}${p.result_pips != null ? " " + fmt.pips(p.result_pips) + " pips" : ""}</div>` : ""}
      ${p.action === "watch" ? `<div class="small"><b>Setup to watch</b> ${p.side ? p.side.toUpperCase() : ""} ${p.entry ? "entry " + p.entry + (p.entry_high ? "-" + p.entry_high : "") : ""} ${p.sl ? "SL " + p.sl : ""} ${(p.tps || []).length ? "TP " + p.tps.join(", ") : ""}</div>` : ""}
      ${p.action && p.action !== "ignore" && p.action !== "watch" && p.action !== "result" ? `<div class="small"><b>${p.action.replace("_", " ")}</b>${p.confidence ? ` <span class="muted">(${Math.round(p.confidence * 100)}% sure)</span>` : ""} — ${esc(planSummary(sig, digits))}</div>` : ""}
      ${(sig.plan?.warnings || []).map((w) => `<div class="warn small">${esc(w)}</div>`).join("")}
      ${sig.error ? `<div class="${sig.status === "ignored" ? "muted" : "error"} small">${esc(sig.error)}</div>` : ""}
      ${pending ? `<div class="btns">
        ${sig.plan?.settings ? `<input type="number" step="0.01" min="0.01" data-lot="${sig.id}" value="${sig.plan.lot}" style="flex:0 0 90px;margin:0" title="lot">` : ""}
        <button class="primary" data-place="${sig.id}">${sig.plan?.action ? "Apply" : "Place trade"}</button>
        <button class="ghost" data-dismiss="${sig.id}">Dismiss</button></div>` : ""}
      ${sig.ticket && sig.status !== "pending" ? `<div class="small" style="margin-top:6px"><a href="#/detail/${sig.ticket}">Open trade #${sig.ticket} →</a></div>` : ""}
    </div>`;
  }

  async function renderSignals() {
    const st = await api("/signals/status");
    if (!st.enabled) {
      view.innerHTML = `<h1>Signals</h1><div class="card"><p>Telegram signals are not enabled on the server.</p>
        <p class="small muted">Add <code>ANTHROPIC_API_KEY</code>, <code>TG_API_ID</code> and <code>TG_API_HASH</code> to <code>.env</code>, run <code>py -m scripts.telegram_login</code> once on the VPS, then restart the server.</p></div>`;
      return;
    }
    const [sigs, chans] = await Promise.all([api("/signals?limit=100"), api("/channels")]);
    const digits = S.status?.info?.digits ?? 2;
    const tg = st.telegram;
    const pending = sigs.filter((s) => s.status === "pending").reverse();
    const watching = sigs.filter((s) => s.status === "watch" && s.ts > Date.now() / 1000 - 86400).reverse();
    const rest = sigs.filter((s) => s.status !== "pending" && s.status !== "watch").reverse();
    view.innerHTML = `
      <div class="row between"><h1>Signals</h1><a href="#/channels"><button class="sm">Channels (${chans.length})</button></a></div>
      <div class="small ${tg ? (tg.connected ? "pos" : "neg") : "warn"}" style="margin-bottom:10px">
        ${tg ? (tg.connected ? `Telegram connected as ${esc(tg.user || "")} · listening to ${chans.filter((c) => c.mode !== "off").length} channel(s)` : `Telegram not connected${tg.error ? ": " + esc(tg.error) : ""}`) : "Telegram not configured (TG_API_ID / TG_API_HASH)"}
      </div>
      <h2>Waiting for you (${pending.length})</h2>
      ${pending.length ? pending.map((s) => signalCard(s, digits)).join("") : `<div class="card empty small">Nothing to confirm.</div>`}
      ${watching.length ? `<h2>Setups to watch (last 24h)</h2>${watching.map((s) => signalCard(s, digits)).join("")}` : ""}
      <h2>Try a message</h2>
      <div class="card">
        <textarea id="sig-test" rows="3" placeholder="Paste a signal message to see how it would be read and traded (nothing is placed)" style="width:100%;margin-top:0;padding:10px;font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:10px"></textarea>
        <div class="btns"><button id="sig-test-btn">Parse</button></div>
        <div id="sig-test-out"></div>
      </div>
      <h2>Recent (${rest.length})</h2>
      ${rest.length ? rest.map((s) => signalCard(s, digits)).join("") : `<div class="card empty small">No signals yet.</div>`}`;
    const act = (fn) => async () => { try { await fn(); toast("Done"); renderSignals(); refreshStatus(); } catch (e) { fail(e); } };
    view.querySelectorAll("[data-place]").forEach((b) => b.addEventListener("click", act(async () => {
      const lotEl = $(`[data-lot="${b.dataset.place}"]`);
      const body = lotEl ? { lot: Number(lotEl.value) } : {};
      const r = await api(`/signals/${b.dataset.place}/place`, { method: "POST", body });
      if (r.status === "failed") throw new Error(r.error || "Failed");
    })));
    view.querySelectorAll("[data-dismiss]").forEach((b) => b.addEventListener("click", act(() => api(`/signals/${b.dataset.dismiss}/dismiss`, { method: "POST" }))));
    $("#sig-test-btn").addEventListener("click", async () => {
      const text = $("#sig-test").value.trim();
      if (!text) return;
      const out = $("#sig-test-out");
      out.innerHTML = `<div class="muted small" style="margin-top:8px">Asking the AI…</div>`;
      try {
        const r = await api("/signals/test", { method: "POST", body: { text } });
        const p = r.parsed, plan = r.plan;
        out.innerHTML = `<div class="kv small" style="margin-top:10px">
          <div>Read as</div><div><b>${esc(p.action)}</b> ${p.side ? p.side.toUpperCase() : ""} ${p.symbol || ""} ${p.confidence ? `(${Math.round(p.confidence * 100)}%)` : ""}</div>
          <div>Entry / SL / TPs</div><div>${p.entry ?? "market"}${p.entry_high ? "-" + p.entry_high : ""} / ${p.sl ?? "—"} / ${(p.tps || []).join(", ") || "—"}</div>
          <div>Why</div><div>${esc(p.reason || "")}</div>
          ${plan ? `<div>Would trade</div><div>${esc(planSummary({ parsed: p, plan }, digits))}</div>` : ""}
          ${(plan?.errors || []).map((e) => `<div>Blocked</div><div class="neg">${esc(e)}</div>`).join("")}
          ${(plan?.warnings || []).map((w) => `<div>Note</div><div class="warn">${esc(w)}</div>`).join("")}
        </div>`;
      } catch (e) { out.innerHTML = ""; fail(e); }
    });
  }

  // --------------------------------------------------------------- channels
  function reviewHtml(r) {
    if (!r || !r.signals) return `<div class="small muted">No signals seen yet.</div>`;
    if (!r.placed && r.claims) return `<div class="small muted" style="margin-top:6px">No trades from this channel yet. Channel's own claims: ${r.claimed_tp} TP hit / ${r.claimed_sl} SL hit${r.claimed_rate != null ? ` (${r.claimed_rate}% hit rate)` : ""}${r.claimed_pips ? ` · ${fmt.pips(r.claimed_pips)} pips claimed` : ""} · ${r.signals} messages seen</div>`;
    return `<div class="stats" style="margin-top:8px;grid-template-columns:repeat(4,1fr)">
        <div class="stat"><div class="label">Signals</div><div class="value">${r.signals}</div></div>
        <div class="stat"><div class="label">Placed</div><div class="value">${r.placed}</div></div>
        <div class="stat"><div class="label">Win rate</div><div class="value">${r.win_rate == null ? "—" : r.win_rate + "%"}</div></div>
        <div class="stat"><div class="label">Result</div><div class="value ${fmt.cls(r.result_usd)}">${r.closed ? fmt.usd(r.result_usd) : "—"}</div></div>
      </div>
      <div class="small muted" style="margin-top:6px">Our trades: ${r.closed} closed (${r.wins} won / ${r.losses} lost) · ${r.open_now} open${r.result_pips != null && r.closed ? " · " + fmt.pips(r.result_pips) + " pips" : ""}</div>
      <div class="small muted" style="margin-top:2px">Channel's own claims: ${r.claims ? `${r.claimed_tp} TP hit / ${r.claimed_sl} SL hit${r.claimed_rate != null ? ` (${r.claimed_rate}% hit rate)` : ""}${r.claimed_pips ? ` · ${fmt.pips(r.claimed_pips)} pips claimed` : ""}` : "none yet"} · ${r.ignored} messages ignored</div>
      ${r.suggestion ? `<div class="warn small" style="margin-top:4px">${esc(r.suggestion)}</div>` : ""}`;
  }

  async function renderChannels() {
    const [chans, cfg, avail] = await Promise.all([api("/channels"), api("/signals/settings"), api("/channels/available").catch(() => ({ connected: false, dialogs: [] }))]);
    if (!S.presets.length) S.presets = await api("/presets").catch(() => []);
    const known = new Set(chans.map((c) => c.id));
    const addable = (avail.dialogs || []).filter((d) => !known.has(d.id));
    const weightSel = (v, id) => `<select data-w="${id}" style="margin:0">${[1, 2, 3, 4, 5].map((w) => `<option value="${w}" ${w === v ? "selected" : ""}>${"★".repeat(w)}${"☆".repeat(5 - w)} ${w}</option>`).join("")}</select>`;
    const modeSel = (v, id) => `<select data-m="${id}" style="margin:0">${["off", "confirm", "auto"].map((m) => `<option value="${m}" ${m === v ? "selected" : ""}>${m}</option>`).join("")}</select>`;
    const presetSel = (v, id) => `<select data-p="${id}" style="margin:0"><option value="">Default preset</option>${S.presets.map((p) => `<option value="${p.id}" ${p.id === v ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>`;
    view.innerHTML = `
      <a class="back" href="#/signals">← Signals</a>
      <h1>Channels & trust</h1>
      <p class="small muted">Trust weight sizes the position: lot = weight × the lot per point below, so weight 1 trades 0.01 and weight 5 trades 0.05. Mode decides what happens: <b>auto</b> places the trade immediately, <b>confirm</b> waits for your tap on the Signals tab, <b>off</b> pauses the channel. The scorecard shows how each channel's calls did under our rules, so you can raise or lower trust over time.</p>
      <h2>Rules</h2>
      <div class="card">
        <div class="grid2">
          <label>Lot per trust point (weight × this)<input type="number" step="0.01" min="0.01" id="c-lot" value="${cfg.lot_per_weight}"></label>
          <label>Entry drift warning (pips)<input type="number" step="5" id="c-drift" value="${cfg.max_entry_drift_pips}"></label>
          <label>SL when the signal gives none (pips)<input type="number" step="10" id="c-dsl" value="${cfg.default_sl_pips}"></label>
          <label>AI model<input id="c-model" value="${esc(cfg.model)}"></label>
        </div>
        <div class="inline"><input type="checkbox" id="c-tps" ${cfg.use_signal_tps ? "checked" : ""}><label for="c-tps" style="margin:0;color:var(--text)">Use the signal's TP1–TP3 as P1–P3 distances (when it gives three)</label></div>
        <div class="inline"><input type="checkbox" id="c-final" ${cfg.final_tp ? "checked" : ""}><label for="c-final" style="margin:0;color:var(--text)">Also set the signal's last TP as a hard take profit</label></div>
        <button class="wide primary" id="c-save">Save rules</button>
      </div>
      <h2>Followed channels (${chans.length})</h2>
      ${chans.length ? chans.map((c) => `<div class="card">
        <div class="row between"><h3>${esc(c.title)}</h3><span class="muted small">${c.id}</span></div>
        <div class="grid3" style="margin-top:8px">
          <label>Trust${weightSel(c.weight, c.id)}</label>
          <label>Mode${modeSel(c.mode, c.id)}</label>
          <label>Rules${presetSel(c.preset_id, c.id)}</label>
        </div>
        <div class="small" style="margin-top:6px" id="topic-${c.id}">${(c.topics || []).length ? `Topics: <b>${c.topics.map((t) => esc(t.title || t.id)).join(", ")}</b> · <a href="#" data-topics="${c.id}">change</a>` : `Whole group · <a href="#" data-topics="${c.id}">pick topics</a>`}</div>
        <div class="btns"><button class="primary sm" data-save="${c.id}" data-title="${esc(c.title)}">Save</button><button class="danger sm" data-del="${c.id}">Remove</button></div>
        ${reviewHtml(c.review)}
      </div>`).join("") : `<div class="card empty small">No channels followed yet.</div>`}
      <h2>Add a channel</h2>
      <div class="card">
        ${avail.connected ? (addable.length ? `<label>From your Telegram<select id="add-sel"><option value="">Choose…</option>${addable.map((d) => `<option value="${d.id}" data-title="${esc(d.title)}" data-forum="${d.forum ? 1 : 0}">${esc(d.title)} (${d.forum ? "group with topics" : d.kind})</option>`).join("")}</select></label>` : `<div class="small muted">All your channels and groups are already followed.</div>`)
          : `<div class="small warn">Telegram is not connected, so the list can't be loaded. You can still add a channel by id (shown by <code>py -m scripts.telegram_login</code>).</div>`}
        <div class="grid2"><label>Channel id<input id="add-id" placeholder="-1001234567890"></label><label>Name<input id="add-title" placeholder="Gold VIP"></label></div>
        <div id="add-topic-wrap" class="hidden"><label>Topics (sub-groups): tick the ones to follow, none = whole group</label><div id="add-topics" class="small"></div></div>
        <div class="grid2"><label>Trust<select id="add-w">${[1, 2, 3, 4, 5].map((w) => `<option value="${w}" ${w === 3 ? "selected" : ""}>${w}</option>`).join("")}</select></label><label>Mode<select id="add-m"><option value="confirm">confirm</option><option value="auto">auto</option><option value="off">off</option></select></label></div>
        <button class="wide primary" id="add-btn">Follow channel</button>
      </div>`;
    const reload = async (msg) => { toast(msg); renderChannels(); };
    $("#c-save").addEventListener("click", async () => {
      try {
        await api("/signals/settings", { method: "PUT", body: { lot_per_weight: Number($("#c-lot").value),
          max_entry_drift_pips: Number($("#c-drift").value), default_sl_pips: Number($("#c-dsl").value), model: $("#c-model").value.trim() || "claude-opus-5",
          use_signal_tps: $("#c-tps").checked, final_tp: $("#c-final").checked } });
        reload("Rules saved");
      } catch (e) { fail(e); }
    });
    const ticked = (root) => [...root.querySelectorAll("input[type=checkbox][data-tid]:checked")].map((i) => ({ id: Number(i.dataset.tid), title: i.dataset.ttitle }));
    const topicBoxes = (topics, selected) => topics.map((t) => `<label class="inline" style="margin-top:6px"><input type="checkbox" data-tid="${t.id}" data-ttitle="${esc(t.title)}" ${selected.has(t.id) ? "checked" : ""}>${esc(t.title)}</label>`).join("");
    const topicOf = (id) => {
      const box = $(`#topic-${id}`);
      if (box && box.querySelector("input[type=checkbox]")) return { topics: ticked(box) };
      const c = chans.find((x) => x.id === Number(id));
      return { topics: c?.topics || [] };
    };
    view.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.save;
      try {
        await api(`/channels/${id}`, { method: "PUT", body: { title: b.dataset.title, weight: Number($(`[data-w="${id}"]`).value), mode: $(`[data-m="${id}"]`).value, preset_id: num($(`[data-p="${id}"]`).value), ...topicOf(id) } });
        reload("Channel saved");
      } catch (e) { fail(e); }
    }));
    view.querySelectorAll("[data-topics]").forEach((a) => a.addEventListener("click", async (e) => {
      e.preventDefault();
      const id = a.dataset.topics;
      const box = $(`#topic-${id}`);
      box.innerHTML = `<span class="muted">Loading topics…</span>`;
      try {
        const topics = await api(`/channels/${id}/topics`);
        if (!topics.length) { box.innerHTML = `<span class="muted">This group has no topics.</span>`; return; }
        const cur = new Set((chans.find((x) => x.id === Number(id))?.topics || []).map((t) => t.id));
        box.innerHTML = `<div class="muted">Tick the topics to follow (none = whole group), then Save:</div>${topicBoxes(topics, cur)}`;
      } catch (err) { box.innerHTML = ""; fail(err); }
    }));
    view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Stop following this channel?")) return;
      try { await api(`/channels/${b.dataset.del}`, { method: "DELETE" }); reload("Removed"); } catch (e) { fail(e); }
    }));
    $("#add-sel")?.addEventListener("change", async (e) => {
      const o = e.target.selectedOptions[0];
      $("#add-id").value = o.value; $("#add-title").value = o.dataset.title || "";
      const wrap = $("#add-topic-wrap"), list = $("#add-topics");
      list.innerHTML = "";
      wrap.classList.add("hidden");
      if (o.dataset.forum === "1") {
        try {
          const topics = await api(`/channels/${o.value}/topics`);
          if (topics.length) { list.innerHTML = topicBoxes(topics, new Set()); wrap.classList.remove("hidden"); }
        } catch (err) { fail(err); }
      }
    });
    $("#add-btn").addEventListener("click", async () => {
      const id = $("#add-id").value.trim(), title = $("#add-title").value.trim();
      if (!id || !title) return fail(new Error("Channel id and name are needed"));
      try {
        await api(`/channels/${id}`, { method: "PUT", body: { title, weight: Number($("#add-w").value), mode: $("#add-m").value, topics: ticked($("#add-topics")) } });
        reload("Following " + title);
      } catch (e) { fail(e); }
    });
  }

  // --------------------------------------------------------------- settings
  async function renderSettings() {
    const L = await api("/limits");
    const st = S.status;
    view.innerHTML = `<h1>Settings</h1>
      <h2>Risk limits</h2>
      <div class="card">
        <div class="grid2">
          <label>Max lot per trade<input type="number" step="0.01" id="l-lot" value="${L.max_lot}"></label>
          <label>Daily loss limit ($)<input type="number" step="5" id="l-loss" value="${L.daily_loss_usd}"></label>
          <label>Max spread (pips)<input type="number" step="1" id="l-spread" value="${L.max_spread_pips}"></label>
        </div>
        <button class="wide primary" id="l-save">Save limits</button>
      </div>
      <h2>Connection</h2>
      <div class="card kv small">
        <div>Account</div><div>${st ? `${st.account.login || "—"} @ ${esc(st.account.server)}` : "—"}</div>
        <div>Symbol</div><div>${st ? st.symbol : "—"}</div>
        <div>Min lot / step</div><div>${st ? `${st.info.vol_min} / ${st.info.vol_step}` : "—"}</div>
        <div>Engine</div><div>${st ? (st.engine.running ? "running" : "stopped") + (st.engine.sim ? " (simulator)" : "") : "—"}</div>
        <div>Last tick</div><div>${st && st.engine.last_tick ? fmt.time(st.engine.last_tick) : "—"}</div>
      </div>
      <h2>More</h2>
      <div class="card">
        <a href="#/presets"><button class="wide">Presets (step rules)</button></a>
        <a href="#/channels"><button class="wide">Telegram channels & trust</button></a>
      </div>
      <h2>Phone</h2>
      <div class="card">
        <button class="wide" id="notif">${"Notification" in window && Notification.permission === "granted" ? "Notifications enabled" : "Enable notifications"}</button>
        <p class="small muted">Alerts show while the app is open or in the background. Install the app from your browser menu ("Add to Home screen") for a full-screen experience.</p>
        <button class="wide danger" id="logout">Disconnect (forget token)</button>
      </div>`;
    $("#l-save").addEventListener("click", async () => {
      try {
        await api("/limits", { method: "PUT", body: { max_lot: Number($("#l-lot").value),
          daily_loss_usd: Number($("#l-loss").value), max_spread_pips: Number($("#l-spread").value) } });
        toast("Limits saved"); refreshStatus();
      } catch (e) { fail(e); }
    });
    $("#notif").addEventListener("click", async () => {
      if (!("Notification" in window)) return fail(new Error("Notifications not supported here"));
      const p = await Notification.requestPermission();
      toast(p === "granted" ? "Notifications enabled" : "Notifications blocked");
      renderSettings();
    });
    $("#logout").addEventListener("click", () => { localStorage.removeItem("gt_token"); S.token = ""; location.reload(); });
  }

  // ------------------------------------------------------------------- boot
  async function boot(attempt = 0) {
    // With no token saved, try anyway: a server with GT_TOKEN empty needs no login.
    try { await api("/me"); } catch (e) {
      if (e.message === "Not authorised") return;            // login box is showing
      view.innerHTML = `<div class="empty">Connecting to the bot…<br><span class="small">${esc(e.message)} Retrying.</span></div>`;
      setConn(false);
      setTimeout(() => boot(attempt + 1), Math.min(15000, 2000 * (attempt + 1)));
      return;
    }
    $("#login").classList.add("hidden");
    S.events = await api("/events?limit=50").catch(() => []);
    S.presets = await api("/presets").catch(() => []);
    await refreshStatus();
    connectEvents();
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, 1500);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { refreshStatus(); if (S.sse?.readyState === 2) connectEvents(); } });
    navigate();
  }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  boot();
})();
