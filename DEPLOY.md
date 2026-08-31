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

## 1. API on Fly.io

### Install flyctl (one time)

```powershell
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup   # or: fly auth login
```

### Launch

From `resto-api/`:

```powershell
fly launch --no-deploy
```

Say **no** to creating a Postgres database and to Redis — Supabase is already
the database. `fly.toml` in this repo already sets the app name, the Mumbai
region (`bom`), the health check, and runs `alembic upgrade head` on every
deploy.

### Set the secrets

These are the values from your local `.env`. They are never baked into the
image.

```powershell
fly secrets set `
  DATABASE_URL="postgresql://postgres.<ref>:<url-encoded-password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres" `
  SUPABASE_URL="https://<ref>.supabase.co" `
  SUPABASE_ANON_KEY="sb_publishable_..." `
  SUPABASE_SERVICE_KEY="sb_secret_..." `
  GOOGLE_API_KEY="..." `
  PEXELS_API_KEY="..." `
  EXTRACTION_PROVIDER="gemini" `
  IMAGE_PROVIDER="pexels" `
  PUBLIC_BASE_URL="https://<your-vercel-app>.vercel.app"
```

> **Use the session-pooler DATABASE_URL**, not the `db.<ref>.supabase.co` one.
> The direct host is IPv6-only and unreachable from most networks.
>
> **Percent-encode special characters in the password** — a literal `@` breaks
> the URI. `Keevan@2815` becomes `Keevan%402815`.

### Deploy

```powershell
fly deploy
fly open /health      # expect {"ok":true}
```

Note the URL it prints (`https://restofood-api.fly.dev`) — the frontend needs it.

---

## 2. Frontend on Vercel

No CLI needed.

1. <https://vercel.com/new> → **Import** `pavan29903/Resto-ui`
2. Framework preset: **Next.js** (detected automatically)
3. Add these environment variables:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://restofood-api.fly.dev` |
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | `sb_publishable_...` |

4. **Deploy**

Only the **publishable** key goes here — it ships to the browser. The secret
key stays on Fly.

---

## 3. Close the loop

Tell the API which origin may call it, then redeploy:

```powershell
fly secrets set CORS_ORIGINS="https://<your-app>.vercel.app"
```

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

Then update the API:

```powershell
fly secrets set `
  MENU_DOMAIN="restofood.in" `
  PUBLIC_BASE_URL="https://restofood.in" `
  CORS_ORIGINS="https://restofood.in"
```

and add `NEXT_PUBLIC_MENU_DOMAIN=restofood.in` in Vercel, so the middleware
knows which host suffix marks a restaurant subdomain.

`MENU_DOMAIN` also changes what QR codes point at — from
`/r/<slug>` to `https://<slug>.restofood.in`. **Regenerate any QR codes printed
before this change.**

---

## Costs

| | |
|---|---|
| Vercel Hobby | Free |
| Fly.io, 1 shared-cpu-1x / 512 MB | ~$2–5/month |
| Supabase Free | Free — **but a project pauses after 7 days idle.** Move to Pro ($25/mo) before a real cafe depends on it |
| Domain | ~₹1,000/year |

## Troubleshooting

| Symptom | Cause |
|---|---|
| `release_command` fails on deploy | `DATABASE_URL` wrong or unencoded password |
| Menu loads, console won't sign in | Supabase Site URL still `localhost` |
| Browser console shows a CORS error | `CORS_ORIGINS` doesn't list the Vercel origin |
| `getaddrinfo failed` in logs | Using the IPv6-only direct DB host instead of the pooler |
| QR codes point at localhost | `PUBLIC_BASE_URL` not set on Fly |
