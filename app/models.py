"""Pydantic schemas for LLM extraction and API I/O."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, BeforeValidator, Field, field_validator


def _to_float(v):
    """Accept Greek-formatted numbers like '1.234,56' or '12,50'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("€", "").replace("%", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


Num = Annotated[Optional[float], BeforeValidator(_to_float)]


# ---------- LLM extraction ----------
class ExtractedItem(BaseModel):
    product_code: Optional[str] = None
    description: str
    quantity: Num = 1.0
    unit_price_before_discount: Num = None
    discount_percent: Num = 0.0
    final_unit_price_net: Num = None
    line_total_net: Num = None

    @field_validator("product_code", mode="before")
    @classmethod
    def _code(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


class ExtractedInvoice(BaseModel):
    supplier_name: Optional[str] = None
    supplier_afm: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    mark: Optional[str] = None
    total_net: Num = None
    total_vat: Num = None
    total_gross: Num = None
    items: list[ExtractedItem] = Field(default_factory=list)

    @field_validator("supplier_afm", "mark", "invoice_number", mode="before")
    @classmethod
    def _str(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


# ---------- QR ----------
class QRData(BaseModel):
    raw_url: str
    mark: Optional[str] = None
    afm: Optional[str] = None
    invoice_date: Optional[str] = None
    total_net: Optional[float] = None
    total_vat: Optional[float] = None
    total_gross: Optional[float] = None


class QRCheckRequest(BaseModel):
    qr_text: str


# ---------- Comparison / reports ----------
class PriceStatus(str, Enum):
    INCREASED = "INCREASED"
    DECREASED = "DECREASED"
    UNCHANGED = "UNCHANGED"
    NEW_ITEM = "NEW_ITEM"


class ComparisonItem(BaseModel):
    item_id: Optional[int] = None
    product_code: Optional[str] = None
    description: str
    quantity: float
    unit_price_net: float
    discount_percent: float = 0.0
    line_total_net: float
    status: PriceStatus
    old_price: Optional[float] = None
    diff_amount: Optional[float] = None
    diff_percent: Optional[float] = None
    previous_date: Optional[str] = None
    matched_description: Optional[str] = None
    match_type: Optional[str] = None  # "code" | "fuzzy"
    match_score: Optional[float] = None


class InvoiceHeader(BaseModel):
    id: int
    mark: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: str
    supplier_id: int
    supplier_name: str
    supplier_afm: str
    total_net: Optional[float] = None
    total_vat: Optional[float] = None
    total_gross: Optional[float] = None


class InvoiceReport(BaseModel):
    invoice: InvoiceHeader
    items: list[ComparisonItem]
    summary: dict[str, int]
    duplicate: bool = False
    warnings: list[str] = Field(default_factory=list)


class ItemUpdate(BaseModel):
    quantity: Optional[float] = Field(default=None, gt=0)
    unit_price_net: Optional[float] = Field(default=None, ge=0)
