/**
 * Vol Infra Dashboard — Plotly charts + API fetch logic
 */

// ── Plotly shared theme ────────────────────────────────────────────────────

const BG    = '#0f172a';
const PANEL = '#1e293b';
const GRID  = '#334155';
const TEXT  = '#e2e8f0';
const MUTED = '#94a3b8';
const ACCENT = '#38bdf8';
const POS   = '#22c55e';
const NEG   = '#ef4444';

const BASE_LAYOUT = {
  paper_bgcolor: PANEL,
  plot_bgcolor:  PANEL,
  font: { color: TEXT, family: 'Inter, system-ui, sans-serif', size: 12 },
  margin: { t: 20, r: 16, b: 40, l: 50 },
  xaxis: { gridcolor: GRID, linecolor: GRID, zerolinecolor: GRID },
  yaxis: { gridcolor: GRID, linecolor: GRID, zerolinecolor: GRID },
};

const PLOTLY_CONFIG = { responsive: true, displayModeBar: false };

// ── State ──────────────────────────────────────────────────────────────────

let state = {
  date:        null,
  underlying:  'ESTX50',
  storageRoot: 'data',
};

// ── Utilities ──────────────────────────────────────────────────────────────

function api(path, params = {}) {
  const qs = new URLSearchParams({
    ...params,
    underlying:   state.underlying,
    storage_root: state.storageRoot,
    ...(state.date ? { date: state.date } : {}),
  }).toString();
  return fetch(`${path}?${qs}`).then(r => r.json());
}

function loading(id) {
  document.getElementById(id).innerHTML =
    '<div class="placeholder"><div class="spinner"></div>Loading…</div>';
}

function noData(id, msg = 'No data available') {
  document.getElementById(id).innerHTML =
    `<div class="placeholder" style="color:var(--muted)">${msg}</div>`;
}

function fmt(v, digits = 2) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  return (v * 100).toFixed(2) + '%';
}

function pnlClass(v) {
  if (v == null) return 'neutral';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';
}

// ── Dates ──────────────────────────────────────────────────────────────────

async function loadDates() {
  const sel = document.getElementById('sel-date');
  sel.innerHTML = '<option>Loading…</option>';
  try {
    const data = await fetch(`/api/dates?underlying=${state.underlying}&storage_root=${state.storageRoot}`)
      .then(r => r.json());
    sel.innerHTML = '';
    if (!data.dates || data.dates.length === 0) {
      sel.innerHTML = '<option value="">No data — run seed script</option>';
      return;
    }
    data.dates.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = d;
      sel.appendChild(opt);
    });
    state.date = data.dates[0];
    sel.value = state.date;
  } catch {
    sel.innerHTML = '<option value="">Error loading dates</option>';
  }
}

// ── 3D Vol Surface ─────────────────────────────────────────────────────────

async function renderSurface() {
  loading('chart-surface');
  const data = await api('/api/surface');

  if (!data.x_values || data.x_values.length === 0) {
    noData('chart-surface', 'No surface data for this date');
    document.getElementById('badge-surface').textContent = '0 pts';
    return;
  }

  const isMoneyness = data.x_key === 'log_moneyness';

  const trace = {
    type: 'surface',
    x: data.x_values,
    y: data.maturities,
    z: data.iv_matrix,
    colorscale: [
      [0.00, '#1e3a5f'],
      [0.25, '#2563eb'],
      [0.50, '#38bdf8'],
      [0.75, '#f59e0b'],
      [1.00, '#ef4444'],
    ],
    colorbar: {
      title: { text: 'IV', side: 'right' },
      tickformat: '.0%',
      len: 0.7,
      thickness: 14,
    },
    hovertemplate:
      (isMoneyness ? 'ln(K/F): %{x:.3f}' : 'Strike: %{x:.0f}') +
      '<br>Maturity: %{y:.3f}y' +
      '<br>IV: %{z:.2%}<extra></extra>',
    contours: {
      z: { show: true, usecolormap: true, highlightcolor: ACCENT, project: { z: true } },
    },
    lighting: { ambient: 0.7, diffuse: 0.85, roughness: 0.4, fresnel: 0.3 },
    lightposition: { x: 1000, y: 1000, z: 1000 },
  };

  const layout = {
    ...BASE_LAYOUT,
    margin: { t: 20, r: 10, b: 10, l: 10 },
    scene: {
      bgcolor: PANEL,
      xaxis: {
        title: { text: data.x_label, font: { color: MUTED } },
        gridcolor: GRID, linecolor: GRID, tickfont: { color: MUTED },
        tickformat: isMoneyness ? '.2f' : '.0f',
      },
      yaxis: {
        title: { text: 'Maturity (years)', font: { color: MUTED } },
        gridcolor: GRID, linecolor: GRID, tickfont: { color: MUTED },
      },
      zaxis: {
        title: { text: 'Implied Volatility', font: { color: MUTED } },
        gridcolor: GRID, linecolor: GRID, tickfont: { color: MUTED },
        tickformat: '.0%',
      },
      camera: { eye: { x: 1.5, y: -1.5, z: 0.8 } },
    },
  };

  Plotly.react('chart-surface', [trace], layout, PLOTLY_CONFIG);
  document.getElementById('badge-surface').textContent = `${data.n_points} pts`;
}

