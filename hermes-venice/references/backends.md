# Backends — which tool runs which operation

One skill, many backends. `ffmpeg` is the default and the only backend wired
into the helpers today. The ArtCraft suite is **verified but not wired** — it
is P3 work per `video-stack/PLAN.md` and must not be assumed capable of
anything this table does not explicitly claim (it was verified by real headless
runs on this box, not by vendor docs).

| Operation | Default (today) | ArtCraft option (P3) | Notes |
|---|---|---|---|
| Inventory / probe | `ffprobe` (CLI) | `filmcraft-cli probe` (JSON) | FilmCraft returns JSON media info — a drop-in for scripted inventory. |
| Transcribe (STT) | `helpers/transcribe.py` — ElevenLabs Scribe (primary) or Venice adapter | — | Provider-agnostic by contract (`{type,text,start,end,speaker_id}` words). Venice path documented fidelity loss; see `transcribe.py` docstring + SKILL.md. |
| Cut / per-segment extract | `helpers/render.py` (ffmpeg per-segment + `-c copy` concat) | `filmcraft-cli` trim/export | FilmCraft H.264 export verified headless; EDL/FCPXML import is an OPEN QUESTION (PLAN.md) — cannot bridge our EDL yet. |
| Overlay / motion graphics | PIL + ffmpeg (PIL/PNG-sequence overlays), or HyperFrames/Remotion/Manim via sub-agents | `effectcraft` | EffectCraft 0.3.1 ships a Windows CLI; motion-graphics engine choice is P3. No capability claim until wired + tested. |
| Grade / LUT | `helpers/grade.py` (presets + signalstats auto) | `filmcraft-cli` grade pipeline | Auto-grade is data-driven; ArtCraft not integrated. |
| Loudness | `render.py` two-pass loudnorm (`-14 LUFS, -1 dBTP, LRA 11`) | — | |
| Subtitles | `render.py` SRT → libass `subtitles` filter, LAST in chain | — | Windows: libass filter confirmed present in local ffmpeg 8.1 gyan build. |
| Self-eval | `helpers/verify.py` (filmstrips + ebur128 + ffprobe) | — | Filmstrips still need a human/LLM look; numbers are objective. |
| Export / deliverable | `render.py` (H.264 + AAC, faststart) | `filmcraft-cli export` | Verified export exists; not yet wired as an option. |

## Selection rule

Nothing in the skill assumes a GUI. ffmpeg is the default. Route an operation
to a native tool only where it demonstrably wins on this box (N150 software
renderer is an open performance question — PLAN.md). Until then, the ArtCraft
rows above are documentation of what was verified, not an active backend.

## Not wired (do not claim)

- `filmcraft-cli` accepting our `edl.json` / FCPXML — untested, open question.
- `effectcraft` motion-graphics overlays from this skill — P3, not built.
- `photocraft` stills/thumbnails — P3, not built.
- Any ArtCraft capability as the critical path — v0.2.x, no release history.
