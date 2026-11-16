"""Product matching (exact code, then RapidFuzz) and price diff calculation."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata

from rapidfuzz import fuzz, process

from . import database as db
from .models import ComparisonItem, PriceStatus

FUZZY_CUTOFF = 85
STATUS_ORDER = {PriceStatus.INCREASED: 0, PriceStatus.DECREASED: 1, PriceStatus.NEW_ITEM: 2, PriceStatus.UNCHANGED: 3}


def normalize_name(text: str) -> str:
    """Lowercase, strip Greek/Latin accents, collapse whitespace & punctuation."""
    if not text:
        return ""
    s = unicodedata.normalize("NFD", text)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.lower().replace("ς", "σ")
    s = re.sub(r"[^\w\s.,/%-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _thresholds() -> tuple[float, float]:
    def f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except ValueError:
            return default
    return f("PRICE_THRESHOLD_AMOUNT", 0.02), f("PRICE_THRESHOLD_PERCENT", 0.5)


def classify(new_price: float, old_price: float) -> tuple[PriceStatus, float, float]:
    amount_thr, pct_thr = _thresholds()
    diff = round(new_price - old_price, 4)
    pct = round(diff / old_price * 100, 2) if old_price else (100.0 if diff else 0.0)
    if abs(diff) >= amount_thr and abs(pct) >= pct_thr:
        return (PriceStatus.INCREASED if diff > 0 else PriceStatus.DECREASED), diff, pct
    return PriceStatus.UNCHANGED, diff, pct


def _find_match(item: dict, history: list[dict], by_code: dict, latest_by_name: dict, names: list[str]):
    code = (item.get("product_code") or "").strip()
    if code and code in by_code:
        return by_code[code], "code", 100.0
    if names:
        res = process.extractOne(item["normalized_name"], names, scorer=fuzz.WRatio, score_cutoff=FUZZY_CUTOFF)
        if res:
            name, score, _ = res
            return latest_by_name[name], "fuzzy", round(score, 1)
    return None, None, None


def compare_items(conn: sqlite3.Connection, invoice: dict, items: list[dict]) -> list[ComparisonItem]:
    """Compare `items` (rows of invoice_items) against the supplier's earlier invoices."""
    history = db.get_supplier_history_items(conn, invoice["supplier_id"], invoice)

    # history is newest-first, so the first occurrence wins
    by_code: dict[str, dict] = {}
    latest_by_name: dict[str, dict] = {}
    for h in history:
        c = (h.get("product_code") or "").strip()
        if c and c not in by_code:
            by_code[c] = h
        if h["normalized_name"] not in latest_by_name:
            latest_by_name[h["normalized_name"]] = h
    names = list(latest_by_name.keys())

    results: list[ComparisonItem] = []
    for it in items:
        match, mtype, score = _find_match(it, history, by_code, latest_by_name, names)
        base = dict(
            item_id=it.get("id"),
            product_code=it.get("product_code"),
            description=it["raw_description"],
            quantity=it["quantity"],
            unit_price_net=it["unit_price_net"],
            discount_percent=it.get("discount_percent") or 0.0,
            line_total_net=it["line_total_net"],
        )
        if not match:
            results.append(ComparisonItem(**base, status=PriceStatus.NEW_ITEM))
            continue
        status, diff, pct = classify(it["unit_price_net"], match["unit_price_net"])
        results.append(ComparisonItem(
            **base, status=status, old_price=match["unit_price_net"], diff_amount=diff, diff_percent=pct,
            previous_date=match["invoice_date"], matched_description=match["raw_description"],
            match_type=mtype, match_score=score,
        ))

    results.sort(key=lambda r: (STATUS_ORDER[r.status], -(abs(r.diff_percent or 0))))
    return results


def summarize(items: list[ComparisonItem]) -> dict[str, int]:
    summary = {s.value: 0 for s in PriceStatus}
    for it in items:
        summary[it.status.value] += 1
    return summary
