# Drive to Text

Paste a public Google Drive folder link, pick the PDFs and images you want, and get their text
back. Native PDFs are read from their text layer; images and scanned PDF pages go through OCR.

Built for the DI Xpertia technical assessment TA-2026-01.

![Selecting files from a public folder](docs/select-files.png)

## Features

- Accepts every public folder link shape (`/drive/folders/<id>`, `/drive/u/0/folders/<id>`,
  `/open?id=<id>`, with or without `?usp=sharing`), and gives a clear error for private or
  invalid links.
- Lists the PDF, PNG and JPEG files in the folder and its subfolders.
- Processes the selected files in parallel, with live per-file progress. One failing file
  never stops the others.
- Detects scanned PDF pages and OCRs them automatically, page by page, so mixed documents work.
- OCR in English, French and Arabic, alone or combined.
- Shows the text, method, pages, characters and time for each file, and exports the batch as JSON.
- Light and dark theme.

## Quick start

With Docker (includes Tesseract and the three language packs):

```bash
docker compose up --build       # http://localhost:8000
```

Or locally, with Python 3.11+ and [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)
installed (`sudo apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara` on Ubuntu):

```bash
git clone https://github.com/AzizBahloul/Drive-to-Text-Document-Extraction-POC.git
cd Drive-to-Text-Document-Extraction-POC
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```

`make install`, `make dev`, `make test`, `make lint` and `make docker` run the same commands.
The API reference is at `/api/docs`.

## How it works

```
drive link -> list files -> select -> batch
   |- PDF          -> text layer, page by page
   |     page with text    -> kept as is                       (pdf-parser)
   |     page without text -> rendered at 200 dpi -> Tesseract (ocr)
   `- PNG / JPEG   -> Tesseract                                (ocr)
results -> text per file, live status, JSON export
```

A PDF whose pages all have text is reported as `pdf-parser`, an image or fully scanned PDF as
`ocr`, and a PDF with both kinds of pages as `mixed`.

```
app/
  drive/        link parsing, folder listing (with subfolders), download
  extraction/   router, pdf_parser (pypdfium2), ocr (Tesseract)
  batch/        in-memory batches, bounded concurrency, progress events
  api/          REST routes and Server-Sent Events stream
  static/       the single-page frontend (HTML, CSS, JavaScript, no build step)
```

| Part | Choice | Why |
| --- | --- | --- |
| Backend | Python, FastAPI | Best open-source PDF and OCR ecosystem; async downloads and a simple SSE stream. |
| PDF | pypdfium2 | Fast text extraction and page rendering in one permissively licensed package. |
| OCR | Tesseract 5 | Free, self-hosted, supports French, English and Arabic. |
| Frontend | Vanilla HTML, CSS, JS | One page, nothing to build. |
| Progress | Server-Sent Events | One-way live updates without a WebSocket library. |

## Google Drive access

- **No key (default):** reads the public folder page. Works out of the box, but sizes are not
  shown and very large folders are cut at 50 entries.
- **With an API key (recommended):** set `GOOGLE_API_KEY` in `.env` (Google Cloud Console,
  Drive API enabled). Listing is exact and complete. No login is needed for public folders.

## Configuration

Everything is optional. Copy `.env.example` to `.env` to change it. The most useful settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `GOOGLE_API_KEY` | none | Use the Drive API instead of the public page |
| `MAX_FILE_SIZE_MB` | 25 | Larger files are refused |
| `MAX_PARALLEL_FILES` | 2 | Files processed at the same time |
| `OCR_DEFAULT_LANGUAGE` | eng | Preselected OCR language |
| `SUBFOLDER_DEPTH` | 3 | How deep subfolders are listed |
| `TESSDATA_DIR` | none | Folder with extra language packs, if not installed system-wide |

## Output

`GET /api/batches/{id}/export` (the "Download JSON" button) returns one entry per file:

```json
{
  "name": "contract.pdf", "status": "done", "method": "pdf-parser",
  "pages": [{ "page": 1, "method": "pdf-parser", "chars": 1834 }],
  "char_count": 1834, "duration_ms": 412, "error": null, "text": "…"
}
```

A full example is in [`samples/sample-output.json`](samples/sample-output.json).

![Reading the results](docs/results.png)

## Tests

```bash
pytest                                      # about 155 tests, a few seconds
ruff check app tests && ruff format --check app tests
```

They cover link parsing, routing, scanned-page detection, the Drive client against a mocked
Google, the batch manager, the HTTP API and the safety limits. Test files are generated on the
fly, so no binary fixtures are stored.

## Security

Files come from folders the user does not control, so they are treated as untrusted: oversized
pages are rendered at a reduced scale, OCR and each file have time limits, and the number of
running batches is capped. The server only answers to allowed hosts, sends strict security
headers, and the API key is never put in a URL. The API has no authentication, so Docker
publishes it on `127.0.0.1` only.

## Limits

- Batches live in memory and are lost on restart.
- OCR quality depends on the scan. Handwriting, low resolution and skew are hard; Arabic is
  weaker than French or English, and tables come out as plain lines.
- Google Docs and Sheets are ignored; only PDF, PNG and JPEG are read.

## Next steps

- Store batches in SQLite or Redis and run extraction in a worker.
- Support TIFF and WebP.
- Add a second OCR engine (PaddleOCR or docTR) for difficult scans.

## Licence

MIT, see [LICENSE](LICENSE). Third-party licences are listed in [LICENSES.md](LICENSES.md).
