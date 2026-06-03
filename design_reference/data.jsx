// Variant data — 28 SKUs (gold/silver/black, sizes 16-22)
// Generates a stable daily-sales series so candlestick charts have something real to show.

function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}

function genSeries(seed, days, mean){
  const rng = mulberry32(seed);
  const out = [];
  let prev = mean;
  for(let i=0;i<days;i++){
    // mean-reverting random walk, clamp to 0..mean*3
    const drift = (mean - prev) * 0.25;
    const shock = (rng() - 0.45) * mean * 0.9;
    const burst = rng() < 0.06 ? Math.round(rng()*mean*2) : 0;
    let v = Math.max(0, Math.round(prev + drift + shock + burst));
    if(v > mean*4) v = mean*4;
    out.push(v);
    prev = v;
  }
  return out;
}

const COLORS_RU = ['Золотой','Серебряный','Чёрный','Розовое золото'];
const SIZES = [16,17,18,19,20,21,22];

// product images (data URIs would be huge — use unsplash-style placeholders via picsum, but
// to stay offline-friendly use inline SVG color thumbs)
function thumb(hue, size){
  const grad1 = `hsl(${hue} 40% 30%)`;
  const grad2 = `hsl(${hue} 60% 65%)`;
  const ring = `hsl(${hue} 70% 50%)`;
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'>
    <defs>
      <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
        <stop offset='0' stop-color='${grad1}'/>
        <stop offset='1' stop-color='${grad2}'/>
      </linearGradient>
    </defs>
    <rect width='80' height='80' fill='#f3f4f6'/>
    <ellipse cx='40' cy='44' rx='26' ry='10' fill='url(#g)' stroke='${ring}' stroke-width='1.5'/>
    <ellipse cx='40' cy='40' rx='26' ry='10' fill='url(#g)' stroke='${ring}' stroke-width='1.5'/>
    <ellipse cx='40' cy='40' rx='18' ry='6' fill='#111' opacity='.85'/>
    <rect x='34' y='37' width='12' height='6' rx='1' fill='#facc15' opacity='.9'/>
  </svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

const COLOR_META = {
  'Золотой':        { hue: 42, thumb: thumb(42), chip: '#f4c95d' },
  'Серебряный':     { hue: 210, thumb: thumb(210), chip: '#c9ced6' },
  'Чёрный':         { hue: 220, thumb: thumb(220), chip: '#1f2937' },
  'Розовое золото': { hue: 14, thumb: thumb(14), chip: '#e8b4a3' },
};

function buildVariants(){
  const variants = [];
  let seed = 9001;
  const colorPlan = [
    { color: 'Золотой',        sizes: SIZES,                 prefix: 'ЗОЛОТ' },
    { color: 'Серебряный',     sizes: SIZES,                 prefix: 'СЕРЕБРН' },
    { color: 'Чёрный',         sizes: [16,17,18,19,20,21],   prefix: 'ЧЕРН' },
    { color: 'Розовое золото', sizes: [17,18,19,20,21],      prefix: 'РОЗЗОЛ' },
  ];
  let row = 0;
  for(const plan of colorPlan){
    for(const size of plan.sizes){
      seed += 7;
      const rng = mulberry32(seed);
      // daily means — some hot, some cold, some out-of-stock
      const archetype = rng();
      let mean;
      if(archetype < 0.15) mean = 0.05;            // slow mover
      else if(archetype < 0.55) mean = 0.25;       // steady
      else if(archetype < 0.85) mean = 0.6;        // good
      else mean = 1.3;                              // hot
      const series90 = genSeries(seed, 90, mean);
      const sum = (n)=>series90.slice(-n).reduce((a,b)=>a+b,0);
      const uzumStock = Math.round(rng()*14);
      const warehouseStock = rng()<0.7 ? 0 : Math.round(rng()*40);
      const price = 280000 + Math.round(rng()*120000);
      const barcode = '1' + String(Math.floor(rng()*1e12)).padStart(12,'0');
      const sku = `LUXUZ-LUX05-${plan.prefix}-LUX05${size}`;
      // status
      let status;
      const s30 = sum(30);
      if(uzumStock === 0 && warehouseStock === 0) status = 'out';
      else if(uzumStock <= 2) status = 'low';
      else if(s30 === 0) status = 'stale';
      else status = 'ok';
      variants.push({
        id: row++,
        sku, size, color: plan.color,
        thumb: COLOR_META[plan.color].thumb,
        chip: COLOR_META[plan.color].chip,
        uzumStock, warehouseStock,
        barcode, price,
        series90,
        sales: { 7:sum(7), 10:sum(10), 15:sum(15), 30:sum(30), 60:sum(60), 90:sum(90) },
        status,
      });
    }
  }
  return variants;
}

const VARIANTS = buildVariants();

// daily aggregate across all variants (for hero candle chart)
function aggregateSeries(days){
  const out = new Array(days).fill(0);
  for(const v of VARIANTS){
    const slice = v.series90.slice(-days);
    for(let i=0;i<days;i++) out[i] += slice[i];
  }
  return out;
}

Object.assign(window, { VARIANTS, aggregateSeries, COLOR_META });