// ── IV Smile ───────────────────────────────────────────────────────────────

const SMILE_COLORS = [ACCENT, '#f59e0b', POS, NEG, '#a78bfa', '#fb7185', '#34d399'];

async function renderSmile() {
  loading('chart-smile');
  const data = await api('/api/iv');

  if (!data.maturities || data.maturities.length === 0) {
    noData('chart-smile');
    return;
  }

  const traces = data.maturities.map((mat, i) => {
    const pts = data.by_maturity[mat] || [];
    return {
      type: 'scatter',
      mode: 'lines+markers',
      name: `${parseFloat(mat).toFixed(2)}y`,
      x: pts.map(p => p.x),
      y: pts.map(p => p.iv),
      line: { color: SMILE_COLORS[i % SMILE_COLORS.length], width: 2 },
      marker: { size: 4 },
    };
  });

  const isMoneyness = data.x_key === 'log_moneyness';
  const layout = {
    ...BASE_LAYOUT,
    xaxis: { ...BASE_LAYOUT.xaxis, title: isMoneyness ? 'Log-Moneyness ln(K/F)' : 'Strike' },
    yaxis: { ...BASE_LAYOUT.yaxis, title: 'Implied Volatility', tickformat: '.0%' },
    legend: { bgcolor: PANEL, bordercolor: GRID, borderwidth: 1 },
    showlegend: true,
  };

  Plotly.react('chart-smile', traces, layout, PLOTLY_CONFIG);
}

// ── ATM Term Structure ─────────────────────────────────────────────────────

async function renderTermStructure() {
  loading('chart-term');
  const data = await api('/api/iv');

  if (!data.maturities || data.maturities.length === 0) {
    noData('chart-term');
    return;
  }

  const atmIVs = data.maturities.map(mat => {
    const pts = data.by_maturity[mat] || [];
    if (!pts.length) return null;
    // closest to 0 log-moneyness or mid-strike
    const sorted = [...pts].sort((a, b) => Math.abs(a.x) - Math.abs(b.x));
    return sorted[0].iv;
  });

  const trace = {
    type: 'scatter',
    mode: 'lines+markers',
    name: 'ATM IV',
    x: data.maturities.map(m => parseFloat(m)),
    y: atmIVs,
    line: { color: ACCENT, width: 2.5 },
    marker: { size: 7, color: ACCENT },
    fill: 'tozeroy',
    fillcolor: ACCENT + '18',
  };

  const layout = {
    ...BASE_LAYOUT,
    xaxis: { ...BASE_LAYOUT.xaxis, title: 'Maturity (years)' },
    yaxis: { ...BASE_LAYOUT.yaxis, title: 'ATM Implied Vol', tickformat: '.0%' },
  };

  Plotly.react('chart-term', [trace], layout, PLOTLY_CONFIG);
}

// ── Scenario Heatmap ───────────────────────────────────────────────────────

