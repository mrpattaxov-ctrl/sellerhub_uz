// Chart primitives — Japanese candlestick sparkline, bar sparkline, line sparkline
// and a larger hero candlestick chart for the stat strip.

const { useMemo } = React;

// ────────────────────────────────────────────────────────────────────
// CandleSpark — Japanese candlestick mini-chart, one candle per day.
// Each candle: body spans [yesterday's value, today's value].
// Green = up day, red = down day, neutral grey for flat.
// ────────────────────────────────────────────────────────────────────
function CandleSpark({ data, width = 96, height = 28, gap = 1, color = 'var(--accent)' }){
  if(!data || data.length < 2) return null;
  const n = data.length;
  const max = Math.max(1, ...data);
  const min = Math.min(0, ...data);
  const range = Math.max(1, max - min);
  const slot = (width - gap*(n-1)) / n;
  const bodyW = Math.max(1.2, slot * 0.78);
  const wickW = 1;
  const y = (v) => height - ((v - min) / range) * (height - 2) - 1;
  const candles = [];
  for(let i=0;i<n;i++){
    const v = data[i];
    const prev = i===0 ? v : data[i-1];
    const x = i*(slot+gap) + slot/2;
    const top = Math.min(prev, v);
    const bot = Math.max(prev, v);
    candles.push(
      <g key={i}>
        <rect x={x - wickW/2} y={y(bot)} width={wickW} height={Math.max(.5, y(top) - y(bot))}
              fill={color} opacity={.35}/>
        <rect x={x - bodyW/2} y={y(bot)} width={bodyW}
              height={Math.max(1.5, y(top) - y(bot))}
              fill={color} rx={.5} opacity={v===0 ? .25 : 1}/>
      </g>
    );
  }
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{display:'block', color}}>
      {candles}
    </svg>
  );
}

// Bar sparkline — each day = vertical bar
function BarSpark({ data, width = 96, height = 28, gap = 1, color = 'var(--accent)' }){
  if(!data || !data.length) return null;
  const n = data.length;
  const max = Math.max(1, ...data);
  const slot = (width - gap*(n-1)) / n;
  const barW = Math.max(1, slot * 0.7);
  const bars = data.map((v,i)=>{
    const h = Math.max(.8, (v/max) * (height-2));
    const x = i*(slot+gap) + (slot-barW)/2;
    const y = height - h;
    return <rect key={i} x={x} y={y} width={barW} height={h}
      fill={color} opacity={v===0 ? .18 : 1} rx={.5}/>;
  });
  return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{display:'block'}}>{bars}</svg>;
}

