/**
 * Endpoint Node de sanitização do callback OAuth AliExpress.
 *
 * Fluxo:
 *   AliExpress -> GET /integrations/aliexpress/callback?code=...&state=...
 *   -> este handler grava code/state em cookies HttpOnly (sem logs)
 *   -> 302 para /integrations/aliexpress/callback/complete (URL limpa)
 *   -> Flask recebe somente a URL sem query.
 *
 * Nenhum console.log / console.error / dump de request aqui.
 */

const COOKIE_PATH = "/integrations/aliexpress/callback";
const COMPLETE_PATH = "/integrations/aliexpress/callback/complete";
const MAX_AGE = 120;
const CODE_COOKIE = "ae_oauth_code";
const STATE_COOKIE = "ae_oauth_state";

function first(value) {
  return Array.isArray(value) ? value[0] : value;
}

export default function handler(req, res) {
  if (req.method !== "GET") {
    res.statusCode = 405;
    return res.end();
  }

  const code = first(req.query?.code);
  const state = first(req.query?.state);

  if (!code || !state) {
    res.statusCode = 400;
    res.setHeader("Cache-Control", "no-store");
    return res.end("Invalid OAuth callback");
  }

  const attrs = `Path=${COOKIE_PATH}; Max-Age=${MAX_AGE}; HttpOnly; Secure; SameSite=Lax`;

  res.setHeader("Set-Cookie", [
    `${CODE_COOKIE}=${encodeURIComponent(String(code))}; ${attrs}`,
    `${STATE_COOKIE}=${encodeURIComponent(String(state))}; ${attrs}`,
  ]);

  res.statusCode = 302;
  res.setHeader("Location", COMPLETE_PATH);
  res.setHeader("Cache-Control", "no-store");
  res.end();
}

export { COOKIE_PATH, COMPLETE_PATH, MAX_AGE, CODE_COOKIE, STATE_COOKIE, first };