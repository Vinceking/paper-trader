// Paper Trading — stopgap frontend logic (see comment at top of index.html).
// Plain vanilla JS, no build step, no dependencies. Talks to the backend
// using same-origin relative fetch() calls per the fixed API contract.

(() => {
  "use strict";

  const TOKEN_KEY = "pt_token";
  const HOLD_MS = 3000;

  // ---------------------------------------------------------------------
  // App state
  // ---------------------------------------------------------------------
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || null,
    account: null, // { id, name, cash, equity, starting_cash, benchmark_symbol }
    positions: [], // last fetched open positions
    authMode: "login", // "login" | "register"
    entryQuote: null, // { symbol, bid, ask, atr_14, typical_bar_volume }
    exitSymbol: null,
    exitQuote: null,
  };

  // ---------------------------------------------------------------------
  // Small DOM helpers
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function money(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    const sign = n < 0 ? "-" : "";
    return sign + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    } catch {
      return iso;
    }
  }

  function randomToken() {
    if (window.crypto && crypto.randomUUID) {
      return "held-3s-" + crypto.randomUUID();
    }
    return "held-3s-" + Math.random().toString(36).slice(2) + Date.now();
  }

  // ---------------------------------------------------------------------
  // API helper
  // ---------------------------------------------------------------------
  async function api(path, { method = "GET", body, auth = true } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (auth && state.token) {
      headers["Authorization"] = "Bearer " + state.token;
    }
    let res;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (networkErr) {
      const err = new Error("Network error — could not reach the server.");
      err.status = 0;
      throw err;
    }

    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch { data = null; }
    }

    if (!res.ok) {
      const err = new Error(extractDetail(data) || `Request failed (${res.status})`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function extractDetail(data) {
    if (!data) return null;
    if (typeof data.detail === "string") return data.detail;
    if (data.detail && typeof data.detail === "object") {
      if (data.detail.veto_reason) return data.detail.veto_reason;
      return JSON.stringify(data.detail);
    }
    return null;
  }

  // ---------------------------------------------------------------------
  // View switching
  // ---------------------------------------------------------------------
  function showView(name) {
    ["view-auth", "view-dashboard", "view-order"].forEach((id) => hide($(id)));
    show($(name));
  }

  // ---------------------------------------------------------------------
  // Auth view
  // ---------------------------------------------------------------------
  const nameField = $("input-name").closest(".field");

  function setAuthMode(mode) {
    state.authMode = mode;
    $("auth-error").classList.add("hidden");
    if (mode === "login") {
      $("auth-title").textContent = "Log in";
      $("btn-auth-submit").textContent = "Log in";
      $("btn-auth-toggle").textContent = "Need an account? Create one";
      hide(nameField);
    } else {
      $("auth-title").textContent = "Create account";
      $("btn-auth-submit").textContent = "Create account";
      $("btn-auth-toggle").textContent = "Already have an account? Log in";
      show(nameField);
    }
  }

  $("btn-auth-toggle").addEventListener("click", () => {
    setAuthMode(state.authMode === "login" ? "register" : "login");
  });

  $("form-auth").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("auth-error");
    errBox.classList.add("hidden");

    const email = $("input-email").value.trim();
    const password = $("input-password").value;
    const displayName = $("input-name").value.trim();

    if (!email || !password) {
      errBox.textContent = "Email and password are required.";
      show(errBox);
      return;
    }
    if (state.authMode === "register" && !displayName) {
      errBox.textContent = "Display name is required.";
      show(errBox);
      return;
    }

    const submitBtn = $("btn-auth-submit");
    submitBtn.disabled = true;
    try {
      let result;
      if (state.authMode === "login") {
        result = await api("/auth/login", { method: "POST", body: { email, password }, auth: false });
      } else {
        result = await api("/auth/register", {
          method: "POST",
          body: { email, password, display_name: displayName },
          auth: false,
        });
      }
      state.token = result.access_token;
      localStorage.setItem(TOKEN_KEY, state.token);
      $("form-auth").reset();
      await enterApp();
    } catch (err) {
      errBox.textContent = err.message || "Something went wrong. Please try again.";
      show(errBox);
    } finally {
      submitBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // Logout
  // ---------------------------------------------------------------------
  $("btn-logout").addEventListener("click", () => {
    state.token = null;
    state.account = null;
    state.positions = [];
    localStorage.removeItem(TOKEN_KEY);
    hide($("btn-logout"));
    setAuthMode("login");
    showView("view-auth");
  });

  // ---------------------------------------------------------------------
  // Dashboard
  // ---------------------------------------------------------------------
  async function enterApp() {
    show($("btn-logout"));
    showView("view-dashboard");
    await loadDashboard();
  }

  async function loadDashboard() {
    try {
      const [account, positions, trades] = await Promise.all([
        api("/account"),
        api("/account/positions"),
        api("/account/trades?limit=20"),
      ]);
      state.account = account;
      state.positions = positions;
      renderAccount(account);
      renderPositions(positions);
      renderTrades(trades);
    } catch (err) {
      if (err.status === 401) {
        // token invalid/expired — send back to login
        state.token = null;
        localStorage.removeItem(TOKEN_KEY);
        hide($("btn-logout"));
        setAuthMode("login");
        showView("view-auth");
        const errBox = $("auth-error");
        errBox.textContent = "Your session expired. Please log in again.";
        show(errBox);
        return;
      }
      alert("Could not load your account: " + err.message);
    }
  }

  function renderAccount(account) {
    $("acct-equity").textContent = money(account.equity);
    $("acct-cash").textContent = money(account.cash);
    $("acct-starting").textContent = money(account.starting_cash);
  }

  function renderPositions(positions) {
    const list = $("positions-list");
    list.innerHTML = "";
    if (!positions || positions.length === 0) {
      list.innerHTML = '<div class="list-empty">No open positions yet.</div>';
      return;
    }
    positions.forEach((p) => {
      const div = document.createElement("div");
      div.className = "list-item";
      div.innerHTML = `
        <div class="list-item-top"><span>${escapeHtml(p.symbol)}</span><span>${p.qty} sh</span></div>
        <div class="list-item-row"><span>Avg entry</span><span>${money(p.avg_entry_price)}</span></div>
        <div class="list-item-row"><span>Stop</span><span>${p.stop_price != null ? money(p.stop_price) : "—"}</span></div>
        <div class="list-item-row"><span>Target</span><span>${p.target_price != null ? money(p.target_price) : "—"}</span></div>
        <div class="list-item-row"><span>Opened</span><span>${fmtDate(p.opened_at)}</span></div>
      `;
      list.appendChild(div);
    });
  }

  function renderTrades(trades) {
    const list = $("trades-list");
    list.innerHTML = "";
    if (!trades || trades.length === 0) {
      list.innerHTML = '<div class="list-empty">No closed trades yet.</div>';
      return;
    }
    trades.forEach((t) => {
      const isWin = t.net_pnl > 0;
      const pnlClass = isWin ? "pnl-positive" : (t.net_pnl < 0 ? "pnl-negative" : "");
      const sign = t.net_pnl > 0 ? "+" : (t.net_pnl < 0 ? "-" : "");
      const winLabel = t.net_pnl > 0 ? "Win" : (t.net_pnl < 0 ? "Loss" : "Flat");
      const div = document.createElement("div");
      div.className = "list-item";
      div.innerHTML = `
        <div class="list-item-top">
          <span>${escapeHtml(t.symbol)} · ${escapeHtml(t.side)}</span>
          <span class="${pnlClass}">${sign}${money(Math.abs(t.net_pnl))} (${winLabel})</span>
        </div>
        <div class="list-item-row"><span>R-multiple</span><span>${t.r_multiple != null ? t.r_multiple.toFixed(2) + "R" : "—"}</span></div>
        <div class="list-item-row"><span>Exit reason</span><span>${escapeHtml(t.exit_reason || "—")}</span></div>
        <div class="list-item-row"><span>Closed</span><span>${fmtDate(t.closed_at)}</span></div>
      `;
      list.appendChild(div);
    });
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---------------------------------------------------------------------
  // Order view — tabs
  // ---------------------------------------------------------------------
  $("btn-open-order").addEventListener("click", () => {
    resetOrderView();
    showView("view-order");
  });
  $("btn-order-back").addEventListener("click", async () => {
    showView("view-dashboard");
    await loadDashboard();
  });

  $("tab-entry").addEventListener("click", () => switchOrderTab("entry"));
  $("tab-exit").addEventListener("click", () => switchOrderTab("exit"));

  function switchOrderTab(which) {
    const entryActive = which === "entry";
    $("tab-entry").classList.toggle("tab-active", entryActive);
    $("tab-exit").classList.toggle("tab-active", !entryActive);
    $("panel-entry").classList.toggle("hidden", !entryActive);
    $("panel-exit").classList.toggle("hidden", entryActive);
    if (!entryActive) {
      renderExitPositionPicker();
    }
  }

  function resetOrderView() {
    // entry
    $("entry-symbol").value = "";
    hide($("entry-quote-box"));
    hide($("entry-quote-error"));
    hide($("entry-fields"));
    hide($("entry-result"));
    $("entry-stop").value = "";
    $("entry-target").value = "";
    state.entryQuote = null;

    // exit
    state.exitSymbol = null;
    state.exitQuote = null;
    hide($("exit-fields"));
    hide($("exit-quote-box"));
    hide($("exit-quote-error"));
    hide($("exit-hold-wrap"));
    hide($("exit-result"));

    switchOrderTab("entry");
  }

  // ---------------------------------------------------------------------
  // Entry flow
  // ---------------------------------------------------------------------
  $("entry-symbol").addEventListener("input", (e) => {
    const upper = e.target.value.toUpperCase();
    if (e.target.value !== upper) e.target.value = upper;
    // symbol changed — any previously fetched quote is stale
    state.entryQuote = null;
    hide($("entry-fields"));
    hide($("entry-quote-box"));
    hide($("entry-quote-error"));
    hide($("entry-result"));
  });

  $("btn-entry-quote").addEventListener("click", async () => {
    const symbol = $("entry-symbol").value.trim().toUpperCase();
    const errBox = $("entry-quote-error");
    hide(errBox);
    hide($("entry-fields"));
    hide($("entry-result"));
    if (!symbol) {
      errBox.textContent = "Enter a symbol first.";
      show(errBox);
      return;
    }
    const btn = $("btn-entry-quote");
    btn.disabled = true;
    btn.textContent = "Fetching…";
    try {
      const q = await api(`/market/quote/${encodeURIComponent(symbol)}`, { auth: false });
      renderQuoteBox($("entry-quote-box"), q);
      show($("entry-quote-box"));

      if (q.atr_14 == null || q.typical_bar_volume == null) {
        errBox.textContent = `Not enough market data available for ${symbol} right now — try a different symbol.`;
        show(errBox);
        state.entryQuote = null;
        hide($("entry-fields"));
      } else {
        state.entryQuote = { symbol, bid: q.bid, ask: q.ask, atr_14: q.atr_14, typical_bar_volume: q.typical_bar_volume };
        show($("entry-fields"));
      }
    } catch (err) {
      errBox.textContent = err.message || "Could not fetch a quote for that symbol.";
      show(errBox);
      state.entryQuote = null;
    } finally {
      btn.disabled = false;
      btn.textContent = "Get live price";
    }
  });

  function renderQuoteBox(box, q) {
    box.innerHTML = `
      <div class="quote-item"><span class="quote-label">Bid</span><span class="quote-value">${money(q.bid)}</span></div>
      <div class="quote-item"><span class="quote-label">Ask</span><span class="quote-value">${money(q.ask)}</span></div>
    `;
  }

  async function submitEntryOrder() {
    const resultBox = $("entry-result");
    hide(resultBox);
    resultBox.classList.remove("veto");

    if (!state.entryQuote) {
      resultBox.classList.add("veto");
      resultBox.textContent = "Fetch a live quote before submitting.";
      show(resultBox);
      return;
    }
    if (!state.account) {
      resultBox.classList.add("veto");
      resultBox.textContent = "Account not loaded. Go back and try again.";
      show(resultBox);
      return;
    }
    const stopRaw = $("entry-stop").value;
    if (!stopRaw || Number.isNaN(parseFloat(stopRaw))) {
      resultBox.classList.add("veto");
      resultBox.textContent = "A stop price is required before you can enter a trade.";
      show(resultBox);
      return;
    }
    const targetRaw = $("entry-target").value;
    const payload = {
      account_id: state.account.id,
      symbol: state.entryQuote.symbol,
      side: "buy",
      intent: "entry",
      quote: {
        bid: state.entryQuote.bid,
        ask: state.entryQuote.ask,
        atr: state.entryQuote.atr_14, // POST /orders' field is "atr", not "atr_14"
        typical_bar_volume: state.entryQuote.typical_bar_volume,
      },
      stop_price: parseFloat(stopRaw),
      target_price: targetRaw ? parseFloat(targetRaw) : null,
      confirm_token: randomToken(),
    };

    try {
      const res = await api("/orders", { method: "POST", body: payload });
      renderOrderResult(resultBox, res, "entry");
      await loadDashboard();
    } catch (err) {
      resultBox.classList.add("veto");
      if (err.status === 409) {
        const reason = (err.data && err.data.detail && err.data.detail.veto_reason) || err.message;
        resultBox.textContent = `Trade blocked by the risk engine: ${reason}`;
      } else {
        resultBox.textContent = err.message || "Order failed.";
      }
      show(resultBox);
    }
  }

  function renderOrderResult(box, res, kind) {
    box.classList.remove("veto");
    let html = `<p><strong>Order ${escapeHtml(res.status || "submitted")}</strong></p>`;
    if (res.fill) {
      const f = res.fill;
      html += `
        <dl>
          <dt>Quantity</dt><dd>${f.qty}</dd>
          <dt>Reference price</dt><dd>${money(f.reference_price)}</dd>
          <dt>Fill price</dt><dd>${money(f.fill_price)}</dd>
          <dt>Slippage</dt><dd>${money(f.slippage_cost)}</dd>
          <dt>Spread cost</dt><dd>${money(f.spread_cost)}</dd>
          <dt>Commission</dt><dd>${money(f.commission)}</dd>
          <dt>Reg fees</dt><dd>${money(f.reg_fees)}</dd>
        </dl>
      `;
    }
    if (kind === "exit" && res.trade) {
      const t = res.trade;
      const winLabel = t.net_pnl > 0 ? "Win" : (t.net_pnl < 0 ? "Loss" : "Flat");
      const sign = t.net_pnl > 0 ? "+" : (t.net_pnl < 0 ? "-" : "");
      html += `
        <dl>
          <dt>Net P&amp;L</dt><dd>${sign}${money(Math.abs(t.net_pnl))} (${winLabel})</dd>
          <dt>Gross P&amp;L</dt><dd>${money(t.gross_pnl)}</dd>
          <dt>Total friction</dt><dd>${money(t.total_friction)}</dd>
          <dt>R-multiple</dt><dd>${t.r_multiple != null ? t.r_multiple.toFixed(2) + "R" : "—"}</dd>
          <dt>Exit reason</dt><dd>${escapeHtml(t.exit_reason || "—")}</dd>
        </dl>
      `;
    }
    box.innerHTML = html;
    show(box);
  }

  // ---------------------------------------------------------------------
  // Exit flow
  // ---------------------------------------------------------------------
  function renderExitPositionPicker() {
    const list = $("exit-position-list");
    list.innerHTML = "";
    hide($("exit-fields"));
    if (!state.positions || state.positions.length === 0) {
      list.innerHTML = '<div class="list-empty">No open positions to close.</div>';
      return;
    }
    state.positions.forEach((p) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "position-pick-btn";
      btn.innerHTML = `<strong>${escapeHtml(p.symbol)}</strong> — ${p.qty} sh @ ${money(p.avg_entry_price)}`;
      btn.addEventListener("click", () => selectExitPosition(p, btn));
      list.appendChild(btn);
    });
  }

  function selectExitPosition(position, btnEl) {
    document.querySelectorAll(".position-pick-btn").forEach((b) => b.classList.remove("selected"));
    btnEl.classList.add("selected");
    state.exitSymbol = position.symbol;
    state.exitQuote = null;
    $("exit-symbol-name").textContent = position.symbol;
    show($("exit-fields"));
    hide($("exit-quote-box"));
    hide($("exit-quote-error"));
    hide($("exit-hold-wrap"));
    hide($("exit-result"));
  }

  $("btn-exit-quote").addEventListener("click", async () => {
    const errBox = $("exit-quote-error");
    hide(errBox);
    hide($("exit-hold-wrap"));
    hide($("exit-result"));
    if (!state.exitSymbol) return;
    const btn = $("btn-exit-quote");
    btn.disabled = true;
    btn.textContent = "Fetching…";
    try {
      const q = await api(`/market/quote/${encodeURIComponent(state.exitSymbol)}`, { auth: false });
      renderQuoteBox($("exit-quote-box"), q);
      show($("exit-quote-box"));

      if (q.atr_14 == null || q.typical_bar_volume == null) {
        errBox.textContent = `Not enough market data available for ${state.exitSymbol} right now — try again shortly.`;
        show(errBox);
        state.exitQuote = null;
        hide($("exit-hold-wrap"));
      } else {
        state.exitQuote = { bid: q.bid, ask: q.ask, atr_14: q.atr_14, typical_bar_volume: q.typical_bar_volume };
        show($("exit-hold-wrap"));
      }
    } catch (err) {
      errBox.textContent = err.message || "Could not fetch a quote for that symbol.";
      show(errBox);
      state.exitQuote = null;
    } finally {
      btn.disabled = false;
      btn.textContent = "Get live price";
    }
  });

  async function submitExitOrder() {
    const resultBox = $("exit-result");
    hide(resultBox);
    resultBox.classList.remove("veto");

    if (!state.exitQuote || !state.exitSymbol || !state.account) {
      resultBox.classList.add("veto");
      resultBox.textContent = "Fetch a live quote before submitting.";
      show(resultBox);
      return;
    }

    const payload = {
      account_id: state.account.id,
      symbol: state.exitSymbol,
      side: "sell",
      intent: "exit",
      quote: {
        bid: state.exitQuote.bid,
        ask: state.exitQuote.ask,
        atr: state.exitQuote.atr_14, // POST /orders' field is "atr", not "atr_14"
        typical_bar_volume: state.exitQuote.typical_bar_volume,
      },
      confirm_token: randomToken(),
    };

    try {
      const res = await api("/orders", { method: "POST", body: payload });
      renderOrderResult(resultBox, res, "exit");
      await loadDashboard();
    } catch (err) {
      resultBox.classList.add("veto");
      if (err.status === 409) {
        const reason = (err.data && err.data.detail && err.data.detail.veto_reason) || err.message;
        resultBox.textContent = `Close blocked by the risk engine: ${reason}`;
      } else {
        resultBox.textContent = err.message || "Order failed.";
      }
      show(resultBox);
    }
  }

  // ---------------------------------------------------------------------
  // Press-and-hold-to-confirm (3 seconds), Pointer Events cover mouse and
  // touch uniformly. Releasing early, or the pointer leaving the button,
  // cancels the hold and resets the fill — only a full, uninterrupted
  // 3-second hold fires onComplete.
  // ---------------------------------------------------------------------
  function attachHoldToConfirm(button, onComplete) {
    const fill = button.querySelector(".hold-fill");
    let active = false;
    let startTime = null;
    let rafId = null;

    function step(ts) {
      if (!active) return;
      if (startTime === null) startTime = ts;
      const elapsed = ts - startTime;
      const pct = Math.min(100, (elapsed / HOLD_MS) * 100);
      fill.style.width = pct + "%";
      if (elapsed >= HOLD_MS) {
        active = false;
        button.classList.remove("holding");
        fill.style.width = "0%";
        onComplete();
        return;
      }
      rafId = requestAnimationFrame(step);
    }

    function start(e) {
      if (button.disabled) return;
      e.preventDefault();
      active = true;
      startTime = null;
      button.classList.add("holding");
      rafId = requestAnimationFrame(step);
    }

    function cancel() {
      if (!active) return;
      active = false;
      startTime = null;
      fill.style.width = "0%";
      button.classList.remove("holding");
      if (rafId) cancelAnimationFrame(rafId);
    }

    button.addEventListener("pointerdown", start);
    button.addEventListener("pointerup", cancel);
    button.addEventListener("pointerleave", cancel);
    button.addEventListener("pointercancel", cancel);
    button.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  attachHoldToConfirm($("hold-entry"), submitEntryOrder);
  attachHoldToConfirm($("hold-exit"), submitExitOrder);

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------
  setAuthMode("login");
  if (state.token) {
    enterApp();
  } else {
    showView("view-auth");
  }
})();
