"""FastAPI application: routes and static file serving."""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from .models import InvoiceHeader, InvoiceReport, ItemUpdate, PriceStatus, QRCheckRequest
from .ocr_extractor import ExtractionError, extract_invoice, load_pages
from .price_comparator import compare_items, normalize_name, summarize
from .qr_parser import ZBAR_OK, best_qr, decode_qr_from_image, decode_qr_from_pil, parse_aade_url

BASE_DIR = db.BASE_DIR
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff", ".pdf"}

load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("invoice_tracker")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_DIR.mkdir(exist_ok=True)
    db.init_db()
    if not ZBAR_OK:
        log.warning("Server-side QR decoding disabled (zbar DLL not loaded). Use live QR scan in the app.")
    yield


app = FastAPI(title="Invoice Price Tracker", lifespan=lifespan)


# ---------- helpers ----------
def build_report(conn: sqlite3.Connection, invoice_id: int, duplicate: bool = False,
                 warnings: Optional[list[str]] = None) -> InvoiceReport:
    header = db.get_invoice_header(conn, invoice_id)
    if not header:
        raise HTTPException(404, "Το τιμολόγιο δεν βρέθηκε.")
    items = db.get_invoice_items(conn, invoice_id)
    compared = compare_items(conn, header, items)
    return InvoiceReport(
        invoice=InvoiceHeader(**{k: header[k] for k in InvoiceHeader.model_fields}),
        items=compared,
        summary=summarize(compared),
        duplicate=duplicate,
        warnings=warnings or [],
    )


def _is_iso_date(s: Optional[str]) -> bool:
    try:
        date.fromisoformat(s or "")
        return True
    except ValueError:
        return False


# ---------- pages ----------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from root so its scope covers the whole app
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- API ----------
@app.get("/api/health")
def health():
    return {"ok": True, "zbar": ZBAR_OK, "ai_configured": bool(os.getenv("API_KEY"))}


@app.post("/api/qr-check")
def qr_check(req: QRCheckRequest):
    qr = parse_aade_url(req.qr_text)
    result: dict = {"duplicate": False, "qr": qr.model_dump(), "existing_report": None}
    if not qr.mark:
        result["message"] = "Δεν βρέθηκε ΜΑΡΚ στο QR. Το τιμολόγιο δεν μπορεί να ελεγχθεί για διπλοεγγραφή."
        return result
    with db.get_conn() as conn:
        existing = db.get_invoice_by_mark(conn, qr.mark)
        if existing:
            result["duplicate"] = True
            result["message"] = f"Το τιμολόγιο με ΜΑΡΚ {qr.mark} έχει ήδη καταχωρηθεί."
            result["existing_report"] = build_report(conn, existing["id"], duplicate=True).model_dump()
        else:
            result["message"] = "Το τιμολόγιο δεν έχει καταχωρηθεί ακόμα."
    return result


@app.post("/api/upload", response_model=InvoiceReport)
async def upload_invoice(file: UploadFile = File(...), qr_text: Optional[str] = Form(None)):
    ext = Path(file.filename or "").suffix.lower() or ".jpg"
    if ext not in ALLOWED_EXT and not (file.content_type or "").startswith("image/"):
        raise HTTPException(415, "Μη υποστηριζόμενος τύπος αρχείου. Ανεβάστε εικόνα ή PDF.")
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    data = await file.read()
    if not data:
        raise HTTPException(400, "Το αρχείο είναι κενό.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Το αρχείο είναι πολύ μεγάλο (μέγιστο 25MB).")

    UPLOAD_DIR.mkdir(exist_ok=True)
    saved = UPLOAD_DIR / f"{date.today():%Y%m%d}_{uuid.uuid4().hex[:12]}{ext}"
    saved.write_bytes(data)
    warnings: list[str] = []

    def discard() -> None:
        saved.unlink(missing_ok=True)

    # 1. QR: from live scan (if provided) or decoded from the file
    qr = parse_aade_url(qr_text) if qr_text else None
    if not (qr and qr.mark):
        try:
            if ext == ".pdf":
                pages = await run_in_threadpool(load_pages, saved)
                payloads = []
                for p in pages:
                    payloads = await run_in_threadpool(decode_qr_from_pil, p)
                    if payloads:
                        break
            else:
                payloads = await run_in_threadpool(decode_qr_from_image, saved)
            found = best_qr(payloads) if payloads else None
            if found and (found.mark or not qr):
                qr = found
        except ExtractionError as exc:
            discard()
            raise HTTPException(exc.status_code, exc.message)

    # 2. Early duplicate check (saves an AI call)
    if qr and qr.mark:
        with db.get_conn() as conn:
            existing = db.get_invoice_by_mark(conn, qr.mark)
            if existing:
                discard()
                return build_report(conn, existing["id"], duplicate=True,
                                    warnings=[f"Το τιμολόγιο με ΜΑΡΚ {qr.mark} έχει ήδη καταχωρηθεί."])
    elif not qr:
        warnings.append("Δεν εντοπίστηκε QR στην εικόνα.")

    # 3. AI extraction
    try:
        inv = await run_in_threadpool(extract_invoice, saved)
    except ExtractionError as exc:
        discard()
        raise HTTPException(exc.status_code, exc.message)

    # 4. Merge: QR is authoritative for AFM / MARK / totals
    afm = inv.supplier_afm
    if qr and qr.afm:
        if afm and afm != qr.afm:
            warnings.append(f"Ο ΑΦΜ του AI ({afm}) διαφέρει από το QR ({qr.afm}). Χρησιμοποιήθηκε του QR.")
        afm = qr.afm
    if not afm:
        discard()
        raise HTTPException(422, "Δεν βρέθηκε ο ΑΦΜ του προμηθευτή. Φωτογραφίστε ξανά ώστε να φαίνεται η κεφαλίδα.")

    mark = (qr.mark if qr and qr.mark else None) or inv.mark
    inv_date = inv.invoice_date if _is_iso_date(inv.invoice_date) else None
    if qr and qr.invoice_date:
        inv_date = qr.invoice_date
    if not inv_date:
        inv_date = date.today().isoformat()
        warnings.append("Δεν αναγνωρίστηκε ημερομηνία· ορίστηκε η σημερινή.")

    totals = {k: (getattr(qr, k) if qr and getattr(qr, k) is not None else getattr(inv, k))
              for k in ("total_net", "total_vat", "total_gross")}

    items_sum = round(sum(i.line_total_net for i in inv.items), 2)
    if totals["total_net"] and abs(items_sum - totals["total_net"]) > max(0.05, totals["total_net"] * 0.01):
        warnings.append(f"Το άθροισμα γραμμών ({items_sum:.2f}€) διαφέρει από την καθαρή αξία "
                        f"({totals['total_net']:.2f}€). Ελέγξτε για λάθη ανάγνωσης.")

    rows = [{
        "product_code": i.product_code,
        "raw_description": i.description.strip(),
        "normalized_name": normalize_name(i.description),
        "quantity": i.quantity,
        "unit_price_net": i.final_unit_price_net,
        "discount_percent": i.discount_percent or 0.0,
        "line_total_net": i.line_total_net,
    } for i in inv.items]

    # 5. Persist + compare
    with db.get_conn() as conn:
        if mark:
            existing = db.get_invoice_by_mark(conn, mark)
            if existing:
                discard()
                return build_report(conn, existing["id"], duplicate=True,
                                    warnings=[f"Το τιμολόγιο με ΜΑΡΚ {mark} έχει ήδη καταχωρηθεί."])
        supplier_id = db.get_or_create_supplier(conn, afm, (inv.supplier_name or "").strip())
        try:
            invoice_id = db.insert_invoice_with_items(conn, {
                "mark": mark,
                "invoice_number": inv.invoice_number,
                "supplier_id": supplier_id,
                "invoice_date": inv_date,
                **totals,
                "qr_raw_url": qr.raw_url if qr else None,
                "image_path": str(saved.relative_to(BASE_DIR)),
            }, rows)
        except sqlite3.IntegrityError:
            discard()
            raise HTTPException(409, "Διπλή καταχώρηση: το ΜΑΡΚ υπάρχει ήδη.")
        return build_report(conn, invoice_id, warnings=warnings)