// Line sparkline with subtle fill
function LineSpark({ data, width = 96, height = 28, color = '#0d1117' }){
  if(!data || data.length < 2) return null;
  const max = Math.max(1, ...data);
  const min = Math.min(0, ...data);
  const range = Math.max(1, max - min);
  const stepX = width / (data.length - 1);
  const points = data.map((v,i)=>[i*stepX, height - ((v-min)/range)*(height-2) - 1]);
  const d = points.map((p,i)=> (i===0?'M':'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const fillD = d + ` L ${width} ${height} L 0 ${height} Z`;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{display:'block'}}>
      <path d={fillD} fill={color} opacity={.08}/>
      <path d={d} fill="none" stroke={color} strokeWidth={1.4} strokeLinejoin="round" strokeLinecap="round"/>
      <circle cx={points[points.length-1][0]} cy={points[points.length-1][1]} r={1.8} fill={color}/>
    </svg>
  );
}

function Spark({ kind, data, width, height, color }){
  if(kind === 'candle') return <CandleSpark data={data} width={width} height={height} color={color}/>;
  if(kind === 'bar')    return <BarSpark    data={data} width={width} height={height} color={color}/>;
  return <LineSpark data={data} width={width} height={height} color={color || '#0d1117'}/>;
}

// ────────────────────────────────────────────────────────────────────
// HeroCandle — larger candlestick chart for the stat strip.
// Daily total sales across all variants. Real candle: body = abs(today vs yesterday),
// wick = mini range proxy. Green up / red down. Includes axis baseline + value pills.
// ────────────────────────────────────────────────────────────────────
function HeroCandle({ data, width = 360, height = 90, gridColor = '#eef0f3', labelColor = '#9ca3af', color = 'var(--accent)' }){
  if(!data || data.length < 2) return null;
  const n = data.length;
  const max = Math.max(1, ...data);
  const padTop = 8, padBot = 14;
  const innerH = height - padTop - padBot;
  const slot = width / n;
  const bodyW = Math.max(2, slot * 0.62);
  const wickW = 1;
  const y = (v) => padTop + (1 - v/max) * innerH;
  const candles = [];
  for(let i=0;i<n;i++){
    const v = data[i];
    const prev = i===0 ? v : data[i-1];
    const x = i*slot + slot/2;
    const top = Math.min(prev, v);
    const bot = Math.max(prev, v);
    // synthetic wick — small jitter above max(prev,v), below min(prev,v) based on value
    const wickHi = Math.max(0, bot - Math.max(0, (bot-top)*0.35) - 0.5);
    const wickLo = Math.min(max, top + Math.max(0, (bot-top)*0.35) + 0.5);
    candles.push(
      <g key={i}>
        <rect x={x - wickW/2} y={y(wickLo)} width={wickW}
              height={Math.max(.5, y(wickHi) - y(wickLo))} fill={color} opacity={.4}/>
        <rect x={x - bodyW/2} y={y(bot)} width={bodyW}
              height={Math.max(2, y(top) - y(bot))} fill={color} rx={1}
              opacity={v===0 ? .2 : 1}/>
      </g>
    );
  }
  // baseline gridlines
  const grid = [0.25, 0.5, 0.75].map((f,i)=>(
    <line key={i} x1={0} x2={width} y1={padTop + innerH*(1-f)} y2={padTop + innerH*(1-f)}
          stroke={gridColor} strokeDasharray="2 4"/>
  ));
  // date labels — first, mid, last
  const labels = [
    { i: 0, t: `−${n}д` },
    { i: Math.floor(n/2), t: `−${Math.floor(n/2)}д` },
    { i: n-1, t: 'сегодня' },
  ];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{display:'block', overflow:'visible'}}>
      {grid}
      {candles}
      {labels.map((l,i)=>(
        <text key={i} x={l.i*slot + slot/2} y={height - 2}
              fill={labelColor} fontSize="9.5" textAnchor={i===0?'start':i===2?'end':'middle'}
              fontFamily="Geist Mono, monospace">{l.t}</text>
      ))}
    </svg>
  );
}

// ────────────────────────────────────────────────────────────────────
// HeroBar — bar version of the hero chart. Simple daily-sales bars.
// ────────────────────────────────────────────────────────────────────
function HeroBar({ data, width = 360, height = 90, gridColor = '#eef0f3', labelColor = '#9ca3af', color = 'var(--accent)' }){
  if(!data || !data.length) return null;
  const n = data.length;
  const max = Math.max(1, ...data);
  const padTop = 8, padBot = 14;
  const innerH = height - padTop - padBot;
  const slot = width / n;
  const barW = Math.max(2, slot * 0.62);
  const bars = data.map((v,i)=>{
    const h = Math.max(1.5, (v/max) * innerH);
    const x = i*slot + (slot - barW)/2;
    const y = padTop + innerH - h;
    return <rect key={i} x={x} y={y} width={barW} height={h}
      fill={color} opacity={v===0 ? .18 : 1} rx={1.5}/>;
  });
  const grid = [0.25, 0.5, 0.75].map((f,i)=>(
    <line key={i} x1={0} x2={width} y1={padTop + innerH*(1-f)} y2={padTop + innerH*(1-f)}
          stroke={gridColor} strokeDasharray="2 4"/>
  ));
  const labels = [
    { i: 0, t: `−${n}д` },
    { i: Math.floor(n/2), t: `−${Math.floor(n/2)}д` },
    { i: n-1, t: 'сегодня' },
  ];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{display:'block', overflow:'visible'}}>
      {grid}
      {bars}
      {labels.map((l,i)=>(
        <text key={i} x={l.i*slot + slot/2} y={height - 2}
              fill={labelColor} fontSize="10.5" textAnchor={i===0?'start':i===2?'end':'middle'}
              fontFamily="Geist Mono, monospace">{l.t}</text>
      ))}
    </svg>
  );
}

Object.assign(window, { CandleSpark, BarSpark, LineSpark, Spark, HeroCandle, HeroBar });
