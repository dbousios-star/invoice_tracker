# Invoice Price Tracker (PWA)

Scan Greek supplier invoices (AADE myDATA QR + photo/PDF). The app extracts line items with a Vision LLM, stores them in SQLite, and flags price changes against the previous invoice from the same supplier.

🔴 increase · 🟢 decrease · ⚪ unchanged · 🔵 new item

## Setup (Windows)

1. Install **Python 3.11+** and tick "Add to PATH".
2. Double-click **`run.bat`**. The first run creates `venv\`, installs the requirements and copies `.env.example` to `.env`.
3. Edit **`.env`** and set `API_KEY` (plus `API_BASE_URL` and `MODEL_NAME` if you don't use Gemini), then restart `run.bat`.

| Provider   | API_BASE_URL                                              | MODEL_NAME                    |
|------------|-----------------------------------------------------------|-------------------------------|
| Gemini     | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-flash-latest`         |
| OpenAI     | `https://api.openai.com/v1`                               | `gpt-4o-mini`                 |
| OpenRouter | `https://openrouter.ai/api/v1`                            | `google/gemini-flash-latest`  |

Only the uncommented `MODEL_NAME=` line counts. Lines starting with `#` are ignored. If the AI reports that the model was not found, the model has been retired: pick a current one from your provider's model list.

`PRICE_THRESHOLD_AMOUNT` (default €0.02) and `PRICE_THRESHOLD_PERCENT` (default 0.5%) control how big a change must be to count. A change is reported only when **both** are exceeded.

## Connecting from an Android phone

1. Connect the PC and the phone to the same Wi-Fi.
2. On the PC, run `ipconfig` and note the IPv4 address (e.g. `192.168.1.20`).
3. Allow inbound TCP port 8000 in Windows Firewall. Run this once in an admin PowerShell:
   `New-NetFirewallRule -DisplayName "Invoice Tracker" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`
4. In Chrome on the phone, open `http://192.168.1.20:8000`, then choose ⋮ → **Add to Home screen**.

**Live QR camera needs HTTPS.** Over plain `http://<LAN-IP>`, the browser blocks the "🔍 Σκανάρισμα Μόνο QR" camera. Photo upload still works, and the QR is decoded from the photo on the server. To enable the live scanner, use either:
- A tunnel such as `cloudflared tunnel --url http://localhost:8000` (gives an `https://…trycloudflare.com` URL), or
- On the phone, go to `chrome://flags/#unsafely-treat-insecure-origin-as-secure` and add `http://192.168.1.20:8000`.

## How it works

1. **QR** (`app/qr_parser.py`): pyzbar decodes the QR from the photo, or it comes from a live scan. The parser reads MARK, AFM, date and totals from AADE or provider URLs. If the MARK is already saved, the existing report is shown instead of logging the invoice twice.
2. **AI extraction** (`app/ocr_extractor.py`): the image, or up to 4 PDF pages rendered as images, goes to the Vision LLM in JSON mode. Each line's unit price is recomputed as `line_total_net / quantity` whenever the two disagree. The AFM, MARK and totals from the QR override the AI's values.
3. **Comparison** (`app/price_comparator.py`): each item is first matched by exact supplier product code. If there is no code match, it falls back to a RapidFuzz WRatio match of 85 or more on the normalized description. Items are compared against the most recent earlier invoice from the same supplier.
4. **UI** (`static/`): three tabs (Σάρωση, Ιστορικό, Προϊόντα). You can correct an item's quantity or price inline and the comparison recalculates immediately.

Data is stored in `invoices.db` and the uploaded files in `uploads\`. Both are in `.gitignore`.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/upload` | multipart `file` (+ optional `qr_text`) → comparison report |
| POST | `/api/qr-check` | `{qr_text}` → duplicate check |
| GET | `/api/invoices?supplier_id=` | history with increase/decrease counts |
| GET | `/api/invoices/{id}/report` | full comparison |
| DELETE | `/api/invoices/{id}` | remove invoice + image |
| PATCH | `/api/items/{id}` | `{quantity?, unit_price_net?}` → refreshed report |
| GET | `/api/suppliers` | supplier list |
| GET | `/api/products/search?q=` | price timeline across suppliers + cheapest |
| GET | `/api/health` | zbar / API key status |

## Troubleshooting

- **`zbar: false` in `/api/health`:** install the Microsoft Visual C++ Redistributable (x64). The app still works without it; live QR scanning and the MARK read by the AI are used instead.
- **"Δεν έχει οριστεί API_KEY":** fill in `.env` and restart the server.
- **HEIC photos:** set the Android camera to save JPEG. The camera button in the app already sends JPEG.
