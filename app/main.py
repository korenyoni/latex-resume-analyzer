"""Minimal CLI entrypoint.

Usage:
    python -m app.main --check                     # validate config + agent wiring
    python -m app.main uploads/resume.pdf          # analyze a PDF
    python -m app.main uploads/resume.pdf --prompt "Score the formatting"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.agent import build_agent, run_agent
from app.config import Settings

DEFAULT_PROMPT = (
    "Extract the text from this resume PDF using the extract-pdf skill, "
    "then summarize the candidate's experience, skills, and education."
)


def _to_virtual_path(settings: Settings, pdf: Path) -> str:
    """Map a host path inside the project to the agent's virtual path."""
    resolved = pdf.resolve()
    try:
        rel = resolved.relative_to(settings.project_root)
    except ValueError:
        sys.exit(
            f"Error: {resolved} is outside the project root.\n"
            "Place the PDF inside the project (e.g. uploads/) and retry."
        )
    return "/" + rel.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a resume PDF with the deep agent.")
    parser.add_argument("pdf", nargs="?", type=Path,
                        help="Path to a PDF inside the project (e.g. uploads/resume.pdf)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Instruction for the agent")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--check", action="store_true",
                        help="Load config and build the agent, then exit without calling the API")
    args = parser.parse_args()

    settings = Settings.load(args.config)
    agent = build_agent(settings)

    if args.check:
        print("Config OK. Agent built successfully.")
        print(f"  base_url:    {settings.llm.base_url}")
        print(f"  model:       {settings.llm.model}")
        print(f"  skills_path: {settings.agent.skills_path}")
        print(f"  api_key:     {'set' if settings.llm.api_key else 'MISSING (set OPENAI_COMPATIBLE_API_KEY in .env)'}")
        return 0

    if args.pdf is None:
        parser.error("pdf is required unless --check is given")
    if not args.pdf.is_file():
        sys.exit(f"Error: {args.pdf} does not exist")
    if not settings.llm.api_key:
        sys.exit("Error: OPENAI_COMPATIBLE_API_KEY is not set. Copy .env.example to .env and add your token.")

    virtual_path = _to_virtual_path(settings, args.pdf)
    prompt = f"{args.prompt}\n\nThe resume PDF is at virtual path: {virtual_path}"
    print(run_agent(agent, prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
