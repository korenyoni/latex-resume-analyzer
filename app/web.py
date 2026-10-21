"""Minimal web UI: upload PDFs, run the agent, show scores and suggestions.

Run locally:
    .venv/bin/uvicorn app.web:app --reload --port 8000
Or in Docker:
    docker compose up --build
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import build_agent, run_agent
from app.config import PROJECT_ROOT, Settings

UPLOADS_DIR = PROJECT_ROOT / "uploads"
FIXTURE_TEX = PROJECT_ROOT / "tests" / "fixtures" / "main.tex"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

ANALYZE_PROMPT = """\
Use the extract-pdf skill to extract the text from the resume PDF, then analyze it
as a resume for a software engineer.

Reply with ONLY a JSON object (no prose, no code fences) of this shape:

{{
  "overall": {{"score": <0-10 number>, "verdict": "<one line>"}},
  "sections": [{{"name": "<Contact|Summary|Experience|Projects|Skills|Education|Formatting>",
                 "score": <0-10>, "note": "<short>"}}],
  "entities": [{{"label": "<Name|Email|Phone|Location|Link|Company|Title|Skill|Degree|School|Graduation Year>",
                 "text": "<exact text as it appears in the resume>"}}],
  "findings": [{{
    "type": "issue" | "strength" | "style",
    "severity": "high" | "medium" | "low",
    "section": "<section name>",
    "quote": "<EXACT contiguous text copied from the extracted resume text>",
    "title": "<short title>",
    "detail": "<what is wrong / why it is good, and why it matters>",
    "rewrite": "<improved version of the quoted text, or empty for strengths>"
  }}]
}}

Rules for findings:
- 6-12 findings, most important first; include 2-4 strengths ("type": "strength").
- "quote" MUST be copied character-for-character from the extracted text and
  stay on ONE line of that text (no line breaks). Keep it short: the key
  phrase or one bullet line, at most ~120 characters. It is used to highlight
  the text on the PDF, so paraphrased quotes are useless.
- Issues about something MISSING (e.g. no GitHub link) quote the nearest
  related text (e.g. the contact line) so they can still be placed.
- "rewrite" for an issue replaces exactly the quoted text, in ready-to-paste
  LaTeX (keep \\item / command structure if the source uses it).

Rules for entities:
- Every distinct employer/company, job title, skill, school, degree, the
  candidate name, and each contact detail (email, phone, location, links).
- "text" must be copied from the extracted text. No duplicates. 10-40 entries.

The resume PDF is at virtual path: {path}
"""

VISUAL_PROMPT = """\
You are reviewing the LAYOUT of a software engineer's resume. The page images
are attached in order. Judge only what you can SEE, not the wording.

Reply with ONLY a JSON object: {"findings": [{
  "page": <1-based page number>,
  "severity": "high"|"medium"|"low",
  "title": "<short>",
  "detail": "<what looks off visually and why it matters>",
  "quote": "<exact visible text this is about, one line, or empty>"
}]}

