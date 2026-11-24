# DeepTutor — Agent-Native Architecture

## Overview

DeepTutor is an **agent-native** intelligent learning companion organized
around a two-layer plugin model — single-shot **Tools** invoked by the
LLM, and multi-stage **Capabilities** that take over a turn — exposed
through three entry points: CLI, WebSocket API, and Python SDK. All three
enter the durable turn application service before the shared turn engine
routes a normalized context to the selected capability.

## Architecture

```
Entry Points:  CLI (Typer)  |  WebSocket /ws  |  Python SDK
                    ↓                   ↓                   ↓
              ┌─────────────────────────────────────────────────┐
              │          TurnApplicationService                 │
              │   persists, coordinates, and replays turns      │
              └──────────────────────┬──────────────────────────┘
                                     ↓
              ┌─────────────────────────────────────────────────┐
              │       TurnEngine → ChatOrchestrator              │
              │   routes UnifiedContext → selected Capability    │
              │   (defaults to `chat`)                           │
              └──────────┬──────────────┬───────────────────────┘
                         │              │
              ┌──────────▼──┐  ┌────────▼──────────┐
              │ ToolRegistry │  │ CapabilityRegistry │
              │  (Level 1)   │  │   (Level 2)        │
              └──────────────┘  └────────────────────┘
```

`TurnApplicationService` owns durable turn state and replay through the
session store and runtime coordinator. Each execution uses a per-turn
`StreamBus`; the orchestrator emits events, the turn runtime persists them,
and adapters replay them to consumers. Runtime settings live in
`data/user/settings/*.json` — project-root `.env` files are intentionally
ignored.

### Level 1 — Tools

Single-function tools the LLM picks on demand. Eight user-toggleable tools
surface in `/settings/tools` (this order is the canonical display order):

| Tool                 | Description                                   |
| -------------------- | --------------------------------------------- |
| `brainstorm`         | Breadth-first idea exploration with rationale |
| `web_search`         | Web search with citations                     |
| `paper_search`       | arXiv preprint search                         |
| `zotero_search`      | Search a Zotero user library by title/creator/year |
| `reason`             | Dedicated deep-reasoning LLM call             |
| `geogebra_analysis`  | Analyze math images into GeoGebra commands    |
| `imagegen`           | Generate images                               |
| `videogen`           | Generate videos                               |

`USER_TOGGLEABLE_TOOL_NAMES` in `deeptutor/tools/builtin/__init__.py` is the
authoritative toggle list. Other built-ins are **context-gated** or
capability-owned: `CONFIGURABLE_BUILTIN_TOOL_NAMES` declares the context-gated
surface, while `deeptutor/agents/_shared/tool_composition.py` owns the mount
rules (`ToolMountFlags`) and the always-available workspace tools. Examples
include `rag`, memory and notebook tools, `read_skill`, deferred MCP/CLI tools,
`exec`, `ask_user`, and mastery navigation. `--tool` selects from the
user-toggleable whitelist; it does not bypass context or capability gates.

### Level 2 — Capabilities

Multi-stage pipelines that own the turn:

| Capability       | Stages                                                |
| ---------------- | ----------------------------------------------------- |
| `chat`           | exploring → responding (single agentic loop, default) |
| `ask_questions`  | responding (chat loop forced through `ask_user`)      |
| `deep_solve`     | responding (chat loop + solve planning tools)         |
| `deep_question`  | ideation → generation                                 |
| `deep_research`  | rephrasing → decomposing → researching → reporting    |
| `visualize`      | analyzing → generating → reviewing (SVG / Chart.js / Mermaid / HTML; or routes to Manim sub-stages via `render_type`) |
| `math_animator`  | concept_analysis → concept_design → code_generation → code_retry → summary → render_output |
| `mastery_path`   | responding (Guided Learning — chat loop + mastery tools, gated per topic type) |
| `immersive_reading` | responding (document- or timestamp-grounded media reading loop) |
| `course_study`   | responding (course-state sensing and hand-off loop)   |
| `audio_overview` | retrieving → script_writing → voice_generation → publishing (grounded two-voice KB overview + transcript) |

