# Deploying RestoFood

Two services, both deployed from GitHub. **You don't need Docker or any CLI
installed locally** — Fly builds the image remotely and Vercel builds from the
repo.

```
Vercel   resto-ui    → the console + every restaurant's menu
Fly.io   resto-api   → the API (Mumbai, next to the database)
Supabase             → Postgres, auth, and image storage (already running)
```

Deploy the **API first** — the frontend needs its URL.

---

## 1. API — pick a host

Both options use the same `Dockerfile`; you can switch later by redeploying.

| | Render (free) | Fly.io (~$2-5/mo) |
|---|---|---|
| Cost | **$0**, no card | Card required |
| Closest region | Singapore, ~100-150ms from India | **Mumbai, ~20-40ms** |
| When idle | **Sleeps after 15 min → 30-60s cold start** | Stays warm |

Start on **Render** while you're testing. Move to Fly before a real cafe
depends on it — a diner who waits 50 seconds for a menu just puts the phone
down.

Two things already soften the cold start: menus are cached at Vercel's edge
for 60 seconds, so most scans never reach the API at all, and
`.github/workflows/keep-warm.yml` pings the service every 10 minutes.

---

### Option A — Render (free, no card)

1. <https://dashboard.render.com> → **New → Web Service**
2. Connect GitHub, pick **`pavan29903/Resto-api`**
3. Render reads `render.yaml` and fills in the Docker settings. Confirm:
   - Runtime **Docker**, Region **Singapore**, Plan **Free**
4. Add the secrets under **Environment** (everything marked `sync: false`):

```
DATABASE_URL          postgresql://postgres.<ref>:<url-encoded-pw>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
SUPABASE_URL          https://<ref>.supabase.co
SUPABASE_ANON_KEY     sb_publishable_...
SUPABASE_SERVICE_KEY  sb_secret_...
GOOGLE_API_KEY        ...
PEXELS_API_KEY        ...
```

5. **Create Web Service.** First build takes ~5 minutes.

Migrations run at container start (`MIGRATE_ON_START=1`) because Render's free
plan has no pre-deploy hook.

Your URL: `https://restofood-api.onrender.com`

**Keep it warm:** in GitHub → Settings → Secrets and variables → Actions →
**Variables**, add `API_URL` = your Render URL. The bundled workflow then pings
it every 10 minutes.

---

### Option B — Fly.io (Mumbai, stays warm)

```powershell
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup
cd resto-api
fly launch --no-deploy      # say NO to Postgres and Redis
```

Set the same secrets:

```powershell
fly secrets set `
  DATABASE_URL="postgresql://postgres.<ref>:<url-encoded-pw>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres" `
  SUPABASE_URL="https://<ref>.supabase.co" `
  SUPABASE_ANON_KEY="sb_publishable_..." `
  SUPABASE_SERVICE_KEY="sb_secret_..." `
  GOOGLE_API_KEY="..." `
  PEXELS_API_KEY="..."
```

```powershell
fly deploy
fly open /health     # expect {"ok":true}
```

Fly runs migrations as a `release_command`, so a failed migration aborts the
deploy instead of crash-looping.

---

> **Whichever you choose**, use the **session-pooler** `DATABASE_URL`, not
> `db.<ref>.supabase.co` — the direct host is IPv6-only and unreachable from
> most networks. And percent-encode the password: `Keevan@2815` becomes
> `Keevan%402815`.

## 2. Frontend on Vercel

No CLI needed.

1. <https://vercel.com/new> → **Import** `pavan29903/Resto-ui`
2. Framework preset: **Next.js** (detected automatically)
3. Add these environment variables:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://restofood-api.onrender.com` (or your Fly URL) |
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | `sb_publishable_...` |

4. **Deploy**

Only the **publishable** key goes here — it ships to the browser. The secret
key stays on Fly.

---

## 3. Close the loop

Tell the API which origin may call it, then redeploy:

Render: add `CORS_ORIGINS` and `PUBLIC_BASE_URL` under Environment.
Fly: `fly secrets set CORS_ORIGINS="https://<your-app>.vercel.app" PUBLIC_BASE_URL="https://<your-app>.vercel.app"`

Then in Supabase → **Authentication → URL Configuration**, set the Site URL to
your Vercel URL and add it under Redirect URLs, or email confirmation links
will point at `localhost`.

Now sign up on the deployed site and publish a menu end to end.

---

## 4. Your own domain (once you buy it)

Point the domain's **nameservers at Vercel** — not Cloudflare, not the
registrar's default. Vercel needs DNS control to issue the wildcard
certificate that makes `*.restofood.in` work.

In Vercel → Project → Domains, add both:

```
restofood.in
*.restofood.in      ← this is what gives each restaurant its own address
```

Then update the API. On **Render**, add these under Settings → Environment
(on Fly, `fly secrets set` with the same three):

```
MENU_DOMAIN     = restofood.in
PUBLIC_BASE_URL = https://restofood.in
CORS_ORIGINS    = https://restofood.in
```

and add `NEXT_PUBLIC_MENU_DOMAIN=restofood.in` in Vercel, so the middleware
knows which host suffix marks a restaurant subdomain.

`CORS_ORIGINS` only needs the apex. Once `MENU_DOMAIN` is set, every
`<slug>.restofood.in` is allowed by the regex in `config.cors_origin_regex`,
which is why each new restaurant doesn't need a deploy.

`MENU_DOMAIN` also changes what *new* QR codes point at — from `/r/<slug>` to
`https://<slug>.restofood.in`.

**Codes printed before this change keep working, and do not need reprinting.**
The subdomain is a rewrite onto the same `/r/<slug>` route, and the middleware
leaves `/r/` paths alone (`middleware.ts`), so both forms serve the same menu
indefinitely. The old `*.vercel.app` address also stays attached to the
project. Nothing a restaurant has already put on its tables goes dead.

---

## Costs

| | |
|---|---|
| Vercel Hobby | Free |
| Render Free | $0 — sleeps when idle |
| Fly.io, 1 shared-cpu-1x / 512 MB | ~$2–5/month, stays warm |
| Supabase Free | Free — **but a project pauses after 7 days idle.** Move to Pro ($25/mo) before a real cafe depends on it |
| Domain | ~₹1,000/year |

## Troubleshooting

| Symptom | Cause |
|---|---|
| Migrations fail on deploy | `DATABASE_URL` wrong or unencoded password |
| First request takes ~50s | Render free tier cold start — set the `API_URL` variable so the keep-warm workflow runs |
| Everything 500s after a quiet week | The Supabase project paused. The keep-warm workflow pings a database-backed endpoint to prevent this; check it's still enabled (GitHub disables schedules after 60 days without commits) |
| Menu loads, console won't sign in | Supabase Site URL still `localhost` |
| Browser console shows a CORS error | `CORS_ORIGINS` doesn't list the Vercel origin |
| `getaddrinfo failed` in logs | Using the IPv6-only direct DB host instead of the pooler |
| QR codes point at localhost | `PUBLIC_BASE_URL` not set on the API host |
| `<slug>.restofood.in` shows the landing page | `NEXT_PUBLIC_MENU_DOMAIN` not set in Vercel — the middleware can't tell a restaurant subdomain from any other host |
| Subdomain gives a certificate warning | The wildcard cert is still issuing, or the domain isn't on Vercel's nameservers |
