/* Capturas do detalhe administrativo do pedido — dados sintéticos locais.
   Uso: OUT_DIR=.tmp_pr/shots/before BASE_URL=http://127.0.0.1:5131 \
        node capture.cjs                                              */
const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright-core");

const BASE = process.env.BASE_URL || "http://127.0.0.1:5131";
const OUT = process.env.OUT_DIR || path.join(__dirname, "shots", "before");
const EXE = "/home/opencode/projects/tmp-exp/glsys/chrome-wrapper.sh";
const IDS = JSON.parse(
  fs.readFileSync(path.join(__dirname, "seed_ids.json"), "utf8")
);

fs.mkdirSync(OUT, { recursive: true });

async function shot(page, name, { fullPage = true } = {}) {
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, name), fullPage });
  console.log("SHOT", name);
}

async function login(page) {
  await page.goto(BASE + "/login", { waitUntil: "domcontentloaded" });
  await page.fill('input[name="email"]', "ana.admin@exemplo.test");
  await page.fill('input[name="password"]', "senha123");
  const form = page
    .locator("form")
    .filter({ has: page.locator('input[name="email"]') })
    .first();
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), {
      waitUntil: "domcontentloaded",
    }),
    form.locator('button[type="submit"]').click(),
  ]);
}

function compraForm(page) {
  return page
    .locator("form")
    .filter({ has: page.locator('input[name="external_order_id"]') })
    .first();
}

(async () => {
  const browser = await chromium.launch({
    executablePath: EXE,
    headless: true,
    env: {
      ...process.env,
      FONTCONFIG_FILE: "/home/opencode/projects/tmp-exp/glsys/fonts.conf",
    },
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 1,
    locale: "pt-BR",
    timezoneId: "America/Sao_Paulo",
  });
  const page = await ctx.newPage();
  await login(page);

  // 01 — preenchido: compra registrada, fornecedor sem envio (pedido B).
  await page.goto(`${BASE}/admin/pedidos/${IDS.B}`, {
    waitUntil: "domcontentloaded",
  });
  await shot(page, "01-detalhe-preenchido-desktop.png");

  // 07 — completo: compra + remessa postada parcial + correções (pedido C).
  await page.goto(`${BASE}/admin/pedidos/${IDS.C}`, {
    waitUntil: "domcontentloaded",
  });
  await shot(page, "07-completo-rastreio-historico-desktop.png");

  // 03 — vazio: nenhuma compra registrada (pedido A).
  await page.goto(`${BASE}/admin/pedidos/${IDS.A}`, {
    waitUntil: "domcontentloaded",
  });
  await shot(page, "03-formulario-vazio-desktop.png");

  // 06 — navegação por teclado: foco visível no primeiro campo.
  await page.focus('input[name="external_order_id"]');
  await shot(page, "06-foco-teclado-desktop.png", { fullPage: false });

  // 04 — erro de validação (número externo curto) com campos preenchidos.
  await page.fill('input[name="external_order_id"]', "X");
  await page.fill('input[name="purchased_at"]', "2026-10-18T15:30");
  await page.fill('input[name="evidence_ref"]', "print-sintetico-erro-01");
  await page.fill(
    '[name="note"]',
    "Observação sintética preenchida para conferir a preservação de valores."
  );
  await compraForm(page)
    .locator('button[type="submit"]')
    .click();
  await page.waitForLoadState("domcontentloaded");
  await shot(page, "04-erro-validacao-desktop.png");

  // 05 — sucesso: registro válido no pedido A (continua sintético).
  await page.goto(`${BASE}/admin/pedidos/${IDS.A}`, {
    waitUntil: "domcontentloaded",
  });
  await page.fill('input[name="external_order_id"]', "9901234567890077");
  await page.fill('input[name="purchased_at"]', "2026-10-18T15:30");
  await page.fill('input[name="evidence_ref"]', "print-sintetico-0077");
  await compraForm(page)
    .locator('input[name^="item_"]')
    .first()
    .fill("1");
  await compraForm(page)
    .locator('button[type="submit"]')
    .click();
  await page.waitForLoadState("domcontentloaded");
  await shot(page, "05-sucesso-registro-desktop.png");

  // 02 — mobile (390×844), mesmo pedido preenchido.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE}/admin/pedidos/${IDS.B}`, {
    waitUntil: "domcontentloaded",
  });
  await shot(page, "02-detalhe-preenchido-mobile.png");

  await browser.close();
  console.log("CAPTURAS_OK", OUT);
})().catch((e) => {
  console.error("ERR", e.message);
  process.exit(1);
});
