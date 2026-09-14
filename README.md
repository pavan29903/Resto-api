# RestoFood

**Photograph your menu card. Get a menu your customers open by scanning a code
on the table — with a photograph of every dish.**

Live at **[restofood.in](https://restofood.in)** · Web app:
**[Resto-ui](https://github.com/pavan29903/Resto-ui)**

---

## The problem

India has millions of small restaurants and cafes still handing out laminated
paper menu cards. Every time a price moves, the owner reprints the batch —
₹3,000–5,000, a few times a year. Meanwhile the cafe two doors down has a QR
code on every table, photographs of every dish, and looks like a different
class of business.

The tools that close that gap assume a marketing budget, a photographer, and
somebody who can operate a CMS. A twelve-table cafe has none of those.

**RestoFood needs one thing: a photograph of the menu card they already own.**

## What it does

```
   a photo of the menu card
            │
            ├─  a vision model reads it into dishes, prices, sections, veg marks
            │
            ├─  the owner checks it — nothing goes live unreviewed
            │
            ├─  a real photograph is found for every dish, and AI-checked
            │   against what the dish actually is
            │
            └─  published at  spicegarden.restofood.in  with a printable
                QR code for the tables
```

About five minutes, start to finish. The owner needs no photographer, no
designer, and no idea what a domain is.

## Who it's for

Independent restaurants, cafes and cloud kitchens with one to five outlets —
the ones for whom Zomato is a commission line and a website is a project they
never started. The buyer is the owner, usually on a mid-range Android phone,
usually between services.

Everything is built for that person: the console explains itself, the diner's
menu is readable one-handed in a dim room, and the whole product speaks in
plain sentences rather than software vocabulary.

---

## What's interesting underneath

This is a small product that takes its hard parts seriously.

**Structured extraction, not OCR.** A vision model returns a validated Pydantic
schema — sections, dishes, prices, veg/non-veg marks, currency detected from
the symbols — so the output is usable data rather than text somebody has to
clean up. Reading a whole 40-dish menu costs about **₹1**.

**A model chain that degrades instead of failing.** Free vision tiers return
`503 — experiencing high demand` in waves. Extraction retries transient faults
with jittered backoff, then falls through to a second model, then to OpenAI if
a key exists — while a permanent error like a bad file is *not* retried, since
it would fail identically every time. An overloaded provider returns **503, not
500**, because "try again in a moment" and "we are broken" are different
things to everyone downstream.

**AI that checks its own output.** Stock libraries are thin on regional Indian
food — searching "Filter Coffee" returns pour-over brewing kits, which is not
what a South Indian cafe serves. So five candidates are fetched and shown to a
vision model, which picks the one that actually depicts the dish **or rejects
all five**, falling through to image generation. On a six-dish sample it
correctly threw away an entire set of wrong photographs and caught a
*Veg* Manchurian result that showed the chicken version.

**Multi-tenant by subdomain.** Every restaurant answers on its own address —
`spicegarden.restofood.in` — served by one Next.js app through a middleware
rewrite, behind a wildcard TLS certificate. Onboarding a restaurant requires no
deploy and no DNS change: the API's CORS policy matches the whole wildcard, and
about thirty reserved subdomains can never be claimed.

**Money without a payment gateway.** Billing is two dates on a row and a status
derived on read — no status column to drift out of sync when a job fails. A
lapsed restaurant loses the *editor* first and its public menu only three weeks
later, because taking a live restaurant offline mid-service is a catastrophe
you caused, not a payment reminder.

**Economics that work at ₹250/month.** Onboarding one restaurant costs roughly
**two paise**. Menus are read once, photographs are found once, and everything
after that is static. That is what makes a price a twelve-table cafe will
actually pay possible at all.

**Verified, not assumed.** `scripts/verify_deploy.py` checks a live deployment
end to end — CORS in both directions, forged-JWT rejection, whether QR codes
encode a real address rather than `localhost`, whether dish photographs are
publicly readable. It exits non-zero, so it can gate a deploy.

---

## Built with

FastAPI · SQLAlchemy 2.0 (async) · Postgres · Alembic · Gemini vision ·
Pexels · Supabase (auth, storage) · Docker · Render

Two repositories, deliberately: the API and the web app deploy independently,
and the domain modules inside this one are separated so image generation can
move to its own service when it needs to, without a rewrite.

## Built, and not yet built

**Working today:** menu extraction and review, dish photographs with AI
vetting, per-dish replacement, logos, publishing, QR codes and printable table
cards, subdomain-per-restaurant, owner accounts, trials and renewals, a back
office for collecting payment.

**Not built:** ordering. A diner reads the menu and then speaks to a server.
There is no cart and no kitchen screen — that is the next substantial piece of
work, and it is not claimed as "coming soon" on a page where somebody might
believe it.

---

**Running it locally:** [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) ·
**Deploying it:** [DEPLOY.md](DEPLOY.md)
