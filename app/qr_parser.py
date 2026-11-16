"""Decode QR codes from invoice images and parse AADE myDATA / provider URLs."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlparse

from PIL import Image, ImageOps

from .models import QRData, _to_float

log = logging.getLogger(__name__)

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as _zbar_decode
    ZBAR_OK = True
except Exception as exc:  # DLL missing on Windows (needs MSVC++ runtime)
    ZBAR_OK = False
    log.warning("pyzbar unavailable, server-side QR decoding disabled: %s", exc)

# Lower-case parameter aliases used by AADE and e-invoicing providers
ALIASES = {
    "mark": ("mark", "m", "invoicemark", "aademark", "minmark"),
    "afm": ("afm", "issuer", "issuervat", "issuer_vat", "vat_number", "vatnumber", "vatissuer", "afmissuer", "a"),
    "date": ("date", "issuedate", "issue_date", "invoicedate", "d", "dt"),
    "net": ("net", "netvalue", "net_value", "totalnet", "totalnetvalue", "n"),
    "vat": ("vat", "vatamount", "vat_amount", "totalvat", "totalvatamount", "v"),
    "total": ("total", "gross", "grossvalue", "totalvalue", "total_value", "totalgross", "totalgrossvalue", "amount", "t"),
}

MARK_RE = re.compile(r"(?<!\d)(\d{15})(?!\d)")


def _norm_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = unquote(s).strip()
    candidates = (s, s[:10], s[:8])
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d", "%d%m%Y"):
        for c in candidates:
            try:
                return datetime.strptime(c, fmt).date().isoformat()
            except ValueError:
                continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse_aade_url(text: str) -> QRData:
    """Parse a QR payload (URL or free text). Never raises; unknown formats keep raw_url only."""
    raw = (text or "").strip()
    data = QRData(raw_url=raw)
    if not raw:
        return data

    params: dict[str, str] = {}
    try:
        parsed = urlparse(raw)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False):
            params.setdefault(k.lower().replace("-", "_"), v)
        for k, v in parse_qsl(parsed.fragment, keep_blank_values=False):
            params.setdefault(k.lower().replace("-", "_"), v)
    except ValueError:
        pass

    def pick(field: str) -> Optional[str]:
        for alias in ALIASES[field]:
            if alias in params and params[alias].strip():
                return params[alias].strip()
        return None

    mark = pick("mark")
    if mark:
        mark = re.sub(r"\D", "", mark) or None
    if not mark:  # many providers embed MARK in the path or plain text
        m = MARK_RE.search(raw)
        mark = m.group(1) if m else None
    data.mark = mark

    afm = pick("afm")
    if afm:
        digits = re.sub(r"\D", "", afm)
        data.afm = digits[-9:] if len(digits) >= 9 else None

    data.invoice_date = _norm_date(pick("date"))
    data.total_net = _to_float(pick("net"))
    data.total_vat = _to_float(pick("vat"))
    data.total_gross = _to_float(pick("total"))
    return data


def decode_qr_from_image(path: Path) -> list[str]:
    """Return all QR payloads found in an image file."""
    if not ZBAR_OK:
        return []
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        log.warning("Cannot open image for QR decode: %s", exc)
        return []
    return decode_qr_from_pil(img)


def decode_qr_from_pil(img: Image.Image) -> list[str]:
    """Return all QR payloads found in a PIL image. Tries several preprocessing variants."""
    if not ZBAR_OK:
        return []
    gray = img.convert("L")
    variants = [img.convert("RGB"), gray, ImageOps.autocontrast(gray)]
    if max(gray.size) < 1600:
        variants.append(gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS))
    else:
        scale = 1600 / max(gray.size)
        variants.append(gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS))
    variants.append(gray.point(lambda p: 255 if p > 140 else 0))

    for v in variants:
        try:
            found = _zbar_decode(v, symbols=[ZBarSymbol.QRCODE])
        except Exception:
            continue
        payloads = [f.data.decode("utf-8", errors="replace") for f in found if f.data]
        if payloads:
            return payloads
    return []


def best_qr(payloads: list[str]) -> Optional[QRData]:
    """Pick the payload that looks most like an AADE invoice QR."""
    best, best_score = None, -1
    for p in payloads:
        q = parse_aade_url(p)
        score = (3 if q.mark else 0) + (1 if q.afm else 0) + (1 if "aade" in p.lower() or "mydata" in p.lower() else 0)
        if score > best_score:
            best, best_score = q, score
    return best
