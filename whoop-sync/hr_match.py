"""
hr_match.py — Matchea FC de Apple Watch con series del gym tracker
==================================================================
Flujo:
  1. Lee ~/iCloud Drive/gym_hr_upload.json  (escrito por el Shortcut iOS)
  2. Lee los logs de sesión de Firestore para la fecha indicada
  3. Para cada serie con t_start/t_end: filtra muestras HR, calcula avg+max
  4. Actualiza Firestore con avg_hr, max_hr, hr_samples por serie
  5. Archiva el archivo procesado

Uso:
  python hr_match.py                       # procesa el upload pendiente (fecha del archivo)
  python hr_match.py --date 2026-05-25     # fuerza una fecha concreta
  python hr_match.py --dry-run             # muestra resultado sin escribir en Firestore
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ssl
import urllib3
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

SA_PATH    = os.getenv("FIREBASE_SERVICE_ACCOUNT", "./firebase-service-account.json")
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "gym-tracker-at")
USER_UID   = os.getenv("FIREBASE_USER_UID", "")

ICLOUD     = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
HR_UPLOAD  = ICLOUD / "gym_hr_upload.json"
HR_ARCHIVE = ICLOUD / "gym_hr_archive"

# Margen extra al final de cada serie (segundos) por latencia del watch
END_MARGIN_SEC = 8

# ── Firestore ─────────────────────────────────────────────────────────────────

def init_firestore():
    if not os.path.exists(SA_PATH):
        print(f"✗ Service account no encontrado: {SA_PATH}"); sys.exit(1)
    cred = credentials.Certificate(SA_PATH)
    firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    return firestore.client()

# ── Carga de muestras HR ──────────────────────────────────────────────────────

def load_hr_samples():
    if not HR_UPLOAD.exists():
        print(f"✗ No encontrado: {HR_UPLOAD}")
        print("  Ejecuta el Shortcut 'Gym HR Upload' en tu iPhone para generar el archivo.")
        sys.exit(1)

    with open(HR_UPLOAD) as f:
        data = json.load(f)

    raw = data.get("samples", [])
    samples = []
    skipped = 0
    for s in raw:
        try:
            # Normalise: handle Z, +0200 (no colon), +02:00, naive datetime
            ts_str = s["ts"].strip().replace("Z", "+00:00")
            # Add colon to timezone offset if missing (+0200 → +02:00)
            import re as _re
            ts_str = _re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', ts_str)
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            samples.append({"ts": ts, "bpm": float(s["bpm"])})
        except Exception:
            skipped += 1
            continue

    if skipped:
        print(f"   ⚠ {skipped} muestras descartadas (formato inválido)")

    samples.sort(key=lambda x: x["ts"])
    print(f"   {len(samples)} muestras de FC cargadas "
          f"({samples[0]['ts'].strftime('%H:%M')}–{samples[-1]['ts'].strftime('%H:%M')} UTC)" if samples else "   0 muestras")
    return samples, data.get("workout_date")

# ── Match FC ↔ serie ──────────────────────────────────────────────────────────

def hr_for_window(samples, t_start_str, t_end_str):
    """Filtra muestras entre t_start y t_end+margen. Devuelve (avg, max, count)."""
    if not t_start_str or not t_end_str:
        return None, None, 0
    t0 = datetime.fromisoformat(t_start_str.replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(t_end_str.replace("Z", "+00:00"))
    if t0.tzinfo is None: t0 = t0.replace(tzinfo=timezone.utc)
    if t1.tzinfo is None: t1 = t1.replace(tzinfo=timezone.utc)
    t1_ext = t1 + timedelta(seconds=END_MARGIN_SEC)

    window = [s["bpm"] for s in samples if t0 <= s["ts"] <= t1_ext]
    if not window:
        return None, None, 0
    return round(sum(window) / len(window), 1), round(max(window)), len(window)

# ── Procesado de una sesión ───────────────────────────────────────────────────

def process_session(db, samples, session_key, doc_data, dry_run=False):
    data = doc_data.get("data", {})
    updates = 0

    for ex_id, ex_data in data.items():
        sets = ex_data.get("sets", [])
        for i, s in enumerate(sets):
            if not s.get("t_start") or not s.get("t_end"):
                continue
            avg, mx, count = hr_for_window(samples, s["t_start"], s["t_end"])
            if avg is not None:
                sets[i]["avg_hr"] = avg
                sets[i]["max_hr"] = mx
                sets[i]["hr_samples"] = count
                dur = round((
                    datetime.fromisoformat(s["t_end"].replace("Z", "+00:00")) -
                    datetime.fromisoformat(s["t_start"].replace("Z", "+00:00"))
                ).total_seconds())
                print(f"   {session_key} · {ex_id[:20]} S{i+1} ({dur}s): "
                      f"avg {avg} / max {mx} bpm  [{count} muestras]")
                updates += 1
            else:
                print(f"   {session_key} · {ex_id[:20]} S{i+1}: sin muestras en ventana "
                      f"({s['t_start'][11:19]}–{s['t_end'][11:19]})")

    if updates > 0 and not dry_run:
        ref = (db.collection("users")
                 .document(USER_UID)
                 .collection("sessions")
                 .document(session_key))
        ref.set(doc_data)
        print(f"   ✓ {session_key} actualizado en Firestore ({updates} series con HR)")

    return updates

# ── Archivo del upload ────────────────────────────────────────────────────────

def archive_upload():
    HR_ARCHIVE.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = HR_ARCHIVE / f"hr_{ts}.json"
    HR_UPLOAD.rename(dest)
    print(f"   Archivo archivado → gym_hr_archive/hr_{ts}.json")

# ── Main ──────────────────────────────────────────────────────────────────────

def main(target_date=None, dry_run=False):
    print(f"── HR Match · {datetime.now().strftime('%Y-%m-%d %H:%M')} ──")
    if dry_run:
        print("   [modo dry-run: no se escribe en Firestore]")

    samples, upload_date = load_hr_samples()
    if not samples:
        print("✗ Sin muestras de FC. Revisa el archivo de upload.")
        sys.exit(1)

    date_filter = target_date or upload_date or datetime.now().strftime("%Y-%m-%d")
    print(f"   Buscando sesiones del: {date_filter}")

    db = init_firestore()
    sessions_ref = (db.collection("users")
                      .document(USER_UID)
                      .collection("sessions"))

    total_updates = 0
    sessions_found = 0
    for doc in sessions_ref.stream():
        data = doc.to_dict()
        if data.get("date") != date_filter:
            continue
        sessions_found += 1
        print(f"\n   → Sesión: {doc.id}")
        total_updates += process_session(db, samples, doc.id, data, dry_run)

    print()
    if sessions_found == 0:
        print(f"⚠ No se encontraron sesiones para {date_filter}.")
        print("  ¿Registraste el entreno en la app web ese día?")
    elif total_updates == 0:
        print("⚠ Sesiones encontradas pero ninguna serie tiene timestamps.")
        print("  Asegúrate de tocar los inputs de peso/reps durante el entreno (no antes).")
    else:
        print(f"✅ HR match completado — {total_updates} series actualizadas.")

    if not dry_run:
        archive_upload()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match Apple Watch HR → gym series")
    parser.add_argument("--date", help="Fecha objetivo YYYY-MM-DD (por defecto: la del archivo)")
    parser.add_argument("--dry-run", action="store_true", help="No escribe en Firestore")
    args = parser.parse_args()
    main(target_date=args.date, dry_run=args.dry_run)
