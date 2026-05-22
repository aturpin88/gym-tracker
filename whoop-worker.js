/**
 * Cloudflare Worker — WHOOP API proxy
 * Proxies token exchange and API calls to bypass browser CORS restrictions.
 * Deploy at: https://workers.cloudflare.com (free account, no credit card)
 *
 * After deploying, paste your worker URL into the gym tracker app settings.
 */

const ALLOWED_ORIGIN = 'https://aturpin88.github.io';
const WHOOP_TOKEN    = 'https://api.prod.whoop.com/oauth/oauth2/token';
const WHOOP_API      = 'https://api.prod.whoop.com/developer/v1';

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin':  ALLOWED_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    'Access-Control-Max-Age':       '86400',
  };
}

export default {
  async fetch(request) {
    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    const url = new URL(request.url);

    // ── /token  →  WHOOP OAuth token endpoint (POST) ──────────────
    if (url.pathname === '/token') {
      const body = await request.text();
      const resp = await fetch(WHOOP_TOKEN, {
        method:  'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
      });
      const text = await resp.text();
      return new Response(text, {
        status:  resp.status,
        headers: { 'Content-Type': 'application/json', ...corsHeaders() },
      });
    }

    // ── /api/* → WHOOP REST API (GET, with Bearer token forwarding) ─
    if (url.pathname.startsWith('/api/')) {
      const apiPath = url.pathname.slice(4); // strip /api → /v1/...
      const upstream = WHOOP_API + apiPath + url.search;
      const resp = await fetch(upstream, {
        headers: { Authorization: request.headers.get('Authorization') || '' },
      });
      const text = await resp.text();
      return new Response(text, {
        status:  resp.status,
        headers: { 'Content-Type': 'application/json', ...corsHeaders() },
      });
    }

    return new Response(JSON.stringify({ error: 'not_found' }), {
      status:  404,
      headers: { 'Content-Type': 'application/json', ...corsHeaders() },
    });
  },
};
