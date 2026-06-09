const { useState, useMemo, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "uzum",
  "sparkStyle": "candle",
  "density": "regular",
  "accent": "#7000ff",
  "showBarcode": true,
  "showWarehouse": true,
  "thumbSize": "md",
  "stickyHero": true
} /*EDITMODE-END*/;

// ──────────────────────────────────────────────────────────────────
// Small UI atoms
// ──────────────────────────────────────────────────────────────────

const Icon = {
  back: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M15 18l-6-6 6-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  search: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.8" /><path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
  cal: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><rect x="3.5" y="5" width="17" height="15" rx="2.5" stroke="currentColor" strokeWidth="1.6" /><path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>,
  filter: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 5h16M7 12h10M10 19h4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
  download: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 4v12m0 0l-4-4m4 4l4-4M5 20h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  print: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M7 9V4h10v5M7 18h10v3H7zM5 9h14a2 2 0 012 2v5a2 2 0 01-2 2h-2v-3H7v3H5a2 2 0 01-2-2v-5a2 2 0 012-2z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>,
  more: <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>,
  up: <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 15l6-6 6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  down: <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  chev: <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  warn: <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M12 4l9.5 16H2.5L12 4z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><path d="M12 10v4M12 17.5v.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
  check: <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M5 12l4 4 10-10" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  sort: <svg width="10" height="10" viewBox="0 0 24 24" fill="none"><path d="M8 9l4-4 4 4M8 15l4 4 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  bell: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M6 16V11a6 6 0 0112 0v5l1.5 2H4.5L6 16z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M10 21h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>,
  refresh: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 12a8 8 0 0114-5.3L20 4M20 4v5h-5M20 12a8 8 0 01-14 5.3L4 20M4 20v-5h5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  lock:    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><rect x="4.5" y="11" width="15" height="10" rx="2" stroke="currentColor" strokeWidth="1.7"/><path d="M8 11V8a4 4 0 018 0v3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"/></svg>,
  unlock:  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><rect x="4.5" y="11" width="15" height="10" rx="2" stroke="currentColor" strokeWidth="1.7"/><path d="M8 11V8a4 4 0 017.5-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"/></svg>,
  warehouse: <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M3 10l9-5 9 5v10H3V10z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"/><path d="M8 20v-5h8v5" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/></svg>,
  minus:   <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M5 12h14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></svg>,
  plus:    <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></svg>,
};

function fmt(n) {return new Intl.NumberFormat('ru-RU').format(n);}
function fmtMoney(n) {return new Intl.NumberFormat('ru-RU').format(Math.round(n)) + ' сум';}
function fmtShort(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + ' млн';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + ' тыс.';
  return String(n);
}

function StatusPill({ status }) {
  const map = {
    ok: { bg: 'var(--green-soft)', fg: 'var(--green)', label: 'Активен', dot: '#16a34a' },
    low: { bg: 'var(--amber-soft)', fg: 'var(--amber)', label: 'Заканчивается', dot: '#d97706' },
    out: { bg: 'var(--red-soft)', fg: 'var(--red)', label: 'Нет в наличии', dot: '#dc2626' },
    stale: { bg: 'var(--surface-2)', fg: 'var(--muted)', label: 'Нет продаж', dot: 'var(--faint)' }
  };
  const s = map[status] || map.ok;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '2px 8px 2px 6px',
      borderRadius: 999, background: s.bg, color: s.fg, fontSize: 12, fontWeight: 500,
      whiteSpace: 'nowrap', textTransform: 'none', letterSpacing: 0 }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: s.dot }} />{s.label}
    </span>);

}

function StockMeter({ value, max = 20, color = 'var(--brand)', subdued = false }) {
  return (
    <span className="tnum mono" style={{
      fontSize: 15, fontWeight: 600,
      color: value === 0 ? 'var(--red)' : 'var(--ink)' }}>{value}</span>);
}

function NeedBadge({ n }) {
  if (n <= 0) return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--green)',
      fontWeight: 600, fontSize: 13 }} className="tnum">
      <span style={{ display: 'inline-flex' }}>{Icon.check}</span>хватает
    </span>);

  const severity = n >= 10 ? 'high' : n >= 5 ? 'mid' : 'low';
  const fg = severity === 'high' ? '#dc2626' : severity === 'mid' ? '#ea580c' : '#d97706';
  const bg = severity === 'high' ? '#fdeaea' : severity === 'mid' ? '#fdecdc' : '#fdf2e3';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '2px 8px',
      borderRadius: 6, background: bg, color: fg, fontSize: 13, fontWeight: 600 }} className="tnum">
      +{n} шт.
    </span>);

}

