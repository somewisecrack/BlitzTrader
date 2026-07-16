---
name: pdf-reader
description: Extract text from a PDF file, including when the Read tool can't render it (no poppler/pdftoppm installed) or pypdf/pdfplumber fail to import (e.g. broken cryptography backend). Use whenever you need the text of a .pdf — reports, docs, statements, papers — and the normal path errors out or returns nothing. Falls back to a dependency-free extractor that decodes ASCII85/Flate/LZW/Hex streams and pulls the text operators.
---

# Reading PDFs

Read a PDF's text using the first method that works, in this order. Don't stop at
the first failure — fall through to the next.

## 1. Try the Read tool first
Call `Read` on the `.pdf` path (optionally with `pages`). If it returns text,
you're done. It fails in this environment when `pdftoppm`/poppler is missing
(error: "pdftoppm is not installed") — fall through.

## 2. Try pdftotext / a library, only if already present
`which pdftotext` → if present, `pdftotext FILE.pdf -` is fastest and most
accurate. A working `pypdf`/`pdfplumber` is also fine. Do **not** burn time
installing these — in this sandbox `pip install pypdf` pulls `cryptography`,
whose `_cffi_backend` is broken, so the import fails. If they're not already
installed, skip to step 3.

## 3. Use the bundled dependency-free extractor (reliable fallback)
```
python3 .claude/skills/pdf-reader/extract_pdf_text.py FILE.pdf
```
Stdlib-only (zlib + base64). It decodes content streams through the common
filters (`FlateDecode`, `ASCII85Decode`, `ASCIIHexDecode`, `LZWDecode`, applied
in `/Filter` order) and extracts the strings drawn by the text operators
(`Tj`, `TJ`, `'`, `"`), with line breaks from `T*`/`Td`/`TD`.

If it prints little or nothing, inspect the decoded streams directly:
```
python3 .claude/skills/pdf-reader/extract_pdf_text.py FILE.pdf --raw
```

## Limitations
The fallback does **not** resolve PDF 1.5+ cross-reference/object streams
(`/ObjStm`) or OCR scanned images. If `--raw` shows only binary/image data or no
content streams, the PDF genuinely needs a full library (pypdf/pdfminer/mutool)
or OCR — say so rather than guessing at the content. Never fabricate text you
could not extract.

## Tips
- Large PDFs: pipe through `| head`/`sed -n` to page through the output.
- To pull just tables/numbers, extract all text first, then grep the result.
- The extractor is generic — copy it anywhere; it has no repo dependencies.
