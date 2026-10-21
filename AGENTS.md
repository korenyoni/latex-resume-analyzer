# AGENTS.md

Instructions for coding agents (Kilo and others) working in this repo.

## Secrets — read this first

- **NEVER read, print, copy, or commit `.env.local` or any `.env*` file.**
  They contain real API tokens. This applies to every tool: file reads, grep,
  glob, shell commands (`cat`, `head`, `env`, `printenv`), and git operations.
- The only env file you may read is `.env.example` — it documents the
  variable names (`OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`)
  with placeholder values.
- If you need to check whether a variable is set, inspect the empty/unset
  state (e.g. `[ -n "$VAR" ]`) — never echo the value.
- If a secret ever appears in tool output or a file you're writing, stop and
  flag it to the user instead of proceeding.

## Project layout

- `app/` — the agent application (`config.py` settings, `agent.py` deepagents
  wiring, `tools.py` skill-gated tools, `main.py` CLI, `web.py` web app).
- `config/config.yaml` — non-secret configuration; secrets enter via env-var
  placeholders (`${OPENAI_COMPATIBLE_API_KEY:-}`), expanded from the
  environment / `.env` / `.env.local`.
- `.agents/skills/` — Agent Skills directories (`extract-pdf`, future
  `score-*`). New skills are added as `SKILL.md` directories here; no code
  changes needed.
- `tests/fixtures/` — sample LaTeX resume (`main.tex`).
- LaTeX is compiled with **tectonic** (in the Docker image; `brew install
  tectonic` for local runs). Only `.tex` uploads are accepted.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Validate wiring (no API call): `.venv/bin/python -m app.main --check`
- Run the agent on a compiled resume: `.venv/bin/python -m app.main uploads/main.pdf`
- Web app (local): `.venv/bin/uvicorn app.web:app --reload --port 8000`
- Web app (Docker): `docker compose up --build` → http://localhost:8000
  (seeds tests/fixtures/main.tex on a fresh volume)
- Compile a .tex directly: `tectonic tests/fixtures/main.tex`

## Conventions

- Never commit secrets; `.env*` files are gitignored (`.env.example` excepted).
- Keep changes minimal and follow the existing module layout.
