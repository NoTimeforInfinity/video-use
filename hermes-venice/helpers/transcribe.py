"""Transcribe a video through a provider adapter.

Every downstream helper reads ONLY `payload["words"]`, whose entries use the
contract keys {type, text, start, end, speaker_id} with type in
'word' | 'spacing' | 'audio_event'. This module normalizes every provider into
that contract so swapping backends is a config change, not a rewrite.

Providers:
  elevenlabs — ElevenLabs Scribe (primary). Verbatim + diarize + audio events
               + true word-level timestamps. Requires ELEVENLABS_API_KEY.
  venice     — Venice /audio/transcriptions (OpenAI-compatible). Segment-level
               timestamps ONLY. Words are synthesized by proportional length
               interpolation inside each segment; 'spacing' entries are
               synthesized from the gaps between synthesized words.
               FIDELITY LOSS vs Scribe (documented, not hidden):
                 - No diarization: every speaker_id is None. Multi-speaker
                   takes lose their S0/S1 separation in takes_packed.md.
                 - No audio events: (laughs)/(applause) are not tagged. They
                   are either transcribed as words or dropped by the ASR.
                 - Whisper-family normalizes fillers (um/uh often deleted) —
                   violates the verbatim-only Hard Rule. The editorial filler
                   signal is lost.
                 - Word boundaries are ESTIMATES (proportional split), tuned to
                   nothing. Scribe's 50-100ms cut padding assumptions may need
                   retuning per provider.
                 - Non-speech audio can be HALLUCINATED as words (e.g. a pure
                   440 Hz tone transcribed as "*phone ringing*"). Guard against
                   tones/music-only sources before transcribing.
                 - 25 MB upload cap: long takes must be chunked client-side
                   (not implemented); Scribe accepts larger files.
  auto       — env VIDEO_USE_TRANSCRIBE_PROVIDER if set; else use elevenlabs
               when ELEVENLABS_API_KEY resolves, else venice.

Output: <edit_dir>/transcripts/<video_stem>.json (cached — never re-upload a
source that already has a transcript).

Config resolution (for API keys): --config FILE > $VIDEO_USE_CONFIG >
<skill_dir>/.env > cwd/.env > environment. Keys are read from the first file
that exists.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --provider venice
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language en
    python helpers/transcribe.py <video_path> --num-speakers 2
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import requests


SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
VENICE_URL = "https://api.venice.ai/api/v1/audio/transcriptions"

PROVIDERS = ("auto", "elevenlabs", "venice")


def _dotenv_paths() -> list[Path]:
    """Candidate .env files, in resolution order."""
    skill_root = Path(__file__).resolve().parent.parent
    explicit = os.environ.get("VIDEO_USE_CONFIG")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates += [skill_root / ".env", Path(".env")]
    return candidates


def load_config() -> dict[str, str]:
    """Parse the first existing .env candidate into a dict."""
    for candidate in _dotenv_paths():
        if candidate.exists():
            cfg: dict[str, str] = {}
            for line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
            return cfg
    return {}


def get_secret(*names: str) -> str:
    """First non-empty value: environment variable, then config file."""
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    cfg = load_config()
    for n in names:
        v = cfg.get(n, "")
        if v:
            return v
    return ""


def resolve_provider() -> str:
    """Pick the transcription provider: explicit env > key availability."""
    override = os.environ.get("VIDEO_USE_TRANSCRIBE_PROVIDER", "").strip().lower()
    if override:
        if override not in PROVIDERS:
            sys.exit(f"VIDEO_USE_TRANSCRIBE_PROVIDER='{override}' invalid; "
                     f"choose from {', '.join(PROVIDERS)}")
        return override
    if get_secret("ELEVENLABS_API_KEY"):
        return "elevenlabs"
    return "venice"


# -------- Audio prep (shared by all providers) -----------------------------


def count_audio_tracks(video_path: Path) -> int:
    """How many audio streams the container holds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    return len([ln for ln in out.stdout.splitlines() if ln.strip()])


def peak_dbfs(wav_path: Path) -> float:
    """Peak level of a 16-bit PCM wav, in dBFS. -inf for digital silence."""
    peak = 0
    with wave.open(str(wav_path), "rb") as w:
        while frames := w.readframes(1 << 16):
            samples = array.array("h", frames)
            peak = max(peak, max(samples), -min(samples))
    return 20 * math.log10(peak / 32768) if peak > 0 else float("-inf")


