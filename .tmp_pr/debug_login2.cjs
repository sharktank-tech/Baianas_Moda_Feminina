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
  page.on('response', r => {
    if (r.url().includes('/login') || r.status() >= 300 && r.status() < 400) {
      console.log('RESP', r.status(), r.url(), '->', r.headers()['location'] || '');
    }
  });
  await page.goto(BASE + '/login');
  const tok = await page.evaluate(() => document.querySelector('input[name="csrf_token"]').value);
  console.log('csrf ok', tok.slice(0, 8));
  await page.fill('input[name="email"]', 'ana.admin@exemplo.test');
  await page.fill('input[name="password"]', 'senha123');
  await page.click('form button[type="submit"]');
  await page.waitForTimeout(1200);
  const cookies = await page.context().cookies();
  console.log('cookies:', cookies.map(c => c.name + '=' + c.value.slice(0, 12)).join('; '));
  console.log('URL:', page.url());
  // try admin page with cookies
  const resp = await page.goto(BASE + '/admin/pedidos/' + ids.D);
  console.log('status pedido D:', resp.status(), 'URL:', page.url());
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
