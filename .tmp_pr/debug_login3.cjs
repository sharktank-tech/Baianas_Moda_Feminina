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
  // Escopar ao formulário que contém o campo email (evita o form de busca da navbar)
  await page.fill('input[name="email"]', 'ana.admin@exemplo.test');
  await page.fill('input[name="password"]', 'senha123');
  // Clicar no submit do formulário pai do input email
  await page.click('form:has(input[name="email"]) button[type="submit"]');
  await page.waitForTimeout(1200);
  console.log('URL após login:', page.url());
  const resp = await page.goto(BASE + '/admin/pedidos/' + ids.D);
  console.log('status pedido D:', resp.status(), 'URL:', page.url());
  const hasForm = await page.$('#om-form-compra');
  console.log('tem #om-form-compra?', !!hasForm);
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
