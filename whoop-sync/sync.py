"""
sync.py — Sincronización WHOOP → Firestore
==========================================
Carga los datos de WHOOP (recovery, sueño, workouts, cuerpo) y los
escribe en Firestore bajo /users/{uid}/whoop/cache, que es exactamente
el mismo schema que usa la web app para renderizar la Home.

Uso:
    python sync.py              # sync de hoy + últimos SYNC_DAYS_BACK días
    python sync.py --days 14    # ampliar ventana de workouts

Programar con cron (cada día a las 9:00):
    0 9 * * * cd /ruta/a/whoop-sync && python sync.py >> sync.log 2>&1
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ssl
import urllib3

# macOS system Python SSL fix
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
SA_PATH       = os.getenv("FIREBASE_SERVICE_ACCOUNT", "./firebase-service-account.json")
PROJECT_ID    = os.getenv("FIREBASE_PROJECT_ID", "gym-tracker-at")
USER_UID      = os.getenv("FIREBASE_USER_UID", "")
SYNC_DAYS     = int(os.getenv("SYNC_DAYS_BACK", "7"))
TOKENS_FILE   = "tokens.json"
WHOOP_TOKEN   = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API     = "https://api.prod.whoop.com/developer/v2"
VERIFY        = False   # macOS SSL fix (ssl module patched above)

# ── Token management ──────────────────────────────────────────────────────────

def load_tokens() -> dict:
    if not os.path.exists(TOKENS_FILE):
        print("✗ tokens.json no encontrado. Ejecuta primero: python auth.py")
        sys.exit(1)
    with open(TOKENS_FILE) as f:
        return json.load(f)

def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

def refresh_access_token(refresh_token: str) -> str:
    """Intercambia el refresh_token por un nuevo access_token."""
    resp = requests.post(WHOOP_TOKEN, data={
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15, verify=VERIFY)
    resp.raise_for_status()
    new_tokens = resp.json()
    # Guarda el nuevo refresh_token (WHOOP rota los refresh tokens)
    tokens = load_tokens()
    tokens["access_token"]  = new_tokens["access_token"]
    tokens["refresh_token"] = new_tokens.get("refresh_token", tokens["refresh_token"])
    tokens["expires_in"]    = new_tokens.get("expires_in", 3600)
    tokens["fetched_at"]    = time.time()
    save_tokens(tokens)
    return tokens["access_token"]

def get_valid_token() -> str:
    tokens = load_tokens()
    fetched_at = tokens.get("fetched_at", time.time())  # assume fresh if missing
    expires_in = tokens.get("expires_in", 3600)
    # Renueva solo si queda menos de 5 minutos de vida
    if time.time() > fetched_at + expires_in - 300:
        print("→ Renovando access token…")
        return refresh_access_token(tokens["refresh_token"])
    return tokens["access_token"]

# ── WHOOP API helpers ─────────────────────────────────────────────────────────

def whoop_get(path: str, token: str, params: dict = None, debug: bool = False) -> dict:
    """GET a la WHOOP API con reintentos automáticos en 401."""
    url = f"{WHOOP_API}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=15, verify=VERIFY)
    if debug:
        print(f"   [debug] GET {resp.url}")
        print(f"   [debug] → {resp.status_code} | body: {resp.text[:400]}")
    if resp.status_code == 401:
        token = refresh_access_token(load_tokens()["refresh_token"])
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(url, headers=headers, params=params, timeout=15, verify=VERIFY)
    if not resp.ok:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} — {resp.text[:300]}", response=resp)
    return resp.json()

def iso_z(dt: datetime) -> str:
    """Formatea una fecha como 'Z' (UTC) que WHOOP espera."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def iso_range(days_back: int = 2) -> tuple[str, str]:
    """Devuelve (start_iso, end_iso) en formato Z para la ventana de búsqueda."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    return iso_z(start), iso_z(now)

# ── Data fetchers ─────────────────────────────────────────────────────────────
# Each fetcher returns an empty dict on failure so the rest of the sync continues.

def safe_get(label: str, fn):
    """Llama fn() y devuelve {} si falla, imprimiendo el error."""
    try:
        return fn()
    except Exception as e:
        print(f"   ⚠ {label} no disponible: {e}")
        return {}

def fetch_recovery(token: str, debug: bool = False) -> dict:
    # WHOOP expone /recovery como colección paginada (igual que /sleep y /activity/workout)
    # Probamos primero sin rango de fechas para validar que el endpoint existe
    for params in [{"limit": 1}, {"start": iso_range(2)[0], "end": iso_range(2)[1], "limit": 2}]:
        try:
            data = whoop_get("/recovery", token, params, debug=debug)
            rec = (data.get("records") or [{}])[0].get("score") or {}
            return {
                "score": rec.get("recovery_score"),
                "hrv":   rec.get("hrv_rmssd_milli"),
                "rhr":   rec.get("resting_heart_rate"),
                "spo2":  rec.get("spo2_percentage"),
            }
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue   # prueba siguiente variante de params
            raise
    # Si ambos fallan intenta vía /cycle (requiere read:cycles, puede no estar disponible)
    try:
        start, end = iso_range(days_back=2)
        data = whoop_get("/cycle", token, {"start": start, "end": end, "limit": 2}, debug=debug)
        cycle = (data.get("records") or [{}])[0]
        rec = cycle.get("recovery", {}).get("score") or {}
        return {
            "score": rec.get("recovery_score"),
            "hrv":   rec.get("hrv_rmssd_milli"),
            "rhr":   rec.get("resting_heart_rate"),
            "spo2":  rec.get("spo2_percentage"),
        }
    except Exception:
        return {}

def fetch_sleep(token: str, debug: bool = False) -> dict:
    start, end = iso_range(days_back=2)
    data = whoop_get("/activity/sleep", token, {"start": start, "end": end, "limit": 2}, debug=debug)
    rec  = (data.get("records") or [{}])[0]
    sl   = rec.get("score") or {}
    return {
        "performance":  sl.get("sleep_performance_percentage"),
        "duration_min": round(sl.get("total_in_bed_time_milli", 0) / 60000) or None,
    }

def fetch_workouts(token: str, days_back: int = 7, debug: bool = False) -> dict:
    """Devuelve {date_str: {strain, avgHr, maxHr, calories, duration}}."""
    start, end = iso_range(days_back=days_back)
    data = whoop_get("/activity/workout", token, {"start": start, "end": end, "limit": 25}, debug=debug)
    workouts = {}
    for w in (data.get("records") or []):
        sc      = w.get("score") or {}
        raw     = w.get("start_time") or w.get("created_at", "")
        date_str = raw[:10] if raw else None
        if not date_str:
            continue
        entry = {
            "strain":   sc.get("strain"),
            "avgHr":    sc.get("average_heart_rate"),
            "maxHr":    sc.get("max_heart_rate"),
            "calories": round(sc["kilojoule"] * 0.239) if sc.get("kilojoule") else None,
            "duration": round(
                (datetime.fromisoformat(w["end_time"].replace("Z", "+00:00")) -
                 datetime.fromisoformat(w["start_time"].replace("Z", "+00:00"))).total_seconds() / 60
            ) if w.get("start_time") and w.get("end_time") else None,
        }
        if date_str not in workouts or (entry["strain"] or 0) > (workouts[date_str]["strain"] or 0):
            workouts[date_str] = entry
    return workouts

def fetch_body(token: str, debug: bool = False) -> dict:
    data = whoop_get("/user/measurement/body", token, debug=debug)
    return {
        "weight_kilogram": data.get("weight_kilogram"),
        "height_meter":    data.get("height_meter"),
        "max_heart_rate":  data.get("max_heart_rate"),
    }

# ── Firestore writer ──────────────────────────────────────────────────────────

def init_firestore():
    if not os.path.exists(SA_PATH):
        print(f"✗ Service account no encontrado en: {SA_PATH}")
        print("  Descárgalo en: Firebase Console → Project Settings → Service Accounts")
        sys.exit(1)
    cred = credentials.Certificate(SA_PATH)
    firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    return firestore.client()

def write_to_firestore(db, payload: dict):
    if not USER_UID:
        print("✗ FIREBASE_USER_UID no configurado en .env")
        sys.exit(1)
    ref = db.collection("users").document(USER_UID).collection("whoop").document("cache")
    ref.set(payload, merge=True)
    print(f"✓ Datos escritos en Firestore: /users/{USER_UID}/whoop/cache")

# ── Main ──────────────────────────────────────────────────────────────────────

def debug_whoop_token(token: str):
    """Llama al endpoint de perfil para verificar que el token y la URL base son correctos."""
    try:
        data = whoop_get("/user/profile/basic", token)
        print(f"   ✓ Token válido — usuario: {data.get('email','?')} (id={data.get('user_id','?')})")
        return True
    except Exception as e:
        print(f"   ✗ Token o URL base inválidos: {e}")
        return False

def main(days_back: int = SYNC_DAYS, debug: bool = False):
    if not CLIENT_ID or not CLIENT_SECRET:
        print("✗ Faltan WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET en .env")
        sys.exit(1)

    print(f"── WHOOP Sync · {datetime.now().strftime('%Y-%m-%d %H:%M')} ──")
    if debug:
        print("   [modo debug activado — se imprime la respuesta raw de cada endpoint]")

    token = get_valid_token()
    print("→ Verificando token WHOOP…")
    if not debug_whoop_token(token):
        print("  Vuelve a ejecutar python3 auth.py para obtener un token nuevo.")
        sys.exit(1)

    print("→ Obteniendo recovery…")
    recovery = safe_get("recovery", lambda: fetch_recovery(token, debug=debug))
    print(f"   Recovery: {recovery.get('score')}% · HRV: {recovery.get('hrv')} ms · RHR: {recovery.get('rhr')} bpm")

    print("→ Obteniendo sueño…")
    sleep = safe_get("sleep", lambda: fetch_sleep(token, debug=debug))
    print(f"   Sueño: {sleep.get('performance')}%")

    print(f"→ Obteniendo workouts (últimos {days_back} días)…")
    workouts = safe_get("workouts", lambda: fetch_workouts(token, days_back, debug=debug))
    for d, w in sorted(workouts.items()):
        print(f"   {d}: strain {w.get('strain')}")

    print("→ Obteniendo medidas corporales…")
    body = safe_get("body_measurement", lambda: fetch_body(token, debug=debug))
    print(f"   Peso: {body.get('weight_kilogram')} kg · Altura: {body.get('height_meter')} m")

    payload = {
        "recovery":  recovery,
        "sleep":     sleep,
        "workouts":  workouts,
        "body":      body,
        "lastSync":  datetime.now(timezone.utc).isoformat(),
    }

    print("→ Conectando a Firestore…")
    db = init_firestore()
    write_to_firestore(db, payload)

    print("\n✅ Sync completado.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync WHOOP → Firestore")
    parser.add_argument("--days", type=int, default=SYNC_DAYS,
                        help="Días hacia atrás para buscar workouts (default: %(default)s)")
    parser.add_argument("--debug", action="store_true",
                        help="Imprime la URL y respuesta raw de cada llamada a la API de WHOOP")
    args = parser.parse_args()
    main(days_back=args.days, debug=args.debug)
