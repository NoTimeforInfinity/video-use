# hermes-venice — video-use for the Venice.ai stack

This branch adds one self-contained layer, `hermes-venice/`, on top of an
otherwise untouched `browser-use/video-use` tree. It is the maintained,
agent-drivable video pipeline this machine actually runs on: the upstream
editing *craft* (the 12 Hard Rules, the EDL schema, the process) ported to
Hermes Agent, with transcription as a provider adapter whose default path is
**Venice.ai** (`/audio/transcriptions`, no paid key needed) and whose
fidelity path is **ElevenLabs Scribe** when a key is present.

Upstream is pinned at `b877063835e6ea6e457124da7e28a0ae26691dc3`
(2026-09-23, "render: phrase-aware captions, fail on a missing subtitles
file; skill: sound, fonts, critic pass (#183)").

---

## What this fork is

- **Same craft, different agent.** The upstream skill assumes Claude Code's
  `Agent` tool and `~/.claude/skills`. We run under Hermes Agent:
  sub-agents go through `delegate_task`, the skill installs into the Hermes
  skills tree, and all session outputs stay in `<videos_dir>/edit/`.
- **Venice-first transcription.** The default provider is Venice's
  OpenAI-shaped `/audio/transcriptions`. ElevenLabs Scribe remains the
  primary high-fidelity path and is auto-selected when `ELEVENLABS_API_KEY`
  resolves.
- **Everything runs on this Windows box.** ffmpeg 8.1 (gyan build: libass,
  zscale/tonemap, loudnorm, ebur128, signalstats), Python 3.11
  (`requests`, `numpy`, `pillow`), `yt-dlp` on demand.
- **One maintenance contract:** upstream files are never edited here, so
  `git merge upstream/main` stays conflict-free forever and our layer is
  trivially diffable against the frozen upstream text. See below.

---

## Layer layout

```
hermes-venice/                     ← the only directory we change
├── SKILL.md                       ← the skill (Hermes frontmatter, 12 Hard
│                                    Rules verbatim, delegate_task glue)
├── references/
│   ├── upstream-rules.md          ← upstream SKILL.md frozen verbatim,
│   │                                SHA-stamped for honest diffing
│   └── backends.md                ← which tool runs which operation
├── helpers/
│   ├── transcribe.py              ← provider adapter: ElevenLabs | Venice
│   ├── transcribe_batch.py        ← 4-worker parallel transcription
│   ├── pack_transcripts.py        ← transcripts/*.json → takes_packed.md
│   ├── timeline_view.py           ← filmstrip + waveform PNGs
│   ├── render.py                  ← per-segment extract → concat → overlays
│   │                                → subtitles LAST → loudnorm
│   ├── grade.py                   ← filter-chain grade (presets / raw / auto)
│   └── verify.py                  ← the self-eval gate (no upstream equivalent)
└── tests/                         ← 5 files, 36 tests, all passing
    ├── test_transcribe_adapter.py
    ├── test_render_captions.py
    ├── test_render_fps.py
    ├── test_render_orientation.py
    └── test_verify_cutedges.py
```

All paths inside the skill are relative to the skill directory
(`<skill_dir>/helpers/…`), so the same tree works in this repo subdir and
installed into a Hermes skills tree. `transcribe.py` locates its own `.env`
as `<skill_dir>/.env` via `Path(__file__).resolve().parent.parent` — that
resolves to the repo subdir here and to the installed skill dir there.

## What changed and where

| Upstream file | Our layer file | Why |
|---|---|---|
| `SKILL.md` | `hermes-venice/SKILL.md` | Ported: Hermes frontmatter (`name: video-use-hermes`), `delegate_task` replaces Claude's `Agent` tool, provider-adapter transcription. The 12 Hard Rules are kept **verbatim** (fidelity-loss notes added inline, e.g. Hard Rule 8). |
| `helpers/transcribe.py` | `hermes-venice/helpers/transcribe.py` | **Rewritten** as a provider adapter. Normalizes both providers into one word contract `{type, text, start, end, speaker_id}`; Venice path is auto-selected when no ElevenLabs key. |
| `helpers/render.py` | `hermes-venice/helpers/render.py` | Near-verbatim upstream + one Windows fix: subtitle paths are normalized to forward slashes before the `:` escape so `C:\…` drive letters survive libass. |
| `helpers/grade.py`, `timeline_view.py`, `pack_transcripts.py`, `transcribe_batch.py` | same names under `hermes-venice/helpers/` | Upstream-derived with minor Windows/Hermes adaptations. |
| *(none)* | `hermes-venice/helpers/verify.py` | **New.** Upstream has no self-eval gate; ours renders boundary filmstrips, runs ebur128, and checks every EDL cut edge lands on a word boundary (Rule 6). |
| `README.md` | `README-HERMES-VENICE.md` (this file) | Upstream README is untouched; our documentation is a separate top-level file. |
| `LICENSE` | *(kept, untouched, repo root)* | Upstream MIT, Copyright (c) 2026 Browser Use. See License & attribution below. |
| `install.md` | instructions in this README (§ Installation) | Hermes install is copy-into-skills-dir, not symlinks (Windows). |
| `skills/manim-video/` | *(not vendored into the layer)* | See "What is not ours". |

## The maintenance contract

1. **Upstream files are never edited in this branch.** Every change lives in
   `hermes-venice/`.
2. Therefore `git merge upstream/main` is always conflict-free — rebase/merge
   freely, upstream can never collide with our layer.
3. Our layer is the only place we change things, so `git diff upstream/main`
   shows exactly our footprint, and `references/upstream-rules.md` (frozen
   verbatim, SHA-stamped) is the honest diffing surface for the skill text.
4. When we adopt an upstream change, re-copy + re-stamp
   `references/upstream-rules.md` with the new SHA so the next diff is honest.

## Installation into a Hermes agent

Windows does not allow unprivileged symlinks, so install by **copy** (or an
NTFS directory junction if you prefer one location to update both):

- The layer is a self-contained skill directory. Copy `hermes-venice/` to
  `<hermes_home>/skills/video/video-use-hermes/` (or use `mklink /J` for a
  junction). Do not try symlinks.
- The skill registers itself from its `SKILL.md` frontmatter
  (`name: video-use-hermes`, description starting "Use when a video must be
  edited or assembled from takes…").
- No global deps beyond: Python 3.11 + `requests` `numpy` `pillow`; `ffmpeg`
  and `ffprobe` on PATH (must include libass, zscale/tonemap, loudnorm,
  ebur128, signalstats — the gyan 8.1 build has all of them).
- Node.js/npm only if a session needs HyperFrames/Remotion slots; `yt-dlp`
  is installed lazily for URL sources.

API keys: never write a key into the user's `<videos_dir>`. Keys resolve
`--config FILE` > `$VIDEO_USE_CONFIG` > `<skill_dir>/.env` > `./.env` >
environment. Prefer `$VIDEO_USE_CONFIG` pointing at a secrets file outside
the skill dir.

## Transcription providers

`helpers/transcribe.py` is a provider adapter. Both providers normalize into
one word contract (`type ∈ word | spacing | audio_event`); every downstream
helper reads only that contract. Selection: `--provider auto` (default)
uses ElevenLabs if `ELEVENLABS_API_KEY` resolves, else Venice;
`--provider elevenlabs` forces Scribe; `--provider venice` forces the
adapter. The transcript JSON carries a `_provider` and `_fidelity` block so
the loss is visible at a glance.

| Capability | ElevenLabs Scribe (fidelity path) | Venice `/audio/transcriptions` (default here) |
|---|---|---|
| Word timestamps | True, from the model | **Estimates** — segment-level only; words are split proportionally by character length |
| Diarization / speaker ids | Yes | **No** (`speaker_id` always `None`) |
| Audio-event tags `(laughs)` | Yes | **No** |
| Fillers | Verbatim | **Normalized** by the Whisper family (violates Hard Rule 8's verbatim intent) |
| Non-speech audio | Rejected/tagged | **Can hallucinate words** — verified on this box: a 440 Hz sine was transcribed as `*phone ringing*` |
| Upload cap | n/a | 25 MB |
| Key | `ELEVENLABS_API_KEY` | `VENICE_API_KEY` / `VENICE_INFERENCE_KEY` (present in the env here) |

Implication: Venice keeps the pipeline fully headless and key-free, but the
fidelity facts above are the reason the cut-padding constants (30–200 ms,
tuned to Scribe's 50–100 ms drift) may need retuning per provider, and the
reason hard-fidelity edits should route through Scribe. The adapter never
hides this: the transcript JSON says which provider produced it and what was
lost.

## Running the pipeline

The verified headless smoke test ran this exact sequence (generated test
clips, no LLM taste calls needed):

```bash
# 0. make three test clips (speech-like tone + sine probe) and transcribe
ffmpeg -f lavfi -i testsrc=duration=9:size=1280x720:rate=30 -i audio_a.wav \
       -c:v libx264 -c:a aac clipA.mp4
# … clipB.mp4, clipC_sine.mp4 (a 440 Hz sine — the hallucination probe)

# 1. inventory + transcribe (Venice adapter, 4 workers) + pack
ffprobe clipA.mp4
python hermes-venice/helpers/transcribe_batch.py <videos_dir> --provider venice
python hermes-venice/helpers/pack_transcripts.py --edit-dir <videos_dir>/edit

# 2. author edl.json (2 ranges, word-boundary-snapped) — see hermes-venice/SKILL.md

# 3. render: per-segment extract → concat → subtitles LAST → loudnorm
python hermes-venice/helpers/render.py <videos_dir>/edit/edl.json \
       -o <videos_dir>/edit/final.mp4

# 4. self-eval gate
python hermes-venice/helpers/verify.py --output <videos_dir>/edit/final.mp4 \
       --edl <videos_dir>/edit/edl.json
```

Real numbers from that smoke test (in `video-stack/eval/edit/verify/verify.json`):

- **3 takes transcribed** via the real Venice adapter (2 used in the edit +
  the sine probe). Final EDL: 2 ranges, `total_duration_s` 11.06 s; verify
  measured **11.174 s** (Δ 0.114 s, tolerance 0.5 s) → ≈11 s render.
- **`verdict: pass` on all 9 checks**: duration vs EDL; video stream
  (h264, 1920×1080@24, yuv420p); audio stream (aac, 48 kHz stereo);
  loudness (**−14.0 LUFS**, LRA 1.2, **true peak −1.0 dBTP**); audio-pop
  (no flag at the cut boundary); luma continuity; **4/4 cut edges 0 ms from a
  word boundary** (Rule 6); subtitles (15 cues); filmstrips (6 PNGs written).

## The self-eval gate — `verify.py`

Run before showing any result: it renders a filmstrip at every cut boundary
(±1.5 s) plus first 2 s / last 2 s / midpoints, computes duration, ebur128
(integrated + true peak + LRA), a per-boundary audio-pop score, and checks
every EDL cut edge lands on a word boundary. Emits pass/fail JSON, exit 0/1.
The filmstrip images still need a human/LLM look for jumps, flashes,
subtitle occlusion and overlay alignment; the numbers are objective. Cap at
3 passes — if issues remain, flag them rather than looping.

## Keeping in sync with upstream

`video-stack/upstream/watch-video-use.py` (fetch-only, **never merges**) +
`pins.json` drive a weekday cron: it fetches upstream, diffs commits touching
`SKILL.md`, `helpers/`, `install.md`, `skills/`, appends new watched commits
to `upstream-queue.md`, and advances the pin. Adoption is always a
deliberate, SHA-stamped act (re-copy + re-stamp `references/upstream-rules.md`).

## License & attribution

This fork is a derivative of **[browser-use/video-use]**, MIT licensed,
Copyright (c) 2026 Browser Use. The repo-root `LICENSE` is upstream's and is
**unchanged**. Our layer `hermes-venice/` is our own work under the same
MIT terms; see `hermes-venice/NOTICE.md`. Attribution stays intact.

## What is not ours

- **`skills/manim-video/`** is a vendored third-party skill inside the
  upstream tree (a Manim Community Edition production pipeline). It has **no
  separate license file** — it ships under the parent repo's MIT license
  (Copyright (c) 2026 Browser Use). We do not vendor it into `hermes-venice/`;
  it is available in the upstream tree and referenced by the skill for Manim
  animation slots.
- The upstream `poster.html`, `static/`, `.env.example`, `install.md` and the
  upstream `README.md` are all upstream's, untouched.
- ArtCraft (FilmCraft/EffectCraft/PhotoCraft) is documented in
  `hermes-venice/references/backends.md` as a possible P3 backend but is
  **not wired** and must not be assumed capable of anything that table does
  not explicitly claim.
