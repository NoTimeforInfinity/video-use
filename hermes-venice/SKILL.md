---
name: video-use-hermes
description: Use when a video must be edited or assembled from takes. Cut, grade, subtitle, animate overlays by conversation. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use (Hermes)

A maintained port of `browser-use/video-use` (upstream SHA `b877063`,
2026-09-23). The craft — principles, the 12 Hard Rules, the EDL schema, the
process — is inherited and kept verbatim where it is correctness. The agent
glue is ours: sub-agents go through `delegate_task`, transcription is a
provider adapter (ElevenLabs Scribe primary, Venice fallback), everything runs
on this Windows box with ffmpeg 8.1.

`references/upstream-rules.md` is the upstream SKILL.md frozen verbatim for
diffing. **This file is authoritative.** `references/backends.md` says which
tool runs which operation (ffmpeg today; ArtCraft is P3 and not wired).

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here, build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal). *[Hermes port note: the Venice adapter path normalizes fillers and estimates word boundaries — a documented fidelity loss, not a license to use SRT mode. See Transcription below.]*
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via `delegate_task`; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the skill directory or the upstream `video-use/` clone.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives under the Hermes skills dir (a real copy or directory junction — Windows symlinks need admin). User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached transcripts (provider-normalized words contract)
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs / verify.py JSON
    ├── preview.mp4
    └── final.mp4
```

## Setup

Helpers live at `<skill_dir>/helpers/` and run as `python <skill_dir>/helpers/<name>.py` (or `python helpers/<name>.py` from the skill dir). Requirements on this box: Python 3.11 (`requests`, `numpy`, `pillow` — that's the real dependency set), `ffmpeg` + `ffprobe` on PATH (gyan 8.1 build, verified: libass/subtitles, zscale/tonemap, loudnorm, ebur128, signalstats).

**Transcription providers** — `transcribe.py` picks the provider:
- `auto` (default): ElevenLabs if `ELEVENLABS_API_KEY` resolves, else Venice.
- `--provider elevenlabs` — Scribe. Verbatim + diarize + audio events + word timestamps. No key on this box today.
- `--provider venice` — the adapter. Uses `VENICE_API_KEY` / `VENICE_INFERENCE_KEY` (present in the environment). See Transcription below.

Keys resolve from: `--config FILE` > `$VIDEO_USE_CONFIG` > `<skill_dir>/.env` > `./.env` > environment. Prefer `$VIDEO_USE_CONFIG` pointing at a secrets file outside the skill dir; never write a key into the user's `<videos_dir>`.

Node.js/npm only if a session needs HyperFrames/Remotion slots. `yt-dlp` installed lazily for URL sources.

## Helpers

- **`transcribe.py <video>`** — single-file provider-adapter transcription. `--provider elevenlabs|venice|auto`, `--num-speakers N`, `--audio-track`, `--language`, `--config`. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s or speaker change).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST → loudnorm. `--preview` 720/1080p fast, `--draft` cut-check, `--build-subtitles`, `--no-subtitles`, `--no-loudnorm`, `--fps`.
- **`grade.py <in> -o <out>`** — ffmpeg filter-chain grade. Presets (`--list-presets`), `--filter '<raw>'`, or default auto-analysis.
- **`verify.py --output final.mp4 --edl edl.json`** — the self-eval sweep: boundary filmstrips, ebur128, ffprobe, duration check, audio-pop check, and a Rule-6 check that every EDL cut edge lands on a word boundary (never inside a word). Emits pass/fail JSON, exit 0/1.

For animations, create `<edit>/animations/slot_<id>/` and spawn a sub-agent via `delegate_task` (brief below).

## Transcription (provider adapter)

Every downstream helper reads only `transcript.words[]` with keys `{type, text, start, end, speaker_id}`, `type ∈ word | spacing | audio_event`. Both providers normalize into this contract:

- **ElevenLabs Scribe** — native contract, true word timestamps, diarization, audio events. Best fidelity.
- **Venice** (`/audio/transcriptions`) — returns **segment-level** timestamps only. The adapter splits each segment's text into words, allots time proportionally by character length, and synthesizes `spacing` entries from the word gaps. **Fidelity loss (documented, not hidden):** no diarization (`speaker_id` always `None` — multi-speaker separation lost), no audio events (`(laughs)` not tagged), Whisper-family normalizes fillers (violates Hard Rule 8's verbatim intent — the filler signal is gone), word boundaries are estimates, and non-speech audio (tones/music) can be **hallucinated as words** — verified on this box: a 440 Hz sine was transcribed as "*phone ringing*". Cut padding tuned to Scribe's 50–100ms drift may need retuning per provider. The transcript JSON carries a `_provider` and `_fidelity` block so the loss is visible at a glance.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents (`delegate_task`). Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `helpers/verify.py --output <out> --edl edl.json` — it renders a filmstrip at every cut boundary (±1.5s) plus first 2s / last 2s / midpoints, and computes duration, ebur128 (integrated + true peak + LRA), and a per-boundary audio pop score. **Then look at the filmstrip images** and check for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade — verify.py flags these)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Measure the audio, don't assume it: verify.py's ebur128 numbers are the record — integrated loudness (target -14 LUFS) and true peak (≤ -1 dBTP), plus RMS per section when dialogue/music/end-card differ. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** (`delegate_task`) with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them. *(Venice path: these tags don't exist — infer beats from transcript + waveform.)*
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent via **`delegate_task`** with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not. The brief must be self-contained (a sub-agent has no parent context), reference files by absolute path, and demand a single JSON output.

```
You are editing a <type> video. Pick the best take of each beat and
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - C:/abs/path/to/<edit>/takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Reason about the image, not a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again. Mental model is ASC CDL: `out = (in * slope + offset) ** power`, then global saturation.

