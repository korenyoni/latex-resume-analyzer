#!/usr/bin/env python3
"""Extract text from a PDF file using pypdf.

Usage:
    python3 extract_pdf.py <input.pdf> [-o <output.txt>] [--metadata]

Exit codes:
    0 - success
    1 - bad arguments / file not found
    2 - PDF could not be read (encrypted, corrupt)
    3 - no extractable text (likely a scanned/image PDF)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypdf import PdfReader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract text from a PDF file.")
    parser.add_argument("pdf", type=Path, help="Path to the input PDF")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Write extracted text here (default: stdout)")
    parser.add_argument("--metadata", action="store_true",
                        help="Also print PDF metadata and page count")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.pdf.is_file():
        print(f"error: file not found: {args.pdf}", file=sys.stderr)
        return 1

    try:
        reader = PdfReader(str(args.pdf))
        if reader.is_encrypted:
            if reader.decrypt("") == 0:  # try empty password
                print("error: PDF is password-protected (empty password failed)",
                      file=sys.stderr)
                return 2
        pages = reader.pages
    except Exception as exc:
        print(f"error: could not read PDF: {exc}", file=sys.stderr)
        return 2

    chunks: list[str] = []
    for i, page in enumerate(pages, start=1):
        text = page.extract_text() or ""
        chunks.append(f"--- page {i} ---\n{text.strip()}")

    full_text = "\n\n".join(chunks)
    if not any(c.split("\n", 1)[-1].strip() for c in chunks):
        print("error: no extractable text found (scanned/image PDF? OCR is not supported)",
              file=sys.stderr)
        return 3

    if args.metadata:
        meta = reader.metadata
        header = [
            f"pages: {len(pages)}",
            f"title: {getattr(meta, 'title', None) or ''}",
            f"author: {getattr(meta, 'author', None) or ''}",
            "",
        ]
        full_text = "\n".join(header) + full_text

    if args.output:
        args.output.write_text(full_text + "\n", encoding="utf-8")
        print(f"wrote {args.output} ({len(pages)} pages)")
    else:
        print(full_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
