const { useState, useMemo, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "sparkStyle": "candle",
  "density": "regular",
  "accent": "#6d4dff",
  "showBarcode": true,
  "showWarehouse": true,
  "thumbSize": "md",
  "stickyHero": true
}/*EDITMODE-END*/;

// ──────────────────────────────────────────────────────────────────
// Small UI atoms
// ──────────────────────────────────────────────────────────────────

const Icon = {
  back:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M15 18l-6-6 6-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  search: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.8"/><path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>,
  cal:    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><rect x="3.5" y="5" width="17" height="15" rx="2.5" stroke="currentColor" strokeWidth="1.6"/><path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg>,
  filter: <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 5h16M7 12h10M10 19h4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>,
  download:<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 4v12m0 0l-4-4m4 4l4-4M5 20h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  print:  <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M7 9V4h10v5M7 18h10v3H7zM5 9h14a2 2 0 012 2v5a2 2 0 01-2 2h-2v-3H7v3H5a2 2 0 01-2-2v-5a2 2 0 012-2z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/></svg>,
  more:   <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>,
  up:     <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 15l6-6 6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  down:   <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  chev:   <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  warn:   <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M12 4l9.5 16H2.5L12 4z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"/><path d="M12 10v4M12 17.5v.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>,
  check:  <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M5 12l4 4 10-10" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  sort:   <svg width="10" height="10" viewBox="0 0 24 24" fill="none"><path d="M8 9l4-4 4 4M8 15l4 4 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  bell:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M6 16V11a6 6 0 0112 0v5l1.5 2H4.5L6 16z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/><path d="M10 21h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg>,
  refresh:<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 12a8 8 0 0114-5.3L20 4M20 4v5h-5M20 12a8 8 0 01-14 5.3L4 20M4 20v-5h5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg>,
  edit:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 20h4l10-10-4-4L4 16v4z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/><path d="M14 6l4 4" stroke="currentColor" strokeWidth="1.6"/></svg>,
};

function fmt(n){ return new Intl.NumberFormat('ru-RU').format(n); }
function fmtMoney(n){ return new Intl.NumberFormat('ru-RU').format(Math.round(n)) + ' сум'; }
function fmtShort(n){
  if(n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,'') + ' млн';
  if(n >= 1e3) return (n/1e3).toFixed(n>=1e4?0:1).replace(/\.0$/,'') + ' тыс.';
  return String(n);
}

function StatusPill({ status }){
  const map = {
    ok:    { bg:'var(--green-soft)', fg:'var(--green)', label:'Активен',     dot:'#16a34a' },
    low:   { bg:'var(--amber-soft)', fg:'var(--amber)', label:'Заканчивается', dot:'#d97706' },
    out:   { bg:'var(--red-soft)',   fg:'var(--red)',   label:'Нет в наличии', dot:'#dc2626' },
    stale: { bg:'#eef0f3',           fg:'#6b7280',     label:'Нет продаж',    dot:'#9ca3af' },
  };
  const s = map[status] || map.ok;
  return (
    <span style={{display:'inline-flex',alignItems:'center',gap:6,padding:'3px 9px 3px 7px',
      borderRadius:999,background:s.bg,color:s.fg,fontSize:11.5,fontWeight:500,
      whiteSpace:'nowrap'}}>
      <span style={{width:6,height:6,borderRadius:'50%',background:s.dot}}/>{s.label}
    </span>
  );
}

function StockMeter({ value, max=20, color='#0d1117', subdued=false }){
  const pct = Math.min(1, value / max);
  return (
    <div style={{display:'flex',alignItems:'center',gap:8,minWidth:0}}>
      <span className="tnum mono" style={{
        fontSize:13,fontWeight:600,color: value===0 ? '#dc2626' : 'var(--ink)',
        minWidth:18,textAlign:'right'}}>{value}</span>
      <div style={{flex:'0 0 56px',height:4,borderRadius:99,background:subdued?'#f1f2f4':'#eef0f3',position:'relative',overflow:'hidden'}}>
        <div style={{position:'absolute',inset:0,width:`${pct*100}%`,
          background: value===0 ? '#fca5a5' : color, borderRadius:99}}/>
      </div>
    </div>
  );
}

function NeedBadge({ n }){
  if(n <= 0) return (
    <span style={{display:'inline-flex',alignItems:'center',gap:5,color:'var(--green)',
      fontWeight:600,fontSize:13}} className="tnum">
      <span style={{display:'inline-flex'}}>{Icon.check}</span>хватает
    </span>
  );
  const severity = n >= 10 ? 'high' : n >= 5 ? 'mid' : 'low';
  const fg = severity==='high' ? '#dc2626' : severity==='mid' ? '#ea580c' : '#d97706';
  const bg = severity==='high' ? '#fdeaea' : severity==='mid' ? '#fdecdc' : '#fdf2e3';
  return (
    <span style={{display:'inline-flex',alignItems:'center',gap:5,padding:'2px 8px',
      borderRadius:6,background:bg,color:fg,fontSize:13,fontWeight:600}} className="tnum">
      +{n} шт.
    </span>
  );
}

