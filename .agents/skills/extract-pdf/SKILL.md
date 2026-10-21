---
name: extract-pdf
description: Extract text, page structure, and metadata from PDF files (resumes, CVs, job descriptions). Use whenever the user uploads or references a PDF, mentions a resume/CV, or asks to read, parse, or analyze a document.
metadata:
  include_tools: extract_pdf
---

# extract-pdf

## Overview

Extracts text from PDF files (pypdf-based, no external services). Produces
plain text with page markers so you can reference page numbers when analyzing
the document.

## Instructions

### 1. Locate the PDF

Determine which PDF the user means. Uploaded files live under `/uploads/`.
If the path is ambiguous, list `/uploads/` first or ask the user.

### 2. Extract the text

Call the `extract_pdf` tool with the PDF's path (e.g.
`/tests/fixtures/example-resume.pdf` or `/uploads/resume.pdf`).

The tool returns the extracted text with `--- page N ---` markers and saves a
full copy as `<name>.txt` next to the PDF. Long documents are truncated in the
tool result; read the saved `.txt` file for the full text.

### 3. Analyze the text

Proceed with the user's task (summarize, analyze, score, etc.). Quote exact
lines and cite page numbers when giving feedback. If the tool reports the
output was truncated, read the saved `.txt` file for the full text.

### 4. Fallback: run the script directly

If the `extract_pdf` tool is unavailable but you have a shell tool, run the
bundled script instead (paths relative to the project root, which is your
filesystem root):

```bash
python3 .agents/skills/extract-pdf/scripts/extract_pdf.py "uploads/<file>.pdf" -o "uploads/<file>.txt"
```

- Omit `-o` to print to stdout instead of writing a file.
- Use `--metadata` to also print PDF metadata (title, author, page count).
- The script exits non-zero with a clear message on failure (missing file,
  encrypted PDF, no extractable text).

### 5. Handle failures

- **Encrypted/password-protected PDF**: the script tries an empty password;
  if it still fails, tell the user to provide an unprotected copy.
- **No extractable text**: the PDF is probably scanned images. Tell the user
  OCR is required and that it is not supported yet.
- **Tool unavailable and no shell**: as a last resort, `read_file` on the PDF
  may return the document as a file content block you can read directly.
  Otherwise, ask the user to paste the resume text directly.
