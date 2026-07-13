/* SHDateRange — ilovadagi YAGONA sana-oraliq tanlagichi.
 *
 * Sabab: kalendar shu paytgacha har sahifada qayta-qayta yozilgan edi
 * (group_detail.html, economics.html, invoice_restock.html — uchtasida bir xil
 * flatpickr sozlamasi nusxalangan). Bu fayl o'sha AYNI sozlamani (flatpickr,
 * mode:'range', ru locale, showMonths:2, ISO YYYY-MM-DD) bitta joyga yig'adi.
 * Yangi sahifalar shu yerdan foydalanadi; eskilarini ham keyin shu yerga
 * ko'chirish mumkin (ko'rinishi bir xil).
 *
 * Ishlatish:
 *     el.innerHTML = SHDateRange.html({ from, to, placeholder });
 *     SHDateRange.wire(el.querySelector('.shdr'), {
 *       from, to,
 *       onPick: (fromISO, toISO) => { ... },   // ikkala sana tanlanганда
 *       onClear: () => { ... },                // «×» bosilganда
 *     });
 *
 * `onPick` HAR DOIM `YYYY-MM-DD` beradi (ilovadagi backend shartnomasi:
 * `?date_from=&date_to=` — products/routes.py'даги `date.fromisoformat`).
 */
(function () {
  'use strict';

  const MON = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));

  function toISO(d) {
    return d.getFullYear() + '-'
      + String(d.getMonth() + 1).padStart(2, '0') + '-'
      + String(d.getDate()).padStart(2, '0');
  }

  /** «2026-06-01» + «2026-06-30» → «1 июн — 30 июн» (inputда ko'rinadigan matn) */
  function label(fromISO, toISO_) {
    if (!fromISO || !toISO_) return '';
    const a = new Date(fromISO + 'T00:00:00');
    const b = new Date(toISO_ + 'T00:00:00');
    const fmt = (d) => d.getDate() + ' ' + MON[d.getMonth()];
    const yr = (a.getFullYear() !== b.getFullYear()) ? (' ' + b.getFullYear()) : '';
    return fmt(a) + ' — ' + fmt(b) + yr;
  }

  const CAL_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    + '<rect x="3.5" y="5" width="17" height="15" rx="2.5" stroke="currentColor" stroke-width="1.6"/>'
    + '<path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
  const X_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    + '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

  /** Trigger markup. Tanlov bo'lsa `is-active` (× tugmasi ko'rinadi). */
  function html(opts) {
    opts = opts || {};
    const val = label(opts.from, opts.to);
    const ph = opts.placeholder || 'Выберите период';
    return '<div class="shdr' + (val ? ' is-active' : '') + '">'
      + CAL_SVG
      + '<input type="text" class="shdr-input" readonly placeholder="' + esc(ph) + '" value="' + esc(val) + '">'
      + '<button type="button" class="shdr-clear" title="Сбросить" aria-label="Сбросить">' + X_SVG + '</button>'
      + '</div>';
  }

  /** flatpickr'ni ulaydi. Bir element ikki marta ulanmaydi (`_shdr` bayrog'i). */
  function wire(root, opts) {
    if (!root || root._shdr) return root && root._shdr;
    opts = opts || {};
    const input = root.querySelector('.shdr-input');
    const clear = root.querySelector('.shdr-clear');
    if (!input) return null;

    // flatpickr yuklanmagan bo'lsa — tanlagichni umuman ko'rsatmaymiz
    // (o'lik tugma qolmasin). Sahifa qolgan qismi ishlayveradi.
    if (typeof flatpickr !== 'function') {
      console.warn('[SHDateRange] flatpickr not loaded — date range disabled.');
      root.style.display = 'none';
      return null;
    }

    // MUHIM: input'да allaqachon o'qiladigan matn turadi («3 июл — 13 июл»).
    // flatpickr init paytida input.value'ни SANA deb parse qilishga urinadi va
    // axlat chiqadi («20 Янв 2026»). Shu bois avval bo'shatamiz — sanalarni
    // faqat `defaultDate` (ISO) orqali beramiz, matnni esa o'zimiz qaytaramiz.
    input.value = '';

    let fp = null;
    try {
      fp = flatpickr(input, {
        mode: 'range',
        locale: 'ru',
        dateFormat: 'd M Y',        // faqat ko'rinish uchun; qiymat — ISO (toISO)
        // Ikki oy — 636px. Tor ekranда (telefon) u ekrandan chiqib ketardi →
        // 720px'дан tor bo'lsa bitta oy ko'rsatamiz.
        showMonths: (window.innerWidth < 720 ? 1 : 2),
        disableMobile: true,
        maxDate: opts.maxDate || 'today',   // sotuv tarixi — kelajakda yo'q
        defaultDate: (opts.from && opts.to) ? [opts.from, opts.to] : null,
        appendTo: document.body,
        // Trigger o'ng chekkada turadi (yon menyu yonida) → kalendarni o'ngga
        // tekislaymiz, aks holda u ekrandan chiqib ketadi.
        position: 'auto right',
        // flatpickr joylashuvni `document.body.offsetWidth` bo'yicha hisoblaydi;
        // bu maketda body viewport'дан keng, shuning uchun u chetga chiqishni
        // «sezmaydi». Ochilgach o'zimiz ekran ichiga QISAMIZ.
        onOpen(_sel, _str, inst) {
          const cal = inst.calendarContainer;
          requestAnimationFrame(() => {
            const r = cal.getBoundingClientRect();
            const pad = 10;
            let left = r.left;
            if (r.right > window.innerWidth - pad) left = window.innerWidth - r.width - pad;
            if (left < pad) left = pad;
            if (Math.round(left) !== Math.round(r.left)) {
              // `left` — sahifa koordinatasida (absolute, body'ga nisbatan)
              cal.style.left = (left + window.scrollX) + 'px';
              cal.style.right = 'auto';
            }
          });
        },
        onChange(dates) {
          if (dates.length !== 2) return;    // yarim tanlov — hech narsa qilmaymiz
          const f = toISO(dates[0]);
          const t = toISO(dates[1]);
          input.value = label(f, t);
          root.classList.add('is-active');
          if (opts.onPick) opts.onPick(f, t);
        },
      });
    } catch (e) {
      console.warn('[SHDateRange] flatpickr init failed:', e);
      root.style.display = 'none';
      return null;
    }

    // flatpickr o'z formatida yozgan matnni bizning qisqa ko'rinishimizga qaytaramiz
    if (opts.from && opts.to) input.value = label(opts.from, opts.to);

    root.addEventListener('click', (e) => {
      if (e.target.closest('.shdr-clear')) return;   // «×» alohida ishlaydi
      fp.open();
    });
    clear.addEventListener('click', (e) => {
      e.stopPropagation();
      fp.clear();
      input.value = '';
      root.classList.remove('is-active');
      if (opts.onClear) opts.onClear();
    });

    root._shdr = { fp, destroy() { try { fp.destroy(); } catch (_) {} root._shdr = null; } };
    return root._shdr;
  }

  window.SHDateRange = { html, wire, label, toISO };
})();
