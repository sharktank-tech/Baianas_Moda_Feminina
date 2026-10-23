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
  await page.click('form button[type="submit"]');
  await page.waitForTimeout(1500);
  console.log('URL após login:', page.url());
  await page.goto(BASE + '/admin/pedidos/' + ids.D);
  await page.waitForTimeout(800);
  console.log('URL pedido D:', page.url());
  const hasForm = await page.$('#om-form-compra');
  console.log('tem #om-form-compra?', !!hasForm);
  const bodyHas = await page.evaluate(() => document.body.innerText.slice(0, 200));
  console.log('corpo:', JSON.stringify(bodyHas));
  const hasSec = await page.evaluate(() => ({
    compra: !!document.querySelector('[id="om-form-compra"]'),
    formTag: !!document.querySelector('form[id="om-form-compra"]'),
    htmlHas: document.documentElement.innerHTML.includes('om-form-compra'),
    cobertura: document.body.innerText.includes('Cobertura completa'),
  }));
  console.log('checks:', JSON.stringify(hasSec));
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
