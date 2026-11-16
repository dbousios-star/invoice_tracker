"""Vision LLM client: extracts structured invoice JSON from images / PDFs."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import ValidationError

from .models import ExtractedInvoice

log = logging.getLogger(__name__)

MAX_SIDE = 2000          # px, keeps token cost down while staying legible
MAX_PDF_PAGES = 4


class ExtractionError(Exception):
    """Error with a user-facing Greek message."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


SYSTEM_PROMPT = """Είσαι ειδικός στην ανάγνωση ελληνικών τιμολογίων αγοράς (B2B).
Επέστρεψε ΑΠΟΚΛΕΙΣΤΙΚΑ ένα έγκυρο JSON αντικείμενο, χωρίς σχόλια ή markdown, με ακριβώς αυτό το σχήμα:
{
  "supplier_name": string|null,        // επωνυμία ΕΚΔΟΤΗ (προμηθευτή), ΟΧΙ του πελάτη
  "supplier_afm": string|null,         // ΑΦΜ εκδότη, ακριβώς 9 ψηφία
  "invoice_number": string|null,       // σειρά + αριθμός παραστατικού
  "invoice_date": string|null,         // YYYY-MM-DD
  "mark": string|null,                 // ΜΑΡΚ ΑΑΔΕ (15 ψηφία) αν είναι τυπωμένο
  "total_net": number|null,            // σύνολο καθαρής αξίας
  "total_vat": number|null,            // σύνολο ΦΠΑ
  "total_gross": number|null,          // πληρωτέο / συνολική αξία
  "items": [
    {
      "product_code": string|null,                // κωδικός είδους του προμηθευτή
      "description": string,                      // περιγραφή όπως είναι τυπωμένη
      "quantity": number,
      "unit_price_before_discount": number|null,  // τιμή μονάδας προ έκπτωσης, χωρίς ΦΠΑ
      "discount_percent": number,                 // % έκπτωση γραμμής (0 αν δεν υπάρχει)
      "final_unit_price_net": number,             // καθαρή τιμή ΑΝΑ ΜΟΝΑΔΑ μετά την έκπτωση, χωρίς ΦΠΑ
      "line_total_net": number                    // καθαρή αξία γραμμής μετά την έκπτωση, χωρίς ΦΠΑ
    }
  ]
}
Κανόνες:
- Αριθμοί ως JSON numbers με τελεία δεκαδικών (π.χ. 1234.56). Μετάτρεψε την ελληνική μορφή 1.234,56 σε 1234.56.
- final_unit_price_net = line_total_net / quantity. Έλεγξε τα μαθηματικά για κάθε γραμμή.
- Αν υπάρχει έκπτωση μόνο σε ποσό, υπολόγισε το discount_percent.
- Μην επινοείς γραμμές. Μην συμπεριλαμβάνεις γραμμές συνόλων, μεταφορικών ΦΠΑ ή ενδιάμεσα αθροίσματα ως είδη.
- Αν το παραστατικό έχει πολλές σελίδες, συνένωσε όλα τα είδη.
- Αν η εικόνα δεν είναι τιμολόγιο ή είναι εντελώς δυσανάγνωστη, επέστρεψε {"items": [], "error": "unreadable"}.
"""


def _config() -> tuple[str, str, str]:
    key = os.getenv("API_KEY", "").strip()
    base = os.getenv("API_BASE_URL", "").strip() or None
    model = os.getenv("MODEL_NAME", "").strip() or "gemini-flash-latest"
    if not key:
        raise ExtractionError("Δεν έχει οριστεί API_KEY στο αρχείο .env. Η εξαγωγή AI δεν είναι διαθέσιμη.", 503)
    return key, base, model


