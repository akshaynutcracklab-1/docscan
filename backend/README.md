# DocScan backend

Django + Django REST Framework backend for a document-scanning web app. The React frontend uploads
camera-scanned pages; this service stores them, runs OCR with **Azure AI Document Intelligence (v4 SDK)**,
builds one searchable PDF, extracts type-specific fields (invoice, receipt, ID, form/table, contract) and
returns everything for preview and download.

* Python 3.11+ (tested on 3.15), Django 5.2, DRF, SimpleJWT
* SQLite (`backend/db.sqlite3`), Celery + Redis, Azure Blob Storage (local filesystem fallback)
* Swagger UI at **`/api/docs/`**, OpenAPI schema at `/api/schema/`
* Built-in **scanner web page at `/scan/`** (`/` redirects there)

### Scanner page (`/scan/`)

A dependency-free page (`scanner/templates`, `scanner/static`) that uses the same REST API:

1. Log in or create an account, then pick a document type (your documents are listed below the types, with search).
2. The live camera opens and **finds the page edges in real time** (green outline, OpenCV.js loaded from jsDelivr
   on first use). Tap the shutter (or press Space), or turn on **Auto capture** to shoot when the page is held still.
   Each capture is **cropped to the page and perspective-corrected** (a flat "scan", no table or background), then
   filtered: **Enhanced** (white paper, dark text), **Original colour**, or **Black & white** (adaptive threshold,
   removes shadows). **Gallery** photos get the same treatment (HEIC is uploaded unchanged).
   Tap a page thumbnail to open the corner editor: drag the 4 corners (with a magnifier), **Detect again**,
   **Whole photo**, or change the filter. If OpenCV.js cannot be loaded, the page falls back to a framing guide.
3. **Done** → review: reorder, rotate, re-crop (✂) or delete pages and set an optional title → **Upload & extract text**.
4. The pages are uploaded and processed with a live progress bar. Failures show the error and a **Retry** button.
5. Results: extracted fields (line items and tables included), the full text (editable, **Save changes**), detected
   dates/amounts/emails/phones/GSTIN/PAN, pages with OCR confidence, **Open PDF** (searchable), .txt/.json download,
   **Add pages** (reprocesses the document) and **Delete**.

Browsers only allow the live camera on **HTTPS or `http://localhost`**. When a phone opens the page over
`http://<your-pc-ip>:8000`, the page shows a message instead of the camera; **Gallery** still works and opens the
phone's camera app. For a live camera on a phone, serve the app over HTTPS (for example a reverse proxy with a
certificate, or a tunnel such as `ngrok http 8000`). The JWT is stored in `localStorage`.

---

## 1. Run with Docker (recommended)

```bash
cd backend
docker compose up --build            # API + Celery worker + Redis
```

* API and Swagger: http://localhost:8000/api/docs/ · admin: http://localhost:8000/admin/
* On first start the `web` container runs the migrations and loads the document types (only if the table is
  empty, so admin edits survive restarts). The `worker` container starts once `web` is healthy.
* The SQLite database and uploaded files live in the `app-data` volume (`/data` in both containers).
  `docker compose down` keeps them; `docker compose down -v` deletes them.
* Configuration: `../.env` (repository root) and `./.env` are both loaded; `./.env` wins, and both are optional.
  The Azure key can stay in the root `.env`. Do not leave a blank `DOCINTEL_KEY=` in `./.env`: it would
  override the real key. Without Azure keys, set `OCR_ENGINE=mock`.
  Set `SECRET_KEY` in `./.env`; the compose default is for local use only.
* Admin user: `docker compose exec web python manage.py createsuperuser`
* Tests in the container: `docker compose run --rm web python -m pytest`
* Logs: `docker compose logs -f web worker`

The compose file runs the production settings (`config.settings.prod`, gunicorn, whitenoise for static files) over
plain HTTP: it turns off the HTTPS redirect, secure-only cookies and HSTS, and serves uploaded files through
Django (`SERVE_MEDIA=True`). Behind a real HTTPS proxy, remove those overrides. For public deployments, prefer
`USE_AZURE_STORAGE=True` so that files are served through signed URLs.

Optional Azure Blob emulator: `docker compose --profile azurite up -d azurite`.

## 2. Local setup (without Docker)

