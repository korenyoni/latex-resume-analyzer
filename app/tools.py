"""Custom tools exposed to the agent through skills.

Tools listed here are gated: the model only sees them after it reads a skill
whose frontmatter lists the tool name under `metadata.include_tools`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

EXTRACT_PDF_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".agents/skills/extract-pdf/scripts/extract_pdf.py"
)

# Cap the text returned to the model; the full text is always saved to disk.
MAX_RETURN_CHARS = 20_000
EXTRACTION_TIMEOUT_SECONDS = 120


def make_extract_pdf_tool(project_root: Path):
    """Build the extract_pdf tool bound to the project root.

    The tool accepts the agent's virtual paths ("/uploads/x.pdf") and maps
    them onto real files under the project root, refusing anything outside it.
    """

    root = project_root.resolve()

    @tool
    def extract_pdf(pdf_path: str) -> str:
        """Extract text from a PDF file.

        Args:
            pdf_path: Path to the PDF, e.g. "/uploads/resume.pdf" or
                "/tests/fixtures/example-resume.pdf".

        Returns:
            The extracted text with "--- page N ---" markers. A full copy is
            also saved as "<name>.txt" next to the PDF; if the result says it
            was truncated, read that .txt file for the rest.
        """
        host_path = (root / pdf_path.lstrip("/")).resolve()
        if not host_path.is_relative_to(root):
            return "error: path is outside the project root"
        if not host_path.is_file():
            return f"error: file not found: {pdf_path}"

        txt_path = host_path.with_suffix(".txt")
        try:
            proc = subprocess.run(
                [sys.executable, str(EXTRACT_PDF_SCRIPT), str(host_path), "-o", str(txt_path)],
                capture_output=True,
                text=True,
                timeout=EXTRACTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return "error: PDF extraction timed out"

        if proc.returncode != 0:
            return f"error: {proc.stderr.strip() or proc.stdout.strip()}"

        text = txt_path.read_text(encoding="utf-8", errors="replace")
        virtual_txt = "/" + txt_path.relative_to(root).as_posix()
        header = f"[extracted -> {virtual_txt}]"
        if len(text) > MAX_RETURN_CHARS:
            return f"{header}\n\n{text[:MAX_RETURN_CHARS]}\n\n[truncated: full text in {virtual_txt}]"
        return f"{header}\n\n{text}"

    return extract_pdf
