"""
auth.py — Autorización WHOOP (ejecución única)
================================================
Abre el navegador en la página de login de WHOOP, captura el callback
localmente en el puerto 8080 y guarda los tokens en tokens.json.

Uso:
    python auth.py

Prerequisitos:
    1. Copia .env.example → .env y rellena WHOOP_CLIENT_ID y WHOOP_CLIENT_SECRET
    2. En el WHOOP Developer Portal añade este Redirect URI:
           http://localhost:8080
    3. pip install -r requirements.txt
"""

import os
import json
import hashlib
import base64
import secrets
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI  = "http://localhost:8080"
WHOOP_AUTH    = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN   = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPE         = "read:recovery read:sleep read:workout read:profile read:body_measurement"
TOKENS_FILE   = "tokens.json"

# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def generate_pkce():
    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge

# ── Local callback server ─────────────────────────────────────────────────────

received_code: dict = {}
done_event = Event()

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
        done_event.set()

    def log_message(self, format, *args):
        pass  # silencia los logs del servidor HTTP

# ── Token exchange ────────────────────────────────────────────────────────────

def exchange_code(code: str, verifier: str) -> dict:
    resp = requests.post(WHOOP_TOKEN, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump({
            "access_token":  tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "expires_in":    tokens.get("expires_in", 3600),
        }, f, indent=2)
    print(f"✓ Tokens guardados en {TOKENS_FILE}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("✗ Falta WHOOP_CLIENT_ID o WHOOP_CLIENT_SECRET en .env")
        return

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    params = urllib.parse.urlencode({
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPE,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{WHOOP_AUTH}?{params}"

    print("→ Abriendo navegador para autorizar WHOOP…")
    print(f"  URL: {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    print("⏳ Esperando callback en http://localhost:8080 …")
    server.handle_request()  # atiende una sola petición y para

    if "error" in received_code:
        print(f"✗ WHOOP devolvió error: {received_code['error']}")
        return

    code = received_code.get("code")
    if not code:
        print("✗ No se recibió código de autorización")
        return

    print("→ Intercambiando código por tokens…")
    tokens = exchange_code(code, verifier)
    save_tokens(tokens)
    print("\n✅ Listo. Ahora puedes ejecutar: python sync.py")

if __name__ == "__main__":
    main()
