# Going live — Render + Neon (free tier)

This walks you from "code on GitHub" to "live public URL" in about 20 minutes.
Everything stays on free tiers; no credit card required.

**End result:** your site at `https://politrack-india.onrender.com` (or
whichever subdomain Render assigns), reading from a hosted Postgres at Neon.

---

## Step 1 — Create the Neon Postgres database (5 min)

1. Sign up at https://neon.tech with your GitHub account (free, no card).
2. After login you'll be in the **Console**. Click **Create Project**.
   - Project name: `politrack`
   - Postgres version: leave default (16)
   - Region: **AWS Asia Pacific (Singapore)** — closest to India.
   - Database name: `politrack`
3. After creation, Neon shows a **Connection string**. It looks like:
   ```
   postgresql://neondb_owner:abc123@ep-xyz-123.ap-southeast-1.aws.neon.tech/politrack?sslmode=require
   ```
   Click the **Show password** toggle, then copy the whole string. Keep this
   tab open — you'll paste it into two places.

> Free-tier note: Neon's free project auto-suspends after 5 minutes of
> inactivity and wakes on the next query (~1s). Fine for launch traffic.

---

## Step 2 — Upload your local data into Neon (5 min)

Your scraped data is currently in `politrack.db` on your laptop. Push it once
to Neon so the live site has data on day one.

```bash
cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"

# Activate your local venv (or create one if you haven't)
source .venv/bin/activate
pip install -r requirements.txt   # picks up the new psycopg2-binary

# Paste the Neon connection string here, in quotes
export DATABASE_URL="postgresql://neondb_owner:...@ep-xyz.neon.tech/politrack?sslmode=require"

# Run the one-time loader
python scripts/sqlite_to_postgres.py
```

You'll see one line per table with row counts. Should finish in under a
minute. The script is **idempotent** — safe to re-run if it dies partway.

---

## Step 3 — Deploy to Render (8 min)

1. Sign up at https://render.com with GitHub (free, no card).
2. From the dashboard, click **New +** → **Blueprint**.
3. Connect your `PoliticansData` repo (Render will ask for GitHub access).
4. Render detects the `render.yaml` we committed and shows a preview of the
   service it will create. Click **Apply**.
5. On the next screen, Render asks for the one env var marked `sync: false`:
   - **`DATABASE_URL`**: paste the same Neon connection string from Step 1.
   - Click **Create Web Service**.
6. The first build runs — `pip install -r requirements.txt` then
   `gunicorn ... app.main:app`. Takes 3–5 minutes. You'll see logs stream
   live in the Render dashboard.
7. When the status flips to **Live**, click the URL at the top of the page
   (something like `https://politrack-india.onrender.com`).

That's the live site.

---

## Step 4 — Sanity-check (2 min)

Open the URL and confirm:

- Homepage loads and shows real numbers (not zeros).
- State selector flips between Punjab / Bihar / Goa.
- A politician detail page opens (click any name in the rankings).
- The India map renders (Leaflet + GeoJSON).

If any KPI shows zero, it usually means the Neon upload didn't finish for that
table — re-run `scripts/sqlite_to_postgres.py` (it picks up where it left off).

---

## What about cold starts?

Render's free tier spins your service down after 15 minutes of no traffic.
First visitor after that waits ~30 seconds for it to wake. Fine for a soft
launch. Two ways to fix when you're ready:

1. **Upgrade to Render Starter ($7/mo)** — always-on, also gives you a small
   persistent disk in case you want to switch back to SQLite.
2. **Hit it with a free uptime monitor** every 10 minutes (UptimeRobot,
   BetterStack). Keeps it warm at no cost. Slightly hacky.

---

## Updating data later

When you re-scrape (e.g. new affidavits) your local `politrack.db` updates.
To push the new rows to Neon, just re-run the loader:

```bash
export DATABASE_URL="postgresql://..."
python scripts/sqlite_to_postgres.py
```

The `ON CONFLICT DO NOTHING` clause means existing rows are skipped; only
new rows get inserted.

If you want to fully replace the data (rare), drop the tables in Neon's
SQL editor first, then re-run the loader.

---

## Attaching a real domain later

Once you buy `politrack.in` (or whatever):

1. In Render: Service → **Settings** → **Custom Domains** → add `politrack.in`.
2. Render gives you a CNAME target. Add that CNAME in your domain registrar's
   DNS panel.
3. Wait 5–60 minutes for DNS to propagate. Render auto-issues a free SSL cert.

---

## Troubleshooting

**Build fails with "psycopg2 error"** — Render is using an old Python image.
In the dashboard, set `PYTHON_VERSION=3.11` under Environment.

**App crashes with "no such table"** — the schema wasn't created in Neon.
The data loader script creates it automatically; if you skipped that step,
run it now. Alternatively connect with `psql` and run
`python -c "from app.database import engine; from app.models import Base; Base.metadata.create_all(engine)"`.

**500s on the live site** — open the Render logs tab. The most common
cause is `DATABASE_URL` not being set or having a typo. Copy-paste it fresh
from Neon.

**Slow first load** — that's the free-tier cold start. See "What about cold
starts?" above.