function SegBar({ options, value, onChange }) {
  const activeIdx = options.findIndex((o) => o.value === value);
  return (
    <div style={{ display: 'inline-flex', padding: 3, background: 'var(--surface-2)', borderRadius: 9,
      border: '.5px solid var(--line)', position: 'relative' }}>
      {options.map((o, i) =>
      <button key={o.value} onClick={() => onChange(o.value)}
      style={{ appearance: 'none', border: 0, background: 'transparent', padding: '5px 12px',
        borderRadius: 7, fontSize: 12.5, fontWeight: i === activeIdx ? 600 : 500,
        color: i === activeIdx ? 'var(--ink)' : 'var(--muted)',
        boxShadow: i === activeIdx ? 'var(--shadow-sm)' : 'none',
        background: i === activeIdx ? 'var(--surface)' : 'transparent',
        cursor: 'pointer', transition: 'all .15s', whiteSpace: 'nowrap' }}>
          {o.label}
        </button>
      )}
    </div>);

}

function Toolbutton({ children, kind = 'ghost', onClick, title, icon }) {
  const styles = {
    ghost: { bg: 'var(--surface)', fg: 'var(--ink)', border: '.5px solid var(--line)' },
    solid: { bg: 'var(--ink)', fg: 'var(--surface)', border: '.5px solid var(--ink)' },
    soft: { bg: 'var(--surface-2)', fg: 'var(--ink)', border: '.5px solid transparent' }
  }[kind];
  return (
    <button onClick={onClick} title={title}
    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 11px',
      borderRadius: 8, fontSize: 12.5, fontWeight: 500,
      background: styles.bg, color: styles.fg, border: styles.border,
      cursor: 'pointer', whiteSpace: 'nowrap', boxShadow: 'var(--shadow-sm)' }}>
      {icon && <span style={{ display: 'inline-flex', color: kind === 'solid' ? 'rgba(255,255,255,.85)' : 'var(--muted)' }}>{icon}</span>}
      {children}
    </button>);

}

// ──────────────────────────────────────────────────────────────────
// Header
// ──────────────────────────────────────────────────────────────────

function ProductHeader() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '16px 28px',
      background: 'var(--surface)', borderBottom: '.5px solid var(--line)' }}>
      <button style={{ appearance: 'none', display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '7px 11px 7px 9px', borderRadius: 8, fontSize: 13, fontWeight: 500,
        background: 'var(--surface)', border: '.5px solid var(--line)', color: 'var(--ink-2)', cursor: 'pointer' }}>
        {Icon.back}<span>Назад</span>
      </button>
      <span style={{ width: 1, height: 22, background: 'var(--line)' }} />
      <nav style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--muted)' }}>
        <span>Каталог</span>
        <span style={{ color: 'var(--faint)' }}>/</span>
        <span style={{ color: 'var(--ink)' }}>Кольцо для измерения температуры</span>
      </nav>
      <div style={{ flex: 1 }} />
    </div>);

}

function ProductCard({ styleSwitcher }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '22px 28px 18px' }}>
      <div style={{ width: 74, height: 74, borderRadius: 14, background: 'var(--surface-2)',
        border: '.5px solid var(--line)', overflow: 'hidden', flex: '0 0 auto',
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <img src={window.COLOR_META['Чёрный'].thumb} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <h1 style={{ margin: '0 0 5px', fontSize: 24, fontWeight: 600, letterSpacing: '-.01em', color: 'var(--ink)' }}>
          Кольцо для измерения температуры
        </h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, fontSize: 13.5, color: 'var(--muted)', flexWrap: 'wrap' }}>
          <span>Uzum ID: <span className="mono" style={{ color: 'var(--ink-2)' }}>311964</span></span>
          <span style={{ color: 'var(--faint)' }}>·</span>
          <span>Кольца и перстни</span>
          <span style={{ color: 'var(--faint)' }}>·</span>
          <span>LUXUZ</span>
        </div>
      </div>
      {styleSwitcher}
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// Stat strip
// ──────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, delta, deltaLabel, children, wide, accent }) {
  const positive = delta == null ? null : delta >= 0;
  return (
    <div style={{
      flex: wide ? '2 1 0' : '1 1 0', minWidth: 0,
      background: 'var(--surface)', border: '.5px solid var(--line)', borderRadius: 'var(--radius)',
      padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 8,
      boxShadow: 'var(--shadow-sm)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ fontSize: 12.5, fontWeight: 500, letterSpacing: '.02em',
          textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</span>
        {delta != null &&
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3,
          padding: '2px 6px', borderRadius: 6, fontSize: 11.5, fontWeight: 600,
          background: positive ? 'var(--green-soft)' : 'var(--red-soft)',
          color: positive ? 'var(--green)' : 'var(--red)' }} className="tnum">
            {positive ? Icon.up : Icon.down}{Math.abs(delta)}%
          </span>
        }
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 40, fontWeight: 600, letterSpacing: '-.02em', color: accent || 'var(--ink)', lineHeight: 1 }} className="tnum">
          {value}
        </span>
        {sub && <span style={{ fontSize: 13, color: 'var(--muted)' }}>{sub}</span>}
      </div>
      {deltaLabel && <span style={{ fontSize: 12.5, color: 'var(--faint)' }}>{deltaLabel}</span>}
      {children}
    </div>);

}

