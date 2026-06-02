# Moving the database to Singapore

Your current setup is:
- **Render** (your app): Singapore region ✅
- **Neon** (your database): us-east-1 (Virginia) ❌

Every page load: India → Singapore (app) → Virginia (DB) → Singapore → India.
Each DB query adds ~500ms of round-trip latency. The homepage runs ~15 queries.

**After this migration:**
- App + DB both in Singapore. Same data center → <5ms per query.
- Page loads drop from 5–10 seconds to ~1 second.

Neon doesn't let you change a project's region after creation, so we create
a new Singapore project and re-upload the data. The original us-east-1 project
can be deleted afterward.

---

## Step 1 — Create a new Neon project in Singapore (3 min)

1. Sign in at https://console.neon.tech
2. Click the project dropdown (top-left) → **Create project**
3. Fill in:
   - Project name: `politrack-sg`
   - Postgres version: 16 (default)
   - Region: **AWS Asia Pacific (Singapore)** — `ap-southeast-1`
   - Database name: `politrack`
4. After creation, copy the new connection string. It will look like:
   ```
   postgresql://neondb_owner:xxxx@ep-something-12345.ap-southeast-1.aws.neon.tech/politrack?sslmode=require
   ```
   Notice the `.ap-southeast-1.` in the host — that's how you know it's Singapore.

---

## Step 2 — Upload your local data to the new Neon (5 min)

```bash
cd /path/to/PoliticansData
source .venv/bin/activate

# Point the loader at the NEW Singapore Neon
export DATABASE_URL="postgresql://neondb_owner:xxxx@ep-something-12345.ap-southeast-1.aws.neon.tech/politrack?sslmode=require"

python scripts/sqlite_to_postgres.py --reset
```

You should see the per-batch progress dots now flying — Singapore is much
closer to wherever you are than us-east-1. The whole load should finish in
under 90 seconds (was several minutes before).

---

## Step 3 — Update Render to use the new database (1 min)

1. Open the Render dashboard → your `politicansdata` service → **Environment**
2. Find `DATABASE_URL`
3. Click the edit icon and paste the **new Singapore** connection string
4. Click **Save Changes**

Render auto-redeploys with the new env var (about 60 seconds). The first
page load after the redeploy will be noticeably faster — sometimes 5x.

---

## Step 4 — Delete the old us-east-1 project (optional, 30 sec)

Once the new setup is confirmed working:

1. Neon dashboard → click the **politrack** project (the old us-east-1 one)
2. **Settings → Delete project**

Free tier lets you keep both, but tidiness counts.

---

## Verifying the speedup

Open https://politicansdata.onrender.com in a fresh browser tab and time it:

- **Before** (us-east-1 DB): homepage typically 5–10 seconds
- **After** (Singapore DB): should be 1–2 seconds after the Render container is warm

The first hit after 15 minutes of idle will still be slow (Render cold start —
unrelated to the DB). Subsequent hits within that window will be fast.

If you want to eliminate cold starts too, upgrade Render to Starter ($7/mo).
The DB move is the bigger win though.