async function renderScenarios() {
  loading('chart-scenarios');
  const data = await api('/api/scenarios');

  if (!data.scenarios || data.scenarios.length === 0) {
    noData('chart-scenarios', 'No scenario data — re-run seed script');
    document.getElementById('badge-scenarios').textContent = '0 scenarios';
    return;
  }

  document.getElementById('badge-scenarios').textContent =
    `${data.scenarios.length} scenarios`;

  const hm = data.heatmap;
  if (hm && hm.pnl_matrix && hm.pnl_matrix.length > 0) {
    // ── 2D Heatmap (77 scenarios) ──
    const zData   = hm.pnl_matrix;
    const allPnl  = zData.flat().filter(v => v != null);
    const absMax  = Math.max(1, Math.max(...allPnl.map(Math.abs)));

    const trace = {
      type: 'heatmap',
      x: hm.vol_shocks.map(v => `${v > 0 ? '+' : ''}${v}pts`),
      y: hm.spot_shocks.map(v => `${v > 0 ? '+' : ''}${v}%`),
      z: zData,
      colorscale: [
        [0.00, '#7f1d1d'],
        [0.25, '#ef4444'],
        [0.50, '#475569'],
        [0.75, '#22c55e'],
        [1.00, '#14532d'],
      ],
      zmid: 0,
      zmin: -absMax, zmax: absMax,
      colorbar: {
        title: { text: 'PnL (€)', side: 'right' },
        tickformat: ',.0f',
        len: 0.8, thickness: 14,
      },
      hovertemplate:
        'Spot: %{y}<br>Vol: %{x}<br>PnL: %{z:,.0f} €<extra></extra>',
      xgap: 1, ygap: 1,
    };

    const layout = {
      ...BASE_LAYOUT,
      xaxis: { ...BASE_LAYOUT.xaxis, title: 'Vol shock (pts)', tickfont: { size: 10 } },
      yaxis: { ...BASE_LAYOUT.yaxis, title: 'Spot shock (%)', tickfont: { size: 10 } },
    };

    Plotly.react('chart-scenarios', [trace], layout, PLOTLY_CONFIG);

  } else {
    // ── Fallback: bar chart ──
    const sorted = [...data.scenarios].sort((a, b) => a.total_pnl - b.total_pnl);
    const trace = {
      type: 'bar',
      orientation: 'h',
      x: sorted.map(s => s.total_pnl),
      y: sorted.map(s => s.scenario_id),
      marker: { color: sorted.map(s => s.total_pnl >= 0 ? POS : NEG) },
      hovertemplate: '%{y}<br>PnL: %{x:,.0f} €<extra></extra>',
    };
    const layout = {
      ...BASE_LAYOUT,
      xaxis: { ...BASE_LAYOUT.xaxis, title: 'PnL (€)', tickformat: ',.0f' },
      yaxis: { ...BASE_LAYOUT.yaxis, automargin: true },
    };
    Plotly.react('chart-scenarios', [trace], layout, PLOTLY_CONFIG);
  }
}

// ── Greeks ─────────────────────────────────────────────────────────────────

async function renderGreeks() {
  loading('chart-greeks');
  const data = await api('/api/greeks');

  if (!data.positions || data.positions.length === 0) {
    noData('chart-greeks', 'No pricing results for this date');
    return;
  }

  const labels = data.positions.map(p =>
    `${p.option_type || '?'} K=${fmt(p.strike, 0)} T=${fmt(p.maturity, 2)}y`
  );

  const traces = [
    {
      name: 'Delta',
      type: 'bar', orientation: 'h',
      x: data.positions.map(p => p.delta),
      y: labels,
      marker: { color: ACCENT },
      visible: true,
    },
    {
      name: 'Vega',
      type: 'bar', orientation: 'h',
      x: data.positions.map(p => p.vega),
      y: labels,
      marker: { color: '#f59e0b' },
      visible: 'legendonly',
    },
    {
      name: 'Theta/day',
      type: 'bar', orientation: 'h',
      x: data.positions.map(p => p.theta),
      y: labels,
      marker: { color: NEG },
      visible: 'legendonly',
    },
  ];

  const layout = {
    ...BASE_LAYOUT,
    barmode: 'overlay',
    xaxis: { ...BASE_LAYOUT.xaxis, title: 'Value', zeroline: true },
    yaxis: { ...BASE_LAYOUT.yaxis, automargin: true, tickfont: { size: 10 } },
    legend: { bgcolor: PANEL, bordercolor: GRID, borderwidth: 1 },
    margin: { t: 20, r: 16, b: 40, l: 160 },
  };

  Plotly.react('chart-greeks', traces, layout, PLOTLY_CONFIG);
}

// ── Straddle ───────────────────────────────────────────────────────────────

