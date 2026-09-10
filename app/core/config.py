"""Central configuration for the Phase 0 pipeline.

Reads from environment variables / a local `.env` file via pydantic-settings.
In Phase 1 this same Settings object grows to hold DB/Redis/storage config for
the FastAPI backend — the extraction/images modules already read from it.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Map ISO currency codes the model may detect to a display symbol.
CURRENCY_SYMBOLS = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "AED": "د.إ",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Extraction. Default is Gemini Flash — free tier, no credit card.
    # Escalate to Claude only if accuracy on real menus isn't good enough.
    extraction_provider: str = "gemini"  # "gemini" | "claude"
    google_api_key: str | None = None
    # "gemini-flash-latest" auto-tracks the current Flash model, so it won't go
    # stale the way a pinned version does (gemini-2.5-flash is already retired
    # for new accounts). Pin a specific version only if you need reproducibility.
    gemini_model: str = "gemini-flash-latest"
    anthropic_api_key: str | None = None
    extraction_model: str = "claude-opus-4-8"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # Tried in order when the primary provider is busy or failing, as
    # "provider[:model]" pairs. A provider with no API key is skipped, so this
    # default is safe to ship as-is: the second Gemini model costs nothing and
    # needs no new key, and OpenAI simply does not engage until you set
    # OPENAI_API_KEY. Set to "" to disable fallbacks entirely.
    #
    # The first fallback is a *different Gemini model* on purpose: an overload
    # is usually per-model capacity, so the cheapest escape is a sibling model
    # on the key you already have.
    extraction_fallbacks: str = "gemini:gemini-flash-lite-latest,openai:gpt-4o-mini"

    def _default_model(self, provider: str) -> str:
        return {
            "gemini": self.gemini_model,
            "claude": self.extraction_model,
            "openai": self.openai_model,
        }.get(provider, "")

    def has_extraction_key(self, provider: str) -> bool:
        return bool(
            {
                "gemini": self.google_api_key,
                "claude": self.anthropic_api_key,
                "openai": self.openai_api_key,
            }.get(provider)
        )

    @property
    def extraction_chain(self) -> list[tuple[str, str]]:
        """(provider, model) pairs to try in order, primary first.

        Providers without a key are filtered out here rather than failing at
        call time, so a missing OPENAI_API_KEY is a shorter chain and not an
        error in the middle of reading someone's menu.
        """
        primary = self.extraction_provider.strip().lower()
        chain: list[tuple[str, str]] = [(primary, self._default_model(primary))]

        for spec in self.extraction_fallbacks.split(","):
            spec = spec.strip()
            if not spec:
                continue
            provider, _, model = spec.partition(":")
            provider = provider.strip().lower()
            model = model.strip() or self._default_model(provider)
            if provider and model and (provider, model) not in chain:
                chain.append((provider, model))

        return [(p, m) for p, m in chain if self.has_extraction_key(p)]

    # Dish images. Default is "pexels": real, properly-licensed food photographs
    # searched by dish name. A real photo of the actual dish beats an AI guess,
    # so we try stock first and only generate when there's no match.
    image_provider: str = "pexels"
    # What to try when Pexels has no usable match: "pollinations" (AI) or
    # "placeholder" (offline card). Set to "placeholder" to avoid slow AI calls.
    image_fallback: str = "pollinations"
    pexels_api_key: str | None = None

    pollinations_model: str = "flux"
    fal_api_key: str | None = None
    replicate_api_token: str | None = None
    image_model: str = "fal-ai/flux/schnell"

    # Database (Supabase Postgres)
    database_url: str | None = None

    # Supabase — project URL + keys. The anon key is safe in the browser; the
    # service key and JWT secret never leave the backend.
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_key: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_storage_bucket: str = "dish-images"

    # Comma-separated origins allowed to call this API from a browser, e.g.
    # "https://restofood.in,https://restofood.vercel.app". Local dev origins are
    # always permitted; anything else must be listed here.
    cors_origins: str = ""

    @property
    def allowed_origins(self) -> list[str]:
        # 3000 is Next's default; 5005 is ours, for when 3000 is taken by
        # something else. Both loopback spellings, because a browser treats
        # localhost and 127.0.0.1 as different origins.
        local = [
            f"http://{host}:{port}"
            for port in (3000, 5005)
            for host in ("localhost", "127.0.0.1")
        ]
        extra = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        return local + extra

    @property
    def cors_origin_regex(self) -> str | None:
        """Every restaurant answers on its own subdomain, so the browser origin
        differs per restaurant and can't be enumerated. Allow exactly
        <anything>.<menu_domain> and nothing else."""
        if not self.menu_domain:
            return None
        escaped = self.menu_domain.replace(".", r"\.")
        return rf"https://([a-z0-9-]+\.)?{escaped}"

    # Presentation
    currency_symbol: str = "₹"
    public_base_url: str = "http://localhost:8000"
    # Root domain for published menus. Each restaurant answers on
    # <slug>.<menu_domain>; empty falls back to path-based URLs.
    menu_domain: str | None = None
    owner_whatsapp: str | None = None

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy needs the asyncpg driver; Supabase hands out a
        postgresql:// URI. Normalise it rather than making people edit it."""
        url = self.database_url or ""
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    def menu_url_for(self, slug: str) -> str:
        """The public URL a QR code should point at."""
        if self.menu_domain:
            return f"https://{slug}.{self.menu_domain}"
        return f"{self.public_base_url.rstrip('/')}/r/{slug}"

    def symbol_for(self, currency_code: str | None) -> str:
        """Display symbol for a detected currency code, with a safe fallback."""
        if not currency_code:
            return self.currency_symbol
        return CURRENCY_SYMBOLS.get(currency_code.upper(), self.currency_symbol)


@lru_cache
def get_settings() -> Settings:
    return Settings()