`deeptutor/runtime/bootstrap/builtin_capabilities.py` is the authoritative
roster: `BUILTIN_CAPABILITY_CLASSES` maps each name to its `module:Class`
path, and `BUILTIN_CAPABILITY_SPECS` adds the manifest (`stages`, `tools_used`,
`cli_aliases`, `config_defaults`). Pipeline implementations sit where those
class paths point: `deeptutor/agents/<name>/` for `chat`, `deep_question`,
`deep_research`, `visualize` and `math_animator`; `deeptutor/capabilities/<name>/`
for the rest.

All capabilities converge on `emit_capability_result()` in
`deeptutor/agents/_shared/capability_result.py` so every turn emits the same
envelope (response payload + `cost_summary` from `UsageTracker`). Status copy
and prompts are i18n'd via `capabilities/prompts/{en,zh}/<name>.yaml`.

### Level 2b — Loop extensions

A second kind of Level 2 plugin: `deeptutor/capabilities/registry.py` declares
14 built-in `LoopExtension`s (protocol in `deeptutor/capabilities/protocol.py`).
A loop extension owns no pipeline — it runs *inside* the chat agent loop,
reusing the full chat tool surface, and adds its `owned_tools` plus a
`system_block()` prompt fragment while `is_active(context)` holds for the turn.
The exception is the knowledge category, `KnowledgeCapability`
(`ObsidianCapability`, `MarginNoteCapability`, `SubagentCapability`): it sets
`exclusive_tools` and *replaces* the surface with its own tools plus the
`ask_user` floor, dropping chat built-ins and composer toggles.

Activation is context-driven, not name-driven: `SolveLoopCapability`,
`MasteryLoopCapability` and `AskQuestionsLoopCapability` read the metadata flag
set by their pipeline counterpart (`solve_mode`, `mastery_mode`,
`ask_questions_mode`), while others compare `context.active_capability`
(e.g. `course_study`). External extensions register through the
`deeptutor.extensions` entry-point group (the legacy
`deeptutor.loop_capabilities` group still loads but is deprecated).

| Extension                | Class path |
| ------------------------ | ---------- |
| `ask_questions`          | `deeptutor.capabilities.ask_questions.loop:AskQuestionsLoopCapability` |
| `mastery`                | `deeptutor.capabilities.mastery.loop:MasteryLoopCapability` |
| `solve`                  | `deeptutor.capabilities.solve.loop:SolveLoopCapability` |
| `obsidian`               | `deeptutor.capabilities.obsidian.capability:ObsidianCapability` |
| `marginnote4`            | `deeptutor.capabilities.marginnote4.capability:MarginNoteCapability` |
| `subagent`               | `deeptutor.capabilities.subagent.capability:SubagentCapability` |
| `ima`                    | `deeptutor.capabilities.ima.capability:ImaCapability` |
| `immersive_reading`      | `deeptutor.capabilities.reading.capability:ReadingCapability` |
| `course_study`           | `deeptutor.capabilities.course_study.capability:CourseStudyLoopCapability` |
| `explore_context`        | `deeptutor.capabilities.explore_context.capability:ExploreContextCapability` |
| `setup`                  | `deeptutor.capabilities.setup.capability:SetupCapability` |
| `partner_authoring`      | `deeptutor.capabilities.partner_authoring.capability:PartnerAuthoringCapability` |
| `partner_group`          | `deeptutor.capabilities.partner_group.capability:PartnerGroupCapability` |
| `visualization_generation` | `deeptutor.visualizers.loop_capability:VisualizationLoopCapability` |

Names can repeat across the two rosters (`ask_questions`, `immersive_reading`,
`course_study`), and some loop names are the short form of the pipeline
capability that runs them (`solve` ↔ `deep_solve`, `mastery` ↔ `mastery_path`).
The pipeline capability still owns the turn — `DeepSolveCapability` drives
`AgenticChatPipeline`, the same agentic loop `chat` uses — and the loop
extension only contributes tools and prompt blocks to it.