async function renderStraddle() {
  const el = document.getElementById('straddle-metrics');
  el.innerHTML = '<div class="placeholder"><div class="spinner"></div>Loading…</div>';
  const data = await api('/api/straddle');

  const pos = data.position;
  if (!pos) {
    el.innerHTML = '<div class="placeholder" style="height:200px;color:var(--muted)">No straddle position saved</div>';
    document.getElementById('badge-straddle').textContent = 'none';
    return;
  }

  const pnl = (pos.current_price ?? pos.open_price ?? 0) - (pos.open_price ?? 0);
  const dte = pos.target_expiry
    ? Math.round((new Date(pos.target_expiry) - new Date()) / 86400000)
    : null;

  document.getElementById('badge-straddle').innerHTML =
    `<span class="dot ${pnl >= 0 ? 'pos' : 'neg'}"></span>${pos.status || 'open'}`;

  const metrics = [
    { label: 'Underlying',  value: pos.underlying || '—', cls: 'neutral' },
    { label: 'Open Date',   value: pos.open_date || '—', cls: 'neutral' },
    { label: 'Expiry',      value: pos.target_expiry || '—', cls: 'neutral' },
    { label: 'DTE',         value: dte != null ? `${dte}d` : '—', cls: dte < 30 ? 'warn' : 'neutral' },
    { label: 'Strike (K)',  value: fmt(pos.strike_k, 0), cls: 'neutral' },
    { label: 'Open Price',  value: `€${fmt(pos.open_price, 0)}`, cls: 'neutral' },
    { label: 'Curr Price',  value: `€${fmt(pos.current_price, 0)}`, cls: 'neutral' },
    { label: 'Spot (open)', value: fmt(pos.straddle_spot, 0), cls: 'neutral' },
    { label: 'Curr Spot',   value: fmt(data.current_spot, 0), cls: 'neutral' },
    { label: 'Total PnL',   value: `€${fmt(pnl, 0)}`, cls: pnlClass(pnl) },
  ];

  el.innerHTML = metrics.map(m =>
    `<div class="metric">
      <div class="label">${m.label}</div>
      <div class="value ${m.cls}">${m.value}</div>
    </div>`
  ).join('');
}

// ── UAM Gauge ─────────────────────────────────────────────────────────────

async function renderUAM() {
  loading('chart-uam');
  const data = await api('/api/uam');

  if (!data.uam) {
    noData('chart-uam', 'No UAM data — no position found');
    return;
  }

  const u = data.uam;
  const ratio  = u.uam_ratio ?? u.margin_ratio ?? 0;
  const margin = u.margin_req ?? 0;
  const gross  = u.portfolio_gross_value ?? 0;
  const limit  = u.uam_limit ?? 0.5;

  const color = ratio >= limit ? NEG : ratio >= limit * 0.8 ? '#f59e0b' : POS;

  const gauge = {
    type: 'indicator',
    mode: 'gauge+number+delta',
    value: +(ratio * 100).toFixed(2),
    number: { suffix: '%', font: { size: 36, color: color } },
    delta: {
      reference: limit * 100,
      valueformat: '.1f',
      suffix: '%',
      increasing: { color: NEG },
      decreasing: { color: POS },
    },
    gauge: {
      axis: {
        range: [0, 100],
        ticksuffix: '%',
        tickcolor: MUTED,
        tickfont: { color: MUTED },
      },
      bar: { color: color, thickness: 0.25 },
      bgcolor: PANEL,
      bordercolor: GRID,
      steps: [
        { range: [0, limit * 80],  color: '#14532d33' },
        { range: [limit * 80, limit * 100], color: '#78350f33' },
        { range: [limit * 100, 100], color: '#7f1d1d33' },
      ],
      threshold: {
        line: { color: NEG, width: 3 },
        thickness: 0.8,
        value: limit * 100,
      },
    },
    title: { text: `UAM Ratio<br><span style="font-size:12px;color:${MUTED}">Margin €${(margin/1000).toFixed(0)}k / Gross €${(gross/1000).toFixed(0)}k</span>` },
  };

  const layout = {
    paper_bgcolor: PANEL,
    font: { color: TEXT },
    margin: { t: 40, r: 20, b: 10, l: 20 },
  };

  Plotly.react('chart-uam', [gauge], layout, PLOTLY_CONFIG);
}

// ── Greek PnL Contributions ────────────────────────────────────────────────

