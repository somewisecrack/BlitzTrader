#!/usr/bin/env python3
"""
extract_pdf_text.py — dependency-free PDF text extractor.

A pure-standard-library fallback for reading text out of a PDF when the usual
tools are unavailable or broken (no poppler/pdftotext, pypdf import failing on a
missing cryptography backend, etc.). Uses only zlib + base64 from the stdlib.

It decodes content streams through the common PDF filters
(FlateDecode, ASCII85Decode, ASCIIHexDecode, LZWDecode — applied in the order
declared by /Filter) and then extracts the strings drawn by the text operators
(Tj, TJ, ', "), inserting line breaks from T*/Td/TD positioning.

Usage:
    python3 extract_pdf_text.py FILE.pdf
    python3 extract_pdf_text.py FILE.pdf --raw   # dump decoded content streams

Limitations: does not resolve PDF 1.5+ cross-reference/object streams (/ObjStm),
so a minority of modern PDFs will yield little text — those need a real library.
This tool targets the common case of simple, single- or few-stream documents and
is meant as a last-resort reader, not a full parser.
"""
from __future__ import annotations

import base64
import re
import sys
import zlib


# ── Filters ─────────────────────────────────────────────────────────────────────


def _flate(data: bytes) -> bytes:
    return zlib.decompress(data)


def _ascii85(data: bytes) -> bytes:
    s = data.strip()
    if s.startswith(b"<~"):
        s = s[2:]
    s = s.split(b"~>")[0]
    return base64.a85decode(s, adobe=False)


def _asciihex(data: bytes) -> bytes:
    s = data.split(b">")[0]
    s = re.sub(rb"\s", b"", s)
    if len(s) % 2:
        s += b"0"
    return bytes.fromhex(s.decode("latin-1"))


def _lzw(data: bytes) -> bytes:
    """PDF LZWDecode (variable-width codes, early-change=1)."""
    out = bytearray()
    table = {i: bytes([i]) for i in range(256)}
    CLEAR, EOD = 256, 257
    next_code = 258
    width = 9
    prev = None
    bitbuf = 0
    bits = 0
    for byte in data:
        bitbuf = (bitbuf << 8) | byte
        bits += 8
        while bits >= width:
            bits -= width
            code = (bitbuf >> bits) & ((1 << width) - 1)
            if code == CLEAR:
                table = {i: bytes([i]) for i in range(256)}
                next_code = 258
                width = 9
                prev = None
                continue
            if code == EOD:
                return bytes(out)
            if prev is None:
                entry = table[code]
            elif code in table:
                entry = table[code]
            else:
                entry = table[prev] + table[prev][:1]
            out += entry
            if prev is not None:
                table[next_code] = table[prev] + entry[:1]
                next_code += 1
                # early change: widen one code before the table fills
                if next_code + 1 >= (1 << width) and width < 12:
                    width += 1
            prev = code
    return bytes(out)


_FILTERS = {
    "FlateDecode": _flate, "Fl": _flate,
    "ASCII85Decode": _ascii85, "A85": _ascii85,
    "ASCIIHexDecode": _asciihex, "AHx": _asciihex,
    "LZWDecode": _lzw, "LZW": _lzw,
}


def _decode_stream(raw: bytes, filters: list[str]) -> bytes:
    data = raw.strip(b"\r\n")
    for f in filters:
        fn = _FILTERS.get(f)
        if fn is None:
            continue  # unsupported filter (e.g. DCTDecode image) — skip
        data = fn(data)
    return data


# ── Stream discovery ─────────────────────────────────────────────────────────────


def _iter_streams(pdf: bytes):
    """Yield (decoded_bytes, filters) for every stream in the file, best-effort.

    The /Filter dict precedes the `stream` keyword; grab the nearest one behind it.
    """
    for m in re.finditer(rb"stream\b(.*?)\bendstream", pdf, re.DOTALL):
        raw = m.group(1)
        head = pdf[max(0, m.start() - 400):m.start()]
        fm = re.search(rb"/Filter\s*(\[[^\]]*\]|/\w+)", head)
        filters: list[str] = []
        if fm:
            filters = re.findall(r"/(\w+)", fm.group(1).decode("latin-1"))
        # try the declared filters; if that fails, try flate then a85+flate
        for attempt in (filters, ["FlateDecode"], ["ASCII85Decode", "FlateDecode"]):
            try:
                yield _decode_stream(raw, attempt), attempt
                break
            except Exception:
                continue


# ── Text operator extraction ─────────────────────────────────────────────────────


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "(": "(", ")": ")",
                        "\\": "\\"}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _hexstr(s: str) -> str:
    h = re.sub(r"\s", "", s)
    if len(h) % 2:
        h += "0"
    try:
        return bytes.fromhex(h).decode("latin-1")
    except ValueError:
        return ""


_STRING = r"(?:\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)"


def _string_val(tok: str) -> str:
    tok = tok.strip()
    if tok.startswith("("):
        return _unescape(tok[1:-1])
    if tok.startswith("<"):
        return _hexstr(tok[1:-1])
    return ""


def extract_text(content: str) -> str:
    """Pull drawn strings from a decoded content stream, in order."""
    lines: list[str] = []
    cur: list[str] = []

    def flush():
        if cur:
            lines.append("".join(cur))
            cur.clear()

    # Tokenise on the operators we care about.
    pat = re.compile(
        rf"(?P<TJ>\[(?:{_STRING}|[^\]])*\]\s*TJ)"
        rf"|(?P<Tj>{_STRING}\s*(?:Tj|'|\"))"
        r"|(?P<nl>T\*)"
        r"|(?P<td>[-\d.]+\s+[-\d.]+\s+(?:Td|TD))"
        r"|(?P<BT>\bBT\b)"
    )
    for m in pat.finditer(content):
        kind = m.lastgroup
        if kind == "TJ":
            body = m.group()[:-2]
            for tok in re.findall(_STRING, body):
                cur.append(_string_val(tok))
        elif kind == "Tj":
            cur.append(_string_val(re.match(_STRING, m.group()).group()))
        elif kind in ("nl", "td", "BT"):
            flush()
    flush()
    return "\n".join(ln for ln in lines if ln.strip())


# ── Main ─────────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    path = argv[0]
    raw_mode = "--raw" in argv[1:]
    with open(path, "rb") as fh:
        pdf = fh.read()

    decoded = [d for d, _ in _iter_streams(pdf)]
    if not decoded:
        print("No decodable streams found. This PDF likely uses object streams "
              "(/ObjStm) or image-only content — use a full PDF library.",
              file=sys.stderr)
        return 1

    if raw_mode:
        for i, d in enumerate(decoded):
            sys.stdout.write(f"\n===== STREAM {i} ({len(d)} bytes) =====\n")
            sys.stdout.write(d.decode("latin-1", "replace"))
        return 0

    parts = [extract_text(d.decode("latin-1", "replace")) for d in decoded]
    text = "\n".join(p for p in parts if p.strip())
    if not text.strip():
        print("Streams decoded but no text operators found — the document may be "
              "scanned images or use object streams. Try --raw to inspect.",
              file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
