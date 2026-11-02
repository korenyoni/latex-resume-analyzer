# resume-analyzer

> [!NOTE]  
> This repository is a work in progress. Screen recordings and additional bug fixes are planned.

Builds and analyzes LaTeX resumes with a [deepagents](https://github.com/langchain-ai/deepagents)
agent running against any OpenAI-compatible LLM endpoint.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in your token
```

`.env` holds the credentials for the endpoint (provider-agnostic; OpenRouter
is the default):

```
OPENAI_COMPATIBLE_API_KEY=sk-...
# OPENAI_COMPATIBLE_BASE_URL=https://openrouter.ai/api/v1/   # optional override
```

Non-secret defaults (model, skills path, system prompt) live in
[`config/config.yaml`](config/config.yaml). The model defaults to
`moonshotai/kimi-k3`.

## Web app

```bash
docker compose up --build        # http://localhost:8000
```

Secrets come from `.env` / `.env.local` at runtime and are never baked into
the image. On a fresh volume the sample resume is copied into `uploads/` and
analyzed automatically. The sidebar lets you upload, delete, and re-analyze
resumes, with an optional extra instruction for each run.

- **PDF view** (pdf.js, vendored in `app/static/vendor/pdfjs`): findings are
  highlighted on the page. Red = issue, green = strength, amber underline = style.
  Click a highlight or card to see the original vs. rewrite.
- **Detected entities**: extracted by the analysis LLM from the resume text
  (verified against the source, plus regex for email/phone).
- **Export checklist**: downloads the suggestions as a markdown `- [ ]` checklist.

Local run without Docker: `.venv/bin/uvicorn app.web:app --reload --port 8000`.

## Usage

Validate the wiring without calling the API:

```bash
.venv/bin/python -m app.main --check
```

Analyze a PDF (must be inside the project; a sample fixture is included):

```bash
.venv/bin/python -m app.main tests/fixtures/example-resume.pdf
.venv/bin/python -m app.main uploads/my-resume.pdf --prompt "Score the formatting"
```

## Skills

Skills are [Agent Skills](https://agentskills.io/) directories under
`.agents/skills/`, loaded by the agent on demand.

- **extract-pdf** — extracts text/page structure from PDFs. The agent calls
  the `extract_pdf` tool (`app/tools.py`), which runs
  `.agents/skills/extract-pdf/scripts/extract_pdf.py` and saves `<name>.txt`
  next to the PDF.

Skill tools are gated: a skill lists them under `metadata.include_tools` in
its frontmatter, and the agent only sees them after reading that `SKILL.md`.
Register new tools in `build_agent` (`app/agent.py`).

To add a `score-*` skill (e.g. `score-format`, `score-sections`), create
`.agents/skills/score-<name>/SKILL.md` with YAML frontmatter (`name`,
`description`) and instructions. Instruction-only skills are picked up
automatically; skills that need a custom tool also need it registered.

## Project structure

```
.agents/skills/        agent skills (extract-pdf, future score-*)
app/
  config.py            Settings: config.yaml + env-var expansion
  agent.py             deepagents agent + ChatOpenAI + FilesystemBackend
  tools.py             skill-gated tools (extract_pdf)
  main.py              CLI entrypoint (--check, analyze a resume)
  web.py               FastAPI web app (uploads, analysis jobs)
  static/index.html    single-page UI (vanilla JS)
config/config.yaml     non-secret configuration
tests/fixtures/        sample PDF source files (main.tex)
uploads/               PDFs + analysis artifacts (gitignored / Docker volume)
docker-compose.yml     app service
Dockerfile             app image
```

## Roadmap

See [`design-spec.md`](design-spec.md).

## Credits

* Refer to: requirements.txt
* [LetX](https://letx.app/): thank you for making the [template resume](tests/fixtures/main.tex) publicly available.
* [pdf.js](https://github.com/mozilla/pdf.js)
* [Prism](https://github.com/PrismLibrary/Prism)