const CONTRIB_SCENARIOS = [
  { label: 'Spot −10%',                 spot: -0.10, vol:  0.00 },
  { label: 'Spot −5%',                  spot: -0.05, vol:  0.00 },
  { label: 'Spot +5%',                  spot: +0.05, vol:  0.00 },
  { label: 'Spot +10%',                 spot: +0.10, vol:  0.00 },
  { label: 'Vol +5 pts',                spot:  0.00, vol: +0.05 },
  { label: 'Vol +15 pts',               spot:  0.00, vol: +0.15 },
  { label: 'Crash (−20% / +20 pts)',    spot: -0.20, vol: +0.20 },
  { label: 'Melt-up (+15% / −10 pts)',  spot: +0.15, vol: -0.10 },
];

function initContribScenarios() {
  const sel = document.getElementById('sel-contrib-scenario');
  sel.innerHTML = CONTRIB_SCENARIOS.map((s, i) =>
    `<option value="${i}">${s.label}</option>`
  ).join('');
}

async function renderGreekContributions() {
  loading('chart-greek-contrib');
  const idx  = parseInt(document.getElementById('sel-contrib-scenario').value || '0', 10);
  const scen = CONTRIB_SCENARIOS[idx] || CONTRIB_SCENARIOS[0];

  const data = await api('/api/greeks_contributions', {
    spot_shift_pct: scen.spot,
    vol_shift_abs:  scen.vol,
  });

  if (!data.positions || data.positions.length === 0) {
    noData('chart-greek-contrib', data.note || 'No risk data — run seed script and set storage root to data/storage');
    return;
  }

  const greekKeys   = ['delta_pnl', 'gamma_pnl', 'vega_pnl', 'theta_pnl', 'rho_pnl'];
  const greekLabels = ['|Δ PnL|', '|Γ PnL|', '|ν PnL|', '|Θ PnL|', '|ρ PnL|'];
  const colors      = ['#2196F3', '#9C27B0', '#FF9800', NEG, POS];
  const labels      = data.positions.map(p => p.label);

  const traces = greekKeys.map((key, i) => ({
    type: 'bar',
    name: greekLabels[i],
    x: labels,
    y: data.positions.map(p => p[key] || 0),
    marker: { color: colors[i], opacity: 0.85 },
    hovertemplate: `${greekLabels[i]}: %{y:,.2f} €<extra>${greekLabels[i]}</extra>`,
  }));

  const spotPct = (scen.spot * 100).toFixed(0);
  const volPts  = (scen.vol  * 100).toFixed(0);
  const title   = `Shock: ${spotPct}% spot / ${volPts} vol pts`;

  const layout = {
    ...BASE_LAYOUT,
    barmode: 'group',
    xaxis: { ...BASE_LAYOUT.xaxis, title: 'Position', tickfont: { size: 10 }, automargin: true },
    yaxis: { ...BASE_LAYOUT.yaxis, title: '|PnL| (€)', tickformat: ',.0f' },
    legend: { bgcolor: PANEL, bordercolor: GRID, borderwidth: 1,
              orientation: 'h', y: 1.10, x: 0.5, xanchor: 'center' },
    title:  { text: title, font: { color: MUTED, size: 11 }, x: 0.01 },
  };

  Plotly.react('chart-greek-contrib', traces, layout, PLOTLY_CONFIG);
}

// ── Historical Prices ──────────────────────────────────────────────────────

async function renderHistorical() {
  loading('chart-historical');
  const ticker = document.getElementById('inp-hist-ticker').value.trim() || '^STOXX50E';

  let data;
  try {
    data = await fetch(`/api/historical?ticker=${encodeURIComponent(ticker)}&start=2022-01-01`)
      .then(r => r.json());
  } catch {
    noData('chart-historical', 'Request failed');
    return;
  }

  if (!data.closes || data.closes.length === 0) {
    noData('chart-historical', `No data for "${ticker}" — check the ticker symbol`);
    return;
  }

  const traces = [{
    type: 'scatter',
    name: data.ticker,
    x: data.dates,
    y: data.closes,
    mode: 'lines',
    line: { color: ACCENT, width: 1.5 },
    hovertemplate: '%{x|%b %d, %Y}<br>%{y:,.2f}<extra></extra>',
  }];

  if (data.ma200 && data.ma200.some(v => v !== null)) {
    traces.push({
      type: 'scatter',
      name: '200-day MA',
      x: data.dates,
      y: data.ma200,
      mode: 'lines',
      line: { color: '#FB8C00', width: 1.5, dash: 'dot' },
      hovertemplate: 'MA200: %{y:,.2f}<extra></extra>',
    });
  }

  const layout = {
    ...BASE_LAYOUT,
    xaxis: { ...BASE_LAYOUT.xaxis, title: '' },
    yaxis: { ...BASE_LAYOUT.yaxis, title: 'Price' },
    legend: { bgcolor: PANEL, bordercolor: GRID, borderwidth: 1 },
    title: {
      text: `${data.ticker}  ·  ${data.n_points} trading days`,
      font: { color: MUTED, size: 11 }, x: 0.01,
    },
  };

  Plotly.react('chart-historical', traces, layout, PLOTLY_CONFIG);
}

