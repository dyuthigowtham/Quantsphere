(() => {
  'use strict';

  const API = '/api';
  // MT5 auto-sync only works for a single local Windows terminal — not
  // available on this hosted multi-user deployment. Flip this if a future
  // desktop/self-hosted build brings it back.
  const MT5_ENABLED = false;
  const state = {
    userId: localStorage.getItem('qs_user_id'),
    portfolioId: localStorage.getItem('qs_portfolio_id'),
    token: localStorage.getItem('qs_token'),
    trades: [],
    closingTradeId: null,
    detailTradeId: null,
    aiTimer: null,
    market: {},
    marketSocket: null,
    chartSymbol: null,
    chartOpenTrade: null,
    chatHistory: [],
    newsLoaded: false,
    activeNewsCategory: 'international',
    btMentorTimer: null,
    tradingProfile: null,
    setupPerformance: null,
    setups: [],
    coachHistory: [],
    coachLoaded: false,
    scMentorTimer: null,
    strategies: [],
    weeklyReview: null,
    progression: null,
    benchmark: null,
    alerts: [],
    alertSocket: null,
    dtSummary: null,
    dtDecisionIndices: [],
    dtAnsweredIndices: new Set(),
    dtPendingIndex: null,
    dtEnabled: true,
  };

  // ---------- Session ----------
  function clearSession() {
    localStorage.removeItem('qs_token');
    localStorage.removeItem('qs_user_id');
    localStorage.removeItem('qs_portfolio_id');
    state.token = null;
    state.userId = null;
    state.portfolioId = null;
    if (state.alertSocket) {
      const socket = state.alertSocket;
      state.alertSocket = null; // clear first so the 'close' handler's reconnect check sees no token
      socket.close();
    }
  }

  function logout() {
    clearSession();
    goToStep(1);
    showView('onboarding');
  }

  // ---------- API helper ----------
  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (state.token) opts.headers['Authorization'] = `Bearer ${state.token}`;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(API + path, opts);
    if (res.status === 401 && path !== '/auth/login') {
      logout();
      toast('Your session expired — please log in again.', 'error');
      throw new Error('Session expired');
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  async function apiUpload(path, file) {
    const form = new FormData();
    form.append('file', file);
    const opts = { method: 'POST', body: form, headers: {} };
    if (state.token) opts.headers['Authorization'] = `Bearer ${state.token}`;
    const res = await fetch(API + path, opts);
    if (res.status === 401) {
      logout();
      toast('Your session expired — please log in again.', 'error');
      throw new Error('Session expired');
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ---------- Toast ----------
  function toast(message, type = 'success') {
    const stack = document.getElementById('toast-stack');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${type === 'success' ? '✓' : '⚠'}</span><span>${escapeHtml(message)}</span>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 320);
    }, 4200);
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ---------- Number count-up ----------
  function animateNumber(el, to, { decimals = 0, prefix = '', duration = 900 } = {}) {
    const from = parseFloat(el.dataset.count || '0');
    const start = performance.now();
    function tick(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const val = from + (to - from) * eased;
      el.textContent = prefix + val.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
      if (t < 1) requestAnimationFrame(tick);
      else el.dataset.count = String(to);
    }
    requestAnimationFrame(tick);
  }

  // ---------- Boot sequence ----------
  async function boot() {
    const bootStatus = document.getElementById('boot-status');
    let healthy = false;
    for (let i = 0; i < 30 && !healthy; i++) {
      try {
        const res = await fetch('/health');
        if (res.ok) { healthy = true; break; }
      } catch (_) { /* retry */ }
      bootStatus.textContent = i < 5 ? 'waking up the engine…' : 'still connecting to the local server…';
      await new Promise((r) => setTimeout(r, 500));
    }
    bootStatus.textContent = healthy ? 'ready' : 'could not reach the server';

    document.getElementById('boot-screen').classList.add('fade-out');
    document.getElementById('app').classList.remove('hidden');
    setConnIndicator(healthy);

    if (!MT5_ENABLED) {
      document.querySelector('.mt5-section').classList.add('hidden');
      document.getElementById('modal-mt5-connect').remove();
    }

    if (!healthy) {
      toast('Cannot reach the QuantSphere server — is it running?', 'error');
      return;
    }

    if (state.token && state.userId && state.portfolioId) {
      try {
        await loadDashboard();
        goToDefaultLandingView();
      } catch (_) {
        clearSession();
        showView('onboarding');
      }
    } else {
      showView('onboarding');
    }

    pollHealth();
    if (MT5_ENABLED) pollMt5Status();
  }

  document.getElementById('btn-logout').addEventListener('click', logout);

  function setConnIndicator(online) {
    const el = document.getElementById('conn-indicator');
    el.classList.toggle('online', online);
    el.classList.toggle('offline', !online);
    el.querySelector('.conn-label').textContent = online ? 'local engine' : 'offline';
  }

  function pollHealth() {
    setInterval(async () => {
      try {
        const res = await fetch('/health');
        setConnIndicator(res.ok);
      } catch (_) {
        setConnIndicator(false);
      }
    }, 8000);
  }

  function showView(name) {
    document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
    document.getElementById(`view-${name}`).classList.remove('hidden');
  }

  function isMobileViewport() {
    return window.matchMedia('(max-width: 860px)').matches;
  }

  // Called right after loadDashboard() succeeds (boot/login/portfolio
  // creation) — on a narrow viewport, land on the condensed Cockpit instead
  // of the full desktop Home dashboard.
  function goToDefaultLandingView() {
    const view = isMobileViewport() ? 'cockpit' : 'home';
    setActiveNav(view);
    showView(view);
    if (view === 'cockpit') renderCockpit();
  }

  function setActiveNav(view) {
    document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    document.querySelectorAll('.tab-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  }

  // ---------- Navigation ----------
  // Every nav tab maps to a real, already-populated-or-Phase-1 section —
  // none of these are empty placeholders.
  const NAV_HANDLERS = {
    home: openHomeView,
    cockpit: openCockpitView,
    markets: openMarketsView,
    news: openNewsView,
    analyze: openAnalyzeView,
    backtest: openBacktestView,
    textbook: openLearnView,
    journal: openJournalView,
    coach: openCoachView,
  };

  function navigateTo(view) {
    if (view !== 'backtest') {
      stopBtPlayback();
      hideDecisionPrompt();
    }
    setActiveNav(view);
    const handler = NAV_HANDLERS[view];
    if (handler) handler();
  }

  document.getElementById('main-nav').addEventListener('click', (e) => {
    const btn = e.target.closest('.nav-item');
    if (!btn) return;
    navigateTo(btn.dataset.view);
  });

  document.getElementById('bottom-tab-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab-item');
    if (!btn) return;
    navigateTo(btn.dataset.view);
  });

  // ---------- Onboarding ----------
  function goToStep(n) {
    document.querySelectorAll('.onboard-step').forEach((s) => s.classList.remove('active'));
    document.querySelectorAll('.onboard-progress .step').forEach((s) => s.classList.remove('active'));
    document.getElementById(`step-${n}`).classList.add('active');
    document.querySelectorAll('.onboard-progress .step')[n - 1].classList.add('active');
  }

  function applySession(session) {
    state.token = session.access_token;
    state.userId = session.user_id;
    localStorage.setItem('qs_token', session.access_token);
    localStorage.setItem('qs_user_id', session.user_id);
  }

  document.getElementById('ob-auth-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    document.querySelectorAll('#ob-auth-mode .seg-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    const mode = btn.dataset.value;
    document.getElementById('ob-signup-fields').classList.toggle('hidden', mode !== 'signup');
    document.getElementById('ob-login-fields').classList.toggle('hidden', mode !== 'login');
  });

  async function handleCreateUser() {
    const email = document.getElementById('ob-email').value.trim();
    const password = document.getElementById('ob-password').value;
    const errEl = document.getElementById('ob-error-1');
    errEl.textContent = '';
    if (!email || password.length < 8) {
      errEl.textContent = 'Enter a valid email and a password of at least 8 characters.';
      return;
    }
    const btn = document.getElementById('ob-create-user');
    btn.disabled = true;
    try {
      await api('POST', '/users', { email, password });
      const session = await api('POST', '/auth/login', { email, password });
      applySession(session);
      goToStep(2);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function handleLogin() {
    const email = document.getElementById('ob-login-email').value.trim();
    const password = document.getElementById('ob-login-password').value;
    const errEl = document.getElementById('ob-login-error');
    errEl.textContent = '';
    if (!email || !password) {
      errEl.textContent = 'Enter your email and password.';
      return;
    }
    const btn = document.getElementById('ob-login-btn');
    btn.disabled = true;
    try {
      const session = await api('POST', '/auth/login', { email, password });
      applySession(session);
      if (session.portfolio_id) {
        state.portfolioId = session.portfolio_id;
        localStorage.setItem('qs_portfolio_id', session.portfolio_id);
        await loadDashboard();
        goToDefaultLandingView();
        toast('Welcome back!');
      } else {
        goToStep(2);
      }
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('ob-login-btn').addEventListener('click', handleLogin);

  async function handleCreatePortfolio() {
    const name = document.getElementById('ob-portfolio-name').value.trim() || 'Main Journal';
    const balance = parseFloat(document.getElementById('ob-portfolio-balance').value);
    const errEl = document.getElementById('ob-error-2');
    errEl.textContent = '';
    if (!balance || balance <= 0) {
      errEl.textContent = 'Enter a starting balance greater than 0.';
      return;
    }
    const btn = document.getElementById('ob-create-portfolio');
    btn.disabled = true;
    try {
      const portfolio = await api('POST', '/portfolios', { name, starting_balance: balance });
      state.portfolioId = portfolio.id;
      localStorage.setItem('qs_portfolio_id', portfolio.id);
      await loadDashboard();
      goToDefaultLandingView();
      toast('Welcome to QuantSphere!');
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- Dashboard ----------
  async function loadDashboard() {
    const [portfolio, trades, metrics] = await Promise.all([
      api('GET', `/portfolios/${state.portfolioId}`),
      api('GET', `/portfolios/${state.portfolioId}/trades`),
      api('GET', `/portfolios/${state.portfolioId}/metrics`),
    ]);
    state.trades = trades;

    try {
      applyMarketSnapshot(await api('GET', '/market/prices'));
    } catch (_) { /* WebSocket will populate this shortly instead */ }
    connectMarketFeed();
    connectAlertsFeed();
    loadAlerts();
    refreshMt5Status();
    loadSetups();
    if (!state.newsLoaded) {
      state.newsLoaded = true;
      loadNews('international');
      startNewsTapePolling();
      startNewsListPolling();
    }

    document.getElementById('stat-sharpe-value').textContent =
      metrics.sharpe_ratio === null ? '—' : metrics.sharpe_ratio.toFixed(2);
    animateNumber(document.getElementById('stat-drawdown-value'), metrics.max_drawdown_amount, { decimals: 2 });
    document.getElementById('stat-drawdown-pct').textContent =
      metrics.max_drawdown_pct === null ? '—' : `${metrics.max_drawdown_pct.toFixed(1)}%`;

    document.getElementById('stat-portfolio-name').textContent = portfolio.name;
    animateNumber(document.getElementById('stat-balance-value'), portfolio.current_balance, { decimals: 2 });

    const closed = trades.filter((t) => t.status === 'closed');
    const open = trades.filter((t) => t.status === 'open');
    const wins = closed.filter((t) => (t.profit ?? 0) > 0);
    const winRate = closed.length ? (wins.length / closed.length) * 100 : 0;

    animateNumber(document.getElementById('stat-trades-value'), trades.length);
    document.getElementById('stat-open-count').textContent = open.length;
    animateNumber(document.getElementById('stat-winrate-value'), winRate, { decimals: 0 });

    const grades = closed
      .map((t) => latestEvaluation(t, 'rules_engine'))
      .filter(Boolean)
      .map((e) => e.grade);
    document.getElementById('stat-grade-value').textContent = grades.length ? mostCommonGrade(grades) : '—';

    renderTradesList(trades);

    await loadTradingProfile({ force: true });
    renderHomeSummary();
    await loadWeeklyReview();
    renderWeeklyReview();
    if (!document.getElementById('view-analyze').classList.contains('hidden')) renderAnalyzeView();
  }

  // ---------- Weekly AI Trading Review ----------
  async function loadWeeklyReview() {
    try {
      state.weeklyReview = await api('GET', `/portfolios/${state.portfolioId}/weekly-review`);
    } catch (_) {
      state.weeklyReview = null;
    }
    return state.weeklyReview;
  }

  function renderWeeklyReview() {
    const review = state.weeklyReview;
    const emptyTextEl = document.getElementById('wr-empty-text');
    const contentEl = document.getElementById('wr-content');
    const statsEl = document.getElementById('wr-stats');
    const bestWorstEl = document.getElementById('wr-best-worst');
    const narrativeResultEl = document.getElementById('wr-narrative-result');
    narrativeResultEl.classList.add('hidden');
    narrativeResultEl.innerHTML = '';

    if (!review || !review.has_sufficient_data) {
      emptyTextEl.classList.remove('hidden');
      contentEl.classList.add('hidden');
      emptyTextEl.textContent = review ? review.note : "Couldn't load this week's review.";
      return;
    }
    emptyTextEl.classList.add('hidden');
    contentEl.classList.remove('hidden');

    statsEl.innerHTML = `
      <div class="wr-stat"><span class="wr-stat-label">Closed Trades</span><span class="wr-stat-value">${review.closed_trade_count}</span></div>
      <div class="wr-stat"><span class="wr-stat-label">Win Rate</span><span class="wr-stat-value">${review.win_rate_pct !== null ? review.win_rate_pct.toFixed(1) + '%' : '—'}</span></div>
      <div class="wr-stat"><span class="wr-stat-label">Total P&amp;L</span><span class="wr-stat-value ${review.total_profit >= 0 ? 'pos' : 'neg'}">${formatMoney(review.total_profit)}</span></div>
      <div class="wr-stat"><span class="wr-stat-label">Avg Realized R</span><span class="wr-stat-value">${review.avg_realized_r !== null ? review.avg_realized_r.toFixed(2) + 'R' : '—'}</span></div>
    `;

    const chips = [];
    if (review.best_trade) {
      chips.push(`<div class="wr-trade-chip best">🏆 Best: ${escapeHtml(review.best_trade.symbol)} ${formatMoney(review.best_trade.profit)}</div>`);
    }
    if (review.worst_trade) {
      chips.push(`<div class="wr-trade-chip worst">⚠️ Worst: ${escapeHtml(review.worst_trade.symbol)} ${formatMoney(review.worst_trade.profit)}</div>`);
    }
    if (review.best_setup_name) {
      chips.push(`<div class="wr-trade-chip best">📌 Best setup: ${escapeHtml(review.best_setup_name)}</div>`);
    }
    bestWorstEl.innerHTML = chips.join('');
  }

  async function requestWeeklyReviewNarrative() {
    const btn = document.getElementById('wr-narrative-btn');
    const thinkingEl = document.getElementById('wr-narrative-thinking');
    const resultEl = document.getElementById('wr-narrative-result');

    btn.disabled = true;
    resultEl.classList.add('hidden');
    thinkingEl.classList.remove('hidden');

    const startedAt = Date.now();
    const elapsedEl = document.getElementById('wr-narrative-elapsed');
    const timer = setInterval(() => {
      elapsedEl.textContent = ` (${Math.floor((Date.now() - startedAt) / 1000)}s)`;
    }, 1000);

    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/weekly-review/narrative`, undefined);
      clearInterval(timer);
      thinkingEl.classList.add('hidden');
      resultEl.innerHTML = `
        <p>${escapeHtml(res.narrative)}</p>
        ${res.focus_goals.length ? `<ul class="wr-focus-goals">${res.focus_goals.map((g) => `<li>${escapeHtml(g)}</li>`).join('')}</ul>` : ''}
        <p class="sl-disclaimer">${escapeHtml(res.disclaimer)}</p>
      `;
      resultEl.classList.remove('hidden');
    } catch (e) {
      clearInterval(timer);
      thinkingEl.classList.add('hidden');
      toast(`AI summary unavailable: ${e.message}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('wr-narrative-btn').addEventListener('click', requestWeeklyReviewNarrative);

  function latestEvaluation(trade, phase) {
    const matches = (trade.evaluations || []).filter((e) => e.phase === phase);
    if (!matches.length) return null;
    return matches.reduce((a, b) => (a.id > b.id ? a : b));
  }

  function mostCommonGrade(grades) {
    const counts = {};
    grades.forEach((g) => { counts[g] = (counts[g] || 0) + 1; });
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
  }

  function gradeClass(grade) {
    if (!grade) return 'grade-none';
    const letter = grade[0].toUpperCase();
    return ['A', 'B', 'C', 'D', 'F'].includes(letter) ? `grade-${letter}` : 'grade-none';
  }

  function formatMoney(n) {
    const v = Number(n);
    const sign = v > 0 ? '+' : '';
    return `${sign}$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function formatDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function renderTradesList(trades) {
    const listEl = document.getElementById('trades-list');
    const emptyEl = document.getElementById('trades-empty');
    listEl.innerHTML = '';

    if (!trades.length) {
      emptyEl.classList.remove('hidden');
      return;
    }
    emptyEl.classList.add('hidden');

    trades.forEach((trade, i) => {
      const grade = latestEvaluation(trade, 'rules_engine');
      const pnlClass = trade.profit > 0 ? 'pos' : trade.profit < 0 ? 'neg' : 'flat';
      const card = document.createElement('div');
      card.className = 'trade-card';
      card.style.animationDelay = `${Math.min(i, 8) * 40}ms`;
      card.innerHTML = `
        <div class="dir-pill ${trade.direction}">${trade.direction.toUpperCase()}</div>
        <div>
          <div class="tc-symbol">${escapeHtml(trade.symbol)}</div>
          <div class="tc-sub">${formatDateTime(trade.open_time)}</div>
        </div>
        <div class="tc-sub">${trade.volume} lots @ ${trade.open_price}</div>
        <div>
          <div class="tc-pnl ${trade.status === 'closed' ? pnlClass : 'flat'}">${trade.status === 'closed' ? formatMoney(trade.profit) : '—'}</div>
          <div class="tc-status">${trade.status === 'closed' ? 'closed' : 'open'}</div>
        </div>
        <div class="grade-chip ${gradeClass(grade && grade.grade)}">${grade ? grade.grade : '·'}</div>
        <div class="tc-chevron">›</div>
      `;
      card.addEventListener('click', () => openDetail(trade.id));
      listEl.appendChild(card);
    });
  }

  // ---------- Trading Profile (Trading DNA / Mistake Detector / Trading Health) ----------
  // Single shared analytics fetch — Home's AI Insight/Edge/Weakness/Focus
  // cards, the Analyze tab, and the AI Coach all render from this one
  // response, so they never disagree with each other.
  async function loadTradingProfile({ force = false } = {}) {
    if (state.tradingProfile && !force) return state.tradingProfile;
    try {
      state.tradingProfile = await api('GET', `/portfolios/${state.portfolioId}/trading-profile`);
    } catch (_) {
      state.tradingProfile = null;
    }
    return state.tradingProfile;
  }

  function openHomeView() {
    showView('home');
    renderHomeSummary();
  }

  // ---------- Mobile Trading Cockpit ----------
  // Presentation-layer only — every number here comes from data already
  // fetched by loadDashboard()/loadTradingProfile(); no new backend call.
  function openCockpitView() {
    showView('cockpit');
    renderCockpit();
  }

  function renderCockpit() {
    const portfolio = document.getElementById('stat-portfolio-name').textContent;
    document.getElementById('cockpit-portfolio-name').textContent = portfolio;
    const balanceValue = document.getElementById('stat-balance-value').textContent;
    document.getElementById('cockpit-balance-value').textContent = `$${balanceValue}`;

    const profile = state.tradingProfile;
    const insightEl = document.getElementById('cockpit-insight');
    insightEl.innerHTML = `
      <div class="mini-card-label">🧠 AI Insight</div>
      <p class="mini-card-body">${escapeHtml((profile && profile.instant_insight) || 'Log at least 10 closed trades to unlock your first insight.')}</p>
    `;

    const open = state.trades.filter((t) => t.status === 'open');
    const positionsEl = document.getElementById('cockpit-positions');
    if (!open.length) {
      positionsEl.innerHTML = '<p class="dna-empty">No open positions.</p>';
      return;
    }
    positionsEl.innerHTML = open
      .map(
        (t) => `
      <div class="cockpit-position-row card-glass">
        <div class="dir-pill ${t.direction}">${t.direction.toUpperCase()}</div>
        <div class="cockpit-position-info">
          <div class="cockpit-position-symbol">${escapeHtml(t.symbol)}</div>
          <div class="cockpit-position-sub">${t.volume} lots @ ${t.open_price}</div>
        </div>
        <button type="button" class="btn btn-primary btn-sm cockpit-close-btn" data-id="${t.id}">Close</button>
      </div>
    `
      )
      .join('');
    positionsEl.querySelectorAll('.cockpit-close-btn').forEach((btn) => {
      btn.addEventListener('click', () => closeTradeAtMarket(Number(btn.dataset.id)));
    });
  }

  function renderHomeSummary() {
    const profile = state.tradingProfile;
    const healthValueEl = document.getElementById('stat-health-value');
    const healthFootEl = document.getElementById('stat-health-foot');
    const insightBody = document.getElementById('insight-body');
    const insightAskBtn = document.getElementById('insight-ask-coach');
    const edgeBody = document.getElementById('edge-body');
    const weaknessBody = document.getElementById('weakness-body');
    const focusBody = document.getElementById('focus-body');

    if (!profile) {
      healthValueEl.textContent = '—';
      return;
    }

    if (profile.health.overall_score !== null) {
      healthValueEl.textContent = profile.health.overall_score;
      healthFootEl.textContent = 'strategy · risk · discipline · execution · consistency';
    } else {
      healthValueEl.textContent = '—';
      healthFootEl.textContent = 'log more trades to unlock';
    }

    insightBody.textContent = profile.instant_insight || 'Keep logging trades — your first insight is close.';
    insightAskBtn.classList.toggle('hidden', !profile.has_sufficient_data);

    edgeBody.textContent = profile.strongest_edge || 'Not enough data yet to identify a clear edge.';
    weaknessBody.textContent = profile.biggest_weakness || 'No recurring weakness detected yet.';

    const topNote = profile.health.notes && profile.health.notes.length ? profile.health.notes[0] : null;
    focusBody.textContent = profile.biggest_weakness
      ? `Focus on: ${profile.biggest_weakness}`
      : topNote || 'Log more trades so QuantSphere can recommend a focus area.';
  }

  document.getElementById('insight-ask-coach').addEventListener('click', () => navigateTo('coach'));

  const MISTAKE_LABELS = {
    overtrading: 'Overtrading',
    oversizing: 'Oversizing',
    poor_risk_reward: 'Poor Risk/Reward',
    early_exit: 'Early Exits',
    revenge_trading: 'Revenge Trading',
    outside_preferred_hours: 'Outside Preferred Hours',
    stop_loss_modification: 'Stop-Loss Modification',
  };

  function openAnalyzeView() {
    showView('analyze');
    loadTradingProfile().then(renderAnalyzeView);
    loadProgression().then(renderProgression);
    loadBenchmark().then(renderBenchmark);
  }

  function renderAnalyzeView() {
    const profile = state.tradingProfile;
    const emptyEl = document.getElementById('analyze-empty');
    const contentEl = document.getElementById('analyze-content');

    if (!profile || !profile.has_sufficient_data) {
      emptyEl.classList.remove('hidden');
      contentEl.classList.add('hidden');
      if (profile) {
        document.getElementById('analyze-empty-text').textContent =
          `Log at least ${profile.min_trades_required} closed trades to unlock this (${profile.trades_analyzed} so far).`;
      }
      return;
    }
    emptyEl.classList.add('hidden');
    contentEl.classList.remove('hidden');

    // ---- Trading DNA ----
    const highlightsEl = document.getElementById('dna-highlights');
    const highlights = [
      ['Your strongest edge', profile.strongest_edge],
      ['Your biggest weakness', profile.biggest_weakness],
      ['Best trading window', profile.best_trading_window],
      ['Worst environment', profile.worst_trading_environment],
    ].filter(([, value]) => value);
    highlightsEl.innerHTML = highlights
      .map(([label, value]) => `<div class="dna-highlight"><div class="dna-highlight-label">${escapeHtml(label)}</div><p>${escapeHtml(value)}</p></div>`)
      .join('') || '<p class="dna-empty">No standout patterns yet.</p>';

    const breakdownsEl = document.getElementById('dna-breakdowns');
    const symbolRows = profile.symbol_breakdown
      .map((s) => `<tr><td>${escapeHtml(s.symbol)}</td><td>${s.trade_count}</td><td>${s.win_rate_pct.toFixed(0)}%</td><td class="${s.total_profit >= 0 ? 'pos' : 'neg'}">${formatMoney(s.total_profit)}</td></tr>`)
      .join('');
    const setupRows = profile.setup_breakdown
      .map((s) => `<tr><td>${escapeHtml(s.setup_name)}</td><td>${s.trade_count}</td><td>${s.win_rate_pct.toFixed(0)}%</td><td class="${s.total_profit >= 0 ? 'pos' : 'neg'}">${formatMoney(s.total_profit)}</td></tr>`)
      .join('');
    breakdownsEl.innerHTML = `
      <div class="dna-table-block">
        <h4>By Symbol</h4>
        <table class="dna-table"><thead><tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>P&amp;L</th></tr></thead><tbody>${symbolRows || '<tr><td colspan="4">Not enough trades per symbol yet.</td></tr>'}</tbody></table>
      </div>
      <div class="dna-table-block">
        <h4>By Setup</h4>
        <table class="dna-table"><thead><tr><th>Setup</th><th>Trades</th><th>Win %</th><th>P&amp;L</th></tr></thead><tbody>${setupRows || `<tr><td colspan="4">${escapeHtml(profile.setup_tagging_hint || 'Tag your trades with a setup to unlock this.')}</td></tr>`}</tbody></table>
      </div>
    `;
    document.getElementById('dna-caveat').textContent = profile.risk_reward.note || profile.timestamp_caveat;

    // ---- Mistake Detector ----
    const mistakesEl = document.getElementById('mistakes-list');
    mistakesEl.innerHTML = profile.mistakes
      .map((m, i) => {
        const label = MISTAKE_LABELS[m.category] || m.category;
        const badge = m.status === 'not_yet_trackable' ? 'not-tracked' : m.status === 'insufficient_data' ? 'insufficient' : m.severity || 'none';
        const badgeText = m.status === 'not_yet_trackable' ? 'not yet trackable' : m.status === 'insufficient_data' ? 'need more data' : m.occurrences > 0 ? `${m.occurrences}x` : 'clear';
        const canExpand = m.status === 'tracked' && m.example_trade_ids.length > 0;
        return `
          <div class="mistake-card ${canExpand ? 'expandable' : ''}" data-index="${i}">
            <div class="mistake-card-head">
              <span class="mistake-name">${escapeHtml(label)}</span>
              <span class="mistake-badge mistake-badge-${badge}">${escapeHtml(badgeText)}</span>
            </div>
            <p class="mistake-desc">${escapeHtml(m.description)}</p>
            ${canExpand ? `<button type="button" class="btn btn-ghost btn-sm mistake-why-btn" data-category="${escapeHtml(m.category)}">✨ Why?</button>` : ''}
            <div class="mistake-trades hidden" id="mistake-trades-${i}"></div>
            <div class="ai-thinking hidden" id="mistake-why-thinking-${i}">
              <div class="ai-orbit"><div class="orbit-dot"></div><div class="orbit-dot"></div><div class="orbit-dot"></div></div>
              <p class="ai-thinking-text">Thinking…</p>
            </div>
            <div class="why-result hidden" id="mistake-why-result-${i}"></div>
          </div>
        `;
      })
      .join('');
    mistakesEl.querySelectorAll('.mistake-card.expandable').forEach((card) => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.mistake-why-btn')) return;
        const index = Number(card.dataset.index);
        const mistake = profile.mistakes[index];
        const tradesEl = document.getElementById(`mistake-trades-${index}`);
        const isHidden = tradesEl.classList.contains('hidden');
        mistakesEl.querySelectorAll('.mistake-trades').forEach((el) => el.classList.add('hidden'));
        if (!isHidden) return;
        const matched = state.trades.filter((t) => mistake.example_trade_ids.includes(t.id));
        tradesEl.innerHTML = matched
          .map((t) => `<div class="mistake-trade-row" data-id="${t.id}"><span class="dir-pill ${t.direction}">${t.direction.toUpperCase()}</span>${escapeHtml(t.symbol)} · ${formatDateTime(t.open_time)} · ${t.status === 'closed' ? formatMoney(t.profit) : 'open'}</div>`)
          .join('') || '<p class="dna-empty">Trade details unavailable.</p>';
        tradesEl.querySelectorAll('.mistake-trade-row').forEach((row) => {
          row.addEventListener('click', (e2) => {
            e2.stopPropagation();
            openDetail(Number(row.dataset.id));
          });
        });
        tradesEl.classList.remove('hidden');
      });
    });
    mistakesEl.querySelectorAll('.mistake-why-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const card = btn.closest('.mistake-card');
        requestMistakeWhy(btn.dataset.category, Number(card.dataset.index));
      });
    });

    // ---- Trading Health ----
    const health = profile.health;
    document.getElementById('health-overall').innerHTML = health.overall_score !== null
      ? `<div class="health-score-big">${health.overall_score}<span>/100</span></div>`
      : '<p class="dna-empty">Not enough data to compute an overall score yet.</p>';

    const subScores = [
      ['Strategy', health.strategy_score],
      ['Risk', health.risk_score],
      ['Discipline', health.discipline_score],
      ['Execution', health.execution_score],
      ['Consistency', health.consistency_score],
    ];
    document.getElementById('health-bars').innerHTML = subScores
      .map(([label, score]) => `
        <div class="health-bar-row">
          <span class="health-bar-label">${label}</span>
          <div class="health-bar-track"><div class="health-bar-fill" style="width:${score ?? 0}%"></div></div>
          <span class="health-bar-value">${score === null ? '—' : score}</span>
        </div>
      `)
      .join('');
    document.getElementById('health-notes').innerHTML = health.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('');

    // ---- Setup Performance (independent of the has_sufficient_data gate above —
    // it works from filtered slices and shouldn't be hidden just because the
    // whole-portfolio DNA/Health view needs more trades) ----
    populateSetupPerfFilters();
    loadSetupPerformance().catch(() => {
      // A Setup Performance failure must never blank out the DNA/Mistakes/Health
      // panels above, which already rendered successfully.
      document.getElementById('setup-perf-results').innerHTML = '<p class="dna-empty">Could not load setup performance.</p>';
    });
  }

  // ---------- "WHY?" Explainability ----------
  function renderWhyResult(resultEl, res) {
    resultEl.innerHTML = `
      <p>${escapeHtml(res.reasoning)}</p>
      ${res.key_observations.length ? `<ul class="why-observations">${res.key_observations.map((o) => `<li>${escapeHtml(o)}</li>`).join('')}</ul>` : ''}
      <p class="sl-disclaimer">${escapeHtml(res.disclaimer)}</p>
    `;
    resultEl.classList.remove('hidden');
  }

  async function requestTradingHealthWhy() {
    if (!state.tradingProfile) return;
    const btn = document.getElementById('health-why-btn');
    const thinkingEl = document.getElementById('health-why-thinking');
    const resultEl = document.getElementById('health-why-result');
    const elapsedEl = document.getElementById('health-why-elapsed');

    btn.disabled = true;
    resultEl.classList.add('hidden');
    thinkingEl.classList.remove('hidden');
    const startedAt = Date.now();
    const timer = setInterval(() => {
      elapsedEl.textContent = ` (${Math.floor((Date.now() - startedAt) / 1000)}s)`;
    }, 1000);

    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/trading-health/why`, undefined);
      renderWhyResult(resultEl, res);
    } catch (e) {
      resultEl.innerHTML = `<p class="dna-empty">${escapeHtml(e.message)}</p>`;
      resultEl.classList.remove('hidden');
    } finally {
      clearInterval(timer);
      thinkingEl.classList.add('hidden');
      btn.disabled = false;
    }
  }

  document.getElementById('health-why-btn').addEventListener('click', requestTradingHealthWhy);

  async function requestMistakeWhy(category, index) {
    const btn = document.querySelector(`.mistake-why-btn[data-category="${category}"]`);
    const thinkingEl = document.getElementById(`mistake-why-thinking-${index}`);
    const resultEl = document.getElementById(`mistake-why-result-${index}`);
    if (!thinkingEl || !resultEl) return;

    if (btn) btn.disabled = true;
    resultEl.classList.add('hidden');
    thinkingEl.classList.remove('hidden');

    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/mistakes/${category}/why`, undefined);
      renderWhyResult(resultEl, res);
    } catch (e) {
      resultEl.innerHTML = `<p class="dna-empty">${escapeHtml(e.message)}</p>`;
      resultEl.classList.remove('hidden');
    } finally {
      thinkingEl.classList.add('hidden');
      if (btn) btn.disabled = false;
    }
  }

  // ---------- Trader Progression ----------
  async function loadProgression() {
    try {
      state.progression = await api('GET', `/portfolios/${state.portfolioId}/progression`);
    } catch (_) {
      state.progression = null;
    }
    return state.progression;
  }

  function renderTrendArrow(direction) {
    if (direction === 'improving') return '<span class="trend-arrow trend-up">▲</span>';
    if (direction === 'declining') return '<span class="trend-arrow trend-down">▼</span>';
    if (direction === 'flat') return '<span class="trend-arrow trend-flat">→</span>';
    return '';
  }

  function renderProgression() {
    const contentEl = document.getElementById('progression-content');
    const p = state.progression;
    if (!p) {
      contentEl.innerHTML = '<p class="dna-empty">Could not load progression.</p>';
      return;
    }

    let trendHtml;
    if (p.has_sufficient_trend_data) {
      trendHtml = `
        <div class="progression-trend-grid">
          ${p.trend
            .map(
              (t) => `
            <div class="progression-trend-card">
              <div class="progression-trend-label">${escapeHtml(t.metric.replace(/_/g, ' '))}</div>
              <div class="progression-trend-values">
                <span>${t.baseline_value ?? '—'}</span>
                <span class="progression-trend-sep">→</span>
                <span>${t.current_value ?? '—'}</span>
                ${renderTrendArrow(t.direction)}
              </div>
            </div>
          `
            )
            .join('')}
        </div>
      `;
    } else {
      trendHtml = `<p class="dna-empty">${escapeHtml(p.trend_note)}</p>`;
    }

    const milestoneHtml = (milestones) =>
      milestones
        .map(
          (m) => `
        <div class="progression-milestone ${m.achieved ? 'achieved' : ''}">
          <span class="progression-milestone-icon">${m.achieved ? '✓' : '🔒'}</span>
          <span>${escapeHtml(m.label)}</span>
          ${m.achieved && m.achieved_at ? `<span class="progression-milestone-date">${formatDateTime(m.achieved_at)}</span>` : ''}
        </div>
      `
        )
        .join('');

    const journalHtml = p.journaling.has_sufficient_data
      ? `
        <div class="progression-journal-bar-track"><div class="progression-journal-bar-fill" style="width:${p.journaling.journaled_pct}%"></div></div>
        <p class="section-sub">${p.journaling.trades_journaled} of ${p.journaling.trades_closed} closed trades journaled (${p.journaling.journaled_pct}%)</p>
      `
      : `<p class="dna-empty">${escapeHtml(p.journaling.note)}</p>`;

    contentEl.innerHTML = `
      <h4>Trend — last ${30} days vs. the ${90} days before that</h4>
      ${trendHtml}
      <h4>Milestones</h4>
      <div class="progression-milestones">${milestoneHtml(p.trade_milestones)}</div>
      <h4>Decision Training Milestones</h4>
      <div class="progression-milestones">${milestoneHtml(p.decision_training_milestones)}</div>
      <h4>Journaling Coverage</h4>
      ${journalHtml}
    `;
  }

  // ---------- Anonymous Benchmarking ----------
  async function loadBenchmark() {
    try {
      state.benchmark = await api('GET', `/portfolios/${state.portfolioId}/benchmark`);
    } catch (_) {
      state.benchmark = null;
    }
    return state.benchmark;
  }

  function renderBenchmark() {
    const contentEl = document.getElementById('benchmark-content');
    const b = state.benchmark;
    if (!b) {
      contentEl.innerHTML = '<p class="dna-empty">Could not load benchmarking.</p>';
      return;
    }
    if (!b.has_sufficient_data) {
      contentEl.innerHTML = `<p class="dna-empty">${escapeHtml(b.note)}</p>`;
      return;
    }
    const bars = [
      ['Win Rate', b.own_win_rate_pct !== null ? `${b.own_win_rate_pct}%` : '—', b.win_rate_percentile],
      ['Avg Realized R', b.own_avg_realized_r !== null ? `${b.own_avg_realized_r.toFixed(2)}R` : '—', b.avg_realized_r_percentile],
    ];
    contentEl.innerHTML = `
      <p class="section-sub">Compared against ${b.peer_trader_count} other real traders on the platform who've each logged at least ${b.min_trades_required} closed trades.</p>
      <div class="benchmark-bars">
        ${bars
          .map(([label, value, percentile]) =>
            percentile === null
              ? `<div class="benchmark-bar-row"><span class="benchmark-bar-label">${label}</span><span class="dna-empty">Not enough of your own trades have both a stop-loss and take-profit to compare.</span></div>`
              : `
            <div class="benchmark-bar-row">
              <span class="benchmark-bar-label">${label}</span>
              <div class="benchmark-bar-track"><div class="benchmark-bar-fill" style="width:${percentile}%"></div></div>
              <span class="benchmark-bar-value">${value} · ${percentile}th percentile</span>
            </div>
          `
          )
          .join('')}
      </div>
    `;
  }

  function populateSetupPerfFilters() {
    const symbolSelect = document.getElementById('sp-filter-symbol');
    const setupSelect = document.getElementById('sp-filter-setup');
    const currentSymbol = symbolSelect.value;
    const currentSetup = setupSelect.value;

    const symbols = [...new Set(state.trades.map((t) => t.symbol))].sort();
    symbolSelect.innerHTML = '<option value="">All symbols</option>' + symbols.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
    symbolSelect.value = currentSymbol;

    setupSelect.innerHTML = '<option value="">All setups</option>' + state.setups.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
    setupSelect.value = currentSetup;
  }

  async function loadSetupPerformance() {
    const symbol = document.getElementById('sp-filter-symbol').value;
    const session = document.getElementById('sp-filter-session').value;
    const direction = document.getElementById('sp-filter-direction').value;
    const setupId = document.getElementById('sp-filter-setup').value;
    const dateFrom = document.getElementById('sp-filter-date-from').value;
    const dateTo = document.getElementById('sp-filter-date-to').value;

    const params = new URLSearchParams();
    if (symbol) params.set('symbol', symbol);
    if (session) params.set('session', session);
    if (direction) params.set('direction', direction);
    if (setupId) params.set('setup_id', setupId);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);

    const result = await api('GET', `/portfolios/${state.portfolioId}/setup-performance?${params.toString()}`);
    state.setupPerformance = result;
    renderSetupPerformanceTable(result);
  }

  function renderSetupPerformanceTable(result) {
    const el = document.getElementById('setup-perf-results');
    if (!result.rows.length) {
      el.innerHTML = `<p class="dna-empty">${escapeHtml(result.note || 'No closed trades match these filters.')}</p>`;
      return;
    }
    const rows = result.rows
      .map((r) => {
        const fmt = (v, digits = 2, suffix = '') => (v === null ? '—' : `${v.toFixed(digits)}${suffix}`);
        return `
          <tr>
            <td>${escapeHtml(r.setup_name)}</td>
            <td>${r.trade_count}</td>
            <td>${r.win_rate_pct.toFixed(0)}%</td>
            <td>${fmt(r.avg_realized_r, 2, 'R')}</td>
            <td>${fmt(r.profit_factor)}</td>
            <td>${fmt(r.expectancy)}</td>
            <td class="${r.total_profit >= 0 ? 'pos' : 'neg'}">${formatMoney(r.total_profit)}</td>
          </tr>
        `;
      })
      .join('');
    el.innerHTML = `
      <table class="dna-table">
        <thead><tr><th>Setup</th><th>Trades</th><th>Win %</th><th>Avg R</th><th>Profit Factor</th><th>Expectancy</th><th>P&amp;L</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  document.getElementById('sp-apply-btn').addEventListener('click', () => {
    loadSetupPerformance().catch((e) => toast(`Couldn't load setup performance: ${e.message}`, 'error'));
  });

  // ---------- Live Market ----------
  const TV_CRYPTO_BASES = new Set(['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'LTC', 'ADA']);
  // NSE/BSE are commonly used as shorthand for their flagship index; gold
  // has no plain "XAUUSD" on TradingView either — it's under OANDA.
  const TV_SPECIAL_SYMBOLS = {
    NIFTY: 'NSE:NIFTY',
    NIFTY50: 'NSE:NIFTY',
    NSE: 'NSE:NIFTY',
    SENSEX: 'BSE:SENSEX',
    BSE: 'BSE:SENSEX',
    XAUUSD: 'OANDA:XAUUSD',
    XAU: 'OANDA:XAUUSD',
    GOLD: 'OANDA:XAUUSD',
  };

  function mapCleanTvSymbol(upper) {
    if (TV_SPECIAL_SYMBOLS[upper]) return TV_SPECIAL_SYMBOLS[upper];
    if (upper.length !== 6 || !/^[A-Z]+$/.test(upper)) return null;
    const base = upper.slice(0, 3);
    if (TV_CRYPTO_BASES.has(base) && upper.endsWith('USD')) {
      return `COINBASE:${base}USD`;
    }
    return `FX:${upper}`;
  }

  function toTradingViewSymbol(symbol) {
    const upper = symbol.toUpperCase();
    const direct = mapCleanTvSymbol(upper);
    if (direct) return direct;

    // MT5-synced trades carry the broker's own symbol, which often tacks on
    // a suffix ("EURUSD.a", "XAUUSDm", "GBPJPY_i") that breaks the clean
    // patterns above — fall back to a leading 6-letter alphabetic run.
    const core = upper.replace(/[^A-Z]/g, '').slice(0, 6);
    if (core.length === 6) {
      const fallback = mapCleanTvSymbol(core);
      if (fallback) return fallback;
    }
    return upper;
  }

  function loadTradingViewWidget(container, src, config) {
    container.innerHTML = '<div class="tradingview-widget-container__widget"></div>';
    const script = document.createElement('script');
    script.src = src;
    script.type = 'text/javascript';
    script.async = true;
    script.text = JSON.stringify(config);
    container.appendChild(script);
  }

  let tickerTapeKey = null;
  function renderTickerTape(symbols) {
    const key = symbols.join(',');
    if (!symbols.length || key === tickerTapeKey) return;
    tickerTapeKey = key;
    loadTradingViewWidget(
      document.getElementById('tv-ticker-tape'),
      'https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js',
      {
        symbols: symbols.map((s) => ({ proName: toTradingViewSymbol(s), title: s })),
        showSymbolLogo: true,
        colorTheme: 'dark',
        isTransparent: true,
        displayMode: 'compact',
        locale: 'en',
      }
    );
  }

  function renderTradingViewChart(symbol) {
    loadTradingViewWidget(
      document.getElementById('tv-chart-container'),
      'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js',
      {
        width: '100%',
        height: 420,
        symbol: toTradingViewSymbol(symbol),
        interval: '60',
        timezone: 'Etc/UTC',
        theme: 'dark',
        style: '1',
        locale: 'en',
        backgroundColor: 'rgba(0, 0, 0, 0)',
        hide_top_toolbar: false,
        allow_symbol_change: false,
        support_host: 'https://www.tradingview.com',
      }
    );
  }

  function drawSparkline(canvas, history) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width = canvas.clientWidth * devicePixelRatio;
    const h = canvas.height = canvas.clientHeight * devicePixelRatio;
    ctx.clearRect(0, 0, w, h);
    if (history.length < 2) return;

    const min = Math.min(...history);
    const max = Math.max(...history);
    const range = max - min || 1;
    const stepX = w / (history.length - 1);
    const rising = history[history.length - 1] >= history[0];

    ctx.beginPath();
    history.forEach((val, i) => {
      const x = i * stepX;
      const y = h - ((val - min) / range) * (h * 0.85) - h * 0.075;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = rising ? '#22e58a' : '#ff5c7c';
    ctx.lineWidth = 1.6 * devicePixelRatio;
    ctx.lineJoin = 'round';
    ctx.stroke();
  }

  function renderMarketTicker() {
    const container = document.getElementById('market-ticker');
    const symbols = Object.keys(state.market).sort();

    if (!symbols.length) {
      container.innerHTML = '<div class="ticker-empty">Waiting for the first live tick…</div>';
      return;
    }

    symbols.forEach((symbol) => {
      const data = state.market[symbol];
      let card = document.getElementById(`ticker-${symbol}`);
      if (!card) {
        card = document.createElement('div');
        card.className = 'ticker-card';
        card.id = `ticker-${symbol}`;
        card.innerHTML = `
          <div class="ticker-symbol">${escapeHtml(symbol)}</div>
          <div class="ticker-price-row"><span class="ticker-price">—</span></div>
          <canvas class="ticker-sparkline"></canvas>
        `;
        card.addEventListener('click', () => openChartModal(symbol));
        container.appendChild(card);
      }

      const priceEl = card.querySelector('.ticker-price');
      const prevPrice = parseFloat(priceEl.dataset.raw || '0');
      const decimals = data.price < 50 ? 5 : 2;
      priceEl.textContent = data.price.toFixed(decimals);
      priceEl.dataset.raw = data.price;

      if (prevPrice && data.price !== prevPrice) {
        priceEl.classList.remove('flash-up', 'flash-down');
        void priceEl.offsetWidth; // restart animation
        priceEl.classList.add(data.price > prevPrice ? 'flash-up' : 'flash-down');
      }

      drawSparkline(card.querySelector('.ticker-sparkline'), data.history || []);

      if (state.chartSymbol === symbol) {
        updateChartLivePrice(data.price, prevPrice);
        if (state.chartOpenTrade && state.chartOpenTrade.symbol === symbol) updateChartPositionPnl(data.price);
      }
    });
  }

  function applyMarketSnapshot(snapshot) {
    state.market = snapshot;
    renderMarketTicker();
  }

  function openMarketsView() {
    showView('markets');
    // Redraw sparklines now that the container has real layout size, and
    // only embed the TradingView ticker-tape widget once this tab is
    // actually visible — instantiating it into a display:none container
    // risks it sizing itself incorrectly.
    renderMarketTicker();
    renderTickerTape(Object.keys(state.market).sort());
    renderTradesList(state.trades);
    refreshMt5Status();
    const bridgeIdEl = document.getElementById('mt5-bridge-portfolio-id');
    if (bridgeIdEl) bridgeIdEl.textContent = state.portfolioId;
  }

  function openNewsView() {
    showView('news');
  }

  function connectMarketFeed() {
    if (state.marketSocket) return;
    const sourceEl = document.getElementById('market-source');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/prices`);
    state.marketSocket = socket;

    socket.addEventListener('open', () => { sourceEl.textContent = 'live · Yahoo Finance'; });
    socket.addEventListener('message', (event) => {
      try {
        applyMarketSnapshot(JSON.parse(event.data));
      } catch (_) { /* ignore malformed frame */ }
    });
    socket.addEventListener('close', () => {
      sourceEl.textContent = 'reconnecting…';
      state.marketSocket = null;
      setTimeout(connectMarketFeed, 3000);
    });
    socket.addEventListener('error', () => socket.close());
  }

  // ---------- Smart Alerts ----------
  function connectAlertsFeed() {
    if (state.alertSocket || !state.token) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/alerts?token=${encodeURIComponent(state.token)}`);
    state.alertSocket = socket;

    socket.addEventListener('message', (event) => {
      try {
        const alert = JSON.parse(event.data);
        state.alerts.unshift(alert);
        toast(alert.message, alert.severity === 'high' ? 'error' : 'success');
        renderAlertBadge();
      } catch (_) { /* ignore malformed frame */ }
    });
    socket.addEventListener('close', () => {
      state.alertSocket = null;
      if (state.token) setTimeout(connectAlertsFeed, 3000);
    });
    socket.addEventListener('error', () => socket.close());
  }

  async function loadAlerts() {
    try {
      state.alerts = await api('GET', `/portfolios/${state.portfolioId}/alerts`);
    } catch (_) {
      state.alerts = [];
    }
    renderAlertBadge();
  }

  function renderAlertBadge() {
    const badge = document.getElementById('alert-bell-badge');
    const unread = state.alerts.filter((a) => !a.read).length;
    badge.textContent = unread > 9 ? '9+' : String(unread);
    badge.classList.toggle('hidden', unread === 0);
  }

  function renderAlertPanel() {
    const listEl = document.getElementById('alert-panel-list');
    if (!state.alerts.length) {
      listEl.innerHTML = '<p class="dna-empty">No alerts yet.</p>';
      return;
    }
    listEl.innerHTML = state.alerts
      .map(
        (a) => `
      <div class="alert-panel-item ${a.read ? '' : 'unread'}" data-id="${a.id}">
        <p class="alert-panel-message">${escapeHtml(a.message)}</p>
        <span class="alert-panel-meta">${formatDateTime(a.created_at)}</span>
      </div>
    `
      )
      .join('');
    listEl.querySelectorAll('.alert-panel-item.unread').forEach((item) => {
      item.addEventListener('click', () => markAlertRead(Number(item.dataset.id)));
    });
  }

  async function markAlertRead(alertId) {
    try {
      const updated = await api('POST', `/alerts/${alertId}/read`, undefined);
      const cached = state.alerts.find((a) => a.id === alertId);
      if (cached) cached.read = updated.read;
      renderAlertBadge();
      renderAlertPanel();
    } catch (_) { /* leave as unread on failure */ }
  }

  document.getElementById('alert-bell').addEventListener('click', () => {
    const panel = document.getElementById('alert-panel');
    const isHidden = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !isHidden);
    if (isHidden) renderAlertPanel();
  });
  document.getElementById('alert-panel-close').addEventListener('click', () => {
    document.getElementById('alert-panel').classList.add('hidden');
  });

  function updateChartLivePrice(price, prevPrice) {
    const el = document.getElementById('chart-live-price');
    const decimals = price < 50 ? 5 : 2;
    el.textContent = price.toFixed(decimals);
    if (prevPrice && price !== prevPrice) {
      el.classList.remove('flash-up', 'flash-down');
      void el.offsetWidth;
      el.classList.add(price > prevPrice ? 'flash-up' : 'flash-down');
    }
  }

  function openChartModal(symbol) {
    state.chartSymbol = symbol;
    document.getElementById('chart-symbol').textContent = symbol;
    document.getElementById('mkt-error').textContent = '';
    document.getElementById('mkt-comment').value = '';
    document.getElementById('mkt-volume').value = '1.0';

    const current = state.market[symbol];
    if (current) updateChartLivePrice(current.price);

    renderTradingViewChart(symbol);

    // Reopening the chart for a symbol that already has an open position
    // (e.g. navigated away and came back) lands straight in monitoring
    // mode instead of showing the order form again.
    const openTrade = state.trades.find((t) => t.symbol === symbol && t.status === 'open');
    if (openTrade) {
      showChartPositionMode(openTrade);
    } else {
      showChartOrderMode();
      refreshPretradeCheck();
    }
    openModal('modal-chart');
  }

  // ---------- Chart Modal: stay-open position monitoring ----------
  // Placing a trade from the chart no longer closes it — the whole point is
  // to keep watching the live price until the position is actually closed.
  function showChartOrderMode() {
    state.chartOpenTrade = null;
    document.getElementById('chart-order-section').classList.remove('hidden');
    document.getElementById('chart-position-section').classList.add('hidden');
  }

  function showChartPositionMode(trade) {
    state.chartOpenTrade = trade;
    document.getElementById('chart-order-section').classList.add('hidden');
    document.getElementById('chart-position-section').classList.remove('hidden');
    document.getElementById('chart-position-dir').textContent = trade.direction === 'buy' ? '▲ Buy' : '▼ Sell';
    const decimals = trade.open_price < 50 ? 5 : 2;
    document.getElementById('chart-position-entry').textContent = trade.open_price.toFixed(decimals);
    document.getElementById('chart-position-volume').textContent = `${trade.volume} lots`;
    const current = state.market[trade.symbol];
    updateChartPositionPnl(current ? current.price : trade.open_price);
  }

  function updateChartPositionPnl(currentPrice) {
    const trade = state.chartOpenTrade;
    if (!trade) return;
    const sign = trade.direction === 'buy' ? 1 : -1;
    const pnl = (currentPrice - trade.open_price) * trade.volume * sign;
    const el = document.getElementById('chart-position-pnl');
    el.textContent = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
    el.classList.toggle('pos', pnl >= 0);
    el.classList.toggle('neg', pnl < 0);
  }

  document.getElementById('chart-position-close-btn').addEventListener('click', async () => {
    const trade = state.chartOpenTrade;
    if (!trade) return;
    const btn = document.getElementById('chart-position-close-btn');
    btn.disabled = true;
    try {
      closeModal('modal-chart');
      state.chartOpenTrade = null;
      await closeTradeAtMarket(trade.id);
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- Pre-Trade AI Check ----------
  let pretradeTimer = null;

  function renderPretradeCheck(result) {
    const bodyEl = document.getElementById('pretrade-body');
    const riskClass = `pretrade-risk-${result.risk_level}`;
    const riskLabel = result.risk_pct === null ? result.risk_level : `${result.risk_level} · ${result.risk_pct.toFixed(0)}%`;
    const warningsHtml = result.warnings.map((w) => `<div class="pretrade-row pretrade-warning">⚠ ${escapeHtml(w)}</div>`).join('');
    const rsiHtml = result.rsi !== null
      ? `<div class="pretrade-row">RSI ${result.rsi.toFixed(0)}${result.rsi_zone && result.rsi_zone !== 'neutral' ? ` (${result.rsi_zone})` : ''}</div>`
      : '';
    bodyEl.innerHTML = `
      <div class="pretrade-row"><span class="pretrade-risk-pill ${riskClass}">${escapeHtml(riskLabel)} risk</span> position size vs. balance</div>
      ${rsiHtml}
      ${warningsHtml}
    `;
    document.getElementById('pretrade-buy-note').textContent = result.buy_note || '';
    document.getElementById('pretrade-sell-note').textContent = result.sell_note || '';
  }

  async function refreshPretradeCheck() {
    const symbol = state.chartSymbol;
    const volume = parseFloat(document.getElementById('mkt-volume').value);
    const bodyEl = document.getElementById('pretrade-body');
    if (!symbol || !volume || volume <= 0) {
      bodyEl.innerHTML = '<span class="pretrade-loading">Enter a volume to see the pre-trade check.</span>';
      document.getElementById('pretrade-buy-note').textContent = '';
      document.getElementById('pretrade-sell-note').textContent = '';
      return;
    }
    try {
      const result = await api('POST', '/trades/precheck', { portfolio_id: state.portfolioId, symbol, volume });
      if (state.chartSymbol === symbol) renderPretradeCheck(result);
    } catch (e) {
      bodyEl.innerHTML = `<span class="pretrade-loading">Pre-trade check unavailable: ${escapeHtml(e.message)}</span>`;
    }
  }

  document.getElementById('mkt-volume').addEventListener('input', () => {
    clearTimeout(pretradeTimer);
    pretradeTimer = setTimeout(refreshPretradeCheck, 400);
  });

  async function submitMarketOrder(direction) {
    const errEl = document.getElementById('mkt-error');
    errEl.textContent = '';
    const volume = parseFloat(document.getElementById('mkt-volume').value);
    const comment = document.getElementById('mkt-comment').value.trim();

    if (!volume || volume <= 0) {
      errEl.textContent = 'Enter a volume greater than 0.';
      return;
    }

    const buyBtn = document.getElementById('mkt-buy');
    const sellBtn = document.getElementById('mkt-sell');
    buyBtn.disabled = true;
    sellBtn.disabled = true;
    try {
      const trade = await api('POST', '/trades/market/open', {
        portfolio_id: state.portfolioId,
        symbol: state.chartSymbol,
        direction,
        volume,
        comment: comment || null,
      });
      await loadDashboard();
      // Stay on the chart instead of closing the modal - the point is to
      // keep watching the live price until this position is closed.
      showChartPositionMode(trade);
      toast(`${direction === 'buy' ? 'Bought' : 'Sold'} ${state.chartSymbol} at market (${trade.open_price})`);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      buyBtn.disabled = false;
      sellBtn.disabled = false;
    }
  }

  document.getElementById('mkt-buy').addEventListener('click', () => submitMarketOrder('buy'));
  document.getElementById('mkt-sell').addEventListener('click', () => submitMarketOrder('sell'));

  async function closeTradeAtMarket(tradeId) {
    try {
      const trade = await api('POST', `/trades/${tradeId}/close/market`, undefined);
      await loadDashboard();
      const grade = latestEvaluation(trade, 'rules_engine');
      toast(`Closed at market (${trade.close_price}) — grade: ${grade ? grade.grade : '—'}`);
      openDetail(trade.id);
    } catch (e) {
      toast(`Could not close at market: ${e.message}`, 'error');
    }
  }

  // ---------- MetaTrader 5 ----------
  let mt5LastSyncedAt = null;

  function renderMt5Status(connection) {
    const disconnectedEl = document.getElementById('mt5-disconnected');
    const connectedEl = document.getElementById('mt5-connected');

    if (!connection || connection.status === 'disconnected') {
      disconnectedEl.classList.remove('hidden');
      connectedEl.classList.add('hidden');
      mt5LastSyncedAt = null;
      return;
    }

    disconnectedEl.classList.add('hidden');
    connectedEl.classList.remove('hidden');

    const dot = document.getElementById('mt5-status-dot');
    const title = document.getElementById('mt5-status-title');
    const sub = document.getElementById('mt5-status-sub');

    dot.className = `mt5-status-dot ${connection.status}`;
    sub.classList.toggle('error-text', connection.status === 'error');

    if (connection.status === 'connected') {
      title.textContent = `Connected · #${connection.login} @ ${connection.server}`;
      sub.textContent = connection.last_synced_at
        ? `Last synced ${formatDateTime(connection.last_synced_at)}`
        : 'Waiting for first sync…';
    } else {
      title.textContent = `Connection error · #${connection.login} @ ${connection.server}`;
      sub.textContent = connection.last_error || 'Could not reach the MT5 terminal.';
    }

    if (connection.last_synced_at && connection.last_synced_at !== mt5LastSyncedAt) {
      const isFirstCheck = mt5LastSyncedAt === null;
      mt5LastSyncedAt = connection.last_synced_at;
      if (!isFirstCheck) loadDashboard();
    }
  }

  async function refreshMt5Status() {
    if (!MT5_ENABLED) return;
    try {
      const connection = await api('GET', `/portfolios/${state.portfolioId}/mt5/status`);
      renderMt5Status(connection);
    } catch (_) { /* leave last known state on transient failure */ }
  }

  function pollMt5Status() {
    setInterval(refreshMt5Status, 15000);
  }

  function openMt5ConnectModal() {
    document.getElementById('mt5-login').value = '';
    document.getElementById('mt5-password').value = '';
    document.getElementById('mt5-server').value = '';
    document.getElementById('mt5-terminal-path').value = '';
    document.getElementById('mt5-connect-error').textContent = '';
    openModal('modal-mt5-connect');
  }

  async function submitMt5Connect() {
    const errEl = document.getElementById('mt5-connect-error');
    errEl.textContent = '';
    const login = parseInt(document.getElementById('mt5-login').value, 10);
    const password = document.getElementById('mt5-password').value;
    const server = document.getElementById('mt5-server').value.trim();
    const terminalPath = document.getElementById('mt5-terminal-path').value.trim();

    if (!login || login <= 0 || !password || !server) {
      errEl.textContent = 'Enter your login, password, and server.';
      return;
    }

    const btn = document.getElementById('mt5-connect-submit');
    btn.disabled = true;
    btn.textContent = 'Connecting…';
    try {
      const connection = await api('POST', `/portfolios/${state.portfolioId}/mt5/connect`, {
        login,
        password,
        server,
        terminal_path: terminalPath || null,
      });
      closeModal('modal-mt5-connect');
      renderMt5Status(connection);
      toast(`MT5 account #${login} connected`);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Connect →';
    }
  }

  async function disconnectMt5() {
    const btn = document.getElementById('btn-mt5-disconnect');
    btn.disabled = true;
    try {
      await api('POST', `/portfolios/${state.portfolioId}/mt5/disconnect`, undefined);
      renderMt5Status(null);
      toast('MT5 account disconnected');
    } catch (e) {
      toast(`Could not disconnect: ${e.message}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('btn-mt5-connect').addEventListener('click', openMt5ConnectModal);
  document.getElementById('mt5-connect-submit').addEventListener('click', submitMt5Connect);
  document.getElementById('btn-mt5-disconnect').addEventListener('click', disconnectMt5);

  // ---------- Market News ----------
  function timeAgo(iso) {
    if (!iso) return '';
    const diffMs = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  function renderNewsList(articles) {
    const listEl = document.getElementById('news-list');
    listEl.innerHTML = '';
    if (!articles.length) {
      listEl.innerHTML = '<div class="news-empty">No headlines available right now.</div>';
      return;
    }
    articles.forEach((article, i) => {
      const item = document.createElement('a');
      item.className = 'news-item';
      item.href = article.link;
      item.target = '_blank';
      item.rel = 'noopener noreferrer';
      item.style.animationDelay = `${Math.min(i, 10) * 30}ms`;
      const thumb = article.image
        ? `<img class="news-item-thumb" src="${escapeHtml(article.image)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;news-item-thumb-placeholder&quot;>📰</div>'" />`
        : '<div class="news-item-thumb-placeholder">📰</div>';
      item.innerHTML = `
        ${thumb}
        <div class="news-item-body">
          <div class="news-item-title">${escapeHtml(article.title)}</div>
          <div class="news-item-meta">${escapeHtml(article.source)} · ${timeAgo(article.published_at)}</div>
        </div>
      `;
      listEl.appendChild(item);
    });
  }

  async function loadNews(category, { silent = false } = {}) {
    state.activeNewsCategory = category;
    const listEl = document.getElementById('news-list');
    if (!silent) listEl.innerHTML = '<div class="news-loading">Loading headlines…</div>';
    try {
      const articles = await api('GET', `/news?category=${category}`);
      // A background refresh may land after the user has switched tabs —
      // drop it rather than overwrite whatever tab they're looking at now.
      if (state.activeNewsCategory !== category) return;
      renderNewsList(articles);
    } catch (e) {
      if (silent) return; // keep showing the last-good list rather than an error on a quiet auto-refresh
      listEl.innerHTML = `<div class="news-empty">Couldn't load news: ${escapeHtml(e.message)}</div>`;
    }
  }

  document.getElementById('news-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.news-tab');
    if (!btn) return;
    document.querySelectorAll('.news-tab').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.activeNewsCategory = btn.dataset.category;
    if (btn.dataset.category === 'my-symbols') {
      loadNewsImpact();
    } else {
      loadNews(btn.dataset.category);
    }
  });

  const NEWS_LIST_REFRESH_MS = 3 * 60 * 1000;
  function startNewsListPolling() {
    setInterval(() => {
      // My Symbols is a separate, portfolio-scoped feed — never let the
      // plain international/national poll silently overwrite it.
      if (state.activeNewsCategory === 'my-symbols') return;
      loadNews(state.activeNewsCategory, { silent: true });
    }, NEWS_LIST_REFRESH_MS);
  }

  // ---------- News → Trade Impact Intelligence ("My Symbols") ----------
  async function loadNewsImpact() {
    const listEl = document.getElementById('news-list');
    listEl.innerHTML = '<div class="news-loading">Loading your symbols…</div>';
    try {
      const articles = await api('GET', `/portfolios/${state.portfolioId}/news-impact?category=international`);
      if (state.activeNewsCategory !== 'my-symbols') return;
      renderNewsImpactList(articles);
    } catch (e) {
      if (state.activeNewsCategory !== 'my-symbols') return;
      listEl.innerHTML = `<div class="news-empty">Couldn't load your symbols: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderNewsImpactList(articles) {
    const listEl = document.getElementById('news-list');
    const tagged = articles.filter((a) => a.matched_symbols.length > 0);
    if (!tagged.length) {
      listEl.innerHTML = '<div class="news-empty">No current headlines mention a symbol you trade. (Only a curated keyword match — not every relevant headline will tag.)</div>';
      return;
    }
    listEl.innerHTML = '';
    tagged.forEach((article, i) => {
      const item = document.createElement('div');
      item.className = 'news-item news-impact-item';
      item.style.animationDelay = `${Math.min(i, 10) * 30}ms`;
      const thumb = article.image
        ? `<img class="news-item-thumb" src="${escapeHtml(article.image)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;news-item-thumb-placeholder&quot;>📰</div>'" />`
        : '<div class="news-item-thumb-placeholder">📰</div>';
      item.innerHTML = `
        ${thumb}
        <div class="news-item-body">
          <a class="news-item-title" href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a>
          <div class="news-item-meta">${escapeHtml(article.source)} · ${timeAgo(article.published_at)}</div>
          <div class="news-impact-tags">${article.matched_symbols.map((s) => `<span class="news-impact-chip">${escapeHtml(s)}</span>`).join('')}</div>
          ${article.your_position_context ? `<p class="news-impact-context">${escapeHtml(article.your_position_context)}</p>` : ''}
          <button type="button" class="btn btn-ghost btn-sm news-impact-why-btn" data-symbol="${escapeHtml(article.matched_symbols[0])}" data-published="${escapeHtml(article.published_at || '')}">Why did this move?</button>
          <div class="news-impact-result hidden"></div>
        </div>
      `;
      listEl.appendChild(item);
    });
    listEl.querySelectorAll('.news-impact-why-btn').forEach((btn) => {
      btn.addEventListener('click', () => requestPriceMoveCheck(btn));
    });
  }

  async function requestPriceMoveCheck(btn) {
    const resultEl = btn.nextElementSibling;
    const symbol = btn.dataset.symbol;
    const publishedAt = btn.dataset.published;
    if (!publishedAt) return;
    btn.disabled = true;
    resultEl.innerHTML = '<span class="pretrade-loading">Checking real price data…</span>';
    resultEl.classList.remove('hidden');
    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/news-impact/price-check`, {
        symbol,
        published_at: publishedAt,
      });
      const sign = res.pct_change >= 0 ? '+' : '';
      resultEl.innerHTML = `
        <p class="${res.pct_change >= 0 ? 'pos' : 'neg'}">${symbol} moved ${sign}${res.pct_change}% in this window (${res.price_before} → ${res.price_after}).</p>
        <p class="sl-disclaimer">${escapeHtml(res.disclaimer)}</p>
      `;
    } catch (e) {
      resultEl.innerHTML = `<p class="dna-empty">${escapeHtml(e.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- Breaking News Tape ----------
  const NEWS_TAPE_MAX_ITEMS = 12;
  const NEWS_TAPE_REFRESH_MS = 5 * 60 * 1000;

  async function loadNewsTape() {
    const track = document.getElementById('news-breaking-track');
    try {
      const [intl, national] = await Promise.all([
        api('GET', '/news?category=international'),
        api('GET', '/news?category=national'),
      ]);
      const seen = new Set();
      const merged = [...intl, ...national]
        .filter((a) => (seen.has(a.link) ? false : (seen.add(a.link), true)))
        .sort((a, b) => new Date(b.published_at || 0) - new Date(a.published_at || 0))
        .slice(0, NEWS_TAPE_MAX_ITEMS);

      if (!merged.length) {
        track.innerHTML = '<span class="news-breaking-item">No headlines available right now.</span>';
        return;
      }

      const itemsHtml = merged
        .map(
          (a) =>
            `<a class="news-breaking-item" href="${a.link}" target="_blank" rel="noopener noreferrer">${escapeHtml(a.source)}: ${escapeHtml(a.title)}</a>`
        )
        .join('<span class="news-breaking-sep">●</span>');
      // Duplicated back-to-back so the -50% translateX loop is seamless.
      track.innerHTML = `${itemsHtml}<span class="news-breaking-sep">●</span>${itemsHtml}<span class="news-breaking-sep">●</span>`;
      track.style.animationDuration = `${Math.max(30, merged.length * 6)}s`;
    } catch (e) {
      track.innerHTML = `<span class="news-breaking-item">Couldn't load breaking news: ${escapeHtml(e.message)}</span>`;
    }
  }

  function startNewsTapePolling() {
    loadNewsTape();
    setInterval(loadNewsTape, NEWS_TAPE_REFRESH_MS);
  }

  // ---------- Modals ----------
  function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
  function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

  document.querySelectorAll('.modal-backdrop').forEach((backdrop) => {
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) backdrop.classList.add('hidden');
    });
    backdrop.querySelector('[data-close]')?.addEventListener('click', () => backdrop.classList.add('hidden'));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') document.querySelectorAll('.modal-backdrop').forEach((b) => b.classList.add('hidden'));
  });

  // ---------- New Trade ----------
  function openNewTradeModal() {
    document.getElementById('nt-symbol').value = '';
    document.getElementById('nt-volume').value = '';
    document.getElementById('nt-open-price').value = '';
    document.getElementById('nt-comment').value = '';
    document.getElementById('nt-error').textContent = '';
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    document.getElementById('nt-open-time').value = now.toISOString().slice(0, 16);
    document.querySelectorAll('#nt-direction .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.value === 'buy'));
    openModal('modal-new-trade');
  }

  document.getElementById('nt-direction').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    document.querySelectorAll('#nt-direction .seg-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
  });

  async function submitNewTrade() {
    const errEl = document.getElementById('nt-error');
    errEl.textContent = '';
    const symbol = document.getElementById('nt-symbol').value.trim().toUpperCase();
    const direction = document.querySelector('#nt-direction .seg-btn.active').dataset.value;
    const volume = parseFloat(document.getElementById('nt-volume').value);
    const openPrice = parseFloat(document.getElementById('nt-open-price').value);
    const openTimeRaw = document.getElementById('nt-open-time').value;
    const comment = document.getElementById('nt-comment').value.trim();

    if (!symbol || !volume || volume <= 0 || !openPrice || openPrice <= 0 || !openTimeRaw) {
      errEl.textContent = 'Fill in symbol, volume, open price, and open time.';
      return;
    }

    const btn = document.getElementById('nt-submit');
    btn.disabled = true;
    try {
      await api('POST', '/trades', {
        portfolio_id: state.portfolioId,
        symbol,
        direction,
        volume,
        open_price: openPrice,
        open_time: new Date(openTimeRaw).toISOString(),
        comment: comment || null,
      });
      closeModal('modal-new-trade');
      await loadDashboard();
      toast(`${symbol} trade opened`);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- Close Trade ----------
  function openCloseTradeModal(tradeId, symbol) {
    state.closingTradeId = tradeId;
    document.getElementById('ct-symbol-label').textContent = symbol;
    document.getElementById('ct-close-price').value = '';
    document.getElementById('ct-swap').value = '0';
    document.getElementById('ct-commission').value = '0';
    document.getElementById('ct-error').textContent = '';
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    document.getElementById('ct-close-time').value = now.toISOString().slice(0, 16);
    openModal('modal-close-trade');
  }

  async function submitCloseTrade() {
    const errEl = document.getElementById('ct-error');
    errEl.textContent = '';
    const closePrice = parseFloat(document.getElementById('ct-close-price').value);
    const closeTimeRaw = document.getElementById('ct-close-time').value;
    const swap = parseFloat(document.getElementById('ct-swap').value) || 0;
    const commission = parseFloat(document.getElementById('ct-commission').value) || 0;

    if (!closePrice || closePrice <= 0 || !closeTimeRaw) {
      errEl.textContent = 'Enter a valid close price and time.';
      return;
    }

    const btn = document.getElementById('ct-submit');
    btn.disabled = true;
    try {
      const trade = await api('POST', `/trades/${state.closingTradeId}/close`, {
        close_price: closePrice,
        close_time: new Date(closeTimeRaw).toISOString(),
        swap,
        commission,
      });
      closeModal('modal-close-trade');
      await loadDashboard();
      const grade = latestEvaluation(trade, 'rules_engine');
      toast(`Trade closed — instant grade: ${grade ? grade.grade : '—'}`);
      openDetail(trade.id);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- Trade Detail ----------
  async function openDetail(tradeId) {
    let trade;
    try {
      trade = await api('GET', `/trades/${tradeId}`);
    } catch (e) {
      toast('Could not load trade', 'error');
      return;
    }
    state.detailTradeId = tradeId;
    renderDetail(trade);
    openModal('modal-detail');
  }

  function renderDetail(trade) {
    document.getElementById('det-symbol').textContent = trade.symbol;
    document.getElementById('det-meta').textContent = `${trade.direction} · ${trade.volume} lots · ${trade.source}`;

    const rulesGrade = latestEvaluation(trade, 'rules_engine');
    const badgesEl = document.getElementById('det-badges');
    badgesEl.innerHTML = `
      <span class="dir-pill ${trade.status === 'closed' ? 'sell' : 'buy'}" style="padding:6px 12px;border-radius:20px;">${trade.status.toUpperCase()}</span>
      ${rulesGrade ? `<div class="grade-chip ${gradeClass(rulesGrade.grade)}">${rulesGrade.grade}</div>` : ''}
    `;

    document.getElementById('det-open').textContent = `${trade.open_price} · ${formatDateTime(trade.open_time)}`;
    document.getElementById('det-close').textContent = trade.close_price ? `${trade.close_price} · ${formatDateTime(trade.close_time)}` : 'still open';
    const pnlEl = document.getElementById('det-pnl');
    if (trade.status === 'closed') {
      pnlEl.textContent = formatMoney(trade.profit);
      pnlEl.style.color = trade.profit > 0 ? 'var(--green)' : trade.profit < 0 ? 'var(--red)' : 'var(--text-dim)';
    } else {
      pnlEl.textContent = '—';
      pnlEl.style.color = 'var(--text-dim)';
    }

    document.getElementById('det-comment').textContent = trade.comment ? `“${trade.comment}”` : '';

    populateSetupSelect(document.getElementById('det-setup-select'), trade.setup_id);
    document.getElementById('det-setup-new-row').classList.add('hidden');
    document.getElementById('det-setup-new-name').value = '';

    // Cheap reset only — no fetch here. "Find Similar Trades" is lazy/on-click
    // so opening the detail modal never adds a mandatory network round-trip.
    document.getElementById('det-similar-result').classList.add('hidden');
    document.getElementById('det-similar-result').innerHTML = '';
    document.getElementById('det-similar-btn').disabled = false;

    const actionsEl = document.getElementById('det-actions');
    actionsEl.innerHTML = '';
    if (trade.status === 'open') {
      const marketBtn = document.createElement('button');
      marketBtn.className = 'btn market-sell';
      marketBtn.textContent = '⚡ Close at Market Price';
      marketBtn.addEventListener('click', () => closeTradeAtMarket(trade.id));
      actionsEl.appendChild(marketBtn);

      const manualBtn = document.createElement('button');
      manualBtn.className = 'btn btn-ghost';
      manualBtn.textContent = 'Close Manually';
      manualBtn.addEventListener('click', () => openCloseTradeModal(trade.id, trade.symbol));
      actionsEl.appendChild(manualBtn);
    }

    // screenshot
    const dropzone = document.getElementById('det-dropzone');
    const preview = document.getElementById('screenshot-preview');
    const img = document.getElementById('screenshot-img');
    if (trade.screenshots && trade.screenshots.length) {
      const latest = trade.screenshots[trade.screenshots.length - 1];
      img.src = latest.url;
      preview.classList.remove('hidden');
      dropzone.querySelector('p').textContent = 'Drop another screenshot to replace the preview';
    } else {
      preview.classList.add('hidden');
      dropzone.querySelector('p').textContent = 'Drop a chart screenshot here, or click to choose';
    }

    // AI section reset
    document.getElementById('ai-idle').classList.remove('hidden');
    document.getElementById('ai-thinking').classList.add('hidden');
    document.getElementById('ai-result').classList.add('hidden');
    document.getElementById('ai-result').innerHTML = '';
    const analyzeBtn = document.getElementById('btn-analyze');
    analyzeBtn.disabled = trade.status !== 'closed';
    analyzeBtn.textContent = trade.status !== 'closed' ? 'Close the trade first' : '✨ Analyze with AI';

    const mentorEvals = (trade.evaluations || []).filter((e) => e.phase === 'llm_mentor').sort((a, b) => b.id - a.id);
    const historyEl = document.getElementById('ai-history');
    historyEl.innerHTML = '';
    if (mentorEvals.length) {
      renderVerdict(mentorEvals[0]);
      document.getElementById('ai-result').classList.remove('hidden');
      mentorEvals.slice(1).forEach((ev) => {
        const item = document.createElement('div');
        item.className = 'ai-history-item';
        item.textContent = `Earlier review (${formatDateTime(ev.created_at)}): ${ev.verdict} · grade ${ev.grade}`;
        historyEl.appendChild(item);
      });
    }
  }

  function renderVerdict(evaluation) {
    const resultEl = document.getElementById('ai-result');
    const verdictClass = evaluation.verdict === 'good' ? 'verdict-good' : evaluation.verdict === 'bad' ? 'verdict-bad' : 'verdict-neutral';
    const reasoning = evaluation.reasoning || {};
    const observations = (reasoning.key_observations || []).map((o) => `<li>${escapeHtml(o)}</li>`).join('');
    resultEl.innerHTML = `
      <div class="verdict-card ${verdictClass}">
        <div class="verdict-top">
          <span class="verdict-label">${escapeHtml(evaluation.verdict || 'reviewed')}</span>
          <span class="verdict-grade">${escapeHtml(evaluation.grade || '—')}</span>
        </div>
        <p class="verdict-reasoning">${escapeHtml(reasoning.summary || '')}</p>
        ${observations ? `<ul class="verdict-observations">${observations}</ul>` : ''}
      </div>
    `;
    resultEl.classList.remove('hidden');
    if (evaluation.verdict === 'good') burstConfetti();
  }

  async function handleAnalyze() {
    const tradeId = state.detailTradeId;
    document.getElementById('ai-idle').classList.add('hidden');
    document.getElementById('ai-result').classList.add('hidden');
    document.getElementById('ai-thinking').classList.remove('hidden');

    const startedAt = Date.now();
    const elapsedEl = document.getElementById('ai-elapsed');
    state.aiTimer = setInterval(() => {
      const secs = Math.floor((Date.now() - startedAt) / 1000);
      elapsedEl.textContent = ` (${secs}s)`;
    }, 1000);

    try {
      const evaluation = await api('POST', `/trades/${tradeId}/analyze`, undefined);
      clearInterval(state.aiTimer);
      document.getElementById('ai-thinking').classList.add('hidden');
      renderVerdict(evaluation);
      loadDashboard();
    } catch (e) {
      clearInterval(state.aiTimer);
      document.getElementById('ai-thinking').classList.add('hidden');
      document.getElementById('ai-idle').classList.remove('hidden');
      toast(`AI mentor unavailable: ${e.message}`, 'error');
    }
  }

  // ---------- Screenshot upload ----------
  function setupDropzone() {
    const dropzone = document.getElementById('det-dropzone');
    const fileInput = document.getElementById('det-file-input');

    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) uploadScreenshot(fileInput.files[0]);
    });
    ['dragover', 'dragenter'].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add('drag-over'); })
    );
    ['dragleave', 'drop'].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove('drag-over'); })
    );
    dropzone.addEventListener('drop', (e) => {
      const file = e.dataTransfer.files[0];
      if (file) uploadScreenshot(file);
    });
  }

  async function uploadScreenshot(file) {
    const tradeId = state.detailTradeId;
    try {
      await apiUpload(`/trades/${tradeId}/screenshot`, file);
      const trade = await api('GET', `/trades/${tradeId}`);
      renderDetail(trade);
      toast('Screenshot attached');
    } catch (e) {
      toast(`Upload failed: ${e.message}`, 'error');
    }
  }

  // ---------- Tiny confetti (no external deps) ----------
  function burstConfetti() {
    const colors = ['#22e58a', '#22d3ee', '#7c5cff', '#ffb454'];
    for (let i = 0; i < 26; i++) {
      const piece = document.createElement('div');
      const size = 6 + Math.random() * 6;
      const startX = window.innerWidth / 2 + (Math.random() - 0.5) * 200;
      piece.style.cssText = `
        position:fixed; top:30%; left:${startX}px; width:${size}px; height:${size}px;
        background:${colors[i % colors.length]}; z-index:80; pointer-events:none;
        border-radius:${Math.random() > 0.5 ? '50%' : '2px'};
        opacity:0.95;
      `;
      document.body.appendChild(piece);
      const dx = (Math.random() - 0.5) * 500;
      const dy = 300 + Math.random() * 300;
      const rot = Math.random() * 720 - 360;
      const anim = piece.animate(
        [
          { transform: 'translate(0,0) rotate(0deg)', opacity: 1 },
          { transform: `translate(${dx}px, ${dy}px) rotate(${rot}deg)`, opacity: 0 },
        ],
        { duration: 1100 + Math.random() * 500, easing: 'cubic-bezier(.16,.6,.4,1)' }
      );
      anim.onfinish = () => piece.remove();
    }
  }

  // ---------- Chat Assistant ----------
  function scrollChatToBottom() {
    const el = document.getElementById('chat-messages');
    el.scrollTop = el.scrollHeight;
  }

  function appendChatMessage(role, text) {
    const el = document.getElementById('chat-messages');
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role}`;
    bubble.textContent = text;
    el.appendChild(bubble);
    scrollChatToBottom();
    return bubble;
  }

  function openChatPanel() {
    document.getElementById('chat-panel').classList.remove('hidden');
    if (!state.chatHistory.length) {
      appendChatMessage(
        'assistant',
        "Hi! Ask me about a specific trade, what the AI mentor said, a live price, or anything trading-related — I can see your journal."
      );
    }
    document.getElementById('chat-input').focus();
  }

  function closeChatPanel() {
    document.getElementById('chat-panel').classList.add('hidden');
  }

  async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    appendChatMessage('user', message);

    const historyForRequest = state.chatHistory.slice(-10);
    state.chatHistory.push({ role: 'user', content: message });

    const sendBtn = document.getElementById('chat-send');
    sendBtn.disabled = true;
    const thinkingBubble = appendChatMessage('assistant', 'Thinking…');
    thinkingBubble.classList.add('chat-thinking');

    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/chat`, {
        message,
        history: historyForRequest,
      });
      thinkingBubble.remove();
      appendChatMessage('assistant', res.reply);
      state.chatHistory.push({ role: 'assistant', content: res.reply });
    } catch (e) {
      thinkingBubble.remove();
      appendChatMessage('assistant', `Sorry, I couldn't reach the local AI: ${e.message}`);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  document.getElementById('btn-chat-toggle').addEventListener('click', openChatPanel);
  document.getElementById('btn-chat-close').addEventListener('click', closeChatPanel);
  document.getElementById('chat-send').addEventListener('click', sendChatMessage);
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendChatMessage();
  });

  // ---------- Textbook ----------
  const TB_CHAPTERS = [
    { id: 'basics', icon: '📖', title: 'Trading Basics', file: '01-basics.html' },
    { id: 'terminology', icon: '🔤', title: 'Key Terminology', file: '02-terminology.html' },
    { id: 'tools', icon: '🛠️', title: 'Tools & Order Types', file: '03-tools.html' },
    { id: 'reading-charts', icon: '📊', title: 'Reading Charts', file: '04-reading-charts.html' },
    { id: 'patterns', icon: '📐', title: 'Chart Patterns', file: '05-patterns.html' },
    { id: 'support-resistance', icon: '📏', title: 'Support & Resistance', file: '06-support-resistance.html' },
    { id: 'price-action', icon: '🕯️', title: 'Price Action', file: '07-price-action.html' },
    { id: 'smart-money', icon: '🧠', title: 'Smart Money Concepts', file: '08-smart-money.html' },
    { id: 'risk-management', icon: '🛡️', title: 'Risk Management', file: '09-risk-management.html' },
  ];
  let tbCurrentChapter = null;
  const tbCache = {};

  function renderTbNav() {
    const nav = document.getElementById('tb-nav');
    nav.innerHTML = TB_CHAPTERS.map(
      (c) => `<div class="tb-nav-item" data-id="${c.id}"><span class="tb-nav-icon">${c.icon}</span>${escapeHtml(c.title)}</div>`
    ).join('');
    nav.querySelectorAll('.tb-nav-item').forEach((el) => {
      el.addEventListener('click', () => loadTbChapter(el.dataset.id));
    });
  }

  async function loadTbChapter(id) {
    const index = TB_CHAPTERS.findIndex((c) => c.id === id);
    if (index === -1) return;
    tbCurrentChapter = id;

    document.querySelectorAll('.tb-nav-item').forEach((el) => el.classList.toggle('active', el.dataset.id === id));

    const contentEl = document.getElementById('tb-content');
    contentEl.innerHTML = '<div class="tb-loading">Loading…</div>';
    contentEl.scrollTop = 0;

    try {
      let html = tbCache[id];
      if (!html) {
        const res = await fetch(`/textbook/${TB_CHAPTERS[index].file}`);
        if (!res.ok) throw new Error('Chapter not found');
        html = await res.text();
        tbCache[id] = html;
      }

      const prev = TB_CHAPTERS[index - 1];
      const next = TB_CHAPTERS[index + 1];
      const navHtml = `
        <div class="tb-chapter-nav">
          ${prev ? `<button class="btn btn-ghost" data-nav="${prev.id}">← ${escapeHtml(prev.title)}</button>` : '<span></span>'}
          ${next ? `<button class="btn btn-ghost" data-nav="${next.id}">${escapeHtml(next.title)} →</button>` : '<span></span>'}
        </div>
      `;
      contentEl.innerHTML = html + navHtml;
      contentEl.querySelectorAll('[data-nav]').forEach((btn) => {
        btn.addEventListener('click', () => loadTbChapter(btn.dataset.nav));
      });
    } catch (e) {
      contentEl.innerHTML = `<div class="tb-loading">Couldn't load this chapter: ${escapeHtml(e.message)}</div>`;
    }
  }

  // Same category-to-chapter mapping trading_profile.generate_instant_insight
  // already uses to pick a trader's top tracked mistake — reused here so the
  // recommendation never disagrees with the Home/Analyze insight.
  const BT_LESSON_MAP = {
    poor_risk_reward: 'risk-management',
    oversizing: 'risk-management',
    overtrading: 'risk-management',
    revenge_trading: 'risk-management',
    stop_loss_modification: 'risk-management',
    early_exit: 'price-action',
    outside_preferred_hours: 'smart-money',
  };

  function renderNextLesson() {
    const bannerEl = document.getElementById('tb-next-lesson');
    const textEl = document.getElementById('tb-next-lesson-text');
    const btnEl = document.getElementById('tb-next-lesson-btn');
    const profile = state.tradingProfile;

    bannerEl.classList.remove('hidden');

    if (!profile || !profile.has_sufficient_data) {
      textEl.textContent = profile
        ? `Log at least ${profile.min_trades_required} closed trades to unlock a personalized lesson recommendation (${profile.trades_analyzed} so far).`
        : 'Log more trades to unlock a personalized lesson recommendation.';
      btnEl.classList.add('hidden');
      return;
    }

    const tracked = (profile.mistakes || []).filter((m) => m.status === 'tracked' && m.occurrences > 0);
    let chapterId = null;
    let prefix = '';
    if (tracked.length) {
      const top = tracked.reduce((a, b) => (a.occurrences > b.occurrences ? a : b));
      chapterId = BT_LESSON_MAP[top.category] || null;
      prefix = `Your recurring weakness: ${MISTAKE_LABELS[top.category] || top.category}. `;
    }

    if (!chapterId) {
      textEl.textContent = "No recurring weakness detected right now — here's a refresher anyway.";
      btnEl.classList.remove('hidden');
      btnEl.onclick = () => loadTbChapter('risk-management');
      return;
    }

    const chapter = TB_CHAPTERS.find((c) => c.id === chapterId);
    textEl.textContent = `${prefix}Recommended next: ${chapter ? chapter.title : 'a refresher chapter'}.`;
    btnEl.classList.remove('hidden');
    btnEl.onclick = () => loadTbChapter(chapterId);
  }

  function openLearnView() {
    showView('textbook');
    if (!document.getElementById('tb-nav').children.length) renderTbNav();
    loadTbChapter(tbCurrentChapter || TB_CHAPTERS[0].id);
    loadTradingProfile().then(renderNextLesson);
  }

  document.getElementById('btn-close-learn').addEventListener('click', () => navigateTo('home'));

  // ---------- Backtest / Replay ----------
  let btChart = null;
  let btCandleSeries = null;
  let btCandles = [];
  let btEntryIndex = -1;
  let btExitIndex = -1;
  let btReplayIndex = 0;
  let btTimer = null;
  let btCurrentTrade = null;
  let btInterval = null;
  let btRange = null;

  // Decision Training — mirrors app/services/decision_training.py's constants
  // and compute_decision_points exactly, so the client selects the same
  // decision points the server will grade against.
  const DT_DECISION_CADENCE_BARS = 10;
  const DT_MAX_DECISION_POINTS_PER_REPLAY = 5;
  const DT_EVAL_LOOKAHEAD_BARS = 5;
  const DT_FLAT_MOVE_THRESHOLD_PCT = 0.05;

  function computeDecisionPoints(startIndex, totalCandles) {
    const lastValidIndex = totalCandles - DT_EVAL_LOOKAHEAD_BARS - 1;
    const points = [];
    let offset = DT_DECISION_CADENCE_BARS;
    while (startIndex + offset <= lastValidIndex && points.length < DT_MAX_DECISION_POINTS_PER_REPLAY) {
      points.push(startIndex + offset);
      offset += DT_DECISION_CADENCE_BARS;
    }
    return points;
  }

  function isPendingDecisionPoint(index) {
    return state.dtEnabled && state.dtDecisionIndices.includes(index) && !state.dtAnsweredIndices.has(index);
  }

  function showDecisionPrompt(index) {
    state.dtPendingIndex = index;
    document.getElementById('bt-decision-prompt').classList.remove('hidden');
    document.getElementById('bt-decision-feedback').classList.add('hidden');
    document.getElementById('bt-decision-feedback').textContent = '';
    document.querySelectorAll('.bt-guess-btn').forEach((b) => { b.disabled = false; });
    document.getElementById('bt-mentor-btn').disabled = true;
    document.getElementById('bt-dt-toggle').disabled = true;
    document.getElementById('bt-step-fwd').disabled = true;
    document.getElementById('bt-scrubber').disabled = true;
  }

  function hideDecisionPrompt() {
    state.dtPendingIndex = null;
    document.getElementById('bt-decision-prompt').classList.add('hidden');
    document.getElementById('bt-mentor-btn').disabled = false;
    document.getElementById('bt-dt-toggle').disabled = false;
    document.getElementById('bt-step-fwd').disabled = false;
    document.getElementById('bt-scrubber').disabled = false;
  }

  async function loadDecisionTrainingSummary() {
    try {
      state.dtSummary = await api('GET', `/portfolios/${state.portfolioId}/decision-training/summary`);
    } catch (_) {
      state.dtSummary = null;
    }
    renderDtAccuracyChip();
  }

  function renderDtAccuracyChip() {
    const el = document.getElementById('bt-dt-accuracy');
    const s = state.dtSummary;
    if (!s) { el.textContent = ''; return; }
    el.textContent = s.has_sufficient_data
      ? `${s.accuracy_pct}% accuracy over ${s.total_attempts} attempts`
      : `${s.total_attempts} attempt(s) so far — ${s.min_attempts_required} needed to unlock accuracy`;
  }

  document.getElementById('bt-dt-toggle').addEventListener('change', (e) => {
    state.dtEnabled = e.target.checked;
  });

  document.getElementById('bt-decision-prompt').addEventListener('click', async (e) => {
    const btn = e.target.closest('.bt-guess-btn');
    if (!btn || state.dtPendingIndex === null) return;
    const guess = btn.dataset.guess;
    const index = state.dtPendingIndex;

    document.querySelectorAll('.bt-guess-btn').forEach((b) => { b.disabled = true; });

    // Instant client-side preview from the candles already loaded — mirrors
    // grade_attempt's logic exactly, but the persisted stat below is always
    // the server's own re-graded value, never this preview.
    const feedbackEl = document.getElementById('bt-decision-feedback');
    const lookaheadIndex = index + DT_EVAL_LOOKAHEAD_BARS;
    let previewOutcome = 'inconclusive';
    if (lookaheadIndex < btCandles.length) {
      const decisionClose = btCandles[index].close;
      const moveP = decisionClose ? ((btCandles[lookaheadIndex].close - decisionClose) / decisionClose) * 100 : 0;
      if (Math.abs(moveP) < DT_FLAT_MOVE_THRESHOLD_PCT) previewOutcome = guess === 'wait' ? 'correct' : 'incorrect';
      else if (moveP > 0) previewOutcome = guess === 'buy' ? 'correct' : 'incorrect';
      else previewOutcome = guess === 'sell' ? 'correct' : 'incorrect';
    }
    feedbackEl.className = `bt-decision-feedback ${previewOutcome}`;
    feedbackEl.textContent =
      previewOutcome === 'correct' ? '✓ Correct!'
      : previewOutcome === 'incorrect' ? '✗ Not quite.'
      : 'Not enough future candles to grade this one.';
    feedbackEl.classList.remove('hidden');

    try {
      await api('POST', '/decision-training/attempts', {
        portfolio_id: state.portfolioId,
        trade_id: btCurrentTrade.id,
        symbol: btCurrentTrade.symbol,
        interval: btInterval,
        range: btRange,
        decision_candle_time: btCandles[index].time,
        guess,
      });
      loadDecisionTrainingSummary();
    } catch (err) {
      toast(`Couldn't save decision-training attempt: ${err.message}`, 'error');
    }

    state.dtAnsweredIndices.add(index);
    setTimeout(() => {
      hideDecisionPrompt();
      btReplayIndex = Math.min(btCandles.length - 1, index + DT_EVAL_LOOKAHEAD_BARS);
      renderBtFrame();
    }, 1400);
  });

  function ensureBtChart() {
    if (btChart) return btChart;
    const container = document.getElementById('bt-chart-container');
    btChart = LightweightCharts.createChart(container, {
      layout: { background: { color: 'transparent' }, textColor: 'rgba(255,255,255,0.55)', attributionLogo: false },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
      timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true },
    });
    btCandleSeries = btChart.addCandlestickSeries({
      upColor: '#22e58a', downColor: '#ff5c7c',
      borderUpColor: '#22e58a', borderDownColor: '#ff5c7c',
      wickUpColor: '#22e58a', wickDownColor: '#ff5c7c',
    });
    new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      btChart.resize(width, height);
    }).observe(container);
    return btChart;
  }

  function pickReplayParams(trade) {
    const ageDays = (Date.now() - new Date(trade.open_time).getTime()) / 86400000;
    if (ageDays <= 5) return { interval: '5m', range: '5d' };
    if (ageDays <= 30) return { interval: '1h', range: '1mo' };
    if (ageDays <= 85) return { interval: '1d', range: '3mo' };
    return { interval: '1d', range: '1y' };
  }

  function closestCandleIndex(candles, targetSeconds) {
    let bestIdx = 0;
    let bestDiff = Infinity;
    candles.forEach((c, i) => {
      const diff = Math.abs(c.time - targetSeconds);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = i;
      }
    });
    return bestIdx;
  }

  function stopBtPlayback() {
    if (btTimer) {
      clearInterval(btTimer);
      btTimer = null;
    }
    document.getElementById('bt-play-pause').textContent = '▶';
  }

  function btMarkersUpTo(index) {
    const markers = [];
    if (btEntryIndex >= 0 && btEntryIndex <= index) {
      markers.push({
        time: btCandles[btEntryIndex].time,
        position: btCurrentTrade.direction === 'buy' ? 'belowBar' : 'aboveBar',
        color: btCurrentTrade.direction === 'buy' ? '#22e58a' : '#ff5c7c',
        shape: btCurrentTrade.direction === 'buy' ? 'arrowUp' : 'arrowDown',
        text: `${btCurrentTrade.direction === 'buy' ? 'Buy' : 'Sell'} @ ${btCurrentTrade.open_price}`,
      });
    }
    if (btExitIndex >= 0 && btExitIndex <= index) {
      markers.push({
        time: btCandles[btExitIndex].time,
        position: btCurrentTrade.direction === 'buy' ? 'aboveBar' : 'belowBar',
        color: '#ffb454',
        shape: 'circle',
        text: `Close @ ${btCurrentTrade.close_price}`,
      });
    }
    return markers.sort((a, b) => a.time - b.time);
  }

  function renderBtFrame() {
    const slice = btCandles.slice(0, btReplayIndex + 1);
    btCandleSeries.setData(slice);
    btCandleSeries.setMarkers(btMarkersUpTo(btReplayIndex));
    btChart.timeScale().fitContent();
    document.getElementById('bt-scrubber').value = btReplayIndex;
    const label = new Date(btCandles[btReplayIndex].time * 1000);
    document.getElementById('bt-time-label').textContent = label.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function toggleBtPlayback() {
    if (btTimer) {
      stopBtPlayback();
      return;
    }
    if (state.dtPendingIndex !== null) return;
    const speed = parseInt(document.getElementById('bt-speed').value, 10);
    document.getElementById('bt-play-pause').textContent = '⏸';
    btTimer = setInterval(() => {
      if (btReplayIndex >= btCandles.length - 1) {
        stopBtPlayback();
        return;
      }
      const next = btReplayIndex + 1;
      btReplayIndex = next;
      renderBtFrame();
      if (isPendingDecisionPoint(next)) {
        stopBtPlayback();
        showDecisionPrompt(next);
      }
    }, speed);
  }

  function stepBt(delta) {
    if (state.dtPendingIndex !== null) return;
    stopBtPlayback();
    const target = Math.min(btCandles.length - 1, Math.max(0, btReplayIndex + delta));
    btReplayIndex = target;
    renderBtFrame();
    if (delta > 0 && isPendingDecisionPoint(target)) {
      showDecisionPrompt(target);
    }
  }

  function renderBtTradeInfo(trade) {
    const grade = latestEvaluation(trade, 'rules_engine');
    const el = document.getElementById('bt-trade-info');
    el.innerHTML = `
      <div class="dir-pill ${trade.direction}">${trade.direction.toUpperCase()}</div>
      <div class="bt-info-main">
        <div class="bt-info-symbol">${escapeHtml(trade.symbol)} ${trade.source === 'mt5' ? '<span class="bt-source-tag">MT5</span>' : ''}</div>
        <div class="bt-info-sub">${trade.volume} lots · opened ${formatDateTime(trade.open_time)} @ ${trade.open_price}${
          trade.status === 'closed' ? ` · closed ${formatDateTime(trade.close_time)} @ ${trade.close_price}` : ' · still open'
        }</div>
      </div>
      ${trade.status === 'closed' ? `<div class="bt-info-pnl ${trade.profit > 0 ? 'pos' : trade.profit < 0 ? 'neg' : ''}">${formatMoney(trade.profit)}</div>` : ''}
      ${grade ? `<div class="grade-chip ${gradeClass(grade.grade)}">${grade.grade}</div>` : ''}
    `;
  }

  async function loadBacktestTrade(trade) {
    btCurrentTrade = trade;
    stopBtPlayback();
    document.getElementById('bt-empty').classList.add('hidden');
    document.getElementById('bt-player').classList.remove('hidden');
    document.querySelectorAll('.bt-trade-item').forEach((el) => {
      el.classList.toggle('active', Number(el.dataset.id) === trade.id);
    });

    clearInterval(state.btMentorTimer);
    document.getElementById('bt-mentor-thinking').classList.add('hidden');
    document.getElementById('bt-mentor-result').classList.add('hidden');
    document.getElementById('bt-mentor-result').textContent = '';
    document.getElementById('bt-mentor-btn').disabled = false;

    populateBtJournal(trade.backtest_journal);
    document.getElementById('bt-journal-status').classList.remove('visible');

    renderBtTradeInfo(trade);
    ensureBtChart();
    btCandleSeries.setData([]);
    btCandleSeries.setMarkers([]);

    hideDecisionPrompt();
    state.dtDecisionIndices = [];
    state.dtAnsweredIndices = new Set();

    const { interval, range } = pickReplayParams(trade);
    btInterval = interval;
    btRange = range;
    try {
      const candles = await api('GET', `/market/candles/${encodeURIComponent(trade.symbol)}?interval=${interval}&range=${range}`);
      if (!candles.length) throw new Error('No candle data available for this window');
      btCandles = candles;

      const openSeconds = Math.floor(new Date(trade.open_time).getTime() / 1000);
      btEntryIndex = closestCandleIndex(btCandles, openSeconds);
      btExitIndex = trade.close_time ? closestCandleIndex(btCandles, Math.floor(new Date(trade.close_time).getTime() / 1000)) : -1;

      btReplayIndex = Math.max(0, btEntryIndex - 15);
      state.dtDecisionIndices = computeDecisionPoints(btReplayIndex, btCandles.length);
      const scrubber = document.getElementById('bt-scrubber');
      scrubber.max = btCandles.length - 1;
      renderBtFrame();
    } catch (e) {
      toast(`Couldn't load replay data: ${e.message}`, 'error');
    }
  }

  function renderBtTradeList() {
    const listEl = document.getElementById('bt-trade-list');
    if (!state.trades.length) {
      listEl.innerHTML = '<div class="tb-loading">No trades logged yet.</div>';
      return;
    }
    listEl.innerHTML = state.trades
      .map(
        (t) => `
      <div class="bt-trade-item" data-id="${t.id}">
        <div class="dir-pill ${t.direction}">${t.direction.toUpperCase()}</div>
        <div class="bt-trade-item-main">
          <div class="bt-trade-item-symbol">${escapeHtml(t.symbol)} ${t.source === 'mt5' ? '<span class="bt-source-tag">MT5</span>' : ''}</div>
          <div class="bt-trade-item-sub">${formatDateTime(t.open_time)}</div>
        </div>
        <div class="bt-trade-item-status">${t.status}</div>
      </div>
    `
      )
      .join('');
    listEl.querySelectorAll('.bt-trade-item').forEach((el) => {
      el.addEventListener('click', () => {
        const trade = state.trades.find((t) => t.id === Number(el.dataset.id));
        if (trade) loadBacktestTrade(trade);
      });
    });
  }

  async function requestReplayFeedback() {
    if (!btCurrentTrade) return;
    const btn = document.getElementById('bt-mentor-btn');
    const thinkingEl = document.getElementById('bt-mentor-thinking');
    const resultEl = document.getElementById('bt-mentor-result');

    btn.disabled = true;
    resultEl.classList.add('hidden');
    thinkingEl.classList.remove('hidden');

    const startedAt = Date.now();
    const elapsedEl = document.getElementById('bt-mentor-elapsed');
    state.btMentorTimer = setInterval(() => {
      elapsedEl.textContent = ` (${Math.floor((Date.now() - startedAt) / 1000)}s)`;
    }, 1000);

    try {
      const res = await api('POST', `/trades/${btCurrentTrade.id}/replay-feedback`, undefined);
      clearInterval(state.btMentorTimer);
      thinkingEl.classList.add('hidden');
      resultEl.textContent = res.feedback;
      resultEl.classList.remove('hidden');
    } catch (e) {
      clearInterval(state.btMentorTimer);
      thinkingEl.classList.add('hidden');
      toast(`AI mentor unavailable: ${e.message}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('bt-mentor-btn').addEventListener('click', requestReplayFeedback);

  // ---------- Backtest Trade Journal ----------
  const BT_JOURNAL_FIELDS = {
    what_worked: 'bt-journal-what-worked',
    what_to_improve: 'bt-journal-what-to-improve',
    pattern_recognized: 'bt-journal-pattern',
    lesson: 'bt-journal-lesson',
    notes: 'bt-journal-notes',
  };

  // Shared with the standalone Journal view (renderJournalList), which
  // generates its own per-trade textarea ids from the same field keys.
  const JOURNAL_PROMPTS = {
    what_worked: 'What did you do well in this trade?',
    what_to_improve: 'What would you do differently next time?',
    pattern_recognized: 'What pattern, level, or concept did you recognize (or miss)?',
    lesson: 'Key takeaway / lesson learned',
    notes: 'Anything else',
  };

  function populateBtJournal(journal) {
    Object.entries(BT_JOURNAL_FIELDS).forEach(([key, id]) => {
      document.getElementById(id).value = (journal && journal[key]) || '';
    });
  }

  async function saveBtJournal() {
    if (!btCurrentTrade) return;
    const payload = {};
    Object.entries(BT_JOURNAL_FIELDS).forEach(([key, id]) => {
      payload[key] = document.getElementById(id).value.trim() || null;
    });

    const btn = document.getElementById('bt-journal-save');
    const statusEl = document.getElementById('bt-journal-status');
    btn.disabled = true;
    try {
      const trade = await api('PUT', `/trades/${btCurrentTrade.id}/journal`, payload);
      btCurrentTrade.backtest_journal = trade.backtest_journal;
      const cached = state.trades.find((t) => t.id === trade.id);
      if (cached) cached.backtest_journal = trade.backtest_journal;

      statusEl.textContent = '✓ Saved';
      statusEl.classList.add('visible');
      setTimeout(() => statusEl.classList.remove('visible'), 2500);
    } catch (e) {
      toast(`Couldn't save journal: ${e.message}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('bt-journal-save').addEventListener('click', saveBtJournal);

  function openBacktestView() {
    showView('backtest');
    document.getElementById('bt-empty').classList.remove('hidden');
    document.getElementById('bt-player').classList.add('hidden');
    renderBtTradeList();
    loadDecisionTrainingSummary();
  }

  document.getElementById('btn-close-backtest').addEventListener('click', () => navigateTo('home'));
  document.getElementById('bt-play-pause').addEventListener('click', toggleBtPlayback);
  document.getElementById('bt-step-back').addEventListener('click', () => stepBt(-1));
  document.getElementById('bt-step-fwd').addEventListener('click', () => stepBt(1));
  document.getElementById('bt-scrubber').addEventListener('input', (e) => {
    if (state.dtPendingIndex !== null) return;
    stopBtPlayback();
    btReplayIndex = Number(e.target.value);
    renderBtFrame();
  });

  // ---------- Backtest mode switch (Decision Training vs Strategy Lab) ----------
  let slListLoaded = false;

  document.getElementById('bt-mode-switch').addEventListener('click', (e) => {
    const btn = e.target.closest('.bt-mode-btn');
    if (!btn) return;
    const mode = btn.dataset.mode;
    stopBtPlayback();
    document.querySelectorAll('.bt-mode-btn').forEach((b) => b.classList.toggle('active', b === btn));
    document.getElementById('bt-mode-replay-panel').classList.toggle('hidden', mode !== 'replay');
    document.getElementById('bt-mode-strategylab-panel').classList.toggle('hidden', mode !== 'strategylab');
    if (mode === 'strategylab' && !slListLoaded) {
      slListLoaded = true;
      loadStrategies().then(renderStrategyList);
    }
  });

  // ---------- Strategy Lab ----------
  // Mirrors app/services/strategy_lab.py's ALLOWED_BACKTEST_WINDOWS exactly —
  // these are real, live-probed Yahoo Finance history-depth limits, not an
  // arbitrary UI choice.
  const SL_ALLOWED_WINDOWS = {
    '15m': ['5d', '1mo'],
    '1h': ['1mo', '3mo', '6mo', '1y', '2y'],
    '1d': ['3mo', '6mo', '1y', '2y', '5y', '10y'],
  };

  const SL_CONDITION_TYPES = {
    ema_cross: {
      label: 'EMA Cross',
      fields: [
        { key: 'fast_period', label: 'Fast EMA', min: 2, max: 100, placeholder: '9' },
        { key: 'slow_period', label: 'Slow EMA', min: 3, max: 300, placeholder: '21' },
        { key: 'cross_direction', label: 'Direction', select: [['up', 'Crosses Up'], ['down', 'Crosses Down']] },
      ],
    },
    rsi_threshold: {
      label: 'RSI Threshold',
      fields: [
        { key: 'rsi_period', label: 'RSI Period', min: 2, max: 50, placeholder: '14' },
        { key: 'rsi_comparison', label: 'Comparison', select: [['above', 'Above'], ['below', 'Below']] },
        { key: 'rsi_value', label: 'RSI Value', min: 0, max: 100, placeholder: '30' },
      ],
    },
    breakout: {
      label: 'Breakout',
      fields: [
        { key: 'breakout_lookback', label: 'Lookback Bars', min: 2, max: 500, placeholder: '20' },
        { key: 'breakout_direction', label: 'Direction', select: [['above_high', 'Above Prior High'], ['below_low', 'Below Prior Low']] },
      ],
    },
  };

  let slChart = null;
  let slSeries = null;
  let slCurrentStrategy = null;
  let slConditionSeq = 0;

  function slConditionFieldsHtml(type, existing = {}) {
    return SL_CONDITION_TYPES[type].fields
      .map((f) => {
        const val = existing[f.key] ?? '';
        if (f.select) {
          return `<label class="field"><span>${f.label}</span><select class="field-select" data-key="${f.key}">${f.select
            .map(([v, l]) => `<option value="${v}" ${v === val ? 'selected' : ''}>${l}</option>`)
            .join('')}</select></label>`;
        }
        return `<label class="field"><span>${f.label}</span><input type="number" data-key="${f.key}" min="${f.min}" max="${f.max}" placeholder="${f.placeholder}" value="${val}" /></label>`;
      })
      .join('');
  }

  function addConditionRow(existing = {}) {
    const container = document.getElementById('sl-conditions');
    if (container.children.length >= 3) {
      toast('A strategy supports at most 3 conditions', 'error');
      return;
    }
    const type = existing.type || 'ema_cross';
    const row = document.createElement('div');
    row.className = 'sl-condition-row';
    row.dataset.rowId = `sl-cond-${slConditionSeq++}`;
    row.innerHTML = `
      <select class="field-select sl-cond-type">
        ${Object.entries(SL_CONDITION_TYPES).map(([key, def]) => `<option value="${key}" ${key === type ? 'selected' : ''}>${def.label}</option>`).join('')}
      </select>
      <div class="sl-cond-fields">${slConditionFieldsHtml(type, existing)}</div>
      <button type="button" class="btn btn-ghost btn-sm sl-remove-condition">✕</button>
    `;
    container.appendChild(row);
    row.querySelector('.sl-cond-type').addEventListener('change', (e) => {
      row.querySelector('.sl-cond-fields').innerHTML = slConditionFieldsHtml(e.target.value);
    });
    row.querySelector('.sl-remove-condition').addEventListener('click', () => row.remove());
  }

  document.getElementById('sl-add-condition').addEventListener('click', () => addConditionRow());

  function collectConditions() {
    return Array.from(document.querySelectorAll('.sl-condition-row')).map((row) => {
      const condition = { type: row.querySelector('.sl-cond-type').value };
      row.querySelectorAll('[data-key]').forEach((input) => {
        const raw = input.value;
        condition[input.dataset.key] = input.tagName === 'SELECT' ? raw : (raw === '' ? null : Number(raw));
      });
      return condition;
    });
  }

  document.getElementById('sl-direction').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    document.querySelectorAll('#sl-direction .seg-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
  });

  function resetStrategyBuilder() {
    document.getElementById('sl-name').value = '';
    document.getElementById('sl-description').value = '';
    document.getElementById('sl-stop-loss-pct').value = '';
    document.getElementById('sl-target-r').value = '';
    document.getElementById('sl-builder-error').textContent = '';
    document.querySelectorAll('#sl-direction .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.value === 'buy'));
    document.getElementById('sl-conditions').innerHTML = '';
    addConditionRow();
  }

  function openStrategyBuilder() {
    resetStrategyBuilder();
    document.getElementById('sl-builder').classList.remove('hidden');
    document.getElementById('sl-backtest-panel').classList.add('hidden');
    document.getElementById('sl-empty').classList.add('hidden');
    document.querySelectorAll('.sl-strategy-item').forEach((el) => el.classList.remove('active'));
  }

  document.getElementById('sl-new-strategy-btn').addEventListener('click', openStrategyBuilder);
  document.getElementById('sl-builder-cancel').addEventListener('click', () => {
    document.getElementById('sl-builder').classList.add('hidden');
    if (!slCurrentStrategy) document.getElementById('sl-empty').classList.remove('hidden');
  });

  async function saveStrategyFromForm() {
    const name = document.getElementById('sl-name').value.trim();
    const errEl = document.getElementById('sl-builder-error');
    errEl.textContent = '';
    if (!name) { errEl.textContent = 'Enter a strategy name.'; return; }

    const direction = document.querySelector('#sl-direction .seg-btn.active').dataset.value;
    const conditions = collectConditions();
    if (!conditions.length) { errEl.textContent = 'Add at least one condition.'; return; }

    const stopLossPct = parseFloat(document.getElementById('sl-stop-loss-pct').value);
    const targetR = parseFloat(document.getElementById('sl-target-r').value);
    if (!stopLossPct || stopLossPct <= 0) { errEl.textContent = 'Enter a stop-loss % greater than 0.'; return; }
    if (!targetR || targetR <= 0) { errEl.textContent = 'Enter a target R multiple greater than 0.'; return; }

    const btn = document.getElementById('sl-save-strategy');
    btn.disabled = true;
    try {
      const strategy = await api('POST', '/strategies', {
        portfolio_id: state.portfolioId,
        name,
        description: document.getElementById('sl-description').value.trim() || null,
        direction,
        conditions,
        stop_loss_pct: stopLossPct,
        target_r: targetR,
      });
      state.strategies.unshift(strategy);
      renderStrategyList();
      document.getElementById('sl-builder').classList.add('hidden');
      selectStrategy(strategy);
      toast(`Strategy "${strategy.name}" saved`);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('sl-save-strategy').addEventListener('click', saveStrategyFromForm);

  async function loadStrategies() {
    try {
      state.strategies = await api('GET', `/portfolios/${state.portfolioId}/strategies`);
    } catch (_) {
      state.strategies = [];
    }
  }

  function renderStrategyList() {
    const container = document.getElementById('sl-strategies-container');
    if (!state.strategies.length) {
      container.innerHTML = '<div class="tb-loading">No strategies yet — create one to backtest it.</div>';
      return;
    }
    container.innerHTML = state.strategies
      .map(
        (s) => `
      <div class="sl-strategy-item ${slCurrentStrategy && slCurrentStrategy.id === s.id ? 'active' : ''}" data-id="${s.id}">
        <div>
          <div class="sl-strategy-item-name">${escapeHtml(s.name)}</div>
          <div class="sl-strategy-item-sub">${s.direction.toUpperCase()} · ${s.conditions.length} condition(s)</div>
        </div>
      </div>
    `
      )
      .join('');
    container.querySelectorAll('.sl-strategy-item').forEach((el) => {
      el.addEventListener('click', () => {
        const strategy = state.strategies.find((s) => s.id === Number(el.dataset.id));
        if (strategy) selectStrategy(strategy);
      });
    });
  }

  function populateSlRangeOptions() {
    const interval = document.getElementById('sl-bt-interval').value;
    document.getElementById('sl-bt-range').innerHTML = (SL_ALLOWED_WINDOWS[interval] || [])
      .map((r) => `<option value="${r}">${r}</option>`)
      .join('');
  }

  function selectStrategy(strategy) {
    slCurrentStrategy = strategy;
    document.querySelectorAll('.sl-strategy-item').forEach((el) => {
      el.classList.toggle('active', Number(el.dataset.id) === strategy.id);
    });
    document.getElementById('sl-builder').classList.add('hidden');
    document.getElementById('sl-empty').classList.add('hidden');
    document.getElementById('sl-backtest-panel').classList.remove('hidden');
    document.getElementById('sl-backtest-title').textContent = `Backtest: ${strategy.name}`;
    document.getElementById('sl-results').classList.add('hidden');
    document.getElementById('sl-backtest-error').textContent = '';

    const symbolInput = document.getElementById('sl-bt-symbol');
    if (!symbolInput.value) symbolInput.value = (state.trades[0] && state.trades[0].symbol) || 'EURUSD';

    const intervalSelect = document.getElementById('sl-bt-interval');
    if (!intervalSelect.children.length) {
      intervalSelect.innerHTML = Object.keys(SL_ALLOWED_WINDOWS).map((i) => `<option value="${i}">${i}</option>`).join('');
      intervalSelect.addEventListener('change', populateSlRangeOptions);
    }
    populateSlRangeOptions();
  }

  document.getElementById('sl-delete-strategy-btn').addEventListener('click', async () => {
    if (!slCurrentStrategy) return;
    const name = slCurrentStrategy.name;
    try {
      await api('DELETE', `/strategies/${slCurrentStrategy.id}`, undefined);
      state.strategies = state.strategies.filter((s) => s.id !== slCurrentStrategy.id);
      slCurrentStrategy = null;
      renderStrategyList();
      document.getElementById('sl-backtest-panel').classList.add('hidden');
      document.getElementById('sl-empty').classList.remove('hidden');
      toast(`Strategy "${name}" deleted`);
    } catch (e) {
      toast(`Couldn't delete strategy: ${e.message}`, 'error');
    }
  });

  function ensureSlChart() {
    if (slChart) return slChart;
    const container = document.getElementById('sl-equity-chart-container');
    slChart = LightweightCharts.createChart(container, {
      layout: { background: { color: 'transparent' }, textColor: 'rgba(255,255,255,0.55)', attributionLogo: false },
      grid: { vertLines: { color: 'rgba(255,255,255,0.04)' }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
      rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
      timeScale: { borderColor: 'rgba(255,255,255,0.08)', visible: false },
    });
    slSeries = slChart.addLineSeries({ color: '#7c5cff', lineWidth: 2 });
    new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      slChart.resize(width, height);
    }).observe(container);
    return slChart;
  }

  function renderBacktestResult(result) {
    document.getElementById('sl-results').classList.remove('hidden');

    const metricsEl = document.getElementById('sl-metrics');
    metricsEl.innerHTML = `
      <div class="sl-metric"><span class="sl-metric-label">Candles</span><span class="sl-metric-value">${result.candle_count}</span></div>
      <div class="sl-metric"><span class="sl-metric-label">Simulated Trades</span><span class="sl-metric-value">${result.trade_count}</span></div>
      <div class="sl-metric"><span class="sl-metric-label">Win Rate</span><span class="sl-metric-value">${result.win_rate_pct !== null ? result.win_rate_pct.toFixed(1) + '%' : '—'}</span></div>
      <div class="sl-metric"><span class="sl-metric-label">Profit Factor</span><span class="sl-metric-value">${result.profit_factor !== null ? result.profit_factor.toFixed(2) : '—'}</span></div>
      <div class="sl-metric"><span class="sl-metric-label">Expectancy</span><span class="sl-metric-value ${result.expectancy_r > 0 ? 'pos' : result.expectancy_r < 0 ? 'neg' : ''}">${result.expectancy_r !== null ? result.expectancy_r.toFixed(2) + 'R' : '—'}</span></div>
      <div class="sl-metric"><span class="sl-metric-label">Max Drawdown</span><span class="sl-metric-value">${result.max_drawdown_r !== null ? result.max_drawdown_r.toFixed(2) + 'R' : '—'}</span></div>
    `;
    if (result.note) {
      metricsEl.insertAdjacentHTML('beforeend', `<p class="sl-gated-note">${escapeHtml(result.note)}</p>`);
    }

    ensureSlChart();
    slSeries.setData(result.equity_curve.map((p) => ({ time: p.trade_index, value: p.cumulative_r })));
    slChart.timeScale().fitContent();

    document.getElementById('sl-trades-tbody').innerHTML =
      result.simulated_trades
        .map(
          (t) => `
      <tr>
        <td>${new Date(t.entry_time * 1000).toLocaleDateString()} @ ${t.entry_price.toFixed(5)}</td>
        <td>${new Date(t.exit_time * 1000).toLocaleDateString()} @ ${t.exit_price.toFixed(5)}</td>
        <td>${t.bars_held}</td>
        <td>${t.exit_reason}</td>
        <td class="${t.r_multiple >= 0 ? 'pos' : 'neg'}">${t.r_multiple.toFixed(2)}R</td>
      </tr>
    `
        )
        .join('') || '<tr><td colspan="5">No simulated trades in this window.</td></tr>';

    document.getElementById('sl-disclaimer').textContent =
      `${result.disclaimer} Fetched ${result.candle_count} ${result.interval} candles over ${result.range_}.`;
  }

  async function runStrategyBacktest() {
    if (!slCurrentStrategy) return;
    const symbol = document.getElementById('sl-bt-symbol').value.trim();
    const interval = document.getElementById('sl-bt-interval').value;
    const range = document.getElementById('sl-bt-range').value;
    const maxHoldingBars = parseInt(document.getElementById('sl-bt-max-bars').value, 10) || 48;
    const errEl = document.getElementById('sl-backtest-error');
    errEl.textContent = '';
    if (!symbol) { errEl.textContent = 'Enter a symbol.'; return; }

    const btn = document.getElementById('sl-run-backtest');
    const loadingEl = document.getElementById('sl-backtest-loading');
    btn.disabled = true;
    document.getElementById('sl-results').classList.add('hidden');
    loadingEl.classList.remove('hidden');

    try {
      const result = await api('POST', `/strategies/${slCurrentStrategy.id}/backtest`, {
        symbol,
        interval,
        range,
        max_holding_bars: maxHoldingBars,
      });
      renderBacktestResult(result);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      loadingEl.classList.add('hidden');
      btn.disabled = false;
    }
  }

  document.getElementById('sl-run-backtest').addEventListener('click', runStrategyBacktest);

  // ---------- Setups (tagging trades for Trading DNA) ----------
  async function loadSetups() {
    try {
      state.setups = await api('GET', `/portfolios/${state.portfolioId}/setups`);
    } catch (_) {
      state.setups = [];
    }
  }

  function populateSetupSelect(selectEl, currentId) {
    const options = ['<option value="">No setup tagged</option>'].concat(
      state.setups.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
    );
    selectEl.innerHTML = options.join('');
    selectEl.value = currentId ? String(currentId) : '';
  }

  async function createSetup(name) {
    const setup = await api('POST', '/setups', { portfolio_id: state.portfolioId, name });
    state.setups.push(setup);
    return setup;
  }

  document.getElementById('det-setup-select').addEventListener('change', async (e) => {
    const tradeId = state.detailTradeId;
    const value = e.target.value;
    try {
      const trade = await api('PUT', `/trades/${tradeId}/setup`, { setup_id: value ? Number(value) : null });
      const cached = state.trades.find((t) => t.id === trade.id);
      if (cached) {
        cached.setup_id = trade.setup_id;
        cached.setup_name = trade.setup_name;
      }
      toast(trade.setup_name ? `Tagged as ${trade.setup_name}` : 'Setup tag cleared');
      loadTradingProfile({ force: true }).then(() => {
        renderHomeSummary();
        if (!document.getElementById('view-analyze').classList.contains('hidden')) renderAnalyzeView();
      });
    } catch (err) {
      toast(`Couldn't update setup: ${err.message}`, 'error');
    }
  });

  document.getElementById('det-setup-new-btn').addEventListener('click', () => {
    document.getElementById('det-setup-new-row').classList.remove('hidden');
    document.getElementById('det-setup-new-name').focus();
  });

  document.getElementById('det-setup-new-save').addEventListener('click', async () => {
    const name = document.getElementById('det-setup-new-name').value.trim();
    if (!name) return;
    try {
      const setup = await createSetup(name);
      populateSetupSelect(document.getElementById('det-setup-select'), setup.id);
      document.getElementById('det-setup-select').dispatchEvent(new Event('change'));
      document.getElementById('det-setup-new-row').classList.add('hidden');
    } catch (err) {
      toast(`Couldn't create setup: ${err.message}`, 'error');
    }
  });

  // ---------- Trade Similarity ("Find Similar Trades") ----------
  function renderSimilarTradesResult(result) {
    const el = document.getElementById('det-similar-result');
    const splitHtml = `
      <div class="similar-split">
        <span class="similar-stat pos">${result.winners} winners</span>
        <span class="similar-stat neg">${result.losers} losers</span>
        ${result.win_rate_pct !== null ? `<span class="similar-stat">${result.win_rate_pct.toFixed(0)}% win rate</span>` : ''}
        ${result.avg_realized_r !== null ? `<span class="similar-stat">${result.avg_realized_r.toFixed(1)}R avg</span>` : ''}
      </div>
    `;
    const conditionsHtml = result.common_conditions.length
      ? `<ul class="similar-conditions">${result.common_conditions.map((c) => `<li>${escapeHtml(c)}</li>`).join('')}</ul>`
      : result.note
        ? `<p class="dna-empty">${escapeHtml(result.note)}</p>`
        : '';
    const tradesHtml = result.matched_trades
      .map(
        (t) => `<div class="mistake-trade-row" data-id="${t.trade_id}"><span class="dir-pill ${t.direction}">${t.direction.toUpperCase()}</span>${escapeHtml(t.symbol)} · ${formatDateTime(t.open_time)} · ${t.status === 'closed' ? formatMoney(t.profit) : 'open'}</div>`
      )
      .join('');

    el.innerHTML = `
      <p class="similar-count">You've taken <strong>${result.total_matched}</strong> similar trade(s) before.</p>
      ${splitHtml}
      ${conditionsHtml}
      <div class="similar-trades-list">${tradesHtml}</div>
    `;
    el.querySelectorAll('.mistake-trade-row').forEach((row) => {
      row.addEventListener('click', () => openDetail(Number(row.dataset.id)));
    });
    el.classList.remove('hidden');
  }

  async function findSimilarTrades() {
    const tradeId = state.detailTradeId;
    const btn = document.getElementById('det-similar-btn');
    btn.disabled = true;
    btn.textContent = 'Searching…';
    try {
      const result = await api('GET', `/trades/${tradeId}/similar`);
      renderSimilarTradesResult(result);
    } catch (e) {
      toast(`Couldn't find similar trades: ${e.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '🔍 Find Similar Trades';
    }
  }

  document.getElementById('det-similar-btn').addEventListener('click', findSimilarTrades);

  // ---------- Check My Trade (setup-quality check) ----------
  let lastSetupCheckPayload = null;

  function openSetupCheckModal() {
    document.getElementById('sc-symbol').value = '';
    document.getElementById('sc-entry').value = '';
    document.getElementById('sc-size').value = '';
    document.getElementById('sc-sl').value = '';
    document.getElementById('sc-tp').value = '';
    document.getElementById('sc-timeframe').value = '';
    document.getElementById('sc-reason').value = '';
    document.getElementById('sc-confirmation').value = '';
    document.getElementById('sc-error').textContent = '';
    document.getElementById('sc-result').classList.add('hidden');
    document.getElementById('sc-regime-badge').classList.add('hidden');
    document.getElementById('sc-regime-badge').innerHTML = '';
    document.querySelectorAll('#sc-direction .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.value === 'buy'));
    populateSetupSelect(document.getElementById('sc-setup-select'), null);
    openModal('modal-setup-check');
  }

  // ---------- Market Regime (live, on-demand) ----------
  async function checkMarketRegime() {
    const symbol = document.getElementById('sc-symbol').value.trim().toUpperCase();
    const badge = document.getElementById('sc-regime-badge');
    if (!symbol) {
      toast('Enter a symbol first.', 'error');
      return;
    }
    badge.classList.remove('hidden');
    badge.innerHTML = '<span class="regime-loading">Checking current regime…</span>';
    try {
      const regime = await api('GET', `/market/regime/${encodeURIComponent(symbol)}`);
      if (!regime.has_sufficient_data) {
        badge.innerHTML = `<span class="regime-loading">${escapeHtml(regime.note || 'Not enough data.')}</span>`;
        return;
      }
      badge.innerHTML = `
        <span class="regime-pill regime-trend-${regime.trend}">${escapeHtml(regime.label)}</span>
        <span class="regime-disclaimer">${escapeHtml(regime.disclaimer)}</span>
      `;
    } catch (e) {
      badge.innerHTML = `<span class="regime-loading">Couldn't check regime: ${escapeHtml(e.message)}</span>`;
    }
  }

  document.getElementById('sc-regime-btn').addEventListener('click', checkMarketRegime);

  document.getElementById('sc-direction').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    document.querySelectorAll('#sc-direction .seg-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
  });

  function renderRiskAssessment(risk) {
    if (!risk) return '';
    const rows = [
      ['Max loss', risk.max_loss_amount !== null ? `-$${risk.max_loss_amount.toFixed(2)} (${risk.max_loss_pct?.toFixed(1)}%)` : 'Unbounded — no stop-loss set'],
      ['Portfolio impact', risk.portfolio_impact_pct !== null ? `${risk.portfolio_impact_pct.toFixed(1)}%` : '—'],
      ['Current exposure', `${risk.existing_exposure_pct.toFixed(1)}% across ${risk.open_position_count} open position(s)`],
    ];
    const correlatedHtml = risk.correlated_positions.length
      ? `<div class="risk-correlation-warning">⚠ ${risk.correlated_positions.map((p) => `${escapeHtml(p.symbol)} (r=${p.correlation.toFixed(2)})`).join(', ')} — highly correlated with your existing position(s).</div>`
      : '';
    return `
      <div class="risk-assessment">
        <h4>🛡️ Risk &amp; Exposure</h4>
        ${rows.map(([label, value]) => `<div class="risk-row"><span>${label}</span><span>${value}</span></div>`).join('')}
        ${correlatedHtml}
        ${risk.note ? `<p class="dna-caveat">${escapeHtml(risk.note)}</p>` : ''}
      </div>
    `;
  }

  function renderSetupCheckResult(result) {
    const el = document.getElementById('sc-result');
    const checksHtml = result.checks
      .map((c) => `<div class="sc-check sc-check-${c.status}"><span class="sc-check-label">${escapeHtml(c.label)}</span><span class="sc-check-detail">${escapeHtml(c.detail)}</span></div>`)
      .join('');
    el.innerHTML = `
      <div class="sc-score-row">
        <div class="sc-score-badge sc-rating-${result.rating}">${result.score}<span>/100</span></div>
        <div class="sc-score-label">${result.rating} setup</div>
      </div>
      <div class="sc-checks">${checksHtml}</div>
      ${renderRiskAssessment(result.risk_assessment)}
      <p class="sc-disclaimer">${escapeHtml(result.disclaimer)}</p>
      <button class="btn btn-gradient" id="sc-explain-btn">✨ Explain with AI</button>
      <div id="sc-narrative" class="ai-result hidden"></div>
      <div id="sc-past-you" class="sc-past-you"></div>
    `;
    el.classList.remove('hidden');
    document.getElementById('sc-explain-btn').addEventListener('click', requestSetupCheckNarrative);
  }

  async function loadPastYouComparison(payload) {
    const el = document.getElementById('sc-past-you');
    if (!el) return;
    el.innerHTML = '<p class="dna-empty">Checking your trade history…</p>';
    try {
      const result = await api('POST', '/trades/similar-history', {
        portfolio_id: payload.portfolio_id,
        symbol: payload.symbol,
        direction: payload.direction,
        setup_id: payload.setup_id,
      });
      const conditionsHtml = result.common_conditions.length
        ? `<ul class="similar-conditions">${result.common_conditions.map((c) => `<li>${escapeHtml(c)}</li>`).join('')}</ul>`
        : '';
      el.innerHTML = `
        <h4>🕰️ What Would Past You Do?</h4>
        <p>You've seen this setup <strong>${result.total_matched}</strong> time(s) before.</p>
        ${result.total_matched > 0 ? `<div class="similar-split"><span class="similar-stat pos">${result.winners} winners</span><span class="similar-stat neg">${result.losers} losers</span>${result.avg_realized_r !== null ? `<span class="similar-stat">${result.avg_realized_r.toFixed(1)}R avg</span>` : ''}</div>` : ''}
        ${conditionsHtml}
        ${!result.has_sufficient_data && result.note ? `<p class="dna-empty">${escapeHtml(result.note)}</p>` : ''}
      `;
    } catch (e) {
      el.innerHTML = `<p class="dna-empty">Couldn't check trade history: ${escapeHtml(e.message)}</p>`;
    }
  }

  async function requestSetupCheckNarrative() {
    if (!lastSetupCheckPayload) return;
    const btn = document.getElementById('sc-explain-btn');
    const narrativeEl = document.getElementById('sc-narrative');
    btn.disabled = true;
    narrativeEl.classList.remove('hidden');
    narrativeEl.innerHTML = '<div class="ai-thinking"><div class="ai-orbit"><div class="orbit-dot"></div><div class="orbit-dot"></div><div class="orbit-dot"></div></div><p class="ai-thinking-text">Your local AI mentor is reviewing the setup…</p></div>';
    try {
      const res = await api('POST', '/trades/setup-check/narrative', lastSetupCheckPayload);
      narrativeEl.innerHTML = `<p>${escapeHtml(res.narrative)}</p><p class="sc-disclaimer">${escapeHtml(res.disclaimer)}</p>`;
    } catch (e) {
      narrativeEl.innerHTML = `<p class="dna-empty">AI mentor unavailable: ${escapeHtml(e.message)}</p>`;
    } finally {
      btn.disabled = false;
      btn.classList.add('hidden');
    }
  }

  async function submitSetupCheck() {
    const errEl = document.getElementById('sc-error');
    errEl.textContent = '';
    const symbol = document.getElementById('sc-symbol').value.trim().toUpperCase();
    const direction = document.querySelector('#sc-direction .seg-btn.active').dataset.value;
    const entryPrice = parseFloat(document.getElementById('sc-entry').value);
    const positionSize = parseFloat(document.getElementById('sc-size').value);
    const stopLoss = parseFloat(document.getElementById('sc-sl').value) || null;
    const takeProfit = parseFloat(document.getElementById('sc-tp').value) || null;
    const setupId = document.getElementById('sc-setup-select').value;
    const timeframe = document.getElementById('sc-timeframe').value.trim() || null;
    const reason = document.getElementById('sc-reason').value.trim() || null;
    const confirmation = document.getElementById('sc-confirmation').value.trim() || null;

    if (!symbol || !entryPrice || entryPrice <= 0 || !positionSize || positionSize <= 0) {
      errEl.textContent = 'Fill in symbol, entry price, and position size.';
      return;
    }

    const payload = {
      portfolio_id: state.portfolioId,
      symbol,
      direction,
      entry_price: entryPrice,
      stop_loss: stopLoss,
      take_profit: takeProfit,
      position_size: positionSize,
      setup_id: setupId ? Number(setupId) : null,
      timeframe,
      reason_for_entry: reason,
      confirmation_notes: confirmation,
    };
    lastSetupCheckPayload = payload;

    const btn = document.getElementById('sc-submit');
    btn.disabled = true;
    try {
      const result = await api('POST', '/trades/setup-check', payload);
      renderSetupCheckResult(result);
      loadPastYouComparison(payload);
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('btn-open-setup-check').addEventListener('click', openSetupCheckModal);
  document.getElementById('sc-submit').addEventListener('click', submitSetupCheck);

  // ---------- Journal (all trades, not just backtest replay) ----------
  function journalFieldId(prefix, tradeId, key) {
    return `${prefix}-${tradeId}-${key}`;
  }

  function renderJournalList(trades) {
    const listEl = document.getElementById('journal-list');
    const emptyEl = document.getElementById('journal-empty');
    const journalable = trades.filter((t) => t.status === 'closed' || t.backtest_journal);
    if (!journalable.length) {
      emptyEl.classList.remove('hidden');
      listEl.innerHTML = '';
      return;
    }
    emptyEl.classList.add('hidden');

    listEl.innerHTML = journalable
      .map((t) => {
        const j = t.backtest_journal || {};
        return `
          <div class="journal-card card-glass" data-id="${t.id}">
            <div class="journal-card-head">
              <div class="dir-pill ${t.direction}">${t.direction.toUpperCase()}</div>
              <div>
                <div class="tc-symbol">${escapeHtml(t.symbol)}</div>
                <div class="tc-sub">${formatDateTime(t.open_time)}${t.status === 'closed' ? ` · ${formatMoney(t.profit)}` : ' · open'}</div>
              </div>
              <span class="journal-toggle">▾</span>
            </div>
            <div class="journal-card-body hidden">
              ${Object.entries(BT_JOURNAL_FIELDS)
                .map(([key, _]) => {
                  const id = journalFieldId('j', t.id, key);
                  return `<label class="field"><span>${escapeHtml(JOURNAL_PROMPTS[key])}</span><textarea id="${id}" rows="2">${escapeHtml(j[key] || '')}</textarea></label>`;
                })
                .join('')}
              <button class="btn btn-primary journal-save-btn" data-id="${t.id}">Save Journal</button>
              <span class="bt-journal-status journal-status" id="journal-status-${t.id}"></span>
            </div>
          </div>
        `;
      })
      .join('');

    listEl.querySelectorAll('.journal-card-head').forEach((head) => {
      head.addEventListener('click', () => {
        head.parentElement.querySelector('.journal-card-body').classList.toggle('hidden');
      });
    });
    listEl.querySelectorAll('.journal-save-btn').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tradeId = Number(btn.dataset.id);
        const payload = {};
        Object.keys(BT_JOURNAL_FIELDS).forEach((key) => {
          payload[key] = document.getElementById(journalFieldId('j', tradeId, key)).value.trim() || null;
        });
        btn.disabled = true;
        try {
          const trade = await api('PUT', `/trades/${tradeId}/journal`, payload);
          const cached = state.trades.find((t) => t.id === tradeId);
          if (cached) cached.backtest_journal = trade.backtest_journal;
          if (btCurrentTrade && btCurrentTrade.id === tradeId) btCurrentTrade.backtest_journal = trade.backtest_journal;
          const statusEl = document.getElementById(`journal-status-${tradeId}`);
          statusEl.textContent = '✓ Saved';
          statusEl.classList.add('visible');
          setTimeout(() => statusEl.classList.remove('visible'), 2500);
        } catch (err) {
          toast(`Couldn't save journal: ${err.message}`, 'error');
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  function openJournalView() {
    showView('journal');
    renderJournalList(state.trades);
  }

  // ---------- AI Coach (full-page, grounded in Trading DNA/Health) ----------
  const COACH_SUGGESTIONS = [
    'Why is my win rate falling?',
    'What is my biggest trading mistake?',
    'What setup works best for me?',
    'Am I overtrading?',
  ];

  function appendCoachMessage(role, text) {
    const el = document.getElementById('coach-messages');
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role}`;
    bubble.textContent = text;
    el.appendChild(bubble);
    el.scrollTop = el.scrollHeight;
    return bubble;
  }

  function openCoachView() {
    showView('coach');
    if (!state.coachLoaded) {
      state.coachLoaded = true;
      appendCoachMessage(
        'assistant',
        "I'm your AI Coach — I can see your Trading DNA, mistake patterns, and Trading Health score. Ask me anything about your trading."
      );
      const suggestionsEl = document.getElementById('coach-suggestions');
      suggestionsEl.innerHTML = COACH_SUGGESTIONS.map((q) => `<button type="button" class="coach-chip">${escapeHtml(q)}</button>`).join('');
      suggestionsEl.querySelectorAll('.coach-chip').forEach((chip) => {
        chip.addEventListener('click', () => {
          document.getElementById('coach-input').value = chip.textContent;
          sendCoachMessage();
        });
      });
    }
    document.getElementById('coach-input').focus();
  }

  async function sendCoachMessage() {
    const input = document.getElementById('coach-input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    appendCoachMessage('user', message);

    const historyForRequest = state.coachHistory.slice(-10);
    state.coachHistory.push({ role: 'user', content: message });

    const sendBtn = document.getElementById('coach-send');
    sendBtn.disabled = true;
    const thinkingBubble = appendCoachMessage('assistant', 'Thinking…');
    thinkingBubble.classList.add('chat-thinking');

    try {
      const res = await api('POST', `/portfolios/${state.portfolioId}/coach/chat`, {
        message,
        history: historyForRequest,
      });
      thinkingBubble.remove();
      appendCoachMessage('assistant', res.reply);
      state.coachHistory.push({ role: 'assistant', content: res.reply });
    } catch (e) {
      thinkingBubble.remove();
      appendCoachMessage('assistant', `Sorry, I couldn't reach the local AI: ${e.message}`);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  document.getElementById('coach-send').addEventListener('click', sendCoachMessage);
  document.getElementById('coach-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendCoachMessage();
  });

  // ---------- Wire up ----------
  document.getElementById('btn-new-trade').addEventListener('click', openNewTradeModal);
  document.getElementById('btn-empty-new-trade').addEventListener('click', openNewTradeModal);
  document.getElementById('ob-create-user').addEventListener('click', handleCreateUser);
  document.getElementById('ob-create-portfolio').addEventListener('click', handleCreatePortfolio);
  document.getElementById('nt-submit').addEventListener('click', submitNewTrade);
  document.getElementById('ct-submit').addEventListener('click', submitCloseTrade);
  document.getElementById('btn-analyze').addEventListener('click', handleAnalyze);

  ['ob-email', 'ob-password'].forEach((id) =>
    document.getElementById(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') handleCreateUser(); })
  );
  ['ob-portfolio-name', 'ob-portfolio-balance'].forEach((id) =>
    document.getElementById(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') handleCreatePortfolio(); })
  );

  setupDropzone();
  boot();
})();