**Example filter chains** (`grade.py --list-presets`):
- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else, invent your own chain; `grade.py --filter '<raw ffmpeg>'` accepts any filter string. Hard rules: apply **per-segment during extraction** (not post-concat), never go aggressive without testing skin tones.

## Subtitles (when requested)

Three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), **placement** (margin from bottom).

**Worked style — `bold-overlay`** (short-form tech launch, fast-paced social): ~2-word chunks, UPPERCASE, break on punctuation and pauses ≥ 0.3s, grow to 3 words rather than flash a cue < 0.35s (`chunk_words` in `render.py`), Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships this as `SUB_FORCE_STYLE` (note: upstream raised MarginV to 90 to clear vertical safe zones — keep ≥ ~75 unless you have a reason).

Invent another style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation.

**Tool options** (pick per slot; don't default to Remotion just because the animation is web-adjacent):
- **HyperFrames** — Browser-native HTML/CSS/GSAP compositions (needs Node 22+). Scaffold in the slot dir with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, lint/validate, render to `render.mp4` (or WebM for alpha).
- **Remotion** — React/CSS compositions (project-local `remotion render`).
- **Manim** — formal diagrams, state machines, equation derivations.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, bar reveals. Fast, any aesthetic. The launch video used this.

**Duration rules of thumb (context-dependent):**
- Sync-to-narration explanations: floor 3s, typical 5–7s simple cards, 8–14s complex diagrams.
- Beat-synced accents: 0.5–2s fine.
- Hold the final frame ≥ 1s before the cut (universal).
- Over voiceover: total duration ≥ `narration_length + 1s` (universal).
- Never parallel-reveal independent elements.

**Payoff timing:** get the payoff word's timestamp; start the overlay `reveal_duration` earlier so the landing frame coincides with the spoken payoff word.

**Easing** (universal — never `linear`): `ease_out_cubic` for single reveals, `ease_in_out_cubic` for continuous draws (see SKILL.md upstream or your own copy). **Typing text anchor:** center on the FULL string's width, not the partial string.

**Fonts fail silently.** In HyperFrames/Remotion, await the font load and assert it: `if (!document.fonts.check('700 76px "Inter"')) throw new Error(...)`. In PIL, pass an explicit font path — on Windows use `C:/Windows/Fonts/...` paths; never rely on the default.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via **`delegate_task`**. Each prompt is self-contained (sub-agents have no parent context). Include:
1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or a design system)
5. Font path with index (Windows: `C:/Windows/Fonts/...`)
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames; parallel agents don't overwrite each other).

## Music and sound effects (when requested)

- **Fewer effects.** Every effect tied to something visible (a cut, a landing, a click). ~20 stock whooshes/risers/impacts in 18s reads as generic; ~8 reads as designed.
- **Hit on the frame.** Measure the effect's attack (first sample above ~-30 dBFS of the peak) and start the file `attack` seconds *before* the visible contact frame.
- **Duck music under speech** (roughly -12 to -15 dB relative to its music-only level); ramp out before a stinger or end card.
- **Master once:** mix to PCM, then two-pass loudnorm (-14 LUFS, true peak ≤ -1 dBTP) on the final mix (`render.py` does this; `--no-loudnorm` to skip). Then measure per section.
- **Music taste is the user's call.** Offer two contrasting beds and let the user listen. Don't claim a mix sounds good — you can only measure it.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--fps` for other rates. Worth asking which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "C:/abs/path/C0103.MP4", "C0108": "C:/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim. *(The Venice adapter synthesizes words — but treat it as degraded verbatim, not as a reason to use SRT mode.)*
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use the hosted adapter path.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
