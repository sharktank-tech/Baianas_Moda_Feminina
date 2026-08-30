/**
 * Vercel Routing Middleware — intercepta o callback OAuth ANTES do runtime Python.
 *
 * Matcher restrito a: /integrations/aliexpress/callback
 *
 * - Requisição COM code+state na query: registra ambos em cookies
 *   (Path=/integrations/aliexpress/callback, Max-Age=120, HttpOnly, Secure,
 *   SameSite=Lax) e responde 302 para o MESMO caminho SEM query string. Assim
 *   a primeira requisição sensível (code/state) NÃO chega ao Flask/Werkzeug.
 * - Requisição SEM code+state: retorna undefined e o request segue para o
 *   Flask, que lê os cookies como fallback (query string continua funcionando
 *   em local/dev).
 *
 * Nenhum log aqui: não registrar request.url, query, code nem state.
 */

const CALLBACK_PATH = "/integrations/aliexpress/callback";
const CODE_COOKIE = "ae_oauth_code";
const STATE_COOKIE = "ae_oauth_state";
const COOKIE_AGE = 120;

const COOKIE_ATTRS = [
  `Path=${CALLBACK_PATH}`,
  `Max-Age=${COOKIE_AGE}`,
  "HttpOnly",
  "Secure",
  "SameSite=Lax",
].join("; ");

const encode = (value) =>
  value === null || value === undefined ? "" : encodeURIComponent(String(value));

export function buildCallbackResponse(request) {
  const url = new URL(request.url);
  if (!url.searchParams.has("code") || !url.searchParams.has("state")) {
    return null; // deixa seguir para o Flask
  }

  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");

  // Sempre 302 para o caminho limpo (sem copiar a query string).
  const redirectUrl = new URL(CALLBACK_PATH, url.origin);
  const response = Response.redirect(redirectUrl, 302);
  response.headers.append(
    "Set-Cookie",
    `${CODE_COOKIE}=${encode(code)}; ${COOKIE_ATTRS}`,
  );
  response.headers.append(
    "Set-Cookie",
    `${STATE_COOKIE}=${encode(state)}; ${COOKIE_ATTRS}`,
  );
  return response;
}

export default function middleware(request) {
  return buildCallbackResponse(request) ?? undefined;
}

export const config = {
  matcher: ["/integrations/aliexpress/callback"],
};