function HeroStat({ totalSales, totalRev, periodDays, series, hero }) {
  return (
    <div style={{
        flex: '2.2 1 0', minWidth: 0,
        background: hero.background,
        color: hero.color,
        border: '.5px solid ' + hero.borderColor,
        borderRadius: 'var(--radius)', padding: '14px 18px 8px',
        display: 'flex', flexDirection: 'column', gap: 6,
        boxShadow: hero.shadow,
        position: 'relative', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 500, letterSpacing: '.02em',
            textTransform: 'uppercase', color: hero.labels }}>
            Продажи за {periodDays} дн.
          </div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 6 }}>
            <span style={{ fontSize: 40, fontWeight: 600, letterSpacing: '-.02em', lineHeight: 1 }} className="tnum">
              {fmt(totalSales)} <span style={{ fontSize: 15, fontWeight: 500, color: hero.labels }}>шт.</span>
            </span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3,
              padding: '2px 8px', borderRadius: 6, fontSize: 12, fontWeight: 600,
              background: 'var(--green-soft)', color: 'var(--green)' }} className="tnum">
              {Icon.up}12%
            </span>
          </div>
          <div style={{ fontSize: 13, color: hero.labels, marginTop: 4 }}>
            <span className="tnum">{fmtMoney(totalRev)}</span> · ср. чек <span className="tnum">{fmtMoney(totalRev / Math.max(1, totalSales))}</span>
          </div>
        </div>
      </div>
      <div style={{ marginTop: 6 }}>
        <window.HeroBar data={series} width={620} height={80}
          gridColor={hero.grid} labelColor={hero.labels} color={hero.accent} />
      </div>
    </div>);

}