function SegBar({ options, value, onChange }){
  const activeIdx = options.findIndex(o => o.value === value);
  return (
    <div style={{display:'inline-flex',padding:3,background:'#f1f2f4',borderRadius:9,
      border:'.5px solid var(--line)',position:'relative'}}>
      {options.map((o, i)=>(
        <button key={o.value} onClick={()=>onChange(o.value)}
          style={{appearance:'none',border:0,background:'transparent',padding:'5px 12px',
            borderRadius:7,fontSize:12.5,fontWeight: i===activeIdx ? 600 : 500,
            color: i===activeIdx ? 'var(--ink)' : 'var(--muted)',
            boxShadow: i===activeIdx ? '0 1px 2px rgba(15,17,24,.08)' : 'none',
            background: i===activeIdx ? '#fff' : 'transparent',
            cursor:'pointer',transition:'all .15s',whiteSpace:'nowrap'}}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Toolbutton({ children, kind='ghost', onClick, title, icon }){
  const styles = {
    ghost: { bg:'#fff', fg:'var(--ink)', border:'.5px solid var(--line)' },
    solid: { bg:'var(--ink)', fg:'#fff', border:'.5px solid var(--ink)' },
    soft:  { bg:'#f1f2f4', fg:'var(--ink)', border:'.5px solid transparent' },
  }[kind];
  return (
    <button onClick={onClick} title={title}
      style={{display:'inline-flex',alignItems:'center',gap:6,padding:'7px 11px',
        borderRadius:8,fontSize:12.5,fontWeight:500,
        background:styles.bg,color:styles.fg,border:styles.border,
        cursor:'pointer',whiteSpace:'nowrap',boxShadow:'var(--shadow-sm)'}}>
      {icon && <span style={{display:'inline-flex',color: kind==='solid' ? 'rgba(255,255,255,.85)' : 'var(--muted)'}}>{icon}</span>}
      {children}
    </button>
  );
}

// ──────────────────────────────────────────────────────────────────
// Header
// ──────────────────────────────────────────────────────────────────

function ProductHeader(){
  return (
    <div style={{display:'flex',alignItems:'center',gap:14,padding:'18px 28px',
      background:'#fff',borderBottom:'.5px solid var(--line)'}}>
      <button style={{appearance:'none',display:'inline-flex',alignItems:'center',gap:6,
        padding:'7px 11px 7px 9px',borderRadius:8,fontSize:12.5,fontWeight:500,
        background:'#fff',border:'.5px solid var(--line)',color:'var(--ink-2)',cursor:'pointer'}}>
        {Icon.back}<span>Все товары</span>
      </button>
      <span style={{width:1,height:22,background:'var(--line)'}}/>
      <nav style={{display:'flex',alignItems:'center',gap:8,fontSize:12.5,color:'var(--muted)'}}>
        <span>Каталог</span>
        <span style={{color:'var(--faint)'}}>/</span>
        <span>Кольца и перстни</span>
        <span style={{color:'var(--faint)'}}>/</span>
        <span style={{color:'var(--ink)'}}>Кольцо для измерения температуры</span>
      </nav>
      <div style={{flex:1}}/>
      <Toolbutton icon={Icon.refresh}>Обновить</Toolbutton>
      <Toolbutton icon={Icon.bell}>Оповещения <span style={{
        display:'inline-flex',alignItems:'center',justifyContent:'center',minWidth:16,height:16,
        background:'var(--red)',color:'#fff',fontSize:10,fontWeight:600,borderRadius:99,
        padding:'0 5px',marginLeft:2}} className="tnum">3</span></Toolbutton>
      <Toolbutton icon={Icon.edit} kind="solid">Редактировать товар</Toolbutton>
    </div>
  );
}

function ProductCard(){
  return (
    <div style={{display:'flex',alignItems:'center',gap:18,padding:'22px 28px 18px'}}>
      <div style={{width:74,height:74,borderRadius:14,background:'#f1f2f4',
        border:'.5px solid var(--line)',overflow:'hidden',flex:'0 0 auto',
        display:'flex',alignItems:'center',justifyContent:'center'}}>
        <img src={window.COLOR_META['Чёрный'].thumb} alt="" style={{width:'100%',height:'100%',objectFit:'cover'}}/>
      </div>
      <div style={{minWidth:0,flex:1}}>
        <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:4}}>
          <h1 style={{margin:0,fontSize:22,fontWeight:600,letterSpacing:'-.01em'}}>
            Кольцо для измерения температуры
          </h1>
          <span style={{display:'inline-flex',alignItems:'center',gap:5,padding:'3px 8px',
            borderRadius:6,background:'var(--green-soft)',color:'var(--green)',fontSize:11.5,fontWeight:500}}>
            <span style={{width:5,height:5,borderRadius:'50%',background:'#16a34a'}}/>
            Опубликован
          </span>
        </div>
        <div style={{display:'flex',alignItems:'center',gap:18,fontSize:12.5,color:'var(--muted)'}}>
          <span>Uzum ID: <span className="mono" style={{color:'var(--ink-2)'}}>311964</span></span>
          <span style={{color:'var(--faint)'}}>·</span>
          <span>Категория: <span style={{color:'var(--ink-2)'}}>Кольца и перстни</span></span>
          <span style={{color:'var(--faint)'}}>·</span>
          <span>Бренд: <span style={{color:'var(--ink-2)'}}>LUXUZ</span></span>
          <span style={{color:'var(--faint)'}}>·</span>
          <span>Обновлено <span style={{color:'var(--ink-2)'}}>сегодня в 14:32</span></span>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// Stat strip
// ──────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, delta, deltaLabel, children, wide, accent }){
  const positive = delta == null ? null : delta >= 0;
  return (
    <div style={{
      flex: wide ? '2 1 0' : '1 1 0', minWidth: 0,
      background:'#fff',border:'.5px solid var(--line)',borderRadius:'var(--radius)',
      padding:'14px 16px',display:'flex',flexDirection:'column',gap:6,
      boxShadow:'var(--shadow-sm)'}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',gap:8}}>
        <span style={{fontSize:11.5,fontWeight:500,letterSpacing:'.02em',
          textTransform:'uppercase',color:'var(--muted)'}}>{label}</span>
        {delta != null && (
          <span style={{display:'inline-flex',alignItems:'center',gap:3,
            padding:'2px 6px',borderRadius:6,fontSize:11,fontWeight:600,
            background: positive ? 'var(--green-soft)' : 'var(--red-soft)',
            color: positive ? 'var(--green)' : 'var(--red)'}} className="tnum">
            {positive ? Icon.up : Icon.down}{Math.abs(delta)}%
          </span>
        )}
      </div>
      <div style={{display:'flex',alignItems:'baseline',gap:8,flexWrap:'wrap'}}>
        <span style={{fontSize:26,fontWeight:600,letterSpacing:'-.02em',color:accent || 'var(--ink)'}} className="tnum">
          {value}
        </span>
        {sub && <span style={{fontSize:12,color:'var(--muted)'}}>{sub}</span>}
      </div>
      {deltaLabel && <span style={{fontSize:11.5,color:'var(--faint)'}}>{deltaLabel}</span>}
      {children}
    </div>
  );
}

function HeroStat({ totalSales, totalRev, periodDays, series }){
  return (
    <div style={{
      flex:'2.5 1 0', minWidth: 0,
      background:'linear-gradient(180deg,#0d1117 0%, #1b2129 100%)',
      color:'#fff', border:'.5px solid #0d1117',
      borderRadius:'var(--radius)', padding:'14px 18px 6px',
      display:'flex',flexDirection:'column',gap:6,
      boxShadow:'0 1px 2px rgba(15,17,24,.1), 0 8px 24px rgba(15,17,24,.08)',
      position:'relative',overflow:'hidden'}}>
      <div style={{display:'flex',alignItems:'flex-start',justifyContent:'space-between',gap:12}}>
        <div>
          <div style={{fontSize:11.5,fontWeight:500,letterSpacing:'.02em',
            textTransform:'uppercase',color:'rgba(255,255,255,.55)'}}>
            Продажи за {periodDays} дн.
          </div>
          <div style={{display:'flex',alignItems:'baseline',gap:10,marginTop:4}}>
            <span style={{fontSize:30,fontWeight:600,letterSpacing:'-.02em'}} className="tnum">
              {fmt(totalSales)} <span style={{fontSize:14,fontWeight:500,color:'rgba(255,255,255,.55)'}}>шт.</span>
            </span>
            <span style={{display:'inline-flex',alignItems:'center',gap:3,
              padding:'2px 7px',borderRadius:6,fontSize:11,fontWeight:600,
              background:'rgba(34,197,94,.18)',color:'#4ade80'}} className="tnum">
              {Icon.up}12%
            </span>
          </div>
          <div style={{fontSize:12,color:'rgba(255,255,255,.6)',marginTop:2}}>
            <span className="tnum">{fmtMoney(totalRev)}</span> · ср. чек <span className="tnum">{fmtMoney(totalRev/Math.max(1,totalSales))}</span>
          </div>
        </div>
        <div style={{display:'flex',gap:14,fontSize:11,color:'rgba(255,255,255,.55)'}}>
          <span style={{display:'inline-flex',alignItems:'center',gap:5}}>
            <span style={{width:8,height:8,background:'#22c55e',borderRadius:1}}/>день роста
          </span>
          <span style={{display:'inline-flex',alignItems:'center',gap:5}}>
            <span style={{width:8,height:8,background:'#ef4444',borderRadius:1}}/>день падения
          </span>
        </div>
      </div>
      <div style={{marginTop:8,filter:'drop-shadow(0 1px 0 rgba(0,0,0,.2))'}}>
        <window.HeroCandle data={series} width={620} height={96}/>
      </div>
    </div>
  );
}

function StatStrip({ variants, periodDays }){
  const totals = useMemo(()=>{
    const t = { sales:0, rev:0, uzum:0, wh:0, low:0, out:0, need:0, bestId:-1, bestSales:-1 };
    for(const v of variants){
      const s = v.sales[periodDays] || 0;
      t.sales += s;
      t.rev += s * v.price;
      t.uzum += v.uzumStock;
      t.wh += v.warehouseStock;
      if(v.status === 'low') t.low++;
      if(v.status === 'out') t.out++;
      // need60 = (sales[30]/30)*60 - uzumStock - warehouseStock
      const need = Math.max(0, Math.round((v.sales[30]/30) * 60 - v.uzumStock - v.warehouseStock));
      t.need += need;
      if(s > t.bestSales){ t.bestSales = s; t.bestId = v.id; }
    }
    const best = variants.find(v => v.id === t.bestId);
    return { ...t, best };
  }, [variants, periodDays]);

  const series = useMemo(()=>window.aggregateSeries(periodDays), [periodDays]);

  return (
    <div style={{display:'flex',gap:12,padding:'4px 28px 18px',alignItems:'stretch'}}>
      <HeroStat totalSales={totals.sales} totalRev={totals.rev}
        periodDays={periodDays} series={series}/>

      <StatCard label="На складе Uzum" value={fmt(totals.uzum)}
        sub={`шт. в ${variants.length} SKU`}
        deltaLabel={`+ ${fmt(totals.wh)} шт. на нашем складе`}>
        <div style={{marginTop:'auto',display:'flex',gap:4,alignItems:'center'}}>
          {variants.map((v,i)=>(
            <div key={i} title={`${v.color}, ${v.size}: ${v.uzumStock} шт.`}
              style={{flex:1,height:18,borderRadius:2,
                background: v.uzumStock===0 ? '#fee2e2' :
                            v.uzumStock<=2 ? '#fde68a' :
                            v.uzumStock<=5 ? '#bbf7d0' : '#22c55e',
                opacity: v.uzumStock===0 ? 1 : .4 + Math.min(.6, v.uzumStock/12)}}/>
          ))}
        </div>
      </StatCard>

      <StatCard label="Требуют пополнения" value={totals.out + totals.low}
        sub={`из ${variants.length} SKU`}
        accent={totals.out+totals.low > 0 ? 'var(--red)' : 'var(--ink)'}>
        <div style={{display:'flex',gap:8,marginTop:'auto'}}>
          <span style={{display:'inline-flex',alignItems:'center',gap:5,fontSize:11.5,
            color:'var(--red)'}}>
            <span style={{width:6,height:6,borderRadius:'50%',background:'var(--red)'}}/>
            {totals.out} нет в наличии
          </span>
          <span style={{display:'inline-flex',alignItems:'center',gap:5,fontSize:11.5,
            color:'var(--amber)'}}>
            <span style={{width:6,height:6,borderRadius:'50%',background:'var(--amber)'}}/>
            {totals.low} заканчивается
          </span>
        </div>
      </StatCard>

      <StatCard label="Нужно заказать (60д)" value={fmt(totals.need)} sub="шт. всего"
        accent="var(--ink)">
        <div style={{marginTop:'auto',display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontSize:11.5,color:'var(--muted)'}}>На основе продаж за 30 дн.</span>
          <button style={{appearance:'none',border:0,background:'var(--ink)',color:'#fff',
            padding:'4px 9px',borderRadius:6,fontSize:11.5,fontWeight:500,cursor:'pointer'}}>
            Создать заказ →
          </button>
        </div>
      </StatCard>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// Toolbar
// ──────────────────────────────────────────────────────────────────

const PERIODS = [
  { value: 7,  label: '7 дн.' },
  { value: 10, label: '10 дн.' },
  { value: 15, label: '15 дн.' },
  { value: 30, label: '30 дн.' },
  { value: 60, label: '60 дн.' },
  { value: 90, label: '90 дн.' },
];

function Toolbar({ period, setPeriod, query, setQuery, colorFilter, setColorFilter,
                   statusFilter, setStatusFilter, selectedCount, variants }){
  const colorCounts = useMemo(()=>{
    const m = {};
    for(const v of variants) m[v.color] = (m[v.color]||0)+1;
    return m;
  }, [variants]);
  return (
    <div style={{display:'flex',alignItems:'center',gap:10,padding:'12px 16px',
      background:'#fff',borderBottom:'.5px solid var(--line)',borderTopLeftRadius:'var(--radius)',
      borderTopRightRadius:'var(--radius)',flexWrap:'wrap'}}>
      <div style={{display:'flex',alignItems:'center',gap:8}}>
        <span style={{fontSize:13,fontWeight:600}}>Все варианты</span>
        <span style={{display:'inline-flex',alignItems:'center',justifyContent:'center',
          minWidth:22,height:20,padding:'0 6px',background:'#f1f2f4',borderRadius:6,
          fontSize:11.5,fontWeight:500,color:'var(--muted)'}} className="tnum">
          {variants.length}
        </span>
      </div>
      <span style={{width:1,height:22,background:'var(--line)',margin:'0 4px'}}/>
      <SegBar options={PERIODS} value={period} onChange={setPeriod}/>
      <button style={{appearance:'none',display:'inline-flex',alignItems:'center',gap:6,
        padding:'6px 11px',background:'#fff',border:'.5px solid var(--line)',borderRadius:8,
        fontSize:12.5,color:'var(--ink-2)',cursor:'pointer'}}>
        {Icon.cal}<span>Период</span><span style={{color:'var(--faint)'}}>{Icon.chev}</span>
      </button>

      <div style={{flex:1}}/>

      <div style={{position:'relative',display:'inline-flex',alignItems:'center'}}>
        <span style={{position:'absolute',left:10,color:'var(--faint)',pointerEvents:'none',display:'inline-flex'}}>{Icon.search}</span>
        <input value={query} onChange={e=>setQuery(e.target.value)}
          placeholder="Поиск по SKU, размеру, штрихкоду…"
          style={{appearance:'none',width:260,height:32,padding:'0 12px 0 32px',
            background:'#f6f7f9',border:'.5px solid transparent',borderRadius:8,
            fontSize:12.5,outline:'none',fontFamily:'inherit',color:'var(--ink)'}}/>
      </div>

      <div style={{display:'inline-flex',gap:4}}>
        {Object.keys(colorCounts).map(c=>{
          const active = colorFilter === c;
          return (
            <button key={c} onClick={()=>setColorFilter(active ? null : c)}
              style={{appearance:'none',display:'inline-flex',alignItems:'center',gap:6,
                padding:'5px 9px 5px 7px',background: active ? 'var(--ink)' : '#fff',
                color: active ? '#fff' : 'var(--ink-2)',
                border:'.5px solid', borderColor: active ? 'var(--ink)' : 'var(--line)',
                borderRadius:8,fontSize:12,cursor:'pointer'}}>
              <span style={{width:10,height:10,borderRadius:3,background:window.COLOR_META[c].chip,
                border:'.5px solid rgba(0,0,0,.1)'}}/>
              {c}
              <span style={{color: active ? 'rgba(255,255,255,.55)' : 'var(--faint)',fontSize:11}} className="tnum">{colorCounts[c]}</span>
            </button>
          );
        })}
      </div>

      <Toolbutton icon={Icon.filter}>Фильтр</Toolbutton>
      <Toolbutton icon={Icon.download}>Экспорт</Toolbutton>

      {selectedCount > 0 && (
        <div style={{display:'inline-flex',alignItems:'center',gap:8,padding:'5px 5px 5px 11px',
          background:'var(--ink)',color:'#fff',borderRadius:8,fontSize:12.5}}>
          <span className="tnum">Выбрано: {selectedCount}</span>
          <button style={{appearance:'none',border:0,background:'rgba(255,255,255,.15)',
            color:'#fff',padding:'4px 9px',borderRadius:6,fontSize:12,cursor:'pointer'}}>Печать ярлыков</button>
          <button style={{appearance:'none',border:0,background:'rgba(255,255,255,.15)',
            color:'#fff',padding:'4px 9px',borderRadius:6,fontSize:12,cursor:'pointer'}}>Заказ на склад</button>
        </div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// Table
// ──────────────────────────────────────────────────────────────────

function TableHeader({ cols, sort, setSort, allSelected, onToggleAll, density }){
  const padY = density === 'compact' ? '8px' : density === 'comfy' ? '14px' : '11px';
  return (
    <div style={{display:'grid',gridTemplateColumns: cols.template,
      alignItems:'center',padding:`${padY} 16px`,background:'#fafbfc',
      borderBottom:'.5px solid var(--line)',position:'sticky',top:0,zIndex:3,
      gap:12,fontSize:11,fontWeight:500,color:'var(--muted)',letterSpacing:'.04em',
      textTransform:'uppercase'}}>
      <input type="checkbox" checked={allSelected} onChange={onToggleAll}
        style={{width:14,height:14,cursor:'pointer',accentColor:'var(--ink)'}}/>
      {cols.list.map(c => (
        <div key={c.key} style={{display:'flex',alignItems:'center',gap:4,
          justifyContent: c.align || 'flex-start',cursor: c.sortable ? 'pointer' : 'default',
          color: sort.key === c.key ? 'var(--ink)' : 'var(--muted)'}}
          onClick={()=>c.sortable && setSort(c.key)}>
          {c.label}
          {c.sortable && <span style={{display:'inline-flex',opacity: sort.key===c.key ? 1 : .4}}>{Icon.sort}</span>}
        </div>
      ))}
    </div>
  );
}

function VariantRow({ v, period, sparkStyle, density, columns, checked, onCheck, thumbSize }){
  const padY = density === 'compact' ? 8 : density === 'comfy' ? 18 : 12;
  const thumbPx = thumbSize === 'sm' ? 36 : thumbSize === 'lg' ? 56 : 44;
  const sales = v.sales[period];
  const need60 = Math.max(0, Math.round((v.sales[30]/30) * 60 - v.uzumStock - v.warehouseStock));
  return (
    <div style={{display:'grid',gridTemplateColumns: columns.template,alignItems:'center',
      padding:`${padY}px 16px`,borderBottom:'.5px solid var(--line-2)',gap:12,
      background:'#fff',transition:'background .12s'}}
      onMouseEnter={e=>e.currentTarget.style.background='#fafbfc'}
      onMouseLeave={e=>e.currentTarget.style.background='#fff'}>
      <input type="checkbox" checked={checked} onChange={onCheck}
        style={{width:14,height:14,cursor:'pointer',accentColor:'var(--ink)'}}/>
      {/* photo + sku */}
      <div style={{display:'flex',alignItems:'center',gap:12,minWidth:0}}>
        <div style={{width:thumbPx,height:thumbPx,borderRadius:8,background:'#f3f4f6',
          border:'.5px solid var(--line)',overflow:'hidden',flex:'0 0 auto'}}>
          <img src={v.thumb} alt="" style={{width:'100%',height:'100%',display:'block'}}/>
        </div>
        <div style={{minWidth:0}}>
          <div className="mono" style={{fontSize:12.5,fontWeight:500,color:'var(--ink)',
            whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis'}}>{v.sku}</div>
          <div style={{display:'flex',alignItems:'center',gap:8,marginTop:3,fontSize:11.5,color:'var(--muted)'}}>
            <span style={{display:'inline-flex',alignItems:'center',gap:5}}>
              <span style={{width:8,height:8,borderRadius:'50%',background:v.chip,
                border:'.5px solid rgba(0,0,0,.12)'}}/>{v.color}
            </span>
            <span style={{color:'var(--faint)'}}>·</span>
            <span>Размер <span style={{color:'var(--ink-2)',fontWeight:500}}>{v.size}</span></span>
          </div>
        </div>
      </div>
      {/* status */}
      <div><StatusPill status={v.status}/></div>
      {/* uzum */}
      <div style={{justifySelf:'end'}}><StockMeter value={v.uzumStock} max={15} color="#6d4dff"/></div>
      {/* warehouse */}
      {columns.showWarehouse && (
        <div style={{justifySelf:'end'}}><StockMeter value={v.warehouseStock} max={20} color="#0ea5e9" subdued/></div>
      )}
      {/* barcode */}
      {columns.showBarcode && (
        <div className="mono tnum" style={{fontSize:12,color:'var(--ink-2)',letterSpacing:'.02em'}}>
          {v.barcode}
        </div>
      )}
      {/* sales + spark */}
      <div style={{display:'flex',alignItems:'center',gap:12,justifyContent:'flex-end'}}>
        <window.Spark kind={sparkStyle} data={v.series90.slice(-period)} width={112} height={28}/>
        <div style={{textAlign:'right',minWidth:46}}>
          <div className="tnum" style={{fontSize:14,fontWeight:600,color:'var(--ink)'}}>{sales}</div>
          <div className="tnum" style={{fontSize:10.5,color:'var(--faint)'}}>
            {(sales/period).toFixed(sales/period >= 1 ? 1 : 2)}/день
          </div>
        </div>
      </div>
      {/* need 60d */}
      <div style={{justifySelf:'end'}}><NeedBadge n={need60}/></div>
      {/* actions */}
      <div style={{display:'flex',justifyContent:'flex-end',gap:4}}>
        <button title="Печать ярлыка" style={{appearance:'none',width:28,height:28,
          border:'.5px solid var(--line)',background:'#fff',borderRadius:7,
          display:'inline-flex',alignItems:'center',justifyContent:'center',
          cursor:'pointer',color:'var(--muted)'}}>{Icon.print}</button>
        <button title="Действия" style={{appearance:'none',width:28,height:28,
          border:'.5px solid transparent',background:'transparent',borderRadius:7,
          display:'inline-flex',alignItems:'center',justifyContent:'center',
          cursor:'pointer',color:'var(--muted)'}}>{Icon.more}</button>
      </div>
    </div>
  );
}

function VariantTable({ variants, period, sparkStyle, density, showBarcode, showWarehouse,
                        thumbSize, selected, setSelected, sort, setSort }){
  const cols = useMemo(()=>{
    const list = [
      { key:'sku',      label:'SKU / Цвет / Размер', sortable:true },
      { key:'status',   label:'Статус' },
      { key:'uzum',     label:'Uzum (шт.)', sortable:true, align:'flex-end' },
    ];
    if(showWarehouse) list.push({ key:'wh', label:'Наш склад', sortable:true, align:'flex-end' });
    if(showBarcode)   list.push({ key:'barcode', label:'Штрихкод' });
    list.push({ key:'sales',  label:`Продажи ${period} дн.`, sortable:true, align:'flex-end' });
    list.push({ key:'need',   label:'Нужно (60д)', sortable:true, align:'flex-end' });
    list.push({ key:'act',    label:'' });
    // template: checkbox 18 | sku flexible | status 130 | uzum 130 | wh? 110 | barcode? 130 | sales 230 | need 110 | actions 70
    const parts = ['18px','minmax(260px, 1.4fr)','130px','130px'];
    if(showWarehouse) parts.push('110px');
    if(showBarcode)   parts.push('140px');
    parts.push('230px','120px','70px');
    return { list, template: parts.join(' '), showBarcode, showWarehouse };
  }, [period, showBarcode, showWarehouse]);

  const sorted = useMemo(()=>{
    const arr = [...variants];
    const dir = sort.dir === 'desc' ? -1 : 1;
    arr.sort((a,b)=>{
      let av, bv;
      switch(sort.key){
        case 'sku': av = a.sku; bv = b.sku; break;
        case 'uzum': av = a.uzumStock; bv = b.uzumStock; break;
        case 'wh': av = a.warehouseStock; bv = b.warehouseStock; break;
        case 'sales': av = a.sales[period]; bv = b.sales[period]; break;
        case 'need': {
          av = Math.max(0, Math.round((a.sales[30]/30)*60 - a.uzumStock - a.warehouseStock));
          bv = Math.max(0, Math.round((b.sales[30]/30)*60 - b.uzumStock - b.warehouseStock));
          break;
        }
        default: av = a.id; bv = b.id;
      }
      if(av < bv) return -1*dir;
      if(av > bv) return 1*dir;
      return 0;
    });
    return arr;
  }, [variants, sort, period]);

  const allSelected = sorted.length > 0 && sorted.every(v => selected[v.id]);
  const toggleAll = () => {
    const next = { ...selected };
    if(allSelected) sorted.forEach(v => delete next[v.id]);
    else sorted.forEach(v => next[v.id] = true);
    setSelected(next);
  };

  const onSort = (key) => {
    setSort(prev => prev.key === key
      ? { key, dir: prev.dir === 'desc' ? 'asc' : 'desc' }
      : { key, dir: key==='sku' ? 'asc' : 'desc' });
  };

  return (
    <div style={{background:'#fff',borderRadius:'0 0 var(--radius) var(--radius)',overflow:'hidden'}}>
      <TableHeader cols={cols} sort={sort} setSort={onSort} allSelected={allSelected}
        onToggleAll={toggleAll} density={density}/>
      {sorted.map(v => (
        <VariantRow key={v.id} v={v} period={period} sparkStyle={sparkStyle}
          density={density} columns={cols}
          checked={!!selected[v.id]} thumbSize={thumbSize}
          onCheck={()=>setSelected(prev => {
            const next = { ...prev };
            if(next[v.id]) delete next[v.id]; else next[v.id] = true;
            return next;
          })}/>
      ))}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// Footer summary
// ──────────────────────────────────────────────────────────────────
function TableFooter({ variants, period }){
  const totals = useMemo(()=>{
    let sales=0, uzum=0, wh=0, need=0;
    for(const v of variants){
      sales += v.sales[period];
      uzum += v.uzumStock;
      wh += v.warehouseStock;
      need += Math.max(0, Math.round((v.sales[30]/30)*60 - v.uzumStock - v.warehouseStock));
    }
    return { sales, uzum, wh, need };
  }, [variants, period]);
  return (
    <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',
      padding:'12px 16px',background:'#fafbfc',borderTop:'.5px solid var(--line)',
      borderRadius:'0 0 var(--radius) var(--radius)',fontSize:12.5,color:'var(--muted)'}}>
      <span>Показано <span className="tnum" style={{color:'var(--ink)',fontWeight:500}}>{variants.length}</span> вариантов</span>
      <div style={{display:'flex',gap:24}}>
        <span>Итого Uzum: <span className="tnum mono" style={{color:'var(--ink)',fontWeight:600}}>{totals.uzum}</span></span>
        <span>Наш склад: <span className="tnum mono" style={{color:'var(--ink)',fontWeight:600}}>{totals.wh}</span></span>
        <span>Продажи: <span className="tnum mono" style={{color:'var(--ink)',fontWeight:600}}>{totals.sales}</span></span>
        <span>Нужно (60д): <span className="tnum mono" style={{color:'var(--red)',fontWeight:600}}>{totals.need}</span></span>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// App
// ──────────────────────────────────────────────────────────────────

function App(){
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [period, setPeriod] = useState(30);
  const [query, setQuery] = useState('');
  const [colorFilter, setColorFilter] = useState(null);
  const [statusFilter, setStatusFilter] = useState(null);
  const [selected, setSelected] = useState({});
  const [sort, setSort] = useState({ key:'sku', dir:'asc' });

  const filtered = useMemo(()=>{
    const q = query.trim().toLowerCase();
    return window.VARIANTS.filter(v => {
      if(colorFilter && v.color !== colorFilter) return false;
      if(statusFilter && v.status !== statusFilter) return false;
      if(q && !(v.sku.toLowerCase().includes(q)
              || String(v.size).includes(q)
              || v.barcode.includes(q)
              || v.color.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [query, colorFilter, statusFilter]);

  const selectedCount = Object.keys(selected).length;

  // CSS var for accent
  useEffect(()=>{
    document.documentElement.style.setProperty('--accent-live', t.accent);
  }, [t.accent]);

  return (
    <div style={{maxWidth:1440,margin:'0 auto'}}>
      <ProductHeader/>
      <ProductCard/>
      <StatStrip variants={window.VARIANTS} periodDays={period}/>

      <div style={{padding:'0 28px 28px'}}>
        <div style={{background:'#fff',border:'.5px solid var(--line)',borderRadius:'var(--radius)',
          boxShadow:'var(--shadow-md)',overflow:'hidden'}}>
          <Toolbar period={period} setPeriod={setPeriod}
            query={query} setQuery={setQuery}
            colorFilter={colorFilter} setColorFilter={setColorFilter}
            statusFilter={statusFilter} setStatusFilter={setStatusFilter}
            selectedCount={selectedCount} variants={filtered}/>
          <VariantTable variants={filtered} period={period}
            sparkStyle={t.sparkStyle} density={t.density}
            showBarcode={t.showBarcode} showWarehouse={t.showWarehouse}
            thumbSize={t.thumbSize}
            selected={selected} setSelected={setSelected}
            sort={sort} setSort={setSort}/>
          <TableFooter variants={filtered} period={period}/>
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="График продаж"/>
        <TweakRadio  label="Тип искры"  value={t.sparkStyle}
          options={[
            {value:'candle', label:'Свеча'},
            {value:'bar',    label:'Бары'},
            {value:'line',   label:'Линия'},
          ]}
          onChange={(v)=>setTweak('sparkStyle', v)}/>

        <TweakSection label="Таблица"/>
        <TweakRadio  label="Плотность" value={t.density}
          options={[
            {value:'compact', label:'Узкая'},
            {value:'regular', label:'Обычная'},
            {value:'comfy',   label:'Просторная'},
          ]}
          onChange={(v)=>setTweak('density', v)}/>
        <TweakRadio  label="Размер фото" value={t.thumbSize}
          options={[
            {value:'sm', label:'S'},
            {value:'md', label:'M'},
            {value:'lg', label:'L'},
          ]}
          onChange={(v)=>setTweak('thumbSize', v)}/>
        <TweakToggle label="Колонка «Наш склад»" value={t.showWarehouse}
          onChange={(v)=>setTweak('showWarehouse', v)}/>
        <TweakToggle label="Колонка «Штрихкод»" value={t.showBarcode}
          onChange={(v)=>setTweak('showBarcode', v)}/>

        <TweakSection label="Тема"/>
        <TweakColor label="Акцент" value={t.accent}
          options={['#6d4dff','#0d1117','#0ea5e9','#16a34a','#ea580c']}
          onChange={(v)=>setTweak('accent', v)}/>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
