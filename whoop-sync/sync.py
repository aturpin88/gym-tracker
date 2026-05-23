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
WHOOP_API     = "https://api.prod.whoop.com/developer/v1"

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
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
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
    fetched_at  = tokens.get("fetched_at", 0)
    expires_in  = tokens.get("expires_in", 3600)
    # Renueva si queda menos de 5 minutos de vida
    if time.time() > fetched_at + expires_in - 300:
        print("→ Renovando access token…")
        return refresh_access_token(tokens["refresh_token"])
    return tokens["access_token"]

# ── WHOOP API helpers ─────────────────────────────────────────────────────────

def whoop_get(path: str, token: str, params: dict = None) -> dict:
    """GET a la WHOOP API con reintentos automáticos en 401."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{WHOOP_API}{path}", headers=headers,
                        params=params, timeout=15)
    if resp.status_code == 401:
        # Token expirado: renueva y reintenta una vez
        token = refresh_access_token(load_tokens()["refresh_token"])
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(f"{WHOOP_API}{path}", headers=headers,
                            params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def iso_range(days_back: int = 2) -> tuple[str, str]:
    """Devuelve (start_iso, end_iso) para la ventana de búsqueda."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    return start.isoformat(), now.isoformat()

# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_recovery(token: str) -> dict:
    start, end = iso_range(days_back=2)
    data = whoop_get("/recovery", token, {"start": start, "end": end, "limit": 2})
    rec = (data.get("records") or [{}])[0].get("score") or {}
    return {
        "score": rec.get("recovery_score"),
        "hrv":   rec.get("hrv_rmssd_milli"),
        "rhr":   rec.get("resting_heart_rate"),
        "spo2":  rec.get("spo2_percentage"),
    }

def fetch_sleep(token: str) -> dict:
    start, end = iso_range(days_back=2)
    data = whoop_get("/sleep", token, {"start": start, "end": end, "limit": 2})
    sl = (data.get("records") or [{}])[0].get("score") or {}
    return {
        "performance": sl.get("sleep_performance_percentage"),
        "duration_min": round(
            ((data.get("records") or [{}])[0].get("score") or {}).get("total_in_bed_time_milli", 0) / 60000
        ) or None,
    }

def fetch_workouts(token: str, days_back: int = 7) -> dict:
    """Devuelve {date_str: {strain, avgHr, maxHr, calories, duration}}."""
    start, end = iso_range(days_back=days_back)
    data = whoop_get("/activity/workout", token,
                     {"start": start, "end": end, "limit": 25})
    workouts = {}
    for w in (data.get("records") or []):
        sc = w.get("score") or {}
        # Fecha local del workout (usamos start_time)
        raw = w.get("start_time") or w.get("created_at", "")
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
        # Si hay varios workouts el mismo día, conserva el de mayor strain
        if date_str not in workouts or (entry["strain"] or 0) > (workouts[date_str]["strain"] or 0):
            workouts[date_str] = entry
    return workouts

def fetch_body(token: str) -> dict:
    data = whoop_get("/body_measurement", token)
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

def main(days_back: int = SYNC_DAYS):
    if not CLIENT_ID or not CLIENT_SECRET:
        print("✗ Faltan WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET en .env")
        sys.exit(1)

    print(f"── WHOOP Sync · {datetime.now().strftime('%Y-%m-%d %H:%M')} ──")

    token = get_valid_token()

    print("→ Obteniendo recovery…")
    recovery = fetch_recovery(token)
    print(f"   Recovery: {recovery.get('score')}% · HRV: {recovery.get('hrv')} ms · RHR: {recovery.get('rhr')} bpm")

    print("→ Obteniendo sueño…")
    sleep = fetch_sleep(token)
    print(f"   Sueño: {sleep.get('performance')}%")

    print(f"→ Obteniendo workouts (últimos {days_back} días)…")
    workouts = fetch_workouts(token, days_back)
    for d, w in sorted(workouts.items()):
        print(f"   {d}: strain {w.get('strain')}")

    print("→ Obteniendo medidas corporales…")
    body = fetch_body(token)
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
    args = parser.parse_args()
    main(days_back=args.days)
