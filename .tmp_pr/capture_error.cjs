const { chromium } = require('playwright-core');
const fs = require('fs');
const ids = JSON.parse(fs.readFileSync('.tmp_pr/seed_ids.json', 'utf8'));
const BASE = 'http://127.0.0.1:5131';
const OUT = '.tmp_pr/shots/erro-foco';
fs.mkdirSync(OUT, { recursive: true });

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

  // GET ?salvo= forjado em C (tem remessa)
  await page.goto(BASE + '/admin/pedidos/' + ids.C + '?salvo=rastreio');
  await page.waitForTimeout(300);
  const forjado = await page.evaluate(() => ({
    salvoBanner: !!document.querySelector('[data-om-salvo]'),
    textoSucesso: document.body.innerText.includes('Rastreio registrado (aguardando postagem).'),
  }));
  console.log('FORJADO:', JSON.stringify(forjado));
  await page.screenshot({ path: OUT + '/01-get-salvo-forjado.png', fullPage: false });

  // D: form de compra visível → external inválido "X" → erro + foco no campo
  await page.goto(BASE + '/admin/pedidos/' + ids.D);
  await page.waitForSelector('#om-form-compra', { timeout: 8000 });
  await page.fill('#om-external', 'X');
  await page.fill('form#om-form-compra input[name^="item_"]', '1');
  await page.fill('#om-purchased-at', '2026-10-19T12:00');
  await page.fill('#om-tz', 'America/Sao_Paulo');
  await page.fill('#om-evidence', 'EV-ERRO-FOCO');
  await page.click('form#om-form-compra [data-om-submit-btn]');
  await page.waitForTimeout(900);

  const erro = await page.evaluate(() => {
    const active = document.activeElement;
    return {
      status: document.querySelector('[data-om-error]') ? 'error-banner' : 'no-banner',
      resumoFocavel: !!document.getElementById('om-error-summary'),
      resumoTabindex: document.getElementById('om-error-summary')?.getAttribute('tabindex'),
      activeId: active ? active.id : null,
      activeAriaInvalid: active ? active.getAttribute('aria-invalid') : null,
      campoErro: document.querySelector('[data-error-for="external_order_id"]')?.textContent?.trim(),
      externalPreservado: document.querySelector('#om-external')?.value,
      evidencePreservada: document.querySelector('#om-evidence')?.value,
      temSalvo: !!document.querySelector('[data-om-salvo]'),
      formAindaVisivel: !!document.getElementById('om-form-compra'),
    };
  });
  console.log('ERRO-FOCO:', JSON.stringify(erro));
  await page.screenshot({ path: OUT + '/02-erro-foco-campo.png', fullPage: true });

  // Sucesso em D → flash + sem banner
  await page.fill('#om-external', '9901234567890CAP1');
  await page.fill('#om-purchased-at', '2026-10-19T13:00');
  await page.click('form#om-form-compra [data-om-submit-btn]');
  await page.waitForTimeout(900);
  const pos = await page.evaluate(() => ({
    url: location.pathname + location.search,
    flash: document.body.innerText.includes('Compra registrada com sucesso.'),
    salvoBanner: !!document.querySelector('[data-om-salvo]'),
  }));
  console.log('POS-SUCESSO:', JSON.stringify(pos));
  await page.screenshot({ path: OUT + '/03-sucesso-flash-sem-banner.png', fullPage: false });

  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
