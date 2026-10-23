const { chromium } = require('playwright-core');
const fs = require('fs');
const ids = JSON.parse(fs.readFileSync('.tmp_pr/seed_ids.json', 'utf8'));
const BASE = 'http://127.0.0.1:5131';

(async () => {
  const browser = await chromium.launch({
    executablePath: '/home/opencode/projects/tmp-exp/glsys/chrome-wrapper.sh',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  // Login (escopo ao form que contém email; navbar tem outro form)
  await page.goto(BASE + '/login');
  await page.fill('input[name="email"]', 'ana.admin@exemplo.test');
  await page.fill('input[name="password"]', 'senha123');
  await page.click('form:has(input[name="email"]) button[type="submit"]');
  await page.waitForURL(u => !u.pathname.includes('/login'));

  // Pedido D com form visível
  await page.goto(BASE + '/admin/pedidos/' + ids.D);
  await page.waitForSelector('#om-form-compra', { timeout: 8000 });

  // --- Foco visível: anel de computed style em #om-external ---
  await page.focus('#om-external');
  const focusInfo = await page.evaluate(() => {
    const cs = getComputedStyle(document.activeElement);
    return { outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`, boxShadow: cs.boxShadow.slice(0, 60) };
  });
  console.log('FOCO:', JSON.stringify(focusInfo));

  // --- Tab stops ---
  let stops = [];
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press('Tab');
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      return el.id || el.name || el.tagName;
    });
    if (!info) break;
    stops.push(info);
    if (info === 'BUTTON') break;
  }
  console.log('TAB-STOPS:', JSON.stringify(stops));

  // --- Duplo clique ---
  await page.fill('#om-external', '990123456789DDBL');
  const qty = await page.$('form#om-form-compra input[name^="item_"]');
  await qty.fill('1');
  await page.fill('#om-purchased-at', '2026-10-19T10:00');
  const tz = await page.$('#om-tz');
  if (tz) await tz.fill('America/Sao_Paulo').catch(async () => { await tz.selectOption('America/Sao_Paulo').catch(() => {}); });
  await page.fill('#om-evidence', 'EV-DBL-CLICK');
  await page.fill('#om-note', 'teste duplo clique');

  await page.evaluate(() => {
    window.__clicks = 0;
    window.__disabledSeen = false;
    const btn = document.querySelector('form#om-form-compra [data-om-submit-btn]');
    new MutationObserver(() => { if (btn.disabled) window.__disabledSeen = true; })
      .observe(btn, { attributes: true, attributeFilter: ['disabled'] });
    btn.addEventListener('click', () => { window.__clicks++; });
  });
  await page.click('form#om-form-compra [data-om-submit-btn]', { clickCount: 2, delay: 25 });
  await page.waitForTimeout(600);
  const dbl = await page.evaluate(() => {
    const btn = document.querySelector('form#om-form-compra [data-om-submit-btn]');
    return {
      clicks: window.__clicks,
      disabledSeen: window.__disabledSeen,
      btnGone: !btn,
      url: location.pathname + location.search,
      bannerSalvo: !!document.querySelector('[data-om-salvo="compra"]'),
    };
  });
  console.log('DBL-CLICK:', JSON.stringify(dbl));

  // --- Mobile 390 (M completo: badges/histórico) ---
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + '/admin/pedidos/' + ids.M);
  await page.waitForTimeout(400);
  const mobile = await page.evaluate(() => {
    const doc = document.documentElement;
    const overflowing = [...document.querySelectorAll('.om-card, .om-btn, table, .om-itemrow')]
      .filter(el => el.scrollWidth > doc.clientWidth + 2)
      .map(el => (el.className || '').toString().slice(0, 50));
    const btn = document.querySelector('.om-btn');
    const input = document.querySelector('input');
    return {
      hScroll: doc.scrollWidth > doc.clientWidth + 1,
      scrollW: doc.scrollWidth, clientW: doc.clientWidth,
      overflowing,
      btnFs: btn ? getComputedStyle(btn).fontSize : null,
      inputFs: input ? getComputedStyle(input).fontSize : null,
      inputH: input ? input.getBoundingClientRect().height : null,
    };
  });
  console.log('MOBILE-390:', JSON.stringify(mobile));

  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
