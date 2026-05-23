# WHOOP Sync — Pipeline de datos

Carga datos de WHOOP (recovery, sueño, workouts, peso) y los escribe en
Firebase Firestore. La web app los lee directamente desde ahí, sin necesidad
de CORS ni de que el navegador llame a la API de WHOOP.

```
WHOOP API → sync.py → Firestore → Web App (solo lectura)
```

---

## Setup (una sola vez)

### 1. Instalar dependencias

```bash
cd whoop-sync
pip install -r requirements.txt
```

### 2. Configurar credenciales

```bash
cp .env.example .env
```

Edita `.env` y rellena:
- `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` — del [WHOOP Developer Portal](https://developer-dashboard.whoop.com)
- `FIREBASE_USER_UID` — tu UID de Firebase Auth (lo encuentras en Firebase Console → Authentication → Users, o escribe `firebase.auth().currentUser.uid` en la consola de la web app)

### 3. Descargar Service Account de Firebase

1. Ve a [Firebase Console](https://console.firebase.google.com) → Proyecto `gym-tracker-at`
2. ⚙️ Project Settings → Service Accounts
3. **Generate new private key** → descarga el JSON
4. Guárdalo como `whoop-sync/firebase-service-account.json`

### 4. Añadir Redirect URI en WHOOP Developer Portal

En [developer-dashboard.whoop.com](https://developer-dashboard.whoop.com) → tu app → edita y añade:

```
http://localhost:8080
```

### 5. Autorizar WHOOP (una sola vez)

```bash
python auth.py
```

Se abre el navegador, inicias sesión en WHOOP y se guardan los tokens en `tokens.json`.

---

## Uso diario

```bash
python sync.py
```

Sincroniza los últimos 7 días de workouts + recovery + sueño de hoy.

```bash
python sync.py --days 30   # ampliar ventana histórica
```

### Automatizar con cron (macOS)

```bash
crontab -e
```

Añade (sincroniza cada día a las 9:00):
```
0 9 * * * cd /Users/kcvc888/Desktop/Fitness\ tracker/whoop-sync && python sync.py >> sync.log 2>&1
```

---

## Estructura en Firestore

```
/users/{uid}/whoop/cache
  recovery:
    score:  78          # 0–100
    hrv:    52.3        # ms
    rhr:    54          # bpm
    spo2:   96.8        # %
  sleep:
    performance: 85.2   # %
    duration_min: 420
  workouts:
    "2026-05-22":
      strain:   12.4
      avgHr:    142
      maxHr:    178
      calories: 480
      duration: 65      # minutos
  body:
    weight_kilogram: 78.5
    height_meter:    1.78
    max_heart_rate:  195
  lastSync: "2026-05-23T09:00:00+00:00"
```

---

## Archivos ignorados por git

- `.env` — credenciales WHOOP y Firebase UID
- `tokens.json` — tokens OAuth de WHOOP
- `firebase-service-account.json` — clave privada de Firebase
