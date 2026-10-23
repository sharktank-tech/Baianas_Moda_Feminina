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
  await page.goto(BASE + '/login');
  await page.fill('input[name="email"]', 'ana.admin@exemplo.test');
  await page.fill('input[name="password"]', 'senha123');
  await page.click('form:has(input[name="email"]) button[type="submit"]');
  await page.waitForURL(u => !u.pathname.includes('/login'));

  // Mobile: pedido C tem remessa (form rastreio some) mas correção de rastreio visível; pedido A tem compra sem remessa → form rastreio visível
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + '/admin/pedidos/' + ids.A); // compra registrada, sem remessa → form rastreio
  await page.waitForTimeout(500);
  const m = await page.evaluate(() => {
    const doc = document.documentElement;
    const visibleInputs = [...document.querySelectorAll('.om-field input, .om-field textarea')]
      .filter(el => el.offsetParent !== null && el.type !== 'hidden');
    const sizes = visibleInputs.map(el => getComputedStyle(el).fontSize);
    const overflowing = [...document.querySelectorAll('.om-card, .om-btn, table, .om-itemrow, .om-form')]
      .filter(el => el.scrollWidth > doc.clientWidth + 2)
      .map(el => (el.className || '').toString().slice(0, 60));
    const btn = document.querySelector('.om-btn--primary');
    return {
      hScroll: doc.scrollWidth > doc.clientWidth + 1,
      scrollW: doc.scrollWidth, clientW: doc.clientWidth,
      overflowing,
      inputCount: visibleInputs.length,
      inputSizes: sizes,
      btnFs: btn ? getComputedStyle(btn).fontSize : null,
      btnH: btn ? btn.getBoundingClientRect().height : null,
    };
  });
  console.log('MOBILE-A:', JSON.stringify(m, null, 1));
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
