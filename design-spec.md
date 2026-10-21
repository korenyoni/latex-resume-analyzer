# Design Specification

## Overview

Working document. Scope: the next iteration of the web app and agent skills.

**Goal:** help software engineers build a generally strong resume, judged
against one opinionated standard. Not an ATS: no job-description matching or
keyword fitting against a specific posting.

**LaTeX source only.** Uploads are `.tex` files; the app compiles them
(tectonic), renders the PDF, analyzes it, and can edit the source.

---

## 1. Baseline (v2, shipped)

- FastAPI app (`app/web.py`) + single static page (`app/static/index.html`),
  no build step. State in Postgres (`app/db.py`): analyses, history, chat,
  settings. Files on the uploads volume (`.tex`, compiled `.pdf`, extracted
  `.txt`).
- Three-pane layout: **resumes sidebar | PDF / Source | analysis + chat**.
- One deepagents agent for analysis (structured JSON: overall + section
  scores, findings with exact quotes, entities); a direct vision-model call
  adds layout findings. Findings anchor to the pdf.js text layer as
  on-PDF highlights; `Accept` applies the finding's rewrite to the `.tex`,
  recompiles, and re-analyzes.
- Chat (per resume, persisted): talks to the main model; proposed
  search/replace edits are applied to the source, recompiled, re-analyzed.
- Model selection: gear modal in the header (main + vision), persisted
  server-side.
- Websocket push (`/ws`) so all clients refresh on any status change.
- Dark mode via CSS variables; checklist export (markdown).

Problems this spec fixes:

1. **Accept lag.** `POST /apply` blocks on the tectonic compile before it
   returns, then the panel wipes to "Analyzing…" for the whole re-analysis.
   Clicking *Accept* feels frozen: the suggestion card stays up for seconds,
   then everything disappears until analysis completes (~30 s+).
2. **Redundant UI.** Three separate "talk to the model" surfaces (additional
   instruction box, analyzing status block, chat box) and a pile of
   per-resume buttons (size, open, download ×2, re-analyze, delete) spread
   across the sidebar and PDF toolbar.

---

## 2. v3 layout consolidation — **Decided** (2026-10-22)

One screen, three zones; resumes become tabs; chat becomes the single
model-facing surface.

```
┌ Resume Analyzer ─ [main.tex ⋯][other ⋯] [+] ───────────────── ☾ ┐
├───────────────┬───────────────────────────┬─────────────────────┤
│ Analysis      │  PDF | Source             │ Chat               │
│  ● 7.5 verdict│  (pdf.js canvas +         │  …messages…        │
│  ▇▇▇ bars     │   text-layer highlights)  │  system notices    │
│  filters      │                           │                    │
│  findings     │                           │ [input          ➤] │
│  entities     │                           │ [main ▾][vision ▾] │
└───────────────┴───────────────────────────┴─────────────────────┘
```

### 2.1 Header — resume tabs
- App title, then **one tab per resume** (horizontal, in the header).
- Each tab shows the resume name; status is a subtle per-tab indicator
  (spinner dot while analyzing, error dot on failure) — no big status block.
- Each tab has a **⋯ kebab menu**: Re-analyze, Export checklist, Download
  PDF, Download .tex, Open PDF, **Delete** (with confirm).
- A compact **+** tab uploads a new `.tex`.
- Theme toggle stays rightmost. The model readout and the gear/settings
  modal in the header are **removed** (models move to the chat pane, §2.4).

### 2.2 Left panel — analysis (was the right sidebar)
- Score ring + verdict, section score bars, type filters, findings cards
  (issue / strength / style / visual) with diffs and *Accept* / *Show in
  source*, entity chips — i.e. the existing analysis panel, unchanged in
  substance, moved to the left.
- While re-analyzing, the **stale analysis stays visible** with a subtle
  "re-analyzing…" indicator; nothing is wiped to a spinner (§3.3).
- The "optional extra instruction" input and the Analyze/Re-analyze button
  row are **removed**. Re-analyze lives in the tab kebab menu; free-form
  guidance belongs in the chat.

### 2.3 Middle — PDF / Source (unchanged core)
- `PDF | Source` tabs stay. PDF keeps pdf.js rendering + anchored
  highlights. Source keeps the syntax-highlighted editor with
  *Save & Recompile* / *Reload from disk*.
