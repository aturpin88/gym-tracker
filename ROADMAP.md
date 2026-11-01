# Roadmap: nutrition, Apple Watch, AI coach

Drafted 1 November 2026. Scope: **just me** for now. Making it work for other people is a separate decision, parked at the end.

## Where the app stands today

- The app is one page (`index.html`) hosted on GitHub Pages.
- Sign-in is Google through Firebase. Workouts sync to Firestore under `users/{uid}/sessions` and `users/{uid}/config`.
- **There is no server code yet.** Firestore is a database the page talks to directly, so it can't hold a secret (like an AI key) or receive data from Health Auto Export. Both need a small piece of server code. The natural fit is **Firebase Cloud Functions** in the same project (`gym-tracker-at`). Then everything stays in one place: the same login, the same database, the same console.
- Recovery, sleep and strain are typed in by hand today. Apple Watch data replaces that.

## The order

| # | Phase | Needs the server? | Why this order |
|---|-------|-------------------|----------------|
| 1 | Nutrition tab | No | Uses only what exists today. Start right away and run it alongside MyFitnessPal. |
| 2 | Server foundation | It *is* the server | Small, done once. Unblocks phases 3 and 5. |
| 3 | Apple Watch data in | Yes | Pull in history once, then keep it topped up automatically. |
| 4 | My own recovery / strain / sleep | No (maths in the app) | Replaces manual entry. Gives the AI coach clean numbers to read. |
| 5 | AI coach (chat) | Yes | Last, because a chat on top of messy data is useless. |
| 6 | Opening it to others | n/a | Decide later. See the end. |

---

## Phase 1: Nutrition tab

**Goal:** logging a meal is as fast as in MyFitnessPal, or faster.

1. **Data model** (Firestore, under my user):
   - `foods/{id}`: my food library. Name, brand, barcode, **nutrients per 100 g** (kcal, protein, carbs, fat, fibre, salt), default portion, source (`off`, `generic`, `manual`).
   - `meals/{id}`: saved meals, for example "usual breakfast" = a list of foods + grams.
   - `nutrition/{YYYY-MM-DD}`: what I ate that day, grouped by breakfast / lunch / dinner / snacks. Each entry is a food + grams, with the nutrients copied in so an old day never changes if I fix a food later.