def _image_to_jpeg_b64(img: Image.Image) -> str:
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def load_pages(path: Path) -> list[Image.Image]:
    """Return PIL images for an image file or the first pages of a PDF."""
    if path.suffix.lower() == ".pdf":
        try:
            import pymupdf
        except ImportError as exc:
            raise ExtractionError("Η υποστήριξη PDF δεν είναι εγκατεστημένη (pymupdf).", 500) from exc
        pages = []
        with pymupdf.open(path) as doc:
            for page in list(doc)[:MAX_PDF_PAGES]:
                pix = page.get_pixmap(dpi=200)
                pages.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        if not pages:
            raise ExtractionError("Το PDF δεν περιέχει σελίδες.")
        return pages
    try:
        img = Image.open(path)
        img.load()
        return [img]
    except Exception as exc:
        raise ExtractionError("Το αρχείο δεν είναι έγκυρη εικόνα ή PDF.") from exc


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _fix_math(inv: ExtractedInvoice) -> ExtractedInvoice:
    """Guarantee final_unit_price_net is the net price per single unit after discount."""
    clean = []
    for it in inv.items:
        qty = it.quantity if it.quantity and it.quantity > 0 else 1.0
        it.quantity = qty
        disc = it.discount_percent or 0.0
        it.discount_percent = disc
        final = it.final_unit_price_net
        line = it.line_total_net

        if final is None and it.unit_price_before_discount is not None:
            final = it.unit_price_before_discount * (1 - disc / 100)
        if line is None and final is not None:
            line = final * qty
        if line is not None and (final is None or abs(final * qty - line) > 0.01):
            final = line / qty
        if final is None or line is None or not it.description.strip():
            continue  # unusable row
        it.final_unit_price_net = round(final, 4)
        it.line_total_net = round(line, 2)
        clean.append(it)
    inv.items = clean
    return inv


def extract_invoice(path: Path) -> ExtractedInvoice:
    """Send the invoice to the Vision LLM and return validated, math-checked data."""
    key, base, model = _config()
    from openai import APIError, OpenAI  # imported lazily so the app starts without network deps

    pages = load_pages(path)
    content: list[dict] = [{"type": "text", "text": "Εξήγαγε τα στοιχεία αυτού του τιμολογίου σε JSON."}]
    for p in pages:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_image_to_jpeg_b64(p)}"}})

    client = OpenAI(api_key=key, base_url=base, timeout=120)
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        )
    except APIError as exc:
        log.exception("Vision API error")
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            raise ExtractionError("Μη έγκυρο API_KEY ή χωρίς δικαίωμα πρόσβασης στο μοντέλο.", 502) from exc
        if status == 404:
            raise ExtractionError(f"Το μοντέλο «{model}» δεν βρέθηκε ή έχει αποσυρθεί. Αλλάξτε το MODEL_NAME στο .env (π.χ. gemini-flash-latest).", 502) from exc
        if status == 429:
            raise ExtractionError("Υπέρβαση ορίου κλήσεων του AI. Δοκιμάστε ξανά σε λίγο.", 503) from exc
        raise ExtractionError(f"Σφάλμα επικοινωνίας με την υπηρεσία AI ({status or 'δίκτυο'}).", 502) from exc
    except Exception as exc:
        log.exception("Vision API failure")
        raise ExtractionError("Αδυναμία σύνδεσης με την υπηρεσία AI. Ελέγξτε τη σύνδεση και το API_BASE_URL.", 502) from exc

    raw = resp.choices[0].message.content if resp.choices else ""
    try:
        data = _parse_json(raw)
    except Exception as exc:
        log.error("Unparseable LLM output: %s", (raw or "")[:500])
        raise ExtractionError("Το AI επέστρεψε μη έγκυρα δεδομένα. Δοκιμάστε ξανά με πιο καθαρή φωτογραφία.", 502) from exc

    if data.get("error") == "unreadable":
        raise ExtractionError("Η εικόνα είναι θολή ή δεν αναγνωρίζεται ως τιμολόγιο. Φωτογραφίστε ξανά με καλό φωτισμό.")

    try:
        inv = ExtractedInvoice.model_validate(data)
    except ValidationError as exc:
        log.error("LLM output failed validation: %s", exc)
        raise ExtractionError("Τα δεδομένα του AI δεν είχαν την αναμενόμενη μορφή. Δοκιμάστε ξανά.", 502) from exc

    inv = _fix_math(inv)
    if not inv.items:
        raise ExtractionError("Δεν βρέθηκαν είδη στο τιμολόγιο. Η φωτογραφία μπορεί να είναι θολή ή κομμένη.")
    if inv.supplier_afm:
        digits = re.sub(r"\D", "", inv.supplier_afm)
        inv.supplier_afm = digits[-9:] if len(digits) >= 9 else None
    if inv.mark:
        digits = re.sub(r"\D", "", inv.mark)
        inv.mark = digits if len(digits) >= 10 else None
    return inv
