"""
auth.py — Autorización WHOOP (ejecución única)
================================================
Intenta primero el flujo confidencial (sin PKCE, solo client_secret).
Si WHOOP devuelve refresh_token → el cron funcionará indefinidamente.
Si no → avisa y guarda lo que haya.

Uso:
    python auth.py
"""

import os
import json
import time
import hashlib
import base64
import secrets
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
import ssl
import urllib3

ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from dotenv import load_dotenv

VERIFY = False
load_dotenv()

CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI  = "http://localhost:8080"
WHOOP_AUTH    = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN   = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPE         = "offline read:recovery read:sleep read:workout read:profile read:body_measurement"
TOKENS_FILE   = "tokens.json"

# ── PKCE helpers (fallback) ───────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def generate_pkce():
    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge

# ── Local callback server ─────────────────────────────────────────────────────

received_code: dict = {}

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if "error" in params:
            received_code["error"] = params["error"]
            body = f"<h2>Error: {params['error']}</h2><p>{params.get('error_description','')}</p>"
        elif "code" in params:
            received_code["code"] = params["code"]
            body = "<h2>✓ Autorización completada</h2><p>Puedes cerrar esta ventana.</p>"
        else:
            body = "<h2>Respuesta inesperada</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass

# ── Token exchange ────────────────────────────────────────────────────────────

def exchange_confidential(code: str) -> dict:
    """Flujo sin PKCE — cliente confidencial (client_secret). Devuelve refresh_token."""
    resp = requests.post(WHOOP_TOKEN, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"},
       timeout=15, verify=VERIFY)
    resp.raise_for_status()
    return resp.json()

def exchange_pkce(code: str, verifier: str) -> dict:
    """Flujo con PKCE — fallback si el confidencial falla."""
    resp = requests.post(WHOOP_TOKEN, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"},
       timeout=15, verify=VERIFY)
    resp.raise_for_status()
    return resp.json()

def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump({
            "access_token":  tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "expires_in":    tokens.get("expires_in", 3600),
            "fetched_at":    time.time(),
        }, f, indent=2)
    print(f"✓ Tokens guardados en {TOKENS_FILE}")

# ── Main ──────────────────────────────────────────────────────────────────────

def authorize_and_get_code(use_pkce: bool):
    """Abre el navegador y espera el callback. Devuelve (code, verifier|None)."""
    state   = secrets.token_urlsafe(16)
    verifier = None

    auth_params = {
        "response_type": "code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPE,
        "state":         state,
    }

    if use_pkce:
        verifier, challenge = generate_pkce()
        auth_params["code_challenge"]        = challenge
        auth_params["code_challenge_method"] = "S256"

    auth_url = f"{WHOOP_AUTH}?{urllib.parse.urlencode(auth_params)}"
    print(f"→ Abriendo navegador ({'PKCE' if use_pkce else 'confidencial'})…")
    webbrowser.open(auth_url)
    print("⏳ Esperando callback en http://localhost:8080 …")

    received_code.clear()
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()

    if "error" in received_code:
        raise RuntimeError(f"WHOOP devolvió error: {received_code['error']}")

    code = received_code.get("code")
    if not code:
        raise RuntimeError("No se recibió código de autorización")

    return code, verifier


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("✗ Falta WHOOP_CLIENT_ID o WHOOP_CLIENT_SECRET en .env")
        return

    # ── Intento 1: flujo confidencial sin PKCE ────────────────────────────────
    print("\n── Intento 1/2: flujo confidencial (sin PKCE) ──")
    try:
        code, _ = authorize_and_get_code(use_pkce=False)
        print("→ Intercambiando código por tokens…")
        tokens = exchange_confidential(code)
        campos = list(tokens.keys())
        print(f"   Campos recibidos: {campos}")

        if tokens.get("refresh_token"):
            print("   ✓ refresh_token obtenido — el cron renovará el token automáticamente")
            save_tokens(tokens)
            print("\n✅ Listo. Ejecuta: python sync.py")
            return
        else:
            print("   ⚠ Sin refresh_token en este intento. Probando con PKCE…")

    except requests.HTTPError as e:
        print(f"   ✗ Error HTTP {e.response.status_code}: {e.response.text[:200]}")
        print("   Probando con PKCE…")
    except Exception as e:
        print(f"   ✗ Error: {e}. Probando con PKCE…")

    # ── Intento 2: flujo PKCE (fallback) ─────────────────────────────────────
    print("\n── Intento 2/2: flujo PKCE ──")
    try:
        code, verifier = authorize_and_get_code(use_pkce=True)
        print("→ Intercambiando código por tokens…")
        tokens = exchange_pkce(code, verifier)
        campos = list(tokens.keys())
        print(f"   Campos recibidos: {campos}")

        if tokens.get("refresh_token"):
            print("   ✓ refresh_token obtenido")
        else:
            print("   ⚠ WHOOP no devuelve refresh_token para esta app.")
            print("     Revisa en developer-dashboard.whoop.com que el tipo de app")
            print("     sea 'Web Application' (no SPA), o contacta el soporte de WHOOP.")

        save_tokens(tokens)
        print("\n✅ Listo. Ejecuta: python sync.py")

    except requests.HTTPError as e:
        print(f"\n✗ WHOOP rechazó el intercambio — HTTP {e.response.status_code}")
        print(f"  Respuesta: {e.response.text}")
    except Exception as e:
        print(f"\n✗ Error inesperado: {e}")


if __name__ == "__main__":
    main()