## CLI Usage

```bash
# Install
pip install deeptutor      # Full app (CLI + Web/API + packaged Web assets)
pip install deeptutor-cli  # CLI-only

# Run any capability
deeptutor run chat "Explain Fourier transform"
deeptutor run deep_solve "Solve x^2=4" -t rag --kb my-kb
deeptutor run visualize "Animate sine wave" --config render_mode=manim_video

# Interactive REPL
deeptutor chat
# (inside the REPL: /regenerate or /retry re-runs the last user message)

# Partners (IM-connected companions)
deeptutor partner list

# Knowledge bases, memory, server
deeptutor kb list
deeptutor kb create my-kb --doc textbook.pdf
deeptutor memory show
deeptutor serve --port 8001       # API server only
deeptutor start                   # backend + frontend together
```

## Key Files

| Path                                       | Purpose                              |
| ------------------------------------------ | ------------------------------------ |
| `deeptutor/runtime/orchestrator.py`        | `ChatOrchestrator` — unified entry   |
| `deeptutor/runtime/launcher.py`            | Backend + frontend lifecycle / port discovery |
| `deeptutor/runtime/registry/`              | Tool + Capability registries         |
| `deeptutor/runtime/bootstrap/builtin_capabilities.py` | Built-in capability class paths |
| `deeptutor/services/config/runtime_settings.py` | JSON settings + process-env overrides |
| `deeptutor/services/subagent/`             | Local/remote agent connectors; register each backend in `registry.py` and its model options in `models.py` (Grok CLI uses native `streaming-json`) |
| `deeptutor/core/stream.py`, `deeptutor/runtime/stream_bus.py` | StreamEvent protocol + async fan-out |
| `deeptutor/core/tool_protocol.py`          | `BaseTool` + `ToolDefinition`         |
| `deeptutor/core/capability_protocol.py`    | `TurnCapability` + `CapabilityManifest` |
| `deeptutor/core/context.py`                | `UnifiedContext` dataclass            |
| `deeptutor/tools/builtin/__init__.py`      | All built-in tool wrappers + toggle/gate name lists |
| `deeptutor/capabilities/`                  | Loop extensions + chat-loop capability implementations |
| `deeptutor/capabilities/registry.py`       | `LoopExtension` roster + entry-point discovery |
| `deeptutor/agents/_shared/tool_composition.py` | Tool mount rules (`ToolMountFlags`) |
| `deeptutor/agents/_shared/capability_result.py` | `emit_capability_result()` turn envelope |
| `deeptutor/app/service.py`                 | `TurnApplicationService` — durable turns |
| `deeptutor/app/facade.py`                  | `DeepTutorApp` — Python SDK facade   |
| `deeptutor/app/container.py`               | `ApplicationContainer` — process DI wiring |
| `deeptutor/runtime/turn_engine.py`         | `TurnEngine` — shared execution engine |
| `deeptutor_cli/main.py`                    | Typer CLI entry point                |
| `deeptutor/api/routers/unified_ws.py`      | Unified WebSocket endpoint           |

## Dependency Layers

Public install paths and source extras are defined in `pyproject.toml`.
Requirements files mirror the same dependency groups for Docker/CI installs.

```
pip install deeptutor      — Full app (CLI + Web/API + packaged Web assets)
pip install deeptutor-cli  — CLI-only (LLM + RAG + providers + document parsing)
pip install -e .           — Source install for development

Source extras (.[ extra ], defined in pyproject.toml):
.[cli]            — CLI-only dependency set
.[server]         — Web/API server dependencies
.[partners]       — Partner channel SDKs  (legacy alias: .[tutorbot])
.[matrix]         — Matrix channel for Partners (matrix-nio; needs libolm)
.[matrix-e2e]     — Matrix with end-to-end encryption (matrix-nio[e2e])
.[math-animator]  — Manim addon (powers `visualize` Manim renders + `deeptutor run math_animator`)
.[dev]            — Test / lint tooling
.[all]            — Everything above
```