function StatStrip({ variants, periodDays, theme }) {
  const totals = useMemo(() => {
    const t = { sales: 0, rev: 0, uzum: 0, wh: 0, low: 0, out: 0, need: 0 };
    for (const v of variants) {
      const s = v.sales[periodDays] || 0;
      t.sales += s;
      t.rev += s * v.price;
      t.uzum += v.uzumStock;
      t.wh += v.warehouseStock;
      if (v.status === 'low') t.low++;
      if (v.status === 'out') t.out++;
      const need = Math.max(0, Math.round(v.sales[30] / 30 * 60 - v.uzumStock - v.warehouseStock));
      t.need += need;
    }
    return t;
  }, [variants, periodDays]);

  const series = useMemo(() => window.aggregateSeries(periodDays), [periodDays]);

  return (
    <div style={{ display: 'flex', gap: 12, padding: '4px 28px 18px', alignItems: 'stretch' }}>
      <HeroStat totalSales={totals.sales} totalRev={totals.rev}
        periodDays={periodDays} series={series} hero={theme.hero} />

      <StatCard label="Склад" value={fmt(totals.uzum + totals.wh)} sub="шт. всего">
        <div style={{ marginTop: 'auto', display: 'flex', gap: 14, fontSize: 12, color: 'var(--muted)' }}>
          <span>Uzum <span className="tnum mono" style={{ color: 'var(--ink)', fontWeight: 600 }}>{fmt(totals.uzum)}</span></span>
          <span>Наш склад <span className="tnum mono" style={{ color: 'var(--ink)', fontWeight: 600 }}>{fmt(totals.wh)}</span></span>
        </div>
      </StatCard>

      <StatCard label="Нужно заказать" value={fmt(totals.need)} sub="шт. на 60 дн."
        accent={totals.need > 0 ? 'var(--red)' : 'var(--ink)'}>
        <div style={{ marginTop: 'auto', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{totals.out + totals.low} SKU требуют внимания</span>
          <button style={{ appearance: 'none', border: 0, background: 'var(--ink)', color: 'var(--surface)',
            padding: '4px 9px', borderRadius: 6, fontSize: 11.5, fontWeight: 500, cursor: 'pointer' }}>
            Создать заказ →
          </button>
        </div>
      </StatCard>
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// Toolbar
// ──────────────────────────────────────────────────────────────────

const PERIODS = [
{ value: 7, label: '7 дн.' },
{ value: 10, label: '10 дн.' },
{ value: 15, label: '15 дн.' },
{ value: 30, label: '30 дн.' },
{ value: 60, label: '60 дн.' },
{ value: 90, label: '90 дн.' }];


function Toolbar({ period, setPeriod, query, setQuery, colorFilter, setColorFilter,
  statusFilter, setStatusFilter, selectedCount, variants, theme }) {
  const colorCounts = useMemo(() => {
    const m = {};
    for (const v of variants) m[v.color] = (m[v.color] || 0) + 1;
    return m;
  }, [variants]);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
      background: 'var(--surface)', borderBottom: '.5px solid var(--line)', borderTopLeftRadius: 'var(--radius)',
      borderTopRightRadius: 'var(--radius)', flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink)' }}>Все варианты</span>
        <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          minWidth: 22, height: 20, padding: '0 6px', background: 'var(--surface-2)', borderRadius: 6,
          fontSize: 11.5, fontWeight: 500, color: 'var(--muted)' }} className="tnum">
          {variants.length}
        </span>
      </div>
      <span style={{ width: 1, height: 22, background: 'var(--line)', margin: '0 4px' }} />
      <SegBar options={PERIODS} value={period} onChange={setPeriod} />

      <div style={{ flex: 1 }} />

      <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
        <span style={{ position: 'absolute', left: 10, color: 'var(--faint)', pointerEvents: 'none', display: 'inline-flex' }}>{Icon.search}</span>
        <input value={query} onChange={(e) => setQuery(e.target.value)}
        placeholder="Поиск по SKU, размеру, штрихкоду…"
        style={{ appearance: 'none', width: 260, height: 32, padding: '0 12px 0 32px',
          background: theme.inputBg, border: '.5px solid transparent', borderRadius: 8,
          fontSize: 12.5, outline: 'none', fontFamily: 'inherit', color: 'var(--ink)' }} />
      </div>

      <div style={{ display: 'inline-flex', gap: 4 }}>
        {Object.keys(colorCounts).map((c) => {
          const active = colorFilter === c;
          return (
            <button key={c} onClick={() => setColorFilter(active ? null : c)}
            style={{ appearance: 'none', display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '5px 9px 5px 7px', background: active ? 'var(--ink)' : 'var(--surface)',
              color: active ? 'var(--surface)' : 'var(--ink-2)',
              border: '.5px solid', borderColor: active ? 'var(--ink)' : 'var(--line)',
              borderRadius: 8, fontSize: 12, cursor: 'pointer' }}>
              <span style={{ width: 10, height: 10, borderRadius: 3, background: window.COLOR_META[c].chip,
                border: '.5px solid rgba(0,0,0,.1)' }} />
              {c}
              <span style={{ color: active ? 'rgba(255,255,255,.55)' : 'var(--faint)', fontSize: 11 }} className="tnum">{colorCounts[c]}</span>
            </button>);

        })}
      </div>

      <Toolbutton icon={Icon.download}>Экспорт</Toolbutton>

      {selectedCount > 0 &&
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '5px 5px 5px 11px',
        background: 'var(--ink)', color: 'var(--surface)', borderRadius: 8, fontSize: 12.5 }}>
          <span className="tnum">Выбрано: {selectedCount}</span>
          <button style={{ appearance: 'none', border: 0, background: 'rgba(255,255,255,.15)',
          color: 'inherit', padding: '4px 9px', borderRadius: 6, fontSize: 12, cursor: 'pointer' }}>Печать ярлыков</button>
          <button style={{ appearance: 'none', border: 0, background: 'rgba(255,255,255,.15)',
          color: 'inherit', padding: '4px 9px', borderRadius: 6, fontSize: 12, cursor: 'pointer' }}>Заказ на склад</button>
        </div>
      }
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// Table
// ──────────────────────────────────────────────────────────────────