def extract_audio(video_path: Path, dest: Path, audio_track: int = 0) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-map", f"0:a:{audio_track}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def duration_of_wav(wav_path: Path) -> float:
    """Duration in seconds of a PCM wav (fallback when the provider omits it)."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError, ZeroDivisionError):
        return 0.0


def transcript_path(edit_dir: Path, video: Path, audio_track: int = 0) -> Path:
    """Where a video's transcript lands (track-suffixed when not track 0)."""
    suffix = "" if audio_track == 0 else f".track{audio_track}"
    return edit_dir / "transcripts" / f"{video.stem}{suffix}.json"


# -------- Provider: ElevenLabs Scribe (primary) -----------------------------


def call_scribe(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
) -> dict:
    data: dict[str, str] = {
        "model_id": "scribe_v1",
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language
    if num_speakers:
        data["num_speakers"] = str(num_speakers)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            SCRIBE_URL,
            headers={"xi-api-key": api_key},
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Scribe returned {resp.status_code}: {resp.text[:500]}")

    payload = resp.json()
    payload.setdefault("_provider", "elevenlabs")
    return payload


# -------- Provider: Venice (adapter — synthesizes the words contract) ------


def _split_segment_words(seg: dict, fallback_duration: float) -> list[dict]:
    """Proportionally split a Whisper segment's [start,end] across its words.

    Venice returns only segment-level timestamps, so word boundaries are
    ESTIMATES: each token is allotted time proportional to its character
    length within the segment span. There is no way to recover true word
    boundaries from this response — documented fidelity loss.
    """
    text = (seg.get("text") or "").strip()
    if not text:
        return []
    start = float(seg.get("start") or 0.0)
    end = float(seg.get("end") or (start + fallback_duration))
    if end <= start:
        end = start + max(fallback_duration, 0.1)
    tokens = text.split()
    total_chars = sum(len(t) for t in tokens) or 1
    words: list[dict] = []
    t = start
    for tok in tokens:
        dur = (len(tok) / total_chars) * (end - start)
        words.append({
            "type": "word",
            "text": tok,
            "start": round(t, 3),
            "end": round(t + dur, 3),
            "speaker_id": None,
        })
        t += dur
    if words and abs(words[-1]["end"] - end) > 0.001:
        words[-1]["end"] = round(end, 3)
    return words


def _synthesize_spacing(words: list[dict], min_gap: float = 0.03) -> list[dict]:
    """Build 'spacing' entries from gaps between consecutive synthesized words.

    Mirrors Scribe's spacing entries (start/end = the gap) so pack_transcripts
    and timeline_view behave identically to the ElevenLabs path. The result is
    a single time-ordered list: each spacing entry sits between the word it
    follows and the word it precedes. (Index-based interleaving is WRONG here —
    spacing[i] is the gap after words[i] only when every adjacent pair is
    contiguous, which is not true for multi-segment responses.)
    """
    spacing: list[dict] = []
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap >= min_gap:
            spacing.append({
                "type": "spacing",
                "text": "",
                "start": round(a["end"], 3),
                "end": round(b["start"], 3),
                "speaker_id": None,
            })
    merged = sorted(words + spacing, key=lambda e: e["start"])
    return merged


def normalize_venice_payload(payload: dict, duration: float) -> dict:
    """Normalize a Venice response into the words contract.

    Adds an explicit `_fidelity` block so downstream/readers can see the loss
    at a glance rather than discovering it in the timestamps.
    """
    words: list[dict] = []
    segments = (payload.get("timestamps") or {}).get("segment") or []
    if segments:
        for seg in segments:
            words.extend(_split_segment_words(seg, fallback_duration=duration / max(len(segments), 1)))
    else:
        text = (payload.get("text") or "").strip()
        if text:
            words.extend(_split_segment_words(
                {"text": text, "start": 0.0, "end": duration}, fallback_duration=duration))

    words = _synthesize_spacing(words)

    normalized = {
        "text": payload.get("text") or "",
        "language": payload.get("language"),
        "duration": payload.get("duration", duration),
        "words": words,
        "_provider": "venice",
        "_fidelity": {
            "diarization": "absent — every speaker_id is None",
            "audio_events": "absent — (laughs)/(applause) not tagged",
            "verbatim": "not guaranteed — Whisper-family normalizes fillers",
            "word_boundaries": "estimated by proportional segment split (not measured)",
            "hallucination": "possible on non-speech audio (tones/music transcribed as words)",
            "upload_cap_mb": 25,
        },
    }
    return normalized


def call_venice(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    duration: float = 0.0,
) -> dict:
    data: dict[str, str] = {
        "model": os.environ.get("VIDEO_USE_VENICE_ASR_MODEL", "openai/whisper-large-v3"),
        "response_format": "json",
        "timestamps": "true",
    }
    if language:
        data["language"] = language

    with open(audio_path, "rb") as f:
        resp = requests.post(
            VENICE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Venice returned {resp.status_code}: {resp.text[:500]}")

    raw = resp.json()
    dur = float(raw.get("duration") or duration or 0.0)
    return normalize_venice_payload(raw, dur)


# -------- Orchestration ------------------------------------------------------


def transcribe_one(
    video: Path,
    edit_dir: Path,
    provider: str | None = None,
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
    audio_track: int = 0,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcript_path(edit_dir, video, audio_track)

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    provider = provider or resolve_provider()

    if verbose:
        print(f"  extracting audio from {video.name}  (provider={provider})", flush=True)

    n_tracks = count_audio_tracks(video)
    if n_tracks > 1 and verbose:
        print(f"  note: {video.name} has {n_tracks} audio tracks, using track "
              f"{audio_track + 1} (--audio-track to change)", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio, audio_track)

        peak = peak_dbfs(audio)
        if peak < -60.0:
            raise RuntimeError(
                f"track {audio_track + 1} of {video.name} is silent "
                f"(peak {peak:.1f} dBFS) - not uploading. "
                + (f"The file has {n_tracks} audio tracks; try --audio-track "
                   + " or ".join(str(i) for i in range(n_tracks) if i != audio_track) + "."
                   if n_tracks > 1 else "Check the source audio.")
            )

        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  uploading {video.stem}.wav ({size_mb:.1f} MB)", flush=True)

        if provider == "elevenlabs":
            api_key = get_secret("ELEVENLABS_API_KEY")
            if not api_key:
                sys.exit("ELEVENLABS_API_KEY not found (config file or environment)")
            payload = call_scribe(audio, api_key, language, num_speakers)
        elif provider == "venice":
            api_key = get_secret("VENICE_API_KEY", "VENICE_INFERENCE_KEY")
            if not api_key:
                sys.exit("VENICE_API_KEY / VENICE_INFERENCE_KEY not found (config file or environment)")
            payload = call_venice(audio, api_key, language, duration=duration_of_wav(audio))
        else:  # pragma: no cover — resolve_provider never returns this
            sys.exit(f"unknown provider: {provider}")

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        if isinstance(payload, dict) and "words" in payload:
            print(f"    words: {len(payload['words'])}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video via a provider adapter")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--provider",
        type=str,
        choices=PROVIDERS,
        default=None,
        help="elevenlabs | venice | auto (default auto: ELEVENLABS key -> elevenlabs, else venice)",
    )
    ap.add_argument(
        "--edit-dir", type=Path, default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language", type=str, default=None,
        help="Optional ISO language code (e.g., 'en'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--num-speakers", type=int, default=None,
        help="Optional number of speakers (ElevenLabs only). Improves diarization.",
    )
    ap.add_argument(
        "--audio-track", type=int, default=0,
        help="Zero-based audio track to transcribe (OBS: 0 = game, 1 = mic).",
    )
    ap.add_argument(
        "--config", type=Path, default=None,
        help="Explicit .env file for API keys (overrides $VIDEO_USE_CONFIG and defaults).",
    )
    args = ap.parse_args()

    if args.config:
        os.environ["VIDEO_USE_CONFIG"] = str(args.config.resolve())

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        provider=args.provider,
        language=args.language,
        num_speakers=args.num_speakers,
        audio_track=args.audio_track,
    )


if __name__ == "__main__":
    main()