// ── ESTX50 Constituents ────────────────────────────────────────────────────

async function renderConstituents() {
  loading('chart-constituents');

  let data;
  try {
    data = await fetch('/api/constituents').then(r => r.json());
  } catch {
    noData('chart-constituents', 'Request failed');
    return;
  }

  if (!data.rows || data.rows.length === 0) {
    noData('chart-constituents', data.error || 'No constituent data — check network / API keys');
    return;
  }

  const altBg    = data.rows.map((_, i) => i % 2 === 0 ? PANEL : '#1a2742');
  const retColor = data.rows.map(r => (r.ret_1d || 0) >= 0 ? POS : NEG);

  const trace = {
    type: 'table',
    columnwidth: [70, 70, 60, 70],
    header: {
      values:  ['<b>Ticker</b>', '<b>Last</b>', '<b>1d %</b>', '<b>Date</b>'],
      fill:    { color: GRID },
      font:    { color: TEXT, size: 11 },
      align:   'center',
      line:    { color: '#475569', width: 1 },
      height:  28,
    },
    cells: {
      values: [
        data.rows.map(r => r.ticker),
        data.rows.map(r => r.last_close != null ? r.last_close.toFixed(2) : '—'),
        data.rows.map(r => r.ret_1d     != null ? (r.ret_1d * 100).toFixed(2) + '%' : '—'),
        data.rows.map(r => r.last_date  || '—'),
      ],
      fill: { color: [altBg, altBg, altBg, altBg] },
      font: {
        color: [TEXT, TEXT, retColor, Array(data.rows.length).fill(MUTED)],
        size:  10,
      },
      align:  ['left', 'right', 'right', 'center'],
      line:   { color: '#334155', width: 1 },
      height: 22,
    },
  };

  const layout = {
    paper_bgcolor: PANEL,
    plot_bgcolor:  PANEL,
    font:    { color: TEXT },
    margin:  { t: 10, r: 8, b: 8, l: 8 },
    title: {
      text: `${data.n_tickers} of 50 ESTX50 constituents (sample)`,
      font: { color: MUTED, size: 11 }, x: 0.01,
    },
  };

  Plotly.react('chart-constituents', [trace], layout, PLOTLY_CONFIG);
}

// ── Options Chain ──────────────────────────────────────────────────────────

