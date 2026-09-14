# Running the API locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra gemini
cp .env.example .env                 # PowerShell: Copy-Item .env.example .env
uv run alembic upgrade head          # create the tables
uv run uvicorn app.main:app --reload --port 8222
```

Interactive API documentation is then at <http://localhost:8222/docs>.

## What you need in `.env`

Only two provider keys, and both are free:

| Key | Where | Free tier |
|---|---|---|
| `GOOGLE_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | 1,500 requests/day |
| `PEXELS_API_KEY` | [pexels.com/api](https://www.pexels.com/api/) | 20,000 images/month |

Plus a [Supabase](https://supabase.com) project for the database, auth and
image storage. Copy `DATABASE_URL` from **Settings → Database → Connection
string**, and the keys from **Settings → API**.

> Use the **session pooler** connection string, not `db.<ref>.supabase.co` —
> the direct host is IPv6-only and unreachable from most networks. And
> percent-encode special characters in the password: `pass@word` becomes
> `pass%40word`.

`ADMIN_TOKEN` guards the two back-office endpoints. Leave it blank locally and
they return 404; generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Layout

```
app/
  main.py                 the app: CORS, extraction, health
  core/
    config.py             settings, provider chains, public URL rules
    db.py                 async engine and per-request session
    storage.py            Supabase Storage — dish photographs and logos
  models/                 owners · restaurants · sections · dishes
  modules/
    auth/                 verifies a Supabase session against its public keys
    billing/              trials, renewals, what a lapsed account can still do
    extraction/           photo → structured menu, with provider fallback
    images/               finding and vetting a photograph for a dish
    restaurants/          the owner's API, publishing, logos, per-dish photos
    qr/                   QR codes and the printable table card
  schemas/menu.py         the shape the vision model must return
alembic/                  migrations
scripts/verify_deploy.py  end-to-end checks against a deployed instance
```

Domain modules are kept separate on purpose. If one needs to scale on its own
later — image generation is the likely first — it can move out to its own
service without a rewrite.

## The endpoints

| | |
|---|---|
| `POST /api/extract` | photo → structured menu *(signed in)* |
| `GET&nbsp;/api/me/restaurants` | the owner's menus |
| `GET&nbsp;/api/me/subscription` | trial and renewal state |
| `POST /api/restaurants` | create one from a reviewed menu |
| `PATCH /api/restaurants/{id}` | name, address, WhatsApp, dishes |
| `POST /api/restaurants/{id}/publish` | find photographs, go live |
| `POST /api/restaurants/{id}/logo` | the restaurant's logo |
| `POST /api/restaurants/{id}/items/{id}/photo` | replace one dish's photograph |
| `GET&nbsp;/api/restaurants/{id}/qr.png` | QR code, or `?tent=true` for the table card |
| `GET&nbsp;/api/public/menus/{slug}` | what a diner sees — no auth, by design |
| `GET&nbsp;/api/internal/expiring` | whose trial ends soon *(admin token)* |
| `POST /api/internal/owners/{email}/paid` | record a payment *(admin token)* |

Owner routes are scoped to the signed-in owner, so one restaurant can never
read or change another's menu.

## Things that will bite you

**Photographs the owner chose are never overwritten.** Each dish records where
its photograph came from — `pexels`, `ai`, or `owner` — and re-publishing skips
anything marked `owner`.

**Renaming a dish keeps its photograph.** Menus are rewritten wholesale on
save, so images are re-matched by name first and position second. Renaming
survives; reordering survives; doing both to the same dish in one save does
not.

**Some subdomains are reserved.** `api`, `www`, `admin` and about twenty-five
others can never become a restaurant's address, or they would shadow our own
hosts. The list lives in `modules/restaurants/service.py` and is mirrored in
the web app's middleware — change both together.

**Nothing is written to local disk.** A deployed container's filesystem is
temporary, so photographs go to object storage and menus to Postgres.

**Migrations run at container start** on Render (`MIGRATE_ON_START=1`), because
the free plan has no pre-deploy hook. Deploy order therefore matters: the
migration runs before the new code serves traffic.

## Verifying a deployment

```bash
python scripts/verify_deploy.py --api <api-url> --web <site-url> --slug <a-restaurant>
```

Checks health, both provider keys, CORS in both directions, that owner routes
reject anonymous callers and forged tokens, that the public menu renders, and
that dish photographs are actually readable. Exit code is non-zero if anything
failed, so it can gate a deploy.

## Deploying

See [DEPLOY.md](../DEPLOY.md).
