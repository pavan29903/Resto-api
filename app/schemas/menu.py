"""Structured menu schema.

Used both as the extraction target (the vision model returns exactly this shape
via structured outputs) and, in Phase 1, as the basis for the SQLAlchemy models
and the owner review/edit screen.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MenuItem(BaseModel):
    name: str = Field(description="Exact dish name as printed on the menu.")
    description: str = Field(
        default="",
        description="Short description if printed on the menu, else empty string.",
    )
    price: float | None = Field(
        default=None,
        description="Numeric price only, no currency symbol. Null if no price is printed.",
    )
    is_vegetarian: bool | None = Field(
        default=None,
        description="True/False only if the menu indicates it (veg/non-veg); else null.",
    )
    spice_level: str | None = Field(
        default=None,
        description='One of "mild", "medium", "spicy" if indicated; else null.',
    )


class MenuSection(BaseModel):
    name: str = Field(description='Section/category heading, e.g. "Starters".')
    items: list[MenuItem]


class Menu(BaseModel):
    restaurant_name: str | None = Field(
        default=None, description="Restaurant name if visible on the menu, else null."
    )
    currency: str = Field(
        default="INR", description='Detected currency code, e.g. "INR", "USD".'
    )
    sections: list[MenuSection]