function TableHeader({ cols, sort, setSort, allSelected, onToggleAll, density, theme, whUnlocked, setWhUnlocked }) {
  const padY = density === 'compact' ? '10px' : density === 'comfy' ? '14px' : '12px';
  const headerCell = (c, content) => {
    const isUzum = c.key === 'uzum';
    const isWh = c.key === 'wh';
    const tint = isUzum ? theme.uzumTint : isWh ? theme.whTint : undefined;
    return (
      <div key={c.key} style={{ display: 'flex', alignItems: 'center', gap: 5,
        paddingTop: padY, paddingBottom: padY,
        paddingLeft: tint ? 10 : (c.key === 'sku' ? 10 : 8),
        paddingRight: tint ? 12 : 8,
        justifyContent: c.align || 'flex-start',
        cursor: c.sortable ? 'pointer' : 'default',
        background: tint || 'transparent',
        color: sort.key === c.key ? 'var(--ink)' : 'var(--muted)' }}
        onClick={() => c.sortable && setSort(c.key)}>
        {content || c.label}
        {c.sortable && <span style={{ display: 'inline-flex', opacity: sort.key === c.key ? 1 : .4 }}>{Icon.sort}</span>}
      </div>);
  };
  return (
    <div style={{ display: 'grid', gridTemplateColumns: cols.template,
      background: 'var(--surface-2)',
      borderBottom: '.5px solid var(--line)', position: 'sticky', top: 0, zIndex: 3,
      padding: '0 16px',
      fontSize: 11.5, fontWeight: 500, color: 'var(--muted)', letterSpacing: '.04em',
      textTransform: 'uppercase' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', paddingTop: padY, paddingBottom: padY, paddingRight: 8 }}>
        <input type="checkbox" checked={allSelected} onChange={onToggleAll}
          style={{ width: 15, height: 15, cursor: 'pointer', accentColor: 'var(--ink)' }} />
      </div>
      {cols.list.map((c) => {
        if (c.key === 'wh') {
          return headerCell(c,
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <button onClick={(e) => { e.stopPropagation(); setWhUnlocked(!whUnlocked); }}
                title={whUnlocked ? 'Заблокировать редактирование' : 'Разблокировать редактирование'}
                style={{ appearance: 'none', display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  width: 22, height: 22, border: '.5px solid', borderRadius: 6, cursor: 'pointer',
                  background: whUnlocked ? 'var(--accent)' : 'var(--surface)',
                  color: whUnlocked ? 'var(--surface)' : 'var(--ink-2)',
                  borderColor: whUnlocked ? 'var(--accent)' : 'var(--line)' }}>
                {whUnlocked ? Icon.unlock : Icon.lock}
              </button>
              <span style={{ color: 'inherit' }}>{c.label}</span>
            </span>);
        }
        return headerCell(c);
      })}
    </div>);
}

// Editable warehouse cell — locked shows number, unlocked shows stepper input.
function WarehouseCell({ value, unlocked, onChange }) {
  if (!unlocked) {
    return (
      <span className="tnum mono" style={{
        fontSize: 15, fontWeight: 600,
        color: value === 0 ? 'var(--red)' : 'var(--ink)' }}>{value}</span>);
  }
  const stop = (e) => e.stopPropagation();
  return (
    <div onClick={stop} style={{ display: 'inline-flex', alignItems: 'center', gap: 0,
      background: 'var(--surface)', border: '.5px solid var(--accent)',
      borderRadius: 7, padding: 1,
      boxShadow: '0 0 0 3px var(--accent-soft)' }}>
      <button onClick={() => onChange(Math.max(0, value - 1))}
        style={{ appearance: 'none', width: 22, height: 22, border: 0, background: 'transparent',
          borderRadius: 5, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', color: 'var(--muted)' }}>{Icon.minus}</button>
      <input type="number" value={value} onClick={stop}
        onChange={(e) => onChange(Math.max(0, parseInt(e.target.value) || 0))}
        style={{ appearance: 'textfield', MozAppearance: 'textfield', width: 38, height: 22,
          border: 0, background: 'transparent', textAlign: 'center', fontSize: 14, fontWeight: 600,
          color: 'var(--ink)', outline: 'none',
          fontFamily: 'Geist Mono, monospace', fontVariantNumeric: 'tabular-nums' }} />
      <button onClick={() => onChange(value + 1)}
        style={{ appearance: 'none', width: 22, height: 22, border: 0, background: 'transparent',
          borderRadius: 5, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', color: 'var(--muted)' }}>{Icon.plus}</button>
    </div>);
}

function VariantRow({ v, period, sparkStyle, density, columns, checked, onCheck, thumbSize, theme,
                     whValue, whUnlocked, setWhValue }) {
  const padY = density === 'compact' ? 9 : density === 'comfy' ? 18 : 13;
  const thumbPx = thumbSize === 'sm' ? 36 : thumbSize === 'lg' ? 56 : 44;
  const sales = v.sales[period];
  const need60 = Math.max(0, Math.round(v.sales[30] / 30 * 60 - v.uzumStock - whValue));
  const cellBase = { display: 'flex', alignItems: 'center', paddingTop: padY, paddingBottom: padY };
  return (
    <div style={{ display: 'grid', gridTemplateColumns: columns.template,
      borderBottom: '.5px solid var(--line-2)',
      background: 'var(--surface)', transition: 'background .12s', padding: '0 16px' }}
      onMouseEnter={(e) => { e.currentTarget.style.background = theme.rowHover;
        e.currentTarget.querySelectorAll('[data-tint]').forEach(el => el.style.filter = 'brightness(.985)'); }}
      onMouseLeave={(e) => { e.currentTarget.style.background = 'var(--surface)';
        e.currentTarget.querySelectorAll('[data-tint]').forEach(el => el.style.filter = ''); }}>
      {/* checkbox */}
      <div style={{ ...cellBase, justifyContent: 'center', paddingRight: 8 }}>
        <input type="checkbox" checked={checked} onChange={onCheck}
          style={{ width: 15, height: 15, cursor: 'pointer', accentColor: 'var(--accent)' }} />
      </div>
      {/* photo + sku + inline status */}
      <div style={{ ...cellBase, gap: 12, minWidth: 0, paddingRight: 10 }}>
        <div style={{ width: thumbPx, height: thumbPx, borderRadius: 8, background: 'var(--surface-2)',
          border: '.5px solid var(--line)', overflow: 'hidden', flex: '0 0 auto' }}>
          <img src={v.thumb} alt="" style={{ width: '100%', height: '100%', display: 'block' }} />
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span className="mono" style={{ fontSize: 13.5, fontWeight: 500, color: 'var(--ink)',
              whiteSpace: 'nowrap' }}>{v.sku}</span>
            <StatusPill status={v.status} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4, fontSize: 12.5, color: 'var(--muted)' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: v.chip,
                border: '.5px solid rgba(0,0,0,.12)' }} />{v.color}
            </span>
            <span style={{ color: 'var(--faint)' }}>·</span>
            <span>Размер <span style={{ color: 'var(--ink-2)', fontWeight: 500 }}>{v.size}</span></span>
          </div>
        </div>
      </div>
      {/* uzum (tinted) */}
      <div data-tint style={{ ...cellBase, justifyContent: 'flex-end',
        background: theme.uzumTint, paddingLeft: 10, paddingRight: 12, transition: 'filter .12s' }}>
        <StockMeter value={v.uzumStock} />
      </div>
      {/* warehouse (tinted, editable) */}
      {columns.showWarehouse &&
        <div data-tint style={{ ...cellBase, justifyContent: 'flex-end',
          background: theme.whTint, paddingLeft: 10, paddingRight: 12, transition: 'filter .12s' }}>
          <WarehouseCell value={whValue} unlocked={whUnlocked} onChange={setWhValue} />
        </div>
      }
      {/* barcode */}
      {columns.showBarcode &&
        <div style={{ ...cellBase, paddingLeft: 14, paddingRight: 8 }}>
          <span className="mono tnum" style={{ fontSize: 12.5, color: 'var(--ink-2)', letterSpacing: '.02em' }}>
            {v.barcode}
          </span>
        </div>
      }
      {/* sales + spark */}
      <div style={{ ...cellBase, justifyContent: 'flex-end', gap: 12, paddingLeft: 12, paddingRight: 8 }}>
        <window.Spark kind={sparkStyle} data={v.series90.slice(-period)} width={112} height={28}
          color={theme.hero.accent} />
        <div className="tnum" style={{ fontSize: 15, fontWeight: 600, color: 'var(--ink)', minWidth: 28, textAlign: 'right' }}>
          {sales}
        </div>
      </div>
      {/* need 60d */}
      <div style={{ ...cellBase, justifyContent: 'flex-end', paddingLeft: 8, paddingRight: 8 }}>
        <NeedBadge n={need60} />
      </div>
      {/* actions */}
      <div style={{ ...cellBase, justifyContent: 'flex-end', paddingLeft: 4 }}>
        <button title="Действия" style={{ appearance: 'none', width: 28, height: 28,
          border: '.5px solid transparent', background: 'transparent', borderRadius: 7,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', color: 'var(--muted)' }}>{Icon.more}</button>
      </div>
    </div>);
}

function VariantTable({ variants, period, sparkStyle, density, showBarcode, showWarehouse,
  thumbSize, selected, setSelected, sort, setSort, theme,
  whOverrides, setWhValue, whUnlocked, setWhUnlocked }) {
  const getWh = (v) => whOverrides[v.id] !== undefined ? whOverrides[v.id] : v.warehouseStock;
  const cols = useMemo(() => {
    const list = [
      { key: 'sku', label: 'SKU · Статус', sortable: true },
      { key: 'uzum', label: 'Uzum', sortable: true, align: 'flex-end' }];

    if (showWarehouse) list.push({ key: 'wh', label: 'Наш склад', sortable: true, align: 'flex-end' });
    if (showBarcode) list.push({ key: 'barcode', label: 'Штрихкод' });
    list.push({ key: 'sales', label: `Продажи ${period} дн.`, sortable: true, align: 'flex-end' });
    list.push({ key: 'need', label: 'Нужно (60д)', sortable: true, align: 'flex-end' });
    list.push({ key: 'act', label: '' });
    // template: checkbox 24 | sku flex | uzum 110 | wh? 150 | barcode? 140 | sales 210 | need 120 | actions 44
    const parts = ['24px', 'minmax(280px, 1.5fr)', '110px'];
    if (showWarehouse) parts.push(whUnlocked ? '170px' : '120px');
    if (showBarcode) parts.push('140px');
    parts.push('210px', '120px', '44px');
    return { list, template: parts.join(' '), showBarcode, showWarehouse };
  }, [period, showBarcode, showWarehouse, whUnlocked]);

  const sorted = useMemo(() => {
    const arr = [...variants];
    const dir = sort.dir === 'desc' ? -1 : 1;
    arr.sort((a, b) => {
      let av, bv;
      switch (sort.key) {
        case 'sku':av = a.sku;bv = b.sku;break;
        case 'uzum':av = a.uzumStock;bv = b.uzumStock;break;
        case 'wh':av = getWh(a);bv = getWh(b);break;
        case 'sales':av = a.sales[period];bv = b.sales[period];break;
        case 'need':{
            av = Math.max(0, Math.round(a.sales[30] / 30 * 60 - a.uzumStock - getWh(a)));
            bv = Math.max(0, Math.round(b.sales[30] / 30 * 60 - b.uzumStock - getWh(b)));
            break;
          }
        default:av = a.id;bv = b.id;
      }
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    return arr;
  }, [variants, sort, period]);

  const allSelected = sorted.length > 0 && sorted.every((v) => selected[v.id]);
  const toggleAll = () => {
    const next = { ...selected };
    if (allSelected) sorted.forEach((v) => delete next[v.id]);else
    sorted.forEach((v) => next[v.id] = true);
    setSelected(next);
  };

  const onSort = (key) => {
    setSort((prev) => prev.key === key ?
    { key, dir: prev.dir === 'desc' ? 'asc' : 'desc' } :
    { key, dir: key === 'sku' ? 'asc' : 'desc' });
  };

  return (
    <div style={{ background: 'var(--surface)', borderRadius: '0 0 var(--radius) var(--radius)', overflow: 'hidden' }}>
      <TableHeader cols={cols} sort={sort} setSort={onSort} allSelected={allSelected}
        onToggleAll={toggleAll} density={density} theme={theme}
        whUnlocked={whUnlocked} setWhUnlocked={setWhUnlocked} />
      {sorted.map((v) =>
        <VariantRow key={v.id} v={v} period={period} sparkStyle={sparkStyle}
          density={density} columns={cols}
          checked={!!selected[v.id]} thumbSize={thumbSize} theme={theme}
          whValue={getWh(v)} whUnlocked={whUnlocked}
          setWhValue={(n) => setWhValue(v.id, n)}
          onCheck={() => setSelected((prev) => {
            const next = { ...prev };
            if (next[v.id]) delete next[v.id]; else next[v.id] = true;
            return next;
          })} />
      )}
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// Footer summary
// ──────────────────────────────────────────────────────────────────
function TableFooter({ variants, period }) {
  const totals = useMemo(() => {
    let need = 0;
    for (const v of variants) {
      need += Math.max(0, Math.round(v.sales[30] / 30 * 60 - v.uzumStock - v.warehouseStock));
    }
    return { need };
  }, [variants, period]);
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '14px 18px', background: 'var(--surface-2)', borderTop: '.5px solid var(--line)',
      borderRadius: '0 0 var(--radius) var(--radius)', fontSize: 13.5, color: 'var(--muted)' }}>
      <span>Показано <span className="tnum" style={{ color: 'var(--ink)', fontWeight: 500 }}>{variants.length}</span> вариантов</span>
      <span>Нужно заказать (60д): <span className="tnum mono" style={{ color: totals.need > 0 ? 'var(--red)' : 'var(--ink)', fontWeight: 600 }}>{totals.need}</span> шт.</span>
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// App
// ──────────────────────────────────────────────────────────────────

// ──────────────────────────────────────────────────────────────────
// Style switcher (the headline 3-version control)
// ──────────────────────────────────────────────────────────────────
function StyleSwitcher({ value, onChange }) {
  const order = ['uzum', 'trader', 'premium'];
  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 10,
      padding: '5px 8px 5px 12px', background: 'var(--surface)',
      border: '.5px solid var(--line)', borderRadius: 999,
      boxShadow: 'var(--shadow-sm)' }}>
      <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: '.04em',
        textTransform: 'uppercase', color: 'var(--muted)' }}>Стиль</span>
      <div style={{ display: 'inline-flex', gap: 3 }}>
        {order.map((k) => {
          const th = window.THEMES[k];
          const active = value === k;
          return (
            <button key={k} onClick={() => onChange(k)}
            style={{ appearance: 'none', display: 'inline-flex', alignItems: 'center', gap: 7,
              padding: '5px 11px 5px 7px',
              background: active ? 'var(--ink)' : 'transparent',
              color: active ? 'var(--surface)' : 'var(--ink-2)',
              border: '.5px solid', borderColor: active ? 'var(--ink)' : 'transparent',
              borderRadius: 999, fontSize: 12.5, fontWeight: active ? 600 : 500,
              cursor: 'pointer', whiteSpace: 'nowrap', transition: 'all .15s' }}>
              <span style={{ display: 'inline-flex', width: 18, height: 14, borderRadius: 3, overflow: 'hidden',
                border: '.5px solid rgba(0,0,0,.1)' }}>
                {th.swatch.map((c, i) =>
                <span key={i} style={{ flex: 1, background: c }} />
                )}
              </span>
              {th.label}
            </button>);

        })}
      </div>
    </div>);

}

// ──────────────────────────────────────────────────────────────────
// App
// ──────────────────────────────────────────────────────────────────

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const theme = window.THEMES[t.theme] || window.THEMES.uzum;
  const [period, setPeriod] = useState(30);
  const [query, setQuery] = useState('');
  const [colorFilter, setColorFilter] = useState(null);
  const [statusFilter, setStatusFilter] = useState(null);
  const [selected, setSelected] = useState({});
  const [sort, setSort] = useState({ key: 'sku', dir: 'asc' });
  const [whOverrides, setWhOverrides] = useState({});
  const [whUnlocked, setWhUnlocked] = useState(false);

  const setWhValue = (id, n) => setWhOverrides((prev) => ({ ...prev, [id]: n }));

  // Variants with live (possibly-overridden) warehouseStock applied.
  const vsLive = useMemo(() => window.VARIANTS.map((v) => ({
    ...v,
    warehouseStock: whOverrides[v.id] !== undefined ? whOverrides[v.id] : v.warehouseStock,
  })), [whOverrides]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return vsLive.filter((v) => {
      if (colorFilter && v.color !== colorFilter) return false;
      if (statusFilter && v.status !== statusFilter) return false;
      if (q && !(v.sku.toLowerCase().includes(q) ||
      String(v.size).includes(q) ||
      v.barcode.includes(q) ||
      v.color.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [query, colorFilter, statusFilter, vsLive]);

  const selectedCount = Object.keys(selected).length;

  useEffect(() => {
    const root = document.documentElement;
    Object.entries(theme.cssVars).forEach(([k, v]) => root.style.setProperty(k, v));
    document.body.style.background = theme.cssVars['--bg'];
    document.body.style.color = theme.cssVars['--ink'];
    if (theme.dark) root.classList.add('theme-dark');else root.classList.remove('theme-dark');
    if (theme.editorial) root.classList.add('theme-editorial');else root.classList.remove('theme-editorial');
  }, [theme]);

  return (
    <div style={{ maxWidth: 1480, margin: '0 auto' }}>
      <ProductHeader />
      <ProductCard styleSwitcher={
        <StyleSwitcher value={t.theme} onChange={(v) => setTweak('theme', v)} />
      } />
      <StatStrip variants={vsLive} periodDays={period} theme={theme} />

      <div style={{ padding: '0 28px 28px' }}>
        <div style={{ background: 'var(--surface)', border: '.5px solid var(--line)', borderRadius: 'var(--radius)',
          boxShadow: 'var(--shadow-md)', overflow: 'hidden' }}>
          <Toolbar period={period} setPeriod={setPeriod}
            query={query} setQuery={setQuery}
            colorFilter={colorFilter} setColorFilter={setColorFilter}
            statusFilter={statusFilter} setStatusFilter={setStatusFilter}
            selectedCount={selectedCount} variants={filtered} theme={theme} />
          <VariantTable variants={filtered} period={period}
            sparkStyle={t.sparkStyle} density={t.density}
            showBarcode={t.showBarcode} showWarehouse={t.showWarehouse}
            thumbSize={t.thumbSize}
            selected={selected} setSelected={setSelected}
            sort={sort} setSort={setSort}
            theme={theme}
            whOverrides={whOverrides} setWhValue={setWhValue}
            whUnlocked={whUnlocked} setWhUnlocked={setWhUnlocked} />
          <TableFooter variants={filtered} period={period} />
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="Дизайн-стиль" />
        <TweakRadio label="Стиль" value={t.theme}
        options={[
        { value: 'uzum', label: 'Uzum' },
        { value: 'trader', label: 'Трейдер' },
        { value: 'premium', label: 'Премиум' }]
        }
        onChange={(v) => setTweak('theme', v)} />

        <TweakSection label="График продаж" />
        <TweakRadio label="Тип искры" value={t.sparkStyle}
        options={[
        { value: 'candle', label: 'Свеча' },
        { value: 'bar', label: 'Бары' },
        { value: 'line', label: 'Линия' }]
        }
        onChange={(v) => setTweak('sparkStyle', v)} />

        <TweakSection label="Таблица" />
        <TweakRadio label="Плотность" value={t.density}
        options={[
        { value: 'compact', label: 'Узкая' },
        { value: 'regular', label: 'Обычная' },
        { value: 'comfy', label: 'Просторная' }]
        }
        onChange={(v) => setTweak('density', v)} />
        <TweakRadio label="Размер фото" value={t.thumbSize}
        options={[
        { value: 'sm', label: 'S' },
        { value: 'md', label: 'M' },
        { value: 'lg', label: 'L' }]
        }
        onChange={(v) => setTweak('thumbSize', v)} />
        <TweakToggle label="Колонка «Наш склад»" value={t.showWarehouse}
        onChange={(v) => setTweak('showWarehouse', v)} />
        <TweakToggle label="Колонка «Штрихкод»" value={t.showBarcode}
        onChange={(v) => setTweak('showBarcode', v)} />
      </TweaksPanel>
    </div>);

}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);