"""FastAPI application: routes and static file serving."""
from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import os
import secrets
import sqlite3
import uuid
import mimetypes
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
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

# ---------- auth ----------
# Paths reachable without credentials so the PWA can install (no invoice data here)
PUBLIC_PATHS = {"/manifest.json", "/sw.js"}


def _auth_enabled() -> bool:
    return bool(os.getenv("APP_PASSWORD", "").strip())


def _parse_basic(header: str) -> Optional[tuple[bytes, bytes]]:
    """Decode a Basic Authorization header. FastAPI's HTTPBasic only accepts ASCII,
    which rejects Greek passwords; browsers send UTF-8, older ones Latin-1."""
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param:
        return None
    try:
        raw = base64.b64decode(param.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    user, sep, pwd = text.partition(":")
    return (user.encode("utf-8"), pwd.encode("utf-8")) if sep else None


def require_auth(request: Request) -> None:
    """Enforce HTTP Basic Auth on every route when APP_PASSWORD is set."""
    if not _auth_enabled() or request.url.path in PUBLIC_PATHS:
        return
    user = (os.getenv("APP_USERNAME", "").strip() or "admin").encode("utf-8")
    pwd = os.getenv("APP_PASSWORD", "").strip().encode("utf-8")
    creds = _parse_basic(request.headers.get("authorization", ""))
    ok = creds is not None and (
        secrets.compare_digest(creds[0], user) & secrets.compare_digest(creds[1], pwd)
    )
    if not ok:
        raise HTTPException(401, "Απαιτείται σύνδεση.", headers={"WWW-Authenticate": 'Basic realm="Invoice Tracker"'})


# ---------- cached price-change counts ----------
def _save_counts(conn: sqlite3.Connection, invoice_id: int) -> None:
    header = db.get_invoice_header(conn, invoice_id)
    if not header:
        return
    s = summarize(compare_items(conn, header, db.get_invoice_items(conn, invoice_id)))
    db.update_invoice_counts(conn, invoice_id, s[PriceStatus.INCREASED.value], s[PriceStatus.DECREASED.value])


def _refresh_supplier_from(conn: sqlite3.Connection, supplier_id: int, invoice_date: str) -> None:
    """Recompute counts for every invoice whose comparison may depend on a change at `invoice_date`."""
    for inv_id in db.supplier_invoice_ids_from(conn, supplier_id, invoice_date):
        _save_counts(conn, inv_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_DIR.mkdir(exist_ok=True)
    db.init_db()
    with db.get_conn() as conn:
        pending = db.invoice_ids_needing_counts(conn)
        for inv_id in pending:
            _save_counts(conn, inv_id)
    if pending:
        log.info("Backfilled price-change counts for %d invoice(s).", len(pending))
    if not ZBAR_OK:
        log.warning("Server-side QR decoding disabled (zbar DLL not loaded). Use live QR scan in the app.")
    if not _auth_enabled():
        log.warning("APP_PASSWORD is empty: the app is open to anyone on the network.")
    yield


app = FastAPI(
    title="Invoice Price Tracker",
    lifespan=lifespan,
    dependencies=[Depends(require_auth)],
    # API docs are not covered by route dependencies, so hide them when auth is on
    docs_url=None if _auth_enabled() else "/docs",
    redoc_url=None,
    openapi_url=None if _auth_enabled() else "/openapi.json",
)


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
        _refresh_supplier_from(conn, supplier_id, inv_date)
        return build_report(conn, invoice_id, warnings=warnings)


@app.get("/api/invoices")
def get_invoices(supplier_id: Optional[int] = None):
    with db.get_conn() as conn:
        invoices = db.list_invoices(conn, supplier_id)
    for inv in invoices:
        inv["increases"] = inv.pop("increases_count") or 0
        inv["decreases"] = inv.pop("decreases_count") or 0
        inv["has_increases"] = inv["increases"] > 0
    return invoices


@app.get("/api/invoices/{invoice_id}/report", response_model=InvoiceReport)
def get_report(invoice_id: int):
    with db.get_conn() as conn:
        return build_report(conn, invoice_id)


CSV_COLUMNS = [
    "ΑΦΜ_ΠΡΟΜΗΘΕΥΤΗ", "ΕΠΩΝΥΜΙΑ", "ΑΡ_ΠΑΡΑΣΤΑΤΙΚΟΥ", "ΗΜΕΡΟΜΗΝΙΑ", "ΜΑΡΚ", "ΚΩΔΙΚΟΣ_ΕΙΔΟΥΣ",
    "ΠΕΡΙΓΡΑΦΗ", "ΠΟΣΟΤΗΤΑ", "ΤΙΜΗ_ΜΟΝΑΔΑΣ_ΚΑΘΑΡΗ", "ΕΚΠΤΩΣΗ_ΠΟΣΟΣΤΟ", "ΚΑΘΑΡΗ_ΑΞΙΑ",
    "ΚΑΤΑΣΤΑΣΗ_ΤΙΜΗΣ", "ΠΡΟΗΓΟΥΜΕΝΗ_ΤΙΜΗ", "ΜΕΤΑΒΟΛΗ_ΠΟΣΟΣΤΟ",
]
STATUS_GR = {
    PriceStatus.INCREASED: "ΑΥΞΗΣΗ", PriceStatus.DECREASED: "ΜΕΙΩΣΗ",
    PriceStatus.UNCHANGED: "ΣΤΑΘΕΡΗ", PriceStatus.NEW_ITEM: "ΝΕΟ",
}


def _gr(n: Optional[float], decimals: int = 2) -> str:
    """Greek Excel / Soft1 number: comma decimal, no thousands separator, blank for None."""
    return "" if n is None else f"{n:.{decimals}f}".replace(".", ",")


@app.get("/api/invoices/{invoice_id}/export.csv")
def export_csv(invoice_id: int):
    with db.get_conn() as conn:
        report = build_report(conn, invoice_id)
    inv = report.invoice
    d = inv.invoice_date
    date_gr = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if _is_iso_date(d) else d

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for it in report.items:
        w.writerow([
            inv.supplier_afm, inv.supplier_name, inv.invoice_number or "", date_gr, inv.mark or "",
            it.product_code or "", it.description,
            _gr(it.quantity, 3), _gr(it.unit_price_net, 4), _gr(it.discount_percent, 2), _gr(it.line_total_net, 2),
            STATUS_GR[it.status], _gr(it.old_price, 4), _gr(it.diff_percent, 2),
        ])
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"timologio_{inv.supplier_afm}_{d}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/invoices/{invoice_id}/file")
def get_invoice_file(invoice_id: int):
    with db.get_conn() as conn:
        header = db.get_invoice_header(conn, invoice_id)
    if not header:
        raise HTTPException(404, "Το τιμολόγιο δεν βρέθηκε.")
    missing = HTTPException(404, "Το αρχείο του τιμολογίου δεν είναι διαθέσιμο.")
    if not header.get("image_path"):
        raise missing
    path = (BASE_DIR / header["image_path"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.is_file():
        raise missing  # also blocks path traversal via a tampered image_path
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, content_disposition_type="inline", filename=path.name)


@app.delete("/api/invoices/{invoice_id}")
def remove_invoice(invoice_id: int):
    with db.get_conn() as conn:
        header = db.get_invoice_header(conn, invoice_id)
        if not header:
            raise HTTPException(404, "Το τιμολόγιο δεν βρέθηκε.")
        db.delete_invoice(conn, invoice_id)
        _refresh_supplier_from(conn, header["supplier_id"], header["invoice_date"])
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
        header = db.get_invoice_header(conn, item["invoice_id"])
        _refresh_supplier_from(conn, header["supplier_id"], header["invoice_date"])
        return build_report(conn, item["invoice_id"])


@app.get("/api/suppliers")
def get_suppliers():
    with db.get_conn() as conn:
        return db.list_suppliers(conn)


@app.get("/api/products/top-increases")
def top_increases(days: int = 30):
    days = max(1, min(days, 365))
    since = (date.today() - timedelta(days=days)).isoformat()
    hikes = []
    with db.get_conn() as conn:
        for inv_id in db.invoice_ids_since(conn, since):
            header = db.get_invoice_header(conn, inv_id)
            for it in compare_items(conn, header, db.get_invoice_items(conn, inv_id)):
                if it.status == PriceStatus.INCREASED:
                    hikes.append({
                        "description": it.description, "supplier_name": header["supplier_name"],
                        "old_price": it.old_price, "new_price": it.unit_price_net,
                        "diff_percent": it.diff_percent, "invoice_date": header["invoice_date"],
                        "invoice_id": inv_id,
                    })
    hikes.sort(key=lambda h: h["diff_percent"] or 0, reverse=True)
    return hikes[:10]


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
