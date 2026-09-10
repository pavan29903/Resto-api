"""Check that a deployed RestoFood is actually wired up correctly.

Most deployment faults here are not crashes — the API is up, the pages render,
and the thing is still broken: a QR code that points at localhost, a frontend
origin missing from the CORS list, an auth route that silently accepts an
unsigned token. Each of those looks fine until a restaurant is standing in
front of you.

Everything is checked against the *deployed* services, over the network, the
way a browser would. Nothing is read from a local .env, because the whole
question is what the running server believes.

Run it:

    python scripts/verify_deploy.py \
        --api https://resto-api-xxxx.onrender.com \
        --web https://your-app.vercel.app

Sign-in is optional but covers the two most expensive faults (QR target and
owner auth). The password is read from a prompt, never a flag, so it stays out
of your shell history:

    python scripts/verify_deploy.py --api ... --web ... \
        --supabase-url https://<ref>.supabase.co \
        --supabase-key sb_publishable_... \
        --email you@example.com

Exit code is 0 only if nothing failed, so this can gate a deploy.

Standard library only — no install step, and no dependency on the API's own
environment being importable.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# Render's free tier sleeps. The first call can take a full minute to wake it,
# and a timeout here would look like a failure rather than a cold start.
TIMEOUT = 120

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    tint = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", INFO: "\033[36m"}
    print(f"  {tint.get(level, '')}[{level}]\033[0m {name}" + (f" — {detail}" if detail else ""))


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Returns (status, headers, body) and treats an HTTP error as a result.

    A 401 is a pass for some of these checks, so an exception would be the
    wrong shape.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def check_health(api: str) -> None:
    print("\nAPI reachable")
    try:
        status, _, _ = request(f"{api}/health")
    except Exception as exc:  # noqa: BLE001 — any failure here is the answer
        record(FAIL, "GET /health", f"unreachable: {exc}")
        return
    record(PASS if status == 200 else FAIL, "GET /health", f"HTTP {status}")


def check_config(api: str) -> dict[str, Any]:
    print("\nProvider configuration")
    status, _, raw = request(f"{api}/api/config")
    if status != 200:
        record(FAIL, "GET /api/config", f"HTTP {status}")
        return {}

    cfg = json.loads(raw)
    record(
        PASS if cfg.get("has_extraction_key") else FAIL,
        "Menu-reading key present",
        f"{cfg.get('extraction_provider')} / {cfg.get('extraction_model')}",
    )
    record(
        PASS if cfg.get("has_image_key") else WARN,
        "Dish-photo key present",
        f"{cfg.get('image_provider')} → {cfg.get('image_fallback')}",
    )
    domain = cfg.get("menu_domain")
    record(
        INFO,
        "Menu addressing",
        f"subdomains on {domain}" if domain else "path URLs (/r/<slug>) — no domain attached",
    )
    return cfg


def check_cors(api: str, web: str) -> None:
    """The frontend's origin must be allowed, and a stranger's must not.

    Only the second half of this is a security property, but the first half is
    the one that silently breaks the owner console in production.
    """
    print("\nCORS")
    status, headers, _ = request(f"{api}/api/config", headers={"Origin": web})
    allowed = headers.get("access-control-allow-origin")
    record(
        PASS if allowed else FAIL,
        f"Frontend origin allowed ({web})",
        allowed or "no Access-Control-Allow-Origin — set CORS_ORIGINS on the API",
    )

    rogue = "https://not-your-site.example.com"
    _, headers, _ = request(f"{api}/api/config", headers={"Origin": rogue})
    leaked = headers.get("access-control-allow-origin")
    record(
        FAIL if leaked else PASS,
        "Unknown origin rejected",
        f"allowed {leaked!r} — CORS is too open" if leaked else "no ACAO, as expected",
    )


def check_auth_enforced(api: str) -> None:
    print("\nAuth enforcement")
    status, _, _ = request(f"{api}/api/me/restaurants")
    record(
        PASS if status == 401 else FAIL,
        "Owner routes reject anonymous callers",
        f"HTTP {status}" + ("" if status == 401 else " — expected 401"),
    )

    # A token signed with "alg": "none" is the classic JWT forgery. It must be
    # refused as unverifiable, not accepted and not crash the server.
    forged = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhdHRhY2tlciJ9."
    status, _, _ = request(
        f"{api}/api/me/restaurants", headers={"Authorization": f"Bearer {forged}"}
    )
    record(
        PASS if status == 401 else FAIL,
        "Unsigned token refused",
        f"HTTP {status}" + ("" if status == 401 else " — expected 401"),
    )


def sign_in(supabase_url: str, supabase_key: str, email: str, password: str) -> str | None:
    print("\nSign-in")
    status, _, raw = request(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        method="POST",
        headers={"apikey": supabase_key},
        body={"email": email, "password": password},
    )
    if status != 200:
        detail = raw.decode(errors="replace")[:160]
        record(FAIL, "Supabase password grant", f"HTTP {status} — {detail}")
        return None

    token = json.loads(raw).get("access_token")
    record(PASS if token else FAIL, "Supabase password grant", "access token issued")
    return token


def check_owner_flow(api: str, web: str, token: str) -> None:
    print("\nOwner data and QR targets")
    status, _, raw = request(
        f"{api}/api/me/restaurants", headers={"Authorization": f"Bearer {token}"}
    )
    if status != 200:
        record(FAIL, "GET /api/me/restaurants", f"HTTP {status}")
        return

    restaurants = json.loads(raw)
    record(PASS, "GET /api/me/restaurants", f"{len(restaurants)} restaurant(s)")
    if not restaurants:
        record(WARN, "QR target", "no restaurants yet — publish one and re-run")
        return

    for r in restaurants:
        slug, menu_url = r.get("slug"), r.get("menu_url", "")

        # This is the check worth running the script for. PUBLIC_BASE_URL
        # defaults to localhost, and nothing about a wrong value shows up in
        # the UI — you only find out when a printed QR code goes nowhere.
        if "localhost" in menu_url or "127.0.0.1" in menu_url:
            record(FAIL, f"QR target for {slug}", f"{menu_url} — set PUBLIC_BASE_URL on the API")
        elif menu_url.startswith(web):
            record(PASS, f"QR target for {slug}", menu_url)
        else:
            record(WARN, f"QR target for {slug}", f"{menu_url} — not under {web}")

        status, headers, _ = request(
            f"{api}/api/restaurants/{r['id']}/qr.png",
            headers={"Authorization": f"Bearer {token}"},
        )
        ctype = headers.get("content-type", "")
        record(
            PASS if status == 200 and "image" in ctype else FAIL,
            f"QR image for {slug}",
            f"HTTP {status} {ctype}",
        )


def check_public_menu(api: str, web: str, slug: str) -> None:
    print("\nWhat a diner sees")
    status, _, raw = request(f"{api}/api/public/menus/{slug}")
    record(
        PASS if status == 200 else FAIL,
        f"API serves /{slug} without auth",
        f"HTTP {status}",
    )
    if status != 200:
        return

    menu = json.loads(raw)
    name = menu.get("name", "")

    # A private bucket fails quietly: the menu still renders, every dish just
    # shows a broken image. Fetch one for real rather than trusting the URL.
    photo = next(
        (u for u in (menu.get("images") or {}).values() if isinstance(u, str) and u.startswith("http")),
        None,
    )
    if not photo:
        record(WARN, "Dish photos publicly readable", "no photo URLs on this menu to test")
        return

    try:
        status_img, headers_img, _ = request(photo)
    except Exception as exc:  # noqa: BLE001 — a transport failure is a result, not a crash
        record(WARN, "Dish photos publicly readable", f"could not reach {photo[:70]}… — {exc}")
        return

    ctype = headers_img.get("content-type", "")
    record(
        PASS if status_img == 200 and "image" in ctype else FAIL,
        "Dish photos publicly readable",
        f"HTTP {status_img} {ctype}"
        + ("" if status_img == 200 else " — is the Storage bucket public?"),
    )

    # The menu page renders server-side, so this exercises the Next server's
    # own call to the API — a different path from the browser's, and the one
    # CORS never applies to.
    status, _, page = request(f"{web}/r/{slug}")
    body = page.decode(errors="replace")
    record(
        PASS if status == 200 and name and name in body else FAIL,
        f"Web page /r/{slug} renders the menu",
        f"HTTP {status}" + ("" if name in body else f" — {name!r} missing from the page"),
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", required=True, help="Deployed API base URL")
    p.add_argument("--web", required=True, help="Deployed frontend base URL")
    p.add_argument("--slug", help="A published restaurant slug to check publicly")
    p.add_argument("--supabase-url", help="https://<ref>.supabase.co — enables signed-in checks")
    p.add_argument("--supabase-key", help="Publishable/anon key")
    p.add_argument("--email", help="An owner account to sign in as")
    args = p.parse_args()

    api = args.api.rstrip("/")
    web = args.web.rstrip("/")

    print(f"\nChecking {api}\n     against {web}")

    check_health(api)
    check_config(api)
    check_cors(api, web)
    check_auth_enforced(api)

    if args.slug:
        check_public_menu(api, web, args.slug)

    if args.supabase_url and args.supabase_key and args.email:
        password = getpass.getpass(f"Password for {args.email}: ")
        token = sign_in(args.supabase_url.rstrip("/"), args.supabase_key, args.email, password)
        if token:
            check_owner_flow(api, web, token)
    else:
        record(WARN, "Signed-in checks skipped", "pass --supabase-url, --supabase-key and --email")

    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    print(
        f"\n{len(results)} checks — "
        f"{len(results) - len(failed) - len(warned) - sum(1 for r in results if r[0] == INFO)} passed, "
        f"{len(failed)} failed, {len(warned)} warned\n"
    )
    for _, name, detail in failed:
        print(f"  FIX  {name}: {detail}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