Rules: 3-8 findings about layout only — density, whitespace, alignment,
column structure, font consistency, date alignment, readability, and ATS risk
(tables, icons, text-in-images). "quote" must copy the exact text you see on
the page if the finding is about a specific element.
"""

# Model output fields we keep; anything else is dropped.
_FINDING_FIELDS = ("type", "severity", "section", "quote", "title", "detail", "rewrite")


class AnalyzeBody(BaseModel):
    instruction: str = ""


class SettingsBody(BaseModel):
    main_model: str = ""
    vision_model: str = ""


# --- Global model settings (persisted in Postgres) ---------------------------

def read_model_settings() -> dict:
    from app import db
    return db.get_setting("models", {})


def write_model_settings(data: dict) -> None:
    from app import db
    db.set_setting("models", data)


def effective_settings() -> Settings:
    """Config defaults overlaid with the saved model selection."""
    s = Settings.load()
    saved = read_model_settings()
    if saved.get("main_model"):
        s = dataclasses.replace(s, llm=dataclasses.replace(s.llm, model=saved["main_model"]))
    if saved.get("vision_model"):
        s = dataclasses.replace(s, llm=dataclasses.replace(s.llm, vision_model=saved["vision_model"]))
    return s


_models_cache: list[dict] = []
_models_cache_at = 0.0


def list_models() -> list[dict]:
    """Model list from the endpoint (cached 10 min). [{id, image}]"""
    global _models_cache, _models_cache_at
    now = time.time()
    if _models_cache and now - _models_cache_at < 600:
        return _models_cache
    import urllib.request

    s = effective_settings()
    req = urllib.request.Request(
        s.llm.base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {s.llm.api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - fall back to the configured models
        return [{"id": s.llm.model, "image": False},
                {"id": s.llm.vision_model, "image": True}]
    out = []
    for m in data.get("data", []):
        mods = (m.get("architecture") or {}).get("input_modalities") or []
        out.append({"id": m.get("id", ""), "image": "image" in mods})
    _models_cache, _models_cache_at = out, now
    return out


# --- LaTeX (tectonic) --------------------------------------------------------

TECTONIC = shutil.which("tectonic")
TEX_TIMEOUT_SECONDS = 120


def compile_tex(source: bytes) -> tuple[bytes, str]:
    """Compile LaTeX source to PDF bytes. Returns (pdf, log_tail)."""
    if not TECTONIC:
        raise HTTPException(status_code=503,
                            detail="LaTeX compiler (tectonic) is not installed. "
                                   "Use the Docker image or `brew install tectonic`.")
    with tempfile.TemporaryDirectory() as td:
        tex = Path(td) / "resume.tex"
        tex.write_bytes(source)
        try:
            proc = subprocess.run(
                [TECTONIC, "--outdir", td, str(tex)],
                capture_output=True, text=True, timeout=TEX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=422, detail="LaTeX compile timed out")
        pdf = Path(td) / "resume.pdf"
        if proc.returncode != 0 or not pdf.is_file():
            log = "\n".join(
                ln for ln in ((proc.stderr or "") + (proc.stdout or "")).splitlines()
                if "Fontconfig" not in ln  # tectonic warning noise, not an error
            )
            raise HTTPException(status_code=422, detail=f"LaTeX compile failed:\n{log[-1500:]}")
        return pdf.read_bytes(), (proc.stderr or "")[-500:]


def _unique_path(stem: str, ext: str) -> Path:
    stem = re.sub(r"[^\w .-]+", "_", stem).strip(" .") or "resume"
    candidate = UPLOADS_DIR / f"{stem}.{ext}"
    i = 1
    while candidate.exists():
        i += 1
        candidate = UPLOADS_DIR / f"{stem}-{i}.{ext}"
    return candidate


def _tex_path(pdf: Path) -> Path:
    return pdf.parent / f"{pdf.name[:-4]}.tex"


# --- Visual inspection (vision model over page images) ----------------------

def render_page_images(pdf_path: Path, max_pages: int = 4) -> list[bytes]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    images = []
    for i in range(min(len(doc), max_pages)):
        img = doc[i].render(scale=1.6).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        images.append(buf.getvalue())
    return images


def visual_findings(pdf_path: Path, settings: Settings, source_text: str) -> list[dict] | None:
    """Layout findings from the vision model, or None if unavailable."""
    if not settings.llm.vision_model or not settings.llm.api_key:
        return None
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    images = render_page_images(pdf_path)
    if not images:
        return None
    model = ChatOpenAI(
        model=settings.llm.vision_model,
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        temperature=0.2,
        timeout=180,
    )
    content: list[dict] = [{"type": "text", "text": VISUAL_PROMPT}]
    for png in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
        })
    raw = model.invoke([HumanMessage(content=content)]).content
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    m = re.search(r"\{.*\}", str(raw), re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group())
    except ValueError:
        return []
    norm_source = _norm(source_text)
    out = []
    for i, f in enumerate(data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        quote = str(f.get("quote") or "").strip()
        out.append({
            "id": f"v{i + 1}", "type": "visual",
            "severity": f.get("severity") if f.get("severity") in ("high", "medium", "low") else "medium",
            "section": "", "page": f.get("page") or 1,
            "quote": quote, "title": str(f.get("title") or "")[:80],
            "detail": str(f.get("detail") or ""), "rewrite": "",
            "anchored": bool(quote) and _norm(quote) in norm_source,
        })
    return out


class AnalysisNotFound(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=404, detail="No analysis yet")


def _pdf_path(name: str) -> Path:
    """Resolve a resume name safely inside uploads/."""
    if not re.fullmatch(r"[\w][\w .-]*\.pdf", name):
        raise HTTPException(status_code=400, detail="Invalid resume name")
    path = (UPLOADS_DIR / name).resolve()
    if not path.is_relative_to(UPLOADS_DIR.resolve()) or not path.is_file():
        raise AnalysisNotFound()
    return path


def _status_path(pdf: Path) -> Path:  # legacy sidecars, cleaned up on delete
    return pdf.parent / f"{pdf.name[:-4]}.analysis.json"


def read_status(pdf: Path) -> dict:
    from app import db
    return db.read_analysis(pdf.name)


def write_status(pdf: Path, status: dict) -> None:
    from app import db
    if not pdf.is_file():  # deleted mid-run: don't resurrect rows
        return
    db.write_analysis(pdf.name, status)
    notify({"type": "update", "name": pdf.name})


def _unique_pdf_path(name: str) -> Path:
    """Sanitized, collision-free uploads/<name>.pdf path."""
    stem = re.sub(r"[^\w .-]+", "_", Path(name).stem).strip(" .") or "resume"
    candidate = UPLOADS_DIR / f"{stem}.pdf"
    i = 1
    while candidate.exists():
        i += 1
        candidate = UPLOADS_DIR / f"{stem}-{i}.pdf"
    return candidate


MAX_HISTORY = 20


def _history_path(pdf: Path) -> Path:
    return pdf.parent / f"{pdf.name[:-4]}.history.json"


def read_history(pdf: Path) -> list[dict]:
    from app import db
    return db.get_history(pdf.name)


def append_history(pdf: Path, run: dict) -> None:
    if not pdf.is_file():
        return
    from app import db
    db.add_history(pdf.name, run)


def _norm(s: str) -> str:
    """Normalize for quote matching: ligatures, quotes/dashes, whitespace."""
    s = (s.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\ufb00", "ff")
          .replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"')
          .replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-"))
    return re.sub(r"\s+", " ", s).strip().lower()


def _extract_json(raw: str) -> dict:
    """Pull the analysis JSON out of a model reply.

    Handles: bare JSON, ```json fenced blocks, prose around the object, and
    multiple/trailing braces. Uses a balanced-brace scan so a greedy regex
    can't swallow trailing prose, and tries every candidate it finds.
    """
    candidates = [raw]
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.S):
        candidates.append(m.group(1))
    # Balanced-brace scan that ignores braces inside JSON strings, so a
    # quote value like "x{" can't break the nesting count.
    for start in (i for i, c in enumerate(raw) if c == "{"):
        depth, in_str, esc = 0, False, False
        for j in range(start, len(raw)):
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:j + 1])
                    break
    for cand in candidates:
        cand = cand.strip()
        if not cand.startswith("{"):
            continue
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("model did not return JSON")


def parse_analysis(raw: str, source_text: str) -> dict:
    """Parse the model's JSON reply and keep only findings with real quotes."""
    data = _extract_json(raw)
    norm_source = _norm(source_text)
    findings = []
    for i, f in enumerate(data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        f = {k: str(f.get(k) or "").strip() for k in _FINDING_FIELDS}
        if f["type"] not in ("issue", "strength", "style"):
            f["type"] = "issue"
        if f["severity"] not in ("high", "medium", "low"):
            f["severity"] = "medium"
        f["id"] = f"f{i + 1}"
        # Unverifiable quotes still show in the list, just not highlighted.
        f["anchored"] = bool(f["quote"]) and _norm(f["quote"]) in norm_source
        findings.append(f)
    overall = data.get("overall") or {}
    return {
        "overall": {"score": overall.get("score"), "verdict": str(overall.get("verdict") or "")},
        "sections": [
            {"name": str(s.get("name") or ""), "score": s.get("score"), "note": str(s.get("note") or "")}
            for s in (data.get("sections") or []) if isinstance(s, dict)
        ],
        "findings": findings,
        "entities": _clean_entities(data.get("entities") or [], source_text),
    }


# Regexes are more reliable than any model for these two.
_REGEX_ENTITIES = (
    ("Email", r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    ("Phone", r"\(?\+?\d[\d\s().-]{7,}\d"),
)


def _clean_entities(raw: list, source_text: str) -> list[dict]:
    norm_source = _norm(source_text)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, text: str) -> None:
        text = re.sub(r"\s+", " ", str(text)).strip()
        if not text or len(text) > 80:
            return
        key = (label, _norm(text))
        if key in seen or _norm(text) not in norm_source:
            return  # hallucinated or duplicated
        seen.add(key)
        out.append({"label": label, "text": text})

    for e in raw:
        if isinstance(e, dict):
            add(str(e.get("label") or ""), e.get("text") or "")
    for label, rx in _REGEX_ENTITIES:
        for m in re.finditer(rx, source_text):
            add(label, m.group())
    return out


def _analyze(pdf: Path, instruction: str, settings: Settings, agent) -> None:
    virtual = "/" + pdf.relative_to(PROJECT_ROOT).as_posix()
    prompt = ANALYZE_PROMPT.format(path=virtual)
    if instruction.strip():
        prompt += f"\nAdditional instruction from the user: {instruction.strip()}\n"
    try:
        raw = run_agent(agent, prompt)
        txt = pdf.parent / f"{pdf.name[:-4]}.txt"  # written by the extract_pdf tool
        source = txt.read_text(encoding="utf-8", errors="replace") if txt.is_file() else ""
        try:
            analysis = parse_analysis(raw, source)
        except ValueError as exc:  # includes JSONDecodeError
            write_status(pdf, {"status": "error",
                               "error": f"Could not parse analysis: {exc}",
                               "raw_preview": str(raw)[:800]})
            return
        analysis["models"] = {"main": settings.llm.model,
                              "vision": settings.llm.vision_model or None}
        # Match every finding to its .tex source line so the UI can jump there.
        tex_file = _tex_path(pdf)
        tex_source = tex_file.read_text(encoding="utf-8", errors="replace") if tex_file.is_file() else ""
        for f in analysis["findings"]:
            ln = find_tex_line(tex_source, f["quote"]) if tex_source and f["quote"] else -1
            f["tex_line"] = ln + 1 if ln >= 0 else None
        visual_error = None
        try:
            visual = visual_findings(pdf, settings, source)
            if visual:
                analysis["findings"] += visual
        except Exception as exc:  # noqa: BLE001 - visual feedback is best-effort
            visual_error = f"Visual inspection failed: {exc}"
        if visual_error:
            analysis["visual_error"] = visual_error
        now = time.time()
        ov = analysis.get("overall") or {}
        note = "Analysis complete"
        if ov.get("score") is not None:
            note += f" \u2014 {ov['score']}/10"
        if ov.get("verdict"):
            note += f": {ov['verdict']}"
        from app import db
        db.add_chat(pdf.name, "system", note)
        write_status(pdf, {"status": "done", "analysis": analysis,
                           "result": None, "error": None})
        append_history(pdf, {
            "id": f"{int(now * 1000)}",
            "finished_at": now,
            "model": settings.llm.model,
            "instruction": instruction.strip(),
            "analysis": analysis,
        })
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        write_status(pdf, {"status": "error", "error": str(exc)})


# --- Checklist export -------------------------------------------------------

def _md_sections(text: str) -> dict[str, str]:
    """Split analysis markdown into {'overall': ..., 'suggestions': ...} by '## ' headings."""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            current = m.group(1).strip().lower()
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return out


def build_checklist(name: str, run: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.get("finished_at", 0)))
    a = run.get("analysis")
    if a:  # structured findings
        lines = [f"# Resume checklist: {name}", ""]
        ov = a.get("overall") or {}
        if ov.get("score") is not None:
            lines += [f"**Overall:** {ov['score']}/10 - {ov.get('verdict', '')}", ""]
        lines += [f"_Model: {run.get('model', '?')} · {when}_", "", "## To do"]
        issues = [f for f in a.get("findings", []) if f["type"] != "strength"]
        for f in issues:
            lines.append(f"- [ ] **{f['section']}: {f['title']}** ({f['severity']})")
            if f.get("quote"):
                lines.append(f"  - Original: \"{f['quote']}\"")
            if f.get("detail"):
                lines.append(f"  - Why: {f['detail']}")
            if f.get("rewrite"):
                lines.append(f"  - Rewrite: \"{f['rewrite']}\"")
        if not issues:
            lines.append("- [x] Nothing to fix")
        keep = [f for f in a.get("findings", []) if f["type"] == "strength"]
        if keep:
            lines += ["", "## Keep"] + [f"- **{f['title']}**: {f['detail']}" for f in keep]
        return "\n".join(lines).rstrip() + "\n"

    # Legacy markdown runs (before structured findings).
    parts = _md_sections(run.get("result", ""))
    lines = [f"# Resume checklist: {name}", ""]
    overall = parts.get("overall", "").strip()
    if overall:
        lines += [f"**Overall:** {overall.splitlines()[0]}", ""]
    lines += [f"_Model: {run.get('model', '?')} · {when}_", ""]

    # Each "### n. Area: title" block becomes one checkbox with its details.
    blocks = re.split(r"^###\s+", parts.get("suggestions", ""), flags=re.M)
    items = [b.strip() for b in blocks if b.strip()]
    lines.append("## To do")
    if items:
        for block in items:
            title, *rest = block.splitlines()
            title = re.sub(r"^\d+\.\s*", "", title).strip()
            lines.append(f"- [ ] **{title}**")
            lines += [f"  {ln.strip()}" for ln in rest if ln.strip()]
    else:
        lines.append("- [ ] _(no structured suggestions found; see full analysis below)_")
    lines.append("")

    strengths = [ln.strip()[2:] for ln in parts.get("strengths", "").splitlines()
                 if ln.strip().startswith("- ")]
    if strengths:
        lines.append("## Keep")
        lines += [f"- {s}" for s in strengths]
        lines.append("")

    if not items:
        lines += ["## Full analysis", "", run.get("result", "")]
    return "\n".join(lines).rstrip() + "\n"


def _kick_analyze(pdf: Path, instruction: str, prev_analysis: dict | None = None) -> None:
    settings = effective_settings()
    if not settings.llm.api_key:
        write_status(pdf, {
            "status": "error",
            "error": "OPENAI_COMPATIBLE_API_KEY is not set (see .env.example).",
        })
        return
    agent = _STATE.agent or build_agent(settings)
    fields: dict = {"status": "running", "error": None, "model": settings.llm.model}
    if prev_analysis is not None:
        fields["analysis"] = prev_analysis
    write_status(pdf, fields)

    def job() -> None:
        _analyze(pdf, instruction, settings, agent)

    _STATE.pool.submit(job)


class _State:
    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analyze")
        # Single worker: accepted edits must compile in submission order so
        # rapid accepts chain onto each other instead of racing.
        self.compile_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compile")
        self.agent = None
        self.lock = threading.Lock()
        self.sockets: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.redis = None


_STATE = _State()


async def _broadcast(msg: dict) -> None:
    for ws in list(_STATE.sockets):
        try:
            await ws.send_json(msg)
        except Exception:  # noqa: BLE001 - drop dead sockets
            _STATE.sockets.discard(ws)


def notify(msg: dict) -> None:
    """Push an event to all connected websockets (safe from worker threads).

    Goes through Redis pub/sub when available so multiple app instances
    would also reach each other's clients; falls back to direct broadcast.
    """
    if _STATE.loop is None:
        return
    if _STATE.redis is not None:
        try:
            _STATE.redis.publish("events", json.dumps(msg))
            return
        except Exception:  # noqa: BLE001 - fall back to direct broadcast
            pass
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(msg), _STATE.loop)
    except RuntimeError:  # loop closed during shutdown
        pass


def _start_redis() -> None:
    """Connect Redis and start the pub/sub -> websocket pump thread."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(url, socket_connect_timeout=2)
        client.ping()
        _STATE.redis = client
    except Exception:  # noqa: BLE001 - Redis is optional
        _STATE.redis = None
        return

    def pump() -> None:
        ps = client.pubsub(ignore_subscribe_messages=True)
        ps.subscribe("events")
        for message in ps.listen():
            if message.get("type") != "message":
                continue
            try:
                msg = json.loads(message["data"])
            except (ValueError, TypeError):
                continue
            try:
                asyncio.run_coroutine_threadsafe(_broadcast(msg), _STATE.loop)
            except RuntimeError:
                return

    threading.Thread(target=pump, daemon=True, name="redis-pump").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app import db

    db.init_db()
    _STATE.loop = asyncio.get_running_loop()
    _start_redis()
    with _STATE.lock:
        _STATE.agent = build_agent(effective_settings())
    # Finish edits a restart interrupted (durable accept pipeline), including
    # pdf refreshes lost mid-flight. Must run before re-kicking analyses below.
    _recover_pending_edits()
    # Analyses interrupted by a restart can never finish. Must run before
    # seeding, or it would flag the freshly kicked seed analysis.
    interrupted = {p.name: read_status(p) for p in UPLOADS_DIR.glob("*.pdf")}
    db.sweep_interrupted()
    for name, st in interrupted.items():
        if st.get("status") == "running":
            # Re-kick instead of leaving an error: the source on disk is what
            # the user last saw, so just analyze it again.
            _kick_analyze(_pdf_path(name), st.get("instruction") or "",
                          prev_analysis=st.get("analysis"))
    # Old markdown analyses can't be highlighted; re-run them as structured.
    for pdf in UPLOADS_DIR.glob("*.pdf"):
        st = read_status(pdf)
        if st.get("status") == "done" and not st.get("analysis"):
            _kick_analyze(pdf, "")
    # Seed the sample .tex on a fresh volume: compile it and analyze.
    if FIXTURE_TEX.is_file() and not list(UPLOADS_DIR.glob("*.tex")):
        tex = UPLOADS_DIR / FIXTURE_TEX.name
        try:
            pdf_bytes, _ = compile_tex(FIXTURE_TEX.read_bytes())
            tex.write_bytes(FIXTURE_TEX.read_bytes())
            (UPLOADS_DIR / "main.pdf").write_bytes(pdf_bytes)
            _kick_analyze(UPLOADS_DIR / "main.pdf", "")
        except HTTPException:
            pass  # tectonic missing or fixture broken: skip seeding

    yield
    _STATE.pool.shutdown(wait=False, cancel_futures=True)
    _STATE.compile_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="resume-analyzer", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    _STATE.sockets.add(socket)
    try:
        while True:
            await socket.receive_text()  # ignore client pings
    except WebSocketDisconnect:
        pass
    finally:
        _STATE.sockets.discard(socket)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/model")
def model() -> str:
    return effective_settings().llm.model


@app.get("/api/models")
def models() -> list[dict]:
    return list_models()


@app.get("/api/settings")
def get_settings() -> dict:
    s = effective_settings()
    return {"main_model": s.llm.model, "vision_model": s.llm.vision_model or ""}


@app.put("/api/settings")
def put_settings(body: SettingsBody) -> dict:
    ids = {m["id"] for m in list_models()}
    data = read_model_settings()
    if body.main_model:
        if ids and body.main_model not in ids:
            raise HTTPException(status_code=400, detail="Unknown main model")
        data["main_model"] = body.main_model
    if body.vision_model:
        if ids and body.vision_model not in ids:
            raise HTTPException(status_code=400, detail="Unknown vision model")
        data["vision_model"] = body.vision_model
    write_model_settings(data)
    with _STATE.lock:
        _STATE.agent = None  # rebuild with the new model on next analysis
    return {"main_model": data.get("main_model", Settings.load().llm.model),
            "vision_model": data.get("vision_model", Settings.load().llm.vision_model)}


@app.get("/api/files")
def list_files() -> list[dict]:
    files = []
    for pdf in sorted(UPLOADS_DIR.glob("*.pdf")):
        st = read_status(pdf)
        files.append({
            "name": pdf.name,
            "size_bytes": pdf.stat().st_size,
            "status": st.get("status", "new"),
            "updated_at": st.get("updated_at"),
            "error": st.get("error"),
            "tex": _tex_path(pdf).is_file(),
        })
    return files


@app.post("/api/files")
async def upload(file: UploadFile) -> dict:
    """Upload a .tex source, compile it to PDF, and analyze the PDF."""
    if not (file.filename or "").lower().endswith(".tex"):
        raise HTTPException(status_code=400, detail="Only .tex sources are accepted")
    return await upload_tex(file)


@app.post("/api/tex")
async def upload_tex(file: UploadFile) -> dict:
    """Upload a .tex source, compile it to PDF, and analyze the PDF."""
    filename = file.filename or "resume.tex"
    if not filename.lower().endswith(".tex"):
        raise HTTPException(status_code=400, detail="Expected a .tex file")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB")
    stem = _unique_path(Path(filename).stem, "tex").stem
    tex_path = UPLOADS_DIR / f"{stem}.tex"
    pdf, _log = compile_tex(data)
    pdf_path = UPLOADS_DIR / f"{stem}.pdf"
    tex_path.write_bytes(data)
    pdf_path.write_bytes(pdf)
    _kick_analyze(pdf_path, "")
    return {"name": pdf_path.name, "status": "running"}


@app.post("/api/files/{name}/recompile")
def recompile(name: str) -> dict:
    pdf = _pdf_path(name)
    tex = _tex_path(pdf)
    if not tex.is_file():
        raise HTTPException(status_code=404, detail="No LaTeX source for this resume")
    pdf_bytes, _log = compile_tex(tex.read_bytes())
    pdf.write_bytes(pdf_bytes)
    _kick_analyze(pdf, "")
    return {"name": name, "status": "running"}


class TexBody(BaseModel):
    source: str


class ApplyBody(BaseModel):
    finding_id: str


class ChatBody(BaseModel):
    message: str


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def find_tex_line(source: str, quote: str) -> int:
    """Best-matching source line for a quote from the compiled PDF text."""
    words = [w for w in _norm_line(quote).split() if len(w) > 2][:10]
    if not words:
        return -1
    best, best_score = -1, 0.0
    for i, line in enumerate(source.splitlines()):
        norm = _norm_line(re.sub(r"(?<!\\)%.*$", "", line))
        norm = _norm_line(re.sub(r"\\[a-zA-Z]+\*?|\\.", " ", norm))
        score = sum(1 for w in words if w in norm) / len(words)
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.5 else -1


_LIG = {"\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff", "\ufb03": "ffi", "\ufb04": "ffl"}


def _build_hay(body: str) -> tuple[str, list[int]]:
    """Alphanumeric-only, lowercased, ligature-expanded body + char->index map.

    Punctuation and symbol glyphs differ between the model's quotes (copied
    from the rendered PDF) and the .tex source, so both sides are reduced to
    the same letter/digit stream before matching — the same normalization the
    UI's highlighter uses.
    """
    chars: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(body):
        for ch in _LIG.get(c, c).lower():
            if not ch.isalnum():
                continue
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def _find_norm_span(body: str, quote: str) -> tuple[int, int] | None:
    """Char span in `body` matching `quote`, ignoring whitespace, case, and
    ligature glyph differences. Falls back to matching the quote's words in
    sequence (so braces/commands between words don't break the match)."""
    hay, idx = _build_hay(body)
    qhay, _ = _build_hay(quote)
    if qhay:
        at = hay.find(qhay)
        if at >= 0:
            return idx[at], idx[at + len(qhay) - 1] + 1
    words = [w for w in re.split(r"[^A-Za-z0-9]+", qhay) if w]
    if len(words) >= 2:
        pat = "[^A-Za-z0-9]*".join(re.escape(w) for w in words)
        m = re.search(pat, hay)
        if m:
            return idx[m.start()], idx[m.end() - 1] + 1
    return None


def _expand_span_braces(body: str, a: int, b: int) -> tuple[int, int]:
    """Grow a span so it doesn't cut a brace group in half.

    If the span swallows a closing brace (e.g. inside \\textbf{Cloud:}),
    pull the matching opening brace — and its command name — into the span
    too, so the replacement is valid LaTeX.
    """
    removed = body[a:b]
    bal = removed.count("{") - removed.count("}")
    while bal < 0:
        j = body.rfind("{", 0, a)
        if j < 0:
            break
        a = j
        bal += 1
        m = re.search(r"\\[a-zA-Z]+\*?$", body[:a])
        if m:
            a = m.start()
    while bal > 0:
        k = body.find("}", b)
        if k < 0:
            break
        b = k + 1
        bal -= 1
    return a, b


def _fit_rewrite(line_before_span: str, rewrite: str) -> str:
    """Adjust a model rewrite so it doesn't duplicate line structure.

    If the span sits after an existing \\item and the rewrite helpfully
    starts with its own \\item, drop the duplicate.
    """
    if re.search(r"\\item\b", line_before_span):
        rewrite = re.sub(r"^\\item\*?\s+", "", rewrite)
    return rewrite


def apply_rewrite(source: str, quote: str, rewrite: str) -> str | None:
    """Replace the quoted text in the .tex source with the LaTeX rewrite."""
    lines = source.splitlines(keepends=True)
    i = find_tex_line(source, quote)
    if i < 0:
        return None
    body = lines[i].rstrip("\n")
    end = lines[i][len(body):]  # preserve the line ending
    span = _find_norm_span(body, quote) if quote.strip() else None
    if span is not None:
        a, b = _expand_span_braces(body, *span)
        lines[i] = (body[:a] + _fit_rewrite(body[:a], _truncate_latex_comment(rewrite))
                    + body[b:] + end)
    else:
        # The quote can't be anchored inside the line (find_tex_line only
        # picks the best-scoring line). Replacing the whole line with the
        # rewrite is safe only when the quote covers essentially all of it;
        # otherwise a fragment rewrite would clobber the rest of the line.
        hay, _ = _build_hay(body)
        qhay, _ = _build_hay(quote)
        if not qhay or len(qhay) < 0.9 * len(hay):
            return None
        indent = body[:len(body) - len(body.lstrip())]
        lines[i] = indent + _fit_rewrite(indent, _truncate_latex_comment(rewrite)) + end
    return "".join(lines)


def apply_search_replace(source: str, search: str, replace: str) -> str | None:
    """Exact-substring edit for the chat agent. Returns None if not found."""
    if search and search in source:
        return source.replace(search, _truncate_latex_comment(replace), 1)
    # Whitespace-insensitive retry over lines.
    tgt = _norm_line(search)
    for i, line in enumerate(source.splitlines()):
        if _norm_line(line) == tgt:
            lines = source.splitlines(keepends=True)
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = indent + _truncate_latex_comment(replace) + ("\n" if lines[i].endswith("\n") else "")
            return "".join(lines)
    return None


def _truncate_latex_comment(s: str) -> str:
    """Drop everything from the first unescaped % (a LaTeX comment).

    Models like to append suggestions as comments ("... Ray % also add
    \\DisableLigatures to the preamble"), which silently comments out the
    rest of the line — including real content.
    """
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "%":
            return s[:i].rstrip()
        i += 1
    return s


def _latex_text_escape(s: str) -> str:
    """Escape unescaped & # _ (models forget these in plain text)."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(s[i:i + 2])
            i += 2
            continue
        if s[i] in "&#_":
            out.append("\\" + s[i])
        else:
            out.append(s[i])
        i += 1
    return "".join(out)


def _pdf_words(pdf_bytes: bytes) -> int:
    import pypdfium2 as pdfium

    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        doc = pdfium.PdfDocument(f.name)
        return sum(len(p.get_textpage().get_text_bounded().split()) for p in doc)


def compile_edited(pdf: Path, variants: list[str], *, guard: bool = True) -> tuple[str, bytes]:
    """Compile the first variant that works: (working_source, pdf_bytes).

    `guard` rejects rewrites that would silently drop a chunk of the resume
    (the model sometimes rewrites the whole document and loses bullets).
    """
    old_words = _pdf_words(pdf.read_bytes()) if (guard and pdf.is_file()) else None
    last: HTTPException | None = None
    for source in variants:
        try:
            pdf_bytes, _log = compile_tex(source.encode("utf-8"))
        except HTTPException as exc:
            last = exc
            continue
        if old_words and _pdf_words(pdf_bytes) < 0.85 * old_words:
            last = HTTPException(
                status_code=422,
                detail="the compiled resume would lose a significant amount of "
                       "content; ask for a smaller change")
            continue
        return source, pdf_bytes
    raise last or HTTPException(status_code=422, detail="edit failed to compile")


def persist_and_analyze(pdf: Path, new_source: str, pdf_bytes: bytes, prev_analysis: dict | None = None) -> None:
    _tex_path(pdf).write_text(new_source, encoding="utf-8")
    pdf.write_bytes(pdf_bytes)
    notify({"type": "recompiled", "name": pdf.name})
    _kick_analyze(pdf, "", prev_analysis=prev_analysis)


# --- Accepted-edit pipeline (fast UI, background compile + analysis) ----------

# Newest source that a queued/finished edit produced, per resume name. Edits
# chain onto this instead of the (not yet rewritten) file on disk, so rapid
# accepts don't overwrite each other. Cleared whenever disk becomes the truth.
_latest_source: dict[str, str] = {}


def _latest_tex(pdf: Path) -> str:
    src = _latest_source.get(pdf.name)
    if src is not None:
        return src
    tex = _tex_path(pdf)
    return tex.read_text(encoding="utf-8") if tex.is_file() else ""


def _restore_analysis(pdf: Path, prev_analysis: dict | None) -> None:
    """Put back the pre-edit analysis after a failed background edit."""
    fields: dict = {"status": "done" if prev_analysis else "new", "error": None}
    if prev_analysis:
        fields["analysis"] = prev_analysis
    write_status(pdf, fields)


def _bak_path(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".tex.bak")


def _recover_pending_edits() -> None:
    """Startup recovery for edits interrupted by a restart.

    _bg_edit writes the accepted source to disk immediately (backing up the
    previous version) so an accept can never be silently lost. If the app died
    before the compile finished, finish that work now: compile the accepted
    source; if it doesn't compile, roll back to the backup.
    """
    for bak in list(UPLOADS_DIR.glob("*.pdf.tex.bak")):
        pdf = _pdf_path(bak.name[: -len(".tex.bak")])
        tex = _tex_path(pdf)
        if not tex.is_file():
            bak.unlink(missing_ok=True)
            continue
        source = tex.read_text(encoding="utf-8")
        try:
            pdf_bytes, _log = compile_tex(source.encode("utf-8"))
        except HTTPException:
            # The accepted edit never compiled; roll back to the last good source.
            tex.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
            bak.unlink(missing_ok=True)
            continue
        pdf.write_bytes(pdf_bytes)
        bak.unlink(missing_ok=True)
        notify({"type": "recompiled", "name": pdf.name})
    # A restart can also lose the pdf refresh of an edit that was persisted
    # (tex written, pdf not). Recompile any resume whose tex is newer than its pdf.
    for tex in UPLOADS_DIR.glob("*.tex"):
        pdf = _pdf_path(tex.name[:-4] + ".pdf")
        if pdf.is_file() and tex.stat().st_mtime > pdf.stat().st_mtime + 1:
            try:
                pdf_bytes, _log = compile_tex(tex.read_bytes())
            except HTTPException:
                continue
            pdf.write_bytes(pdf_bytes)
            notify({"type": "recompiled", "name": pdf.name})


def _bg_edit(pdf: Path, apply_fn, prev_analysis: dict | None = None) -> None:
    """Queue an off-thread source edit: apply_fn(source) -> variant sources.

    Runs against the newest accepted source (so queued edits chain), compiles
    the first variant that works, persists it, and kicks a fresh analysis.
    The accepted source is written to disk immediately (with a .bak of the
    previous version) so a restart can't lose the edit; the backup is removed
    once the compile succeeds and restored if it fails.
    """
    def job() -> None:
        from app import db
        source = _latest_tex(pdf)
        if not source:
            db.add_chat(pdf.name, "system", "Edit not saved: no LaTeX source.")
            _restore_analysis(pdf, prev_analysis)
            return
        variants, seen = [], set()
        for ns in apply_fn(source):
            if ns and ns not in seen:
                seen.add(ns)
                variants.append(ns)
        if not variants:
            db.add_chat(pdf.name, "system",
                        "Edit not saved: could not locate the text it should replace.")
            _restore_analysis(pdf, prev_analysis)
            return
        tex = _tex_path(pdf)
        bak = _bak_path(pdf)
        bak.write_text(source, encoding="utf-8")
        tex.write_text(variants[0], encoding="utf-8")  # durable right away
        try:
            new_source, pdf_bytes = compile_edited(pdf, variants)
        except HTTPException as exc:
            tex.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
            bak.unlink(missing_ok=True)
            db.add_chat(pdf.name, "system", "Edit not saved: " + str(exc.detail))
            _restore_analysis(pdf, prev_analysis)
            return
        _latest_source.pop(pdf.name, None)  # disk is authoritative again
        bak.unlink(missing_ok=True)
        persist_and_analyze(pdf, new_source, pdf_bytes, prev_analysis=prev_analysis)

    _STATE.compile_pool.submit(job)


CHAT_PROMPT = """\
You help improve a software engineer's LaTeX resume. You may answer questions, \
and when the user asks for a change to the resume, make exactly one edit.

Current analysis summary:
{summary}

The full LaTeX source:
```latex
{source}
```

Recent conversation:
{history}

The user says: {message}

Reply with ONLY a JSON object (no fences):
{{"reply": "<answer or a short note about what you changed>",
  "edit": {{"search": "<EXACT contiguous text copied from the LaTeX source above, \
preferably one full line>", "replace": "<replacement LaTeX>"}}, or "edit": null}}

Rules: only include "edit" when the user asked for a change. "search" must be
copied character-for-character from the source. Keep the document compilable; \
use the packages already loaded. Do not change the preamble unless asked."""


def _chat_summary(analysis: dict) -> str:
    if not analysis:
        return "No analysis yet."
    ov = analysis.get("overall") or {}
    parts = [f"Overall {ov.get('score')}/10 - {ov.get('verdict', '')}"]
    for f in analysis.get("findings", [])[:15]:
        parts.append(f"- [{f['type']}/{f.get('severity', '')}] {f['title']}: {f.get('quote', '')[:60]}")
    return "\n".join(parts)


def read_chat(pdf: Path) -> list[dict]:
    from app import db
    return db.get_chat(pdf.name)


def write_chat(pdf: Path, msgs: list[dict]) -> None:
    from app import db
    db.clear_chat(pdf.name)
    for m in msgs[-40:]:
        db.add_chat(pdf.name, m["role"], m["content"])


@app.get("/api/files/{name}/tex")
def get_tex(name: str) -> Response:
    pdf = _pdf_path(name)
    # Serve the latest accepted source (in-memory if a background edit is
    # still compiling) so a refresh never shows a reverted older version.
    src = _latest_source.get(pdf.name)
    if src is None:
        tex = _tex_path(pdf)
        if not tex.is_file():
            raise HTTPException(status_code=404, detail="No LaTeX source for this resume")
        src = tex.read_text(encoding="utf-8")
    return Response(src, media_type="text/plain; charset=utf-8")


@app.put("/api/files/{name}/tex")
def put_tex(name: str, body: TexBody) -> dict:
    """Edit the source: compile the new version and re-run the analysis."""
    pdf = _pdf_path(name)
    tex = _tex_path(pdf)
    if not tex.is_file():
        raise HTTPException(status_code=404, detail="No LaTeX source for this resume")
    pdf_bytes, _log = compile_tex(body.source.encode("utf-8"))
    tex.write_text(body.source, encoding="utf-8")
    pdf.write_bytes(pdf_bytes)
    _latest_source.pop(name, None)  # disk is authoritative again
    notify({"type": "recompiled", "name": name})
    _kick_analyze(pdf, "", prev_analysis=read_status(pdf).get("analysis"))
    return {"name": name, "status": "running"}


@app.get("/api/files/{name}/pdf")
def get_pdf(name: str) -> FileResponse:
    return FileResponse(_pdf_path(name), media_type="application/pdf")


@app.get("/api/files/{name}/analysis")
def get_analysis(name: str) -> dict:
    pdf = _pdf_path(name)
    st = read_status(pdf)
    if st.get("status") == "new":
        raise AnalysisNotFound()
    return {"name": name, **st}


@app.post("/api/files/{name}/analyze")
def analyze(name: str, body: AnalyzeBody) -> dict:
    pdf = _pdf_path(name)
    st = read_status(pdf)
    if st.get("status") == "running":
        raise HTTPException(status_code=409, detail="Analysis already running")
    _kick_analyze(pdf, body.instruction, prev_analysis=st.get("analysis"))
    return {"name": name, "status": "running"}


@app.get("/api/files/{name}/export.md")
def export_checklist(name: str, run: str | None = None) -> Response:
    pdf = _pdf_path(name)
    runs = read_history(pdf)
    if not runs:
        raise AnalysisNotFound()
    chosen = next((r for r in runs if r["id"] == run), None) if run else runs[-1]
    if chosen is None:
        raise HTTPException(status_code=404, detail="Run not found")
    filename = f"{pdf.name[:-4]}-checklist.md"
    return Response(
        build_checklist(name, chosen),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/files/{name}")
def delete(name: str) -> Response:
    pdf = _pdf_path(name)
    pdf.unlink()
    _latest_source.pop(name, None)
    stem = pdf.name[:-4]
    for extra in (f"{stem}.analysis.json", f"{stem}.history.json", f"{stem}.txt",
                  f"{stem}.tex", f"{stem}.analysis.tmp", f"{stem}.history.tmp",
                  f"{stem}.pdf.tex.bak"):
        p = pdf.parent / extra
        if p.exists():
            p.unlink()
    from app import db
    db.delete_resume(name)
    notify({"type": "update", "name": name})
    return Response(status_code=204)


# --- Chat / accept (edits the LaTeX source, recompiles, re-analyzes) ----------

def _fmt_history(hist: list[dict]) -> str:
    # System notices (progress messages) are UI-only: don't feed them to the model.
    return "\n".join(f"{m['role']}: {m['content'][:400]}"
                     for m in hist[-10:] if m.get("role") in ("user", "assistant")) or "(none)"


def _invoke_main_json(settings: Settings, prompt: str) -> dict:
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model=settings.llm.model, base_url=settings.llm.base_url,
                       api_key=settings.llm.api_key, temperature=0.2, timeout=240)
    raw = model.invoke([HumanMessage(content=prompt)]).content
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    m = re.search(r"\{.*\}", str(raw), re.S)
    if not m:
        raise ValueError("model did not return JSON")
    return json.loads(m.group())


@app.get("/api/files/{name}/chat")
def get_chat(name: str) -> list[dict]:
    return read_chat(_pdf_path(name))


@app.post("/api/files/{name}/chat")
def chat(name: str, body: ChatBody) -> dict:
    """Talk to the main model; edits it proposes are applied to the source.

    On a successful edit the source is recompiled and a fresh analysis starts.
    """
    pdf = _pdf_path(name)
    tex = _tex_path(pdf)
    if not tex.is_file():
        raise HTTPException(status_code=404, detail="No LaTeX source for this resume")
    settings = effective_settings()
    if not settings.llm.api_key:
        raise HTTPException(status_code=503, detail="No API key configured")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    source = tex.read_text(encoding="utf-8")
    st = read_status(pdf)
    prompt = CHAT_PROMPT.format(summary=_chat_summary(st.get("analysis")),
                                source=source, history=_fmt_history(read_chat(pdf)),
                                message=message)
    try:
        data = _invoke_main_json(settings, prompt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Chat model failed: {exc}")

    reply = str(data.get("reply") or "").strip() or "(no reply)"
    applied = False
    applied_source = None
    edit = data.get("edit")
    if isinstance(edit, dict) and edit.get("search"):
        search = str(edit["search"])
        # Full-document rewrites are where models lose content; refuse them.
        if len(search) > 400 or len(search) > 0.25 * len(source):
            reply += ("\n\n(The edit was NOT applied: it tried to replace too much "
                      "of the document at once. Ask for smaller, line-level changes.)")
        else:
            raw = _truncate_latex_comment(str(edit.get("replace", "")))
            rewrites = [raw, _latex_text_escape(raw)]
            variants, seen = [], set()
            for rep in rewrites:
                ns = apply_search_replace(source, search, rep)
                if ns and ns not in seen:
                    seen.add(ns)
                    variants.append(ns)
            if not variants:
                reply += "\n\n(The edit was NOT applied: its search text wasn't found in the source.)"
            else:
                # Compile + persist + re-analysis continue in the background;
                # any failure is reported back as a system message in the chat.
                applied = True
                applied_source = variants[0]
                _latest_source[pdf.name] = applied_source
                reply += "\n\n(Edit applied; recompiling — a fresh analysis will follow.)"
                _bg_edit(pdf,
                         lambda s: [apply_search_replace(s, search, rep) for rep in rewrites],
                         prev_analysis=st.get("analysis"))

    add_chat = {"role": "user", "content": message}, {"role": "assistant", "content": reply}
    from app import db
    for m in add_chat:
        db.add_chat(name, m["role"], m["content"])
    return {"reply": reply, "applied": applied, "source": applied_source}


@app.delete("/api/files/{name}/chat")
def clear_chat(name: str) -> Response:
    from app import db
    db.clear_chat(_pdf_path(name).name)
    return Response(status_code=204)


@app.post("/api/files/{name}/apply")
def apply_finding(name: str, body: ApplyBody) -> dict:
    """Apply a finding's rewrite to the source and return immediately.

    The tectonic compile, persist, and re-analysis run in the background
    (see _bg_edit); the response carries the edited source so the UI can
    update the editor at once.
    """
    pdf = _pdf_path(name)
    tex = _tex_path(pdf)
    if not tex.is_file():
        raise HTTPException(status_code=404, detail="No LaTeX source for this resume")
    st = read_status(pdf)
    prev_analysis = st.get("analysis")
    findings = ((prev_analysis or {}).get("findings") or [])
    f = next((x for x in findings if x.get("id") == body.finding_id), None)
    if f is None:
        raise HTTPException(status_code=404, detail="Finding not found (re-analyze first?)")
    if not f.get("rewrite"):
        raise HTTPException(status_code=400, detail="This finding has no rewrite to apply")

    source = _latest_tex(pdf)
    raw = _truncate_latex_comment(f["rewrite"])
    rewrites = [raw, _latex_text_escape(raw)]
    variants, seen = [], set()
    for rw in rewrites:
        ns = apply_rewrite(source, f.get("quote", ""), rw)
        if ns and ns not in seen:
            seen.add(ns)
            variants.append(ns)
    if not variants:
        # The source drifted since the analysis (e.g. another accepted edit or
        # a chat edit rewrote that part), or the quote can't be anchored to
        # real source text. Refresh the analysis so an up-to-date suggestion
        # replaces the stale one, and note it in the chat.
        from app import db
        db.add_chat(pdf.name, "system",
                    "Suggestion not applied: couldn't locate its quote in the "
                    "current source. Re-analyzing — accept the updated "
                    "suggestion when it appears.")
        if st.get("status") != "running":
            _kick_analyze(pdf, "", prev_analysis=prev_analysis)
        raise HTTPException(
            status_code=409,
            detail="Couldn't locate this quote in the current source — it changed "
                   "since the analysis ran. Re-analyzing; accept the updated "
                   "suggestion when it appears.")

    # Record the optimistic source in _latest_source immediately so any subsequent
    # rapid accepts or queries see this new source state without waiting for the compile.
    _latest_source[pdf.name] = variants[0]

    # Optimistic bookkeeping: mark running and drop the accepted finding from
    # the (stale) analysis so it disappears from the UI right away. The
    # background job restores it if the compile fails.
    updated_analysis = None
    if prev_analysis:
        updated_analysis = {
            **prev_analysis,
            "findings": [x for x in (prev_analysis.get("findings") or [])
                         if x.get("id") != body.finding_id],
        }
    fields: dict = {"status": "running", "error": None}
    if updated_analysis:
        fields["analysis"] = updated_analysis
    write_status(pdf, fields)
    from app import db
    db.add_chat(pdf.name, "system", "Suggestion applied \u2014 recompiling\u2026")
    _bg_edit(pdf, lambda s: [apply_rewrite(s, f.get("quote", ""), rw) for rw in rewrites],
             prev_analysis=updated_analysis or prev_analysis)
    return {"name": name, "applied": body.finding_id, "status": "recompiling",
            "source": variants[0]}