@app.get("/api/invoices")
def get_invoices(supplier_id: Optional[int] = None):
    with db.get_conn() as conn:
        invoices = db.list_invoices(conn, supplier_id)
        for inv in invoices:
            header = {"id": inv["id"], "supplier_id": inv["supplier_id"], "invoice_date": inv["invoice_date"]}
            compared = compare_items(conn, header, db.get_invoice_items(conn, inv["id"]))
            s = summarize(compared)
            inv["increases"] = s[PriceStatus.INCREASED.value]
            inv["decreases"] = s[PriceStatus.DECREASED.value]
            inv["has_increases"] = inv["increases"] > 0
        return invoices


@app.get("/api/invoices/{invoice_id}/report", response_model=InvoiceReport)
def get_report(invoice_id: int):
    with db.get_conn() as conn:
        return build_report(conn, invoice_id)


@app.delete("/api/invoices/{invoice_id}")
def remove_invoice(invoice_id: int):
    with db.get_conn() as conn:
        header = db.get_invoice_header(conn, invoice_id)
        if not header:
            raise HTTPException(404, "Το τιμολόγιο δεν βρέθηκε.")
        db.delete_invoice(conn, invoice_id)
    if header.get("image_path"):
        (BASE_DIR / header["image_path"]).unlink(missing_ok=True)
    return {"deleted": True}


@app.patch("/api/items/{item_id}", response_model=InvoiceReport)
def patch_item(item_id: int, upd: ItemUpdate):
    with db.get_conn() as conn:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, "Το είδος δεν βρέθηκε.")
        qty = upd.quantity if upd.quantity is not None else item["quantity"]
        price = upd.unit_price_net if upd.unit_price_net is not None else item["unit_price_net"]
        db.update_item(conn, item_id, qty, price)
        return build_report(conn, item["invoice_id"])


@app.get("/api/suppliers")
def get_suppliers():
    with db.get_conn() as conn:
        return db.list_suppliers(conn)


@app.get("/api/products/search")
def search_products(q: str = ""):
    nq = normalize_name(q)
    if len(nq) < 2:
        return []
    with db.get_conn() as conn:
        rows = db.search_products(conn, nq)
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["normalized_name"], {
            "name": r["raw_description"], "product_code": r["product_code"], "timeline": [],
        })
        g["timeline"].append({
            "date": r["invoice_date"], "supplier_id": r["supplier_id"], "supplier": r["supplier_name"],
            "unit_price": r["unit_price_net"], "invoice_id": r["invoice_id"],
        })
    out = []
    for g in groups.values():
        latest_by_supplier: dict[int, dict] = {}
        for t in g["timeline"]:  # newest first
            latest_by_supplier.setdefault(t["supplier_id"], t)
        cheapest = min(latest_by_supplier.values(), key=lambda t: t["unit_price"])
        g["cheapest"] = {"supplier": cheapest["supplier"], "unit_price": cheapest["unit_price"], "date": cheapest["date"]}
        g["supplier_count"] = len(latest_by_supplier)
        out.append(g)
    return out[:50]