```bash
cd backend
python -m venv ../venv && source ../venv/bin/activate      # Windows: ..\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                       # then edit .env

# Redis is needed for the cache and the worker; without it, set USE_LOCMEM_CACHE=True and
# CELERY_TASK_ALWAYS_EAGER=True in .env (see below).
python manage.py migrate                                   # creates backend/db.sqlite3
python manage.py loaddata document_types                   # seeds the 6 document types
python manage.py createsuperuser                           # optional, for /admin/
python manage.py init_storage                              # only if USE_AZURE_STORAGE=True (creates the container)
```

`.env` is read from `backend/.env`; a `.env` in the repository root is used as a fallback (values in
`backend/.env` win). All secrets (Django key, DB password, Azure keys) come from there.

### Run

```bash
# API server
python manage.py runserver

# Celery worker (another terminal). On Windows add --pool=solo
celery -A config worker -l info
```

**No Redis / no worker?** Put `USE_LOCMEM_CACHE=True` and `CELERY_TASK_ALWAYS_EAGER=True` in `.env`;
the pipeline then runs inside the submit request (fine for development, not for production).

### Database

The database is a SQLite file, `backend/db.sqlite3` (`DATABASE_URL=sqlite:///db.sqlite3`; relative paths are
resolved inside `backend/`). Because the API and the Celery worker write to it at the same time, it is opened in
**WAL** mode with `transaction_mode=IMMEDIATE` and a 20 s busy timeout: writers queue up instead of failing with
"database is locked", and submit/upload transactions are serialised, so the double-submit check is safe even though
SQLite ignores `select_for_update`. Keep the worker on the same machine as the database file, and back it up with
`sqlite3 db.sqlite3 ".backup backup.sqlite3"` (not a plain file copy while running).

Search (`?q=`) is a case-insensitive substring match on title and text. The code still contains a
PostgreSQL full-text path (ranked `SearchVectorField` + GIN index), which turns on automatically if you ever set
`DATABASE_URL=postgres://...`; on SQLite it is skipped.

Production: `DJANGO_SETTINGS_MODULE=config.settings.prod` (WSGI/ASGI default to it), `DEBUG=False`,
a real `SECRET_KEY`, `ALLOWED_HOSTS`, and `python manage.py check --deploy`.

### Tests

```bash
pytest            # 56 tests, SQLite in memory, Celery eager, Azure always mocked
```

---

## 3. OCR engine: `mock` vs `azure`

| `OCR_ENGINE` | What happens |
|---|---|
| `mock` | No Azure calls. Every page gets fixed sample text (with a date, ₹/$ amounts, email, phone, GSTIN, PAN), confidence 0.95, an image-only page PDF, and realistic fake fields per document type. Use it for frontend work and CI. |
| `azure` | Real calls to Document Intelligence. Needs `DOCINTEL_ENDPOINT` and `DOCINTEL_KEY`. |

Other knobs:

