# resto-api — RestoFood backend

FastAPI backend for **RestoFood**: small cafes upload photos of their paper menu
and get a hosted online menu with AI dish images, an ordering flow, and a printable
QR — fully automatic. See the plan in `~/.claude/plans/` for the full picture.

This repo currently contains **Phase 0**: the extraction + image + site + QR
**pipeline**, written as reusable `app/modules/*` code (the seed of the Phase 1
FastAPI backend) plus a CLI runner. No web server or database yet.

## What Phase 0 does

```
menu photo(s) ──▶ structured menu (vision LLM) ──▶ menu.json  (owner reviews/edits)
              └─▶ one AI image per dish ──────────┐
                                                  ▼
                          nice static menu page (index.html) + QR + printable table tent
```

## Setup

Prereqs: Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`).

```bash
cd resto-api
uv sync --extra gemini        # default setup: free Gemini extraction + free images
cp .env.example .env          # paste your GOOGLE_API_KEY (free, 30 seconds)
# (PowerShell: Copy-Item .env.example .env)

# optional paid providers, only if you escalate later:
uv sync --extra fal           # AI dish images via fal.ai (FLUX)
uv sync --extra replicate     # AI dish images via Replicate
# (Claude extraction needs no extra — the anthropic SDK is a base dependency)
```

### Not using uv?

Dependencies live in `pyproject.toml`, with exact versions locked in `uv.lock`.
A pinned `requirements.txt` is also committed for plain-pip and deploy targets
that expect one:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

Regenerate it after changing dependencies:

```bash
uv export --format requirements-txt --no-hashes --no-dev --extra gemini -o requirements.txt
```

### Models — free by default, escalate only if accuracy demands it

| Step | Default | Cost | Escalate to |
|------|---------|------|-------------|
| **Extraction** | `gemini-2.5-flash` | **Free** — 1,500 req/day, no card | `claude-opus-4-8` (~$0.13/menu) |
| **Dish images** | `pollinations` (FLUX) | **Free** — no key, ~15s/image | `fal-ai/flux/schnell` (~$0.003/image) |

Get a free Gemini key at <https://aistudio.google.com/apikey>. That is the only
key needed to run the whole pipeline. Note: on Gemini's free tier Google may use
your data to improve their products — fine for test menus, revisit before real
customer data.

`IMAGE_PROVIDER=placeholder` skips AI images entirely (instant, offline) if you
just want to test the flow.

## Run

### Option A — the web UI (recommended)

```bash
uv run uvicorn app.main:app --reload --port 8100
```

Open <http://localhost:8100> and work through the three steps:

1. **Upload** your menu-card photos (drag & drop, multiple pages OK)
2. **Review & fix** — the extracted menu appears in an editable table. AI
   extraction is never perfect; correct names/prices here before publishing.
3. **Publish** — generates dish images, the menu site, and the QR code, with a
   live progress bar. The finished menu previews inline next to its QR.

> While testing, keep the **"limit to N dishes"** box small (default 6). The free
> image API takes ~15s per dish, so a 40-dish menu would take ~10 minutes.

API docs (auto-generated) are at <http://localhost:8100/docs>.

### Option B — the CLI

```bash
# 1. Drop menu-card photos in samples/
# 2. Run the pipeline (free models by default)
uv run python scripts/phase0.py --images samples/menu.jpg --name "Blue Tokai"
#    or point it at a whole folder:
uv run python scripts/phase0.py --images samples/ --name "Blue Tokai"
```

### Escalating when the free model gets it wrong

Override per-run without editing `.env`, and write to a separate folder so you
can diff the two `menu.json` files side by side:

```bash
# Free baseline
uv run python scripts/phase0.py --images samples/menu.jpg --name X --out out/gemini

# Same photo on Claude, to see what accuracy you're missing
uv run python scripts/phase0.py --images samples/menu.jpg --name X --out out/claude \
    --provider claude --model claude-opus-4-8 --skip-images
```

Flags: `--provider {gemini,claude}` · `--model <id>` ·
`--image-provider {pollinations,placeholder,fal,replicate}`.
Add `--skip-images` when you only care about extraction accuracy — it makes the
run seconds instead of minutes.

Output lands in `samples/output/<slug>/`:

- `menu.json` — the extracted, structured menu (the owner's review/edit artifact)
- `index.html` — the menu website (open it in a browser)
- `images/` — one image per dish
- `qr.png`, `tent.png` — the QR code and a printable table tent

### Test the QR on a real phone

```bash
cd samples/output/blue-tokai
python -m http.server 8000
# On your phone (same Wi-Fi) open http://<your-computer-LAN-ip>:8000
```

To bake that LAN URL into the QR, re-run with `--menu-url http://<lan-ip>:8000 --skip-images`.

Set `OWNER_WHATSAPP` in `.env` to add an "Order on WhatsApp" button to the page —
a cheap way to measure ordering intent in Phase 0 before the real ordering backend.

## Layout

```
app/
  core/config.py            # settings (extends into the Phase 1 backend config)
  schemas/menu.py           # Menu / MenuSection / MenuItem (extraction target)
  modules/
    extraction/service.py   # photo -> structured menu (Claude default, Gemini optional)
    images/service.py       # dish images (placeholder default, fal / replicate)
    site/service.py + templates/menu.html.j2
    qr/service.py           # QR + printable table tent
scripts/phase0.py           # the CLI runner (this is throwaway; the modules are not)
```

Phase 1 adds `app/main.py` (FastAPI), `app/modules/*/router.py`, async SQLAlchemy
models, Alembic, Redis/arq jobs, and WebSocket order delivery — reusing the modules
above.

## Verify

```bash
uv run python scripts/smoke_test.py   # offline: images + site + QR on a sample menu (no API key)
```