- The PDF toolbar (zoom %, Open PDF, Download PDF, Download .tex) is
  **removed** — downloads live in the tab kebab menu (§2.1).
- Zoom shrinks to a minimal floating − / + control in the corner of the
  PDF pane.

### 2.4 Right panel — chat (the single model surface)
- Full-height chat log per resume, persisted as today. User + assistant
  messages; **system notices** inline (e.g. "Edit applied — recompiling",
  "Re-analysis complete: 7.5/10", compile errors) so progress that used to
  be the "Analyzing…" block surfaces where the user is looking.
- Bottom bar: text input + **send icon button** (Enter also sends), and two
  compact dropdowns: **main model** and **vision model** (the old header
  modal, demoted to where it's used). Changing a model saves immediately.
- The "Chat with the model — it can edit…" caption and Clear button are
  **removed**; the placeholder text carries that hint instead.

### 2.5 Removed elements (explicit)
| Removed | Replaced by |
|---|---|
| Additional-instruction input + Analyze button | chat; kebab *Re-analyze* |
| "Analyzing… (~30s)" status block | stale-visible findings + subtle indicator; chat system notices |
| "Chat with the model" caption box | full-height chat pane |
| Resumes sidebar | header tabs |
| Header model readout + gear modal | dropdowns in the chat bottom bar |
| PDF toolbar buttons (size / open / downloads) | kebab menu + floating zoom |

---

## 3. Accept / edit responsiveness — **Decided**

Requirement: *Accept* must feel instant — apply, save, and recompile right
away; analysis must never block the UI.

### 3.1 Async apply pipeline
1. `POST /apply` applies the rewrite **in memory**, returns **immediately**
   (no compile in the request) with the edited source.
2. The frontend updates at once: card removed, editor shows the new source,
   tab marked "recompiling…".
3. A background job compiles (variants + guard as today), then atomically
   writes `.tex` + `.pdf`, pushes a websocket `update`, and kicks the
   re-analysis (also background, as today).
4. Clients re-render the PDF when the `update` arrives; analysis replaces
   the stale one when it finishes.

Chat-proposed edits get the same treatment: the model reply returns as soon
as the LLM answers; compile/persist/re-analyze continue in the background.

### 3.2 Stale analysis stays visible
`_kick_analyze` keeps the previous `analysis` payload in the `running`
status (merged, not wiped). The UI shows old findings dimmed/refreshing
until the new analysis lands, so accepting never blanks the panel.

### 3.3 Failure handling
If the background compile fails: old `.tex`/`.pdf` stay on disk, the editor
reverts to the disk content, and the error surfaces as a chat system notice
(+ error dot on the tab). No silent divergence between editor and disk.

---

## 4. API changes (v3)

| Method | Path | Change |
|--------|------|--------|
| POST | `/api/files/{name}/apply` | returns immediately `{name, applied, status:"recompiling"}` + `source`; compile+persist+analyze in background |
| POST | `/api/files/{name}/chat` | reply returns after the LLM answers; edit compile/persist moved to background |
| POST | `/api/files/{name}/analyze` | unchanged path; keeps stale analysis visible while running |
| ws | `/ws` | same `update` events; clients refresh files/analysis |
| — | `/api/settings`, `/api/models` | unchanged; consumed by chat-pane dropdowns |

---

## 5. Non-goals

- Job-description matching / ATS keyword fitting.
- Multi-user, auth, accounts.
- Streaming chat replies (SSE) — still a plain request/response for now.
- Per-resume model selection — models remain a global setting.

---

## 6. Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Where do resumes live? | Horizontal tabs in the header, kebab menu per tab, + tab to upload |
| 2 | Where does the score go? | Left panel (analysis), replacing the old resumes sidebar |
| 3 | Where does chat go? | Full-height right pane; consolidates instruction box, status text, and chat |
| 4 | Where do model pickers go? | Compact dropdowns in the chat bottom bar; header modal removed |
| 5 | Accept flow | Async: instant response, background compile + analysis, stale findings stay visible |
| 6 | PDF toolbar | Removed; downloads in kebab menu, zoom floats in the PDF pane |