2. **Barcode scan → Open Food Facts.** Camera button → the `barcode-detector` polyfill (iOS Safari still has no native support) → `GET world.openfoodfacts.org/api/v2/product/{barcode}.json` → show the label values → pick grams → log. Save the product into `foods` on first use.
3. **My library first, OFF second.** The search box looks in my own foods, recents and saved meals instantly. Open Food Facts only gets searched when I press "Search online" (their limit is 10 searches a minute, so no search-as-you-type).
4. **Speed features** (they matter more than charts): recents at the top, "copy yesterday's breakfast", saved meals, a quick-add of plain kcal/protein.
5. **Generic foods** for things without a barcode (fruit, butcher's chicken, cooked rice). Seed a small list from CIQUAL or USDA, or add my own as I go.
6. **Manual add/edit**, always per 100 g, so I can fix wrong Open Food Facts entries.
7. **Day view:** totals vs targets (kcal, protein). A weekly trend comes later.
8. **Migration:** use both apps in parallel for 2–3 weeks. Then export my MyFitnessPal history (CSV) and import it into `nutrition/` so past months show up too.

Done when: I've stopped opening MyFitnessPal for a week.

---

## Phase 2: Server foundation

1. Switch the Firebase project to the **Blaze (pay-as-you-go) plan**, which Cloud Functions require. At my volume it should stay inside the free allowance. **Set a budget alert (e.g. €5/month) on day one.**
2. Add a `functions/` folder to the repo (Node/TypeScript) and a `firebase.json`, and deploy with the Firebase CLI.
3. Store secrets (ingest token, AI key) in **Firebase's secret storage**, never in the repo or in `index.html`.
4. Put the **Firestore security rules** in the repo (`firestore.rules`) so they're versioned. Rule: a user can only read and write their own `users/{uid}/…`. Server-written collections (health data) can be read but not written from the browser.

Done when: a test function deploys and the page can call it while signed in.

---

## Phase 3: Apple Watch data in

Apple Health data only leaves the iPhone through an app on the phone, so: **Health Auto Export (iPhone) → my ingest function → Firestore.**

1. **Ingest function** `healthIngest` (HTTPS):
   - Checks a long secret token sent in a header. Wrong or missing token → rejected.
   - For now the token maps straight to my user ID (just me).
   - Saves the raw payload (for re-processing later), then splits it into tidy records.
2. **Storage** (under my user):
   - `health_raw/{receivedAt}`: untouched JSON, kept for a while in case parsing changes.
   - `health_daily/{YYYY-MM-DD}`: one doc per day. Resting HR, HRV, sleep stages and duration, active energy, steps, VO₂max, weight.
   - `health_workouts/{id}`: Apple Watch workouts with start/end and heart-rate samples.
   - Split by day because Firestore docs max out at 1 MB, and a raw export is bigger.
   - Writes are "upsert by date/ID", so sending the same data twice does no harm.
3. **Health Auto Export setup:** buy Premium (scheduled automations need it). Create a REST API automation pointing at the function URL with the token header, JSON format, the metrics above + workouts, hourly or a few times a day.
4. **Backfill:** one manual export of all history, posted to the same endpoint (in chunks if needed).
5. **In the app:** read `health_daily` and show it on the day/home view next to training.
6. **Match heart rate to sets:** match Apple Watch heart-rate samples to my set timestamps to get **peak HR per set and how fast it drops during rest**. This was in my backlog, and peak HR is more useful than an average.
7. **A "last synced" indicator,** because the iPhone app sometimes needs opening to wake up.

Done when: last night's sleep and HRV show up in the app by morning without me doing anything.

---

## Phase 4: My own recovery, strain and sleep scores

Replaces the manual numbers WHOOP used to give me.

- **Recovery (0–100%):** today's HRV and resting HR compared with my own rolling 30-day baseline, plus last night's sleep.
- **Strain (0–21):** heart-rate load from the day's workouts and activity (time spent in each HR zone, weighted).
- **Sleep:** duration and consistency vs a target.
- Keep the formulas simple and in one place, show the inputs next to each score, and tune them over a few weeks against how I actually feel.
- Manual entry stays as an override.

Done when: the manual strain/recovery boxes fill themselves in.

---

## Phase 5: AI coach

**Principle:** code does the maths, the model explains it. The model never gets thousands of raw rows to add up.

1. **Chat function** `coachChat` (callable, so only I can use it while signed in). The AI key lives on the server.
2. **A handful of query tools** the model can call. Each runs real code against Firestore and returns small, already-calculated results:
   - `nutrition_summary(from, to)`: daily kcal/protein/carbs/fat, averages, days on target.
   - `training_summary(from, to, exercise?)`: sessions, volume, top sets, PRs.
   - `exercise_progress(name)`: estimated 1RM / best sets over time.
   - `health_summary(metric, from, to)`: HRV, resting HR, sleep, recovery, strain.
   - `compare(metric_a, metric_b, from, to)`: for example "sleep vs next-day volume", with the correlation computed in code.
3. **Chat screen** in the app: a simple thread, with answers that show the numbers they used.
4. **Guard rails:** a monthly spend cap with the AI provider, a message limit per day, and no medical advice.
5. **Later:** a label photo → nutrients, using the same server; quick questions during a workout ("what did I do on this last time?").

Provider: still to pick (Gemini fits the Google/Firebase setup; Claude or others also work the same way through the function).

Done when: I can ask "why was my bench flat this month?" and get an answer that points at real numbers (sleep, protein, volume).

---

## Phase 6: Opening it to others (decide later, not now)

What changes the moment someone else signs up:

- **GDPR:** heart rate, sleep and food logs count as special-category health data. That means explicit consent, a real privacy policy (`privacy.html` needs rewriting), data export and deletion, an EU data location, and probably an agreement with each provider that processes the data (Firebase, the AI provider).
- **Apple Watch:** I can't ask everyone to buy Health Auto Export. That means my own small iPhone app, which is a bigger project.
- **Ingest tokens:** one per user, created and revocable from the app.
- **AI costs:** per-user limits, or a paid tier.
- **Open Food Facts:** credit them in the app. Any food database I share that's built from theirs has to stay open (ODbL).
- **Don't scrape** supermarket sites (e.g. Mercadona). Their terms forbid it and it breaks without warning.

---

## Decisions still open

- [ ] Nutrition targets: fixed numbers, or different on training vs rest days?
- [ ] AI provider for phase 5.
- [ ] How often Health Auto Export syncs (hourly vs a few times a day).
- [ ] How long to keep raw health payloads.