async function renderOptionsChain() {
  loading('chart-chain');
  const sel    = document.getElementById('sel-expiry');
  const expiry = sel.value || null;

  const data = await api('/api/options_chain', expiry ? { expiry } : {});

  // Populate expiry selector
  if (data.expiries && data.expiries.length > 0) {
    const cur = sel.value;
    sel.innerHTML = data.expiries.map(e =>
      `<option value="${e}" ${e === (data.selected_expiry || cur) ? 'selected' : ''}>${e}</option>`
    ).join('');
    if (!cur) sel.value = data.selected_expiry || data.expiries[0];
  }

  document.getElementById('badge-chain').textContent =
    data.n_rows
      ? `${data.n_rows} rows · r=${(data.risk_free_rate * 100).toFixed(1)}%`
      : '—';

  if (!data.rows || data.rows.length === 0) {
    noData('chart-chain', 'No options chain data for this date / expiry');
    return;
  }

  // Pivot: one row per strike, C and P as separate columns
  const strikes = [...new Set(data.rows.map(r => r.strike))].sort((a, b) => a - b);
  const callMap = {}, putMap = {}, lmMap = {}, matMap = {};
  data.rows.forEach(r => {
    if (r.option_right === 'C') callMap[r.strike] = r.implied_vol;
    else                        putMap[r.strike]  = r.implied_vol;
    lmMap[r.strike] = r.log_moneyness;
    matMap[r.strike] = r.maturity_years;
  });

  const callIVs = strikes.map(k => callMap[k] != null ? (callMap[k] * 100).toFixed(2) + '%' : '—');
  const putIVs  = strikes.map(k => putMap[k]  != null ? (putMap[k]  * 100).toFixed(2) + '%' : '—');
  const lms     = strikes.map(k => lmMap[k]   != null ? lmMap[k].toFixed(4) : '—');

  // ITM calls (lm ≤ 0) = blue, ITM puts (lm ≥ 0) = red, OTM = muted
  const callColor = strikes.map(k => (lmMap[k] || 0) <= 0 ? '#60a5fa' : MUTED);
  const putColor  = strikes.map(k => (lmMap[k] || 0) >= 0 ? '#f87171' : MUTED);
  const altBg     = strikes.map((_, i) => i % 2 === 0 ? PANEL : '#1a2742');

  const midIdx = Math.floor(strikes.length / 2);
  const mat    = matMap[strikes[midIdx]];
  const matLabel = data.selected_expiry
    ? `${data.selected_expiry}   ·   T = ${mat ? mat.toFixed(4) : '?'} y   ·   r = ${(data.risk_free_rate * 100).toFixed(1)}%`
    : '';

  const trace = {
    type: 'table',
    columnwidth: [80, 80, 90, 80, 80],
    header: {
      values: ['<b>Call IV</b>', '<b>ln(K/F)</b>', '<b>Strike</b>', '<b>ln(K/F)</b>', '<b>Put IV</b>'],
      fill:   { color: GRID },
      font:   { color: TEXT, size: 12 },
      align:  'center',
      line:   { color: '#475569', width: 1 },
      height: 30,
    },
    cells: {
      values: [callIVs, lms, strikes.map(k => k.toFixed(0)), lms, putIVs],
      fill:   { color: [altBg, altBg, altBg, altBg, altBg] },
      font: {
        color: [
          callColor,
          Array(strikes.length).fill(MUTED),
          Array(strikes.length).fill(TEXT),
          Array(strikes.length).fill(MUTED),
          putColor,
        ],
        size: 11,
      },
      align:  'center',
      line:   { color: '#334155', width: 1 },
      height: 24,
    },
  };

  const layout = {
    paper_bgcolor: PANEL,
    plot_bgcolor:  PANEL,
    font:   { color: TEXT },
    margin: { t: 10, r: 8, b: 8, l: 8 },
    title:  { text: matLabel, font: { color: MUTED, size: 11 }, x: 0.01 },
  };

  Plotly.react('chart-chain', [trace], layout, PLOTLY_CONFIG);
}

// ── Main: load everything ──────────────────────────────────────────────────

async function loadAll() {
  state.underlying  = document.getElementById('sel-underlying').value;
  state.storageRoot = document.getElementById('inp-storage').value;

  // Reset expiry selector so it repopulates for the new date / underlying
  document.getElementById('sel-expiry').innerHTML = '<option value="">—</option>';

  await Promise.all([
    renderSurface(),
    renderSmile(),
    renderTermStructure(),
    renderScenarios(),
    renderGreeks(),
    renderStraddle(),
    renderUAM(),
    renderGreekContributions(),
    renderOptionsChain(),
  ]);
}

async function init() {
  initContribScenarios();
  await loadDates();
  await loadAll();
}

// ── Event listeners ────────────────────────────────────────────────────────

document.getElementById('btn-refresh').addEventListener('click', async () => {
  state.underlying  = document.getElementById('sel-underlying').value;
  state.storageRoot = document.getElementById('inp-storage').value;
  state.date        = document.getElementById('sel-date').value || null;
  await loadAll();
});

document.getElementById('sel-date').addEventListener('change', e => {
  state.date = e.target.value;
  loadAll();
});

document.getElementById('sel-underlying').addEventListener('change', async e => {
  state.underlying = e.target.value;
  await loadDates();
  await loadAll();
});

document.getElementById('sel-contrib-scenario').addEventListener('change', renderGreekContributions);
document.getElementById('sel-expiry').addEventListener('change', renderOptionsChain);
document.getElementById('btn-load-hist').addEventListener('click', renderHistorical);
document.getElementById('btn-load-const').addEventListener('click', renderConstituents);

// ── Boot ───────────────────────────────────────────────────────────────────
init();
