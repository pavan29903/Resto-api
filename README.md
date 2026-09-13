# RestoFood — API

Turns a photograph of a paper menu card into a menu people can read on their
phones.

A restaurant owner uploads a photo of the card they already have. A vision
model reads it into structured data, the owner corrects anything wrong, a
photograph is found for every dish, and the result is published at the
restaurant's own web address with a QR code for the tables.

FastAPI, Postgres, deployed on Render. The web app is a separate repository:
**[Resto-ui](https://github.com/pavan29903/Resto-ui)**.

---

## What happens when a menu is published

```
menu photo
   │
   ├─ vision model reads it into sections, dishes, prices, veg marks
   │
   ├─ owner reviews and corrects            ← nothing is published unreviewed
   │
   ├─ a photograph is found for each dish   ← Pexels, then AI, then a plain card
   │
   └─ stored, and served at <slug>.restofood.in with a printable QR code
```

Two decisions in there are worth stating plainly.

**The review step is mandatory.** Extraction is good but not perfect, and a
restaurant will not tolerate a wrong price on a customer's screen. The owner
sees every dish before anything goes live.

**Dish photographs are licensed, not scraped.** Pexels content is free for
commercial use with no attribution required, which is what makes it safe on a
paying restaurant's menu. Images pulled from a web search are not, and the
liability would land on both us and the restaurant.

**Stock results are checked before they're kept.** Five candidates are fetched
and shown to a vision model, which picks the one that actually depicts the
dish — or rejects all five, in which case the chain moves on to generating an
image. A keyword search cannot tell that "Filter Coffee" on an Indian menu
means a steel tumbler and not a pour-over kit; looking at the photograph can.

The check has its own fallback chain, running the opposite way round to the
extraction one: it starts on the *lighter* model and escalates, because the
question is easy and a publish asks it once per dish. Candidate thumbnails are
downloaded once and reused across the whole chain, so escalating costs no extra
bandwidth. If every model fails the first result is kept — a broken checker
must never stop a restaurant getting its menu online.

It is an improvement rather than a cure. On a sample of six dishes it correctly
rejected an entire set of wrong photographs once, corrected a `Veg Manchurian`
result that showed the non-vegetarian version, and left already-correct choices
alone. The owner replacing a photo by hand remains the final word on accuracy.

---

## Running it locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra gemini
cp .env.example .env                 # PowerShell: Copy-Item .env.example .env
uv run uvicorn app.main:app --reload --port 8222
```

Interactive API documentation is then at <http://localhost:8222/docs>.

### What you need in `.env`

Only two keys, and both are free:

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

```bash
uv run alembic upgrade head          # create the tables
```

### Costs, in practice

Onboarding one restaurant costs about **two paise** — a fraction of a cent —
and nothing after that. Menus are read once; photographs are found once.

| | Provider | Per restaurant |
|---|---|---|
| Reading the menu | Gemini Flash | ~₹1 |
| Dish photographs | Pexels | free |
| Everything after | — | nothing |

Both can be swapped for stronger paid models (`claude-opus-4-8` for reading,
FLUX via fal.ai for images) by changing two lines in `.env`.

### When the vision model is busy

Free vision tiers return `503 — this model is currently experiencing high
demand` often enough that a single attempt makes the product look broken. This
is the one step an owner cannot route around, so it gets three defences:

1. **Retry** the same model up to three times with backoff and jitter — but
   only on transient faults. A bad key or an unreadable file fails identically
   every time, so retrying it just makes the owner wait longer for the same
   answer.
2. **Fall through** to the next provider in `EXTRACTION_FALLBACKS`. The first
   fallback is a *second Gemini model*, because a 503 is usually per-model
   capacity — the cheapest escape is a sibling model on the key you already
   have. OpenAI comes after that and stays dormant until `OPENAI_API_KEY` is
   set.
3. **Answer 503, not 500.** Nothing is broken and the next attempt will
   probably work; the owner is told exactly that, and the provider's raw error
   goes to the log rather than to their screen.

`GET /api/config` reports `extraction_chain` — the providers that would
actually be tried, with unkeyed ones already filtered out.

---

## How it's put together

```
app/
  main.py                 the app: CORS, extraction, health
  core/
    config.py             settings, and the rules for a menu's public URL
    db.py                 async engine and per-request session
    storage.py            Supabase Storage — dish photographs and logos
  models/                 owners · restaurants · sections · dishes
  modules/
    auth/                 verifies a Supabase session against its public keys
    extraction/           photo → structured menu
    images/               finding a photograph for a dish
    restaurants/          the owner's API, publishing, logos, per-dish photos
    qr/                   QR codes and the printable table card
  schemas/menu.py         the shape the vision model must return
alembic/                  migrations
```

Domain modules are kept separate on purpose. If one needs to scale on its own
later — image generation is the likely first — it can move out to its own
service without a rewrite.

### The endpoints

| | |
|---|---|
| `POST /api/extract` | photo → structured menu *(signed in)* |
| `GET&nbsp;/api/me/restaurants` | the owner's menus |
| `POST /api/restaurants` | create one from a reviewed menu |
| `PATCH /api/restaurants/{id}` | name, address, WhatsApp, dishes |
| `POST /api/restaurants/{id}/publish` | find photographs, go live |
| `POST /api/restaurants/{id}/logo` | the restaurant's logo |
| `POST /api/restaurants/{id}/items/{id}/photo` | replace one dish's photograph |
| `GET&nbsp;/api/restaurants/{id}/qr.png` | QR code, or `?tent=true` for the table card |
| `GET&nbsp;/api/public/menus/{slug}` | what a diner sees — no auth, by design |

Owner routes are scoped to the signed-in owner, so one restaurant can never
read or change another's menu.

---

## A few things that will bite you

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

---

## Deploying

See **[DEPLOY.md](DEPLOY.md)** — Render (free) or Fly.io (Mumbai, no cold
start), with the frontend on Vercel. Neither needs Docker installed locally.

---

## Not built yet

Ordering. This serves menus; it does not take orders. There is no orders table
and no live connection to a kitchen screen, so a diner reads the menu and then
speaks to a server. That is the next substantial piece of work.