* `DOCINTEL_TIER=F0|S0`: on **F0**, each page is sent separately (F0 analyses at most 2 pages per request and
  4 MB per file), and field models get at most the first 2 page images (capped by the type's `max_pages`), merged
  into one result. `form_table` (layout) gets every page. On **S0**, the merged PDF is sent once.
* `DOCINTEL_RATE_LIMIT=20/m`: shared across workers through the cache (Redis), so all workers together stay
  under the limit.
* `DOCINTEL_DETECT_LANGUAGE=True`: requests the `languages` add-on so `language` is filled.
* `LOW_CONFIDENCE_THRESHOLD=0.80`: pages below it get `is_low_quality=true` and show up in
  `analysis.low_confidence_pages`.

All SDK calls live in `processing/azure_client.py` (`analyze()`, `get_result_pdf()`), used by `ocr.py`
and `extractors.py`. Tests patch `azure_client.get_client` with a fake that returns real SDK model objects.
The key is never logged or returned (the `azure` loggers are set to WARNING).

---

## 4. Processing pipeline

`POST /submit/` → a Celery chain (`processing/tasks.py`). Every step updates `progress` and `current_step`:

| Step | Progress | Work |
|---|---|---|
| `prepare_pages` | 0→10 | EXIF orientation + user rotation, HEIC/PNG/WebP → JPEG, long side ≤ 3000 px, compressed < 4 MB |
| `ocr_pages` | 10→70 | `prebuilt-read` per page with `output=[PDF]`, then `get_analyze_result_pdf(...)` → `ocr_text`, `ocr_data` (lines and words with polygons + confidence), `ocr_confidence`, `page_pdf` |
| `build_pdf` | 70→80 | pikepdf merges the page PDFs in order → `document.pdf`; `full_text` = pages joined with `--- Page N ---`; language; average confidence |
| `extract_fields` | 80→90 | the type's `azure_field_model` (see table below) |
| `analyze` | 90→100 | regex dates (dateparser, day-first), ₹/$ amounts, emails, Indian mobiles, GSTIN, PAN, word count, low-confidence pages; search vector; `status=ready` |

* HTTP 429/5xx and network errors are retried up to **3 times with exponential backoff**
  (`PIPELINE_RETRY_BACKOFF` × 2ⁿ seconds). Other errors are not retried.
* Any unrecoverable error → `status=failed`, a user-safe `error_message`, `current_step=failed:<step>`, and
  the stack trace in the logs. `POST /retry/` resumes: pages that already finished are not OCR'd again.
* `reprocess` (on a ready document) also processes only new or changed pages, then rebuilds the PDF and fields.

| Type | `azure_field_model` | `extracted_fields` shape |
|---|---|---|
| general | – | `{}` |
| invoice | `prebuilt-invoice` | `{Field: {value, content, confidence}}`; `Items.value = [{Description, Quantity, UnitPrice, Amount}]` |
| receipt | `prebuilt-receipt` | same (receipt `Price`/`TotalPrice` are mapped to `UnitPrice`/`Amount`) |
| id_document | `prebuilt-idDocument` | `{Field: {value, content, confidence}}` |
| contract | `prebuilt-contract` | `{Field: {value, content, confidence}}` |
| form_table | `prebuilt-layout` | `{tables: [{row_count, column_count, cells: [{row, col, content, kind}]}], markdown}` |

Currency values are `{amount, currency_symbol, currency_code}`; dates are ISO strings.
Document types (names, models, `display_fields`, `max_pages`) can be edited in the admin.

Storage layout (local `MEDIA_ROOT` or the Azure container):
`users/<user_id>/documents/<document_id>/pages/<order>.jpg`, `.../pages/<order>.pdf`, `.../document.pdf`.
With Azure storage, every file URL in a response is a read-only **SAS URL valid for 15 minutes**.

---

## 5. Rules

* Every endpoint except register/token requires `Authorization: Bearer <access>`.
* Users only see their own documents and pages; anything else returns **404**.
* Status flow: `draft` → `uploading` (after the first page) → `processing` → `ready` | `failed`.
* Pages can be added, rotated, reordered or deleted in `draft`/`uploading`, and also in `ready`/`failed`
  (the changes apply through `reprocess`/`retry`). Never while `processing` (**409**).
* Submit needs ≥ 1 page and ≤ `max_pages`; a second submit gets **409** (row lock with `select_for_update` plus a status check).
* Uploads: JPEG/PNG/HEIC/WebP, checked **by content with Pillow** (the extension is ignored), ≤ 15 MB each.
* Throttling: 120 requests/min per user, 60 uploads/min per user, 30/min anonymous.
* The document type can only change before submit.

---

## 6. API reference and the full flow

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | create an account → user + JWT pair |
| POST | `/api/auth/token/` | login → `{access, refresh}` |
| POST | `/api/auth/token/refresh/` | new access token |
| GET | `/api/auth/me/` | current user |
| GET | `/api/document-types/` | active types, ordered |
| POST | `/api/documents/` | create a draft `{document_type, title?}` |
| GET | `/api/documents/?type=&status=&q=&page=` | my documents (paginated, 20 per page) |
| GET / PATCH / DELETE | `/api/documents/{id}/` | detail / rename, change type, edit text / delete with files |
| POST | `/api/documents/{id}/pages/` | upload one page (`image`, `order?`, `rotation?`) |
| POST | `/api/documents/{id}/pages/bulk/` | upload many (`images[]`) |
| PATCH | `/api/documents/{id}/pages/reorder/` | `{"page_ids": [...]}` |
| PATCH / DELETE | `/api/pages/{id}/` | rotate / delete (remaining pages renumbered) |
| POST | `/api/documents/{id}/submit/` | start processing → 202 |
| GET | `/api/documents/{id}/status/` | lightweight status for polling |
| POST | `/api/documents/{id}/retry/` | failed → processing |
| POST | `/api/documents/{id}/reprocess/` | ready → processing (after page changes) |
| GET | `/api/documents/{id}/download/?format=pdf\|txt\|json` | download (pdf: add `&link=true` for `{url, expires_in}`) |

### Walk-through (curl)

```bash
API=http://localhost:8000/api
```

**Register / login**

```bash
curl -s -X POST $API/auth/register/ -H 'Content-Type: application/json' \
  -d '{"username":"asha","email":"asha@example.com","password":"Str0ng-pass!"}'
# 201 {"user":{"id":1,"username":"asha","email":"asha@example.com",...},"access":"eyJ...","refresh":"eyJ..."}

curl -s -X POST $API/auth/token/ -H 'Content-Type: application/json' \
  -d '{"username":"asha","password":"Str0ng-pass!"}'
# 200 {"refresh":"eyJ...","access":"eyJ..."}
TOKEN=eyJ...   # the access token
AUTH="Authorization: Bearer $TOKEN"
```

**Step 1: choose a type**

```bash
curl -s $API/document-types/ -H "$AUTH"
```
```json
[
  {"code": "general", "name": "General", "description": "Any document. ...", "icon": "file-text",
   "max_pages": 50, "display_fields": [], "has_field_extraction": false},
  {"code": "invoice", "name": "Invoice", "description": "Vendor invoices: ...", "icon": "file-invoice",
   "max_pages": 50, "display_fields": ["VendorName", "InvoiceId", "InvoiceDate", "DueDate", "SubTotal",
   "TotalTax", "InvoiceTotal", "Items"], "has_field_extraction": true}
]
```

**Create the document**

```bash
curl -s -X POST $API/documents/ -H "$AUTH" -H 'Content-Type: application/json' -d '{"document_type":"invoice"}'
# 201 {"id":"5f0c7c3e-8a51-4d0e-9f7a-2b1d5c9e0a11","title":"Invoice – 23 Nov 2026","document_type":"invoice","status":"draft"}
DOC=5f0c7c3e-8a51-4d0e-9f7a-2b1d5c9e0a11
```

**Step 2: upload pages**

```bash
curl -s -X POST $API/documents/$DOC/pages/ -H "$AUTH" -F image=@page1.jpg
curl -s -X POST $API/documents/$DOC/pages/ -H "$AUTH" -F image=@page2.heic -F rotation=90
# 201 {"id":12,"order":2,"image_url":"http://localhost:8000/media/users/1/documents/5f0c.../pages/2.heic",
#      "page_pdf_url":null,"rotation":90,"width":3024,"height":4032,"file_size":1834221,"status":"pending",
#      "ocr_text":"","ocr_confidence":null,"is_low_quality":false}

# gallery fallback: several files in order
curl -s -X POST $API/documents/$DOC/pages/bulk/ -H "$AUTH" -F 'images[]=@a.jpg' -F 'images[]=@b.png'

# reorder / rotate / delete
curl -s -X PATCH $API/documents/$DOC/pages/reorder/ -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"page_ids":[13,11,12,14]}'
curl -s -X PATCH $API/pages/12/ -H "$AUTH" -H 'Content-Type: application/json' -d '{"rotation":180}'
curl -s -X DELETE $API/pages/14/ -H "$AUTH"          # 204, the remaining pages become 1..n
```

**Step 3: submit and poll**

```bash
curl -s -X POST $API/documents/$DOC/submit/ -H "$AUTH"
# 202 {"document_id":"5f0c7c3e-...","status":"processing"}
# 409 {"detail":"Cannot submit a document that is processing."}   (double submit)

curl -s $API/documents/$DOC/status/ -H "$AUTH"      # every 2–3 s
# {"id":"5f0c...","status":"processing","progress":43,"current_step":"ocr_pages","error_message":"","page_count":3,"updated_at":"..."}
# {"id":"5f0c...","status":"ready","progress":100,"current_step":"done","error_message":"","page_count":3,"updated_at":"..."}
# {"id":"5f0c...","status":"failed","progress":10,"current_step":"failed:ocr_pages",
#  "error_message":"Azure Document Intelligence analysis with prebuilt-read failed (HTTP 429): ...", ...}

curl -s -X POST $API/documents/$DOC/retry/ -H "$AUTH"   # only when failed → 202
```

**Get the result**

```bash
curl -s $API/documents/$DOC/ -H "$AUTH"
```
```json
{
  "id": "5f0c7c3e-8a51-4d0e-9f7a-2b1d5c9e0a11",
  "title": "Invoice – 23 Nov 2026",
  "document_type": {"code": "invoice", "name": "Invoice", "display_fields": ["VendorName", "..."], "...": "..."},
  "status": "ready", "progress": 100, "current_step": "done", "error_message": "",
  "page_count": 3,
  "pages": [
    {"id": 13, "order": 1, "image_url": "https://<account>.blob.core.windows.net/documents/users/1/documents/5f0c.../pages/1.jpg?se=...&sig=...",
     "page_pdf_url": "https://...pages/1.pdf?...", "rotation": 0, "width": 2250, "height": 3000,
     "file_size": 912345, "status": "done", "ocr_text": "Contoso Ltd\nINVOICE ...", "ocr_confidence": 0.982,
     "is_low_quality": false}
  ],
  "pdf_url": "https://...document.pdf?se=...&sig=...",
  "full_text": "--- Page 1 ---\n\nContoso Ltd\nINVOICE ...\n\n--- Page 2 ---\n\n...",
  "language": "en",
  "extracted_fields": {
    "VendorName": {"value": "Contoso Ltd", "content": "Contoso Ltd", "confidence": 0.95},
    "InvoiceDate": {"value": "2025-01-15", "content": "15 Jan 2025", "confidence": 0.94},
    "InvoiceTotal": {"value": {"amount": 1250.5, "currency_symbol": "₹", "currency_code": "INR"},
                     "content": "₹1,250.50", "confidence": 0.93},
    "Items": {"value": [{"Description": "A4 paper", "Quantity": 5, "UnitPrice": 211.95, "Amount": 1059.75}],
              "content": null, "confidence": null}
  },
  "analysis": {
    "word_count": 412, "character_count": 2630,
    "dates": [{"raw": "15/01/2025", "date": "2025-01-15"}],
    "amounts": [{"raw": "₹1,250.50", "currency": "INR", "value": "1250.50"}],
    "emails": ["billing@contoso.in"], "phones": ["+919876543210"],
    "gstins": ["27AAPFU0939F1ZV"], "pans": ["AAPFU0939F"],
    "low_confidence_pages": [{"page": 3, "confidence": 0.71}]
  },
  "average_confidence": 0.951,
  "created_at": "2026-11-23T10:15:02Z", "updated_at": "2026-11-23T10:15:31Z"
}
```

**Download**

```bash
curl -s -L -OJ "$API/documents/$DOC/download/?format=pdf" -H "$AUTH"   # local: file; Azure: 302 → SAS URL
curl -s "$API/documents/$DOC/download/?format=pdf&link=true" -H "$AUTH"
# {"url":"https://...document.pdf?se=...&sig=...","expires_in":900}
curl -s -OJ "$API/documents/$DOC/download/?format=txt"  -H "$AUTH"     # full_text
curl -s -OJ "$API/documents/$DOC/download/?format=json" -H "$AUTH"     # fields + analysis
```

**List, search, edit, delete**

```bash
curl -s "$API/documents/?type=invoice&status=ready&q=contoso&page=1" -H "$AUTH"
# {"count":1,"next":null,"previous":null,"results":[{"id":"5f0c...","title":"...","document_type":"invoice",
#   "document_type_name":"Invoice","status":"ready","progress":100,"page_count":3,"average_confidence":0.951,
#   "language":"en","thumbnail_url":"...","created_at":"...","updated_at":"..."}]}

curl -s -X PATCH $API/documents/$DOC/ -H "$AUTH" -H 'Content-Type: application/json' -d '{"title":"Contoso March"}'
curl -s -X DELETE $API/documents/$DOC/ -H "$AUTH"   # 204, deletes every stored file too

# after adding pages to a ready document
curl -s -X POST $API/documents/$DOC/reprocess/ -H "$AUTH"   # 202
```

For a browser SPA, prefer `?format=pdf&link=true` and open the returned URL: a plain `<a href>` cannot send
the JWT header.

---

## 7. Project layout

```
backend/
├── config/            settings/{base,dev,prod,test}.py, urls.py, celery.py, wsgi.py, asgi.py
├── accounts/          custom User, register / me, JWT URLs
├── documents/         models, serializers, views, urls, permissions, filters, services (writes),
│                      selectors (reads), signals (file cleanup), admin, management/commands/init_storage.py
├── processing/        azure_client, ocr, extractors, pdf_utils, image_utils, analysis, tasks
├── seed/document_types.json
├── tests/             pytest suite (Azure mocked)
├── requirements.txt, .env.example, docker-compose.yml, pytest.ini
```

On SQLite, `?q=` uses `icontains`; the `search_vector` column and GIN-index migration are only used on PostgreSQL.
