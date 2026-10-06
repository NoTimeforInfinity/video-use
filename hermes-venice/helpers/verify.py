"""Self-eval sweep for a rendered video. The 'verify' half of the skill loop.

Turns the skill's step-7 self-eval procedure into a runnable pass/fail check on
the RENDERED output (not the sources):

  - ffprobe the output: duration, resolution, fps, audio codec
  - duration vs EDL total_duration_s
  - ebur128 integrated loudness / true peak / LRA
  - a filmstrip PNG at every cut boundary (±1.5 s), plus first 2 s, last 2 s
    and midpoints — the images a human or LLM inspects for jump/flash/misalign
  - an audio pop check at every boundary (30 ms fade failure -> impulse)
  - subtitle file + cue-count check when the EDL references one
  - a luma-continuity number at each boundary (informational)

Outputs a machine-readable pass/fail JSON. Exit code 0 = all hard checks pass,
1 = at least one failed check. The filmstrip images are the *visual* evidence
that a model still has to look at — this script produces them and computes the
objective numbers, it does not pretend to see.

Usage:
    python helpers/verify.py --output final.mp4 --edl edl.json
    python helpers/verify.py --output final.mp4 --edl edl.json --json verify.json
    python helpers/verify.py --output out.mp4 --boundaries 3.2,6.7 --duration 8.9
    python helpers/verify.py --output out.mp4   # first/last/midpoints only
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

# Make sibling helpers importable regardless of cwd
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from timeline_view import render_timeline  # noqa: E402


# -------- thresholds (heuristics — tuned to Scribe drift, documented) -------

DURATION_TOLERANCE_S = 0.5        # output vs EDL total
LUFS_TARGET = -14.0
LUFS_TOLERANCE = 2.0              # integrated loudness may sit in [-16, -12]
TRUE_PEAK_MAX_DBTP = -0.5         # loudnorm targets -1 dBTP; allow slack
POP_RATIO_FLAG = 4.0              # boundary impulse vs median |diff| > 4x
POP_STEP_FLOOR = 512.0            # single-sample step must exceed ~-36 dBFS
POP_NOISE_FLOOR = 16.0            # ~-66 dBFS — silence must not inflate ratios
LUMA_JUMP_DELTA = 60.0            # mean-luma delta across a cut > 60/255 = jarring


def ffprobe_duration(path: Path) -> float | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True)
    try:
        return json.loads(out.stdout).get("streams", [])
    except json.JSONDecodeError:
        return []


def ebur128(path: Path) -> dict[str, float]:
    """Run ebur128 and parse the SUMMARY block (integrated loudness, LRA, true peak).

    ebur128 prints one line per measurement block AND a summary at the end.
    The blocks before real speech start can read near-silence (-70 LUFS); only
    the summary line is meaningful. We take the LAST match of each metric.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = proc.stderr
    res: dict[str, float] = {}
    for key, pat in (
        ("integrated_lufs", r"I:\s+(-?[\d.]+)\s+LUFS"),
        ("lra_lu", r"LRA:\s+([\d.]+)\s+LU"),
        ("true_peak_dbtp", r"Peak:\s+(-?[\d.]+)\s+dB(?:TP|FS)"),
    ):
        matches = re.findall(pat, stderr)
        if matches:
            res[key] = float(matches[-1])
    return res


def decode_pcm(path: Path, start: float, dur: float, rate: int = 48000) -> np.ndarray:
    """Decode a short mono window to int16 samples via ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "w.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
             "-t", f"{dur:.3f}", "-i", str(path), "-ac", "1", "-ar", str(rate),
             "-c:a", "pcm_s16le", str(wav)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raw = wav.read_bytes()
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32)


def audio_pop_score(path: Path, boundary: float, window: float = 0.25) -> dict:
    """Impulse detection at a cut boundary.

    A failed 30 ms fade leaves a sample discontinuity exactly at the cut. We
    decode a window straddling the boundary, take the absolute first-difference
    (a cheap high-pass), and compare the maximum |diff| within ±2 ms of the
    boundary to the window median. A real pop is an outlier impulse; speech
    transients are comparatively smooth. ratio = max(spike zone) / median.
    """
    samples = decode_pcm(path, boundary - window, window * 2)
    if samples.size < 10:
        return {"ok": False, "detail": "could not decode boundary audio"}
    diff = np.abs(np.diff(samples))
    center = int(round(window * 48000))
    half = max(1, int(0.002 * 48000))  # ±2 ms
    lo, hi = max(0, center - half), min(diff.size, center + half)
    spike_zone = diff[lo:hi]
    baseline = float(np.median(diff)) if diff.size else 0.0
    # Silence must not inflate the ratio: floor the reference at ~-66 dBFS and
    # require the spike to be genuinely large in absolute terms as well. A
    # speech onset 250ms into the fade is a *transient*, not a pop — a real
    # 30ms-fade failure is a single-sample discontinuity at the cut itself.
    ref = max(baseline, POP_NOISE_FLOOR)
    peak = float(np.max(spike_zone)) if spike_zone.size else 0.0
    ratio = peak / ref if ref > 0 else (0.0 if peak == 0 else 99.0)
    flag = peak > POP_STEP_FLOOR and ratio > POP_RATIO_FLAG
    return {
        "boundary_s": round(boundary, 3),
        "ratio": round(ratio, 2),
        "peak_step": round(peak, 1),
        "median_step": round(baseline, 1),
        "flag": flag,
    }


def luma_delta_across_cut(path: Path, boundary: float, gap: float = 0.06) -> float:
    """Mean-luma delta between a frame just before and just after a cut."""
    vals = []
    for offset in (-gap, gap):
        t = boundary + offset
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "f.png"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                 "-i", str(path), "-frames:v", "1", str(out)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            img = np.asarray(Image.open(out).convert("L"), dtype=np.float32)
        vals.append(float(img.mean()))
    return abs(vals[0] - vals[1]) if len(vals) == 2 else -1.0


def edl_boundaries(edl: dict) -> tuple[list[float], float | None]:
    """Output-timeline cut boundaries + total duration from an EDL.

    Boundaries are the cumulative ends of each range (the instant the next
    range starts). The final cumulative sum is the total_duration_s check.
    """
    boundaries: list[float] = []
    offset = 0.0
    for r in edl.get("ranges", []):
        try:
            dur = float(r["end"]) - float(r["start"])
        except (KeyError, TypeError, ValueError):
            continue
        offset += dur
        boundaries.append(round(offset, 3))
    if not boundaries:
        return [], None
    total = boundaries[-1]
    boundaries = boundaries[:-1]  # last edge is the end, not a cut
    return boundaries, total


def midpoints(duration: float, n: int = 3) -> list[float]:
    return [round(duration * (i + 1) / (n + 1), 2) for i in range(n)]


def check_cut_edges(edl: dict, edit_dir: Path) -> dict:
    """Enforce Hard Rule 6 in code: never cut inside a word.

    For every EDL range edge, load the source transcript and check the edge is
    not strictly inside a word. An edge is also warned when it sits more than
    ~250ms from the nearest word boundary (Hard Rule 7's 30-200ms pad window,
    plus tolerance for provider drift).
    """
    transcripts_dir = edit_dir / "transcripts"
    results: list[dict] = []
    sources = edl.get("sources") or {}
    for r in edl.get("ranges", []):
        src = r.get("source")
        tr = transcripts_dir / f"{src}.json"
        if not tr.exists():
            results.append({"source": src, "status": "info",
                            "detail": "no transcript to check against"})
            continue
        words = [w for w in json.loads(tr.read_text(encoding="utf-8")).get("words", [])
                 if w.get("type") == "word" and w.get("start") is not None]
        words.sort(key=lambda w: w["start"])
        for edge_name, t in (("start", float(r["start"])), ("end", float(r["end"]))):
            inside = [w for w in words if w["start"] < t < w["end"]]
            if inside:
                w = inside[0]
                results.append({"source": src, "edge": edge_name, "time_s": round(t, 3),
                                "status": "fail",
                                "detail": f"cut inside word '{w.get('text')}' "
                                          f"({w['start']:.2f}-{w['end']:.2f})"})
                continue
            prev_end = max((w["end"] for w in words if w["end"] <= t), default=t)
            next_start = min((w["start"] for w in words if w["start"] >= t), default=t)
            pad = min(t - prev_end, next_start - t)
            status = "pass" if pad <= 0.25 else "warn"
            results.append({"source": src, "edge": edge_name, "time_s": round(t, 3),
                            "status": status,
                            "detail": f"{pad*1000:.0f}ms from nearest word boundary"})
    return {"results": results,
            "verdict": "fail" if any(x["status"] == "fail" for x in results) else
                       ("warn" if any(x["status"] == "warn" for x in results) else "pass")}


def render_filmstrips(video: Path, times: list[tuple[str, float]], out_dir: Path,
                      duration: float | None = None) -> list[dict]:
    """Render a ±1.5 s filmstrip for each named time. Returns metadata list.

    The window is clamped to [0, duration] so a 'last' sample never asks for
    frames past EOF (which would abort the whole strip).
    """
    strips: list[dict] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, t in times:
        start = max(0.0, t - 1.5)
        # Clamp to just short of the true content end. ffprobe's format
        # duration includes trailing AAC priming (e.g. 11.174s vs 11.06s of
        # content), so seeking to the estimate itself can overshoot the last
        # decodable video frame. The EDL total is the authoritative content end.
        end = min(t + 1.5, duration - 0.15) if duration else (t + 1.5)
        if end <= start:
            end = start + 0.1
        out = out_dir / f"{label}_{t:06.2f}s.png"
        try:
            render_timeline(
                video=video, start=start, end=end, out_path=out,
                n_frames=10, transcript=None)
            strips.append({"label": label, "time_s": t, "png": str(out),
                           "bytes": out.stat().st_size})
        except Exception as e:  # noqa: BLE001
            strips.append({"label": label, "time_s": t, "png": str(out),
                           "error": str(e)})
    return strips


def run_checks(output: Path, edl: dict | None, edl_path: Path | None,
               boundaries: list[float], total: float | None, out_dir: Path) -> tuple[dict, int]:
    checks: list[dict] = []
    verdict = "pass"

    def add(name: str, status: str, value, detail: str = "") -> None:
        nonlocal verdict
        checks.append({"check": name, "status": status, "value": value, "detail": detail})
        if status == "fail":
            verdict = "fail"

    # --- duration ---
    actual_dur = ffprobe_duration(output)
    expected = total if total is not None else (
        edl.get("total_duration_s") if edl else None)
    if expected is not None and actual_dur is not None:
        delta = abs(actual_dur - expected)
        add("duration_vs_edl",
            "pass" if delta <= DURATION_TOLERANCE_S else "fail",
            {"actual_s": round(actual_dur, 3), "edl_total_s": round(float(expected), 3),
             "delta_s": round(delta, 3)},
            f"tolerance {DURATION_TOLERANCE_S}s")
    elif actual_dur is not None:
        add("duration", "info", round(actual_dur, 3), "no EDL total to compare")

    # --- streams / codecs ---
    streams = ffprobe_streams(output)
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v:
        fps = v.get("avg_frame_rate", "?")
        add("video_stream", "pass",
            {"codec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
             "fps": fps, "pix_fmt": v.get("pix_fmt")})
    else:
        add("video_stream", "fail", None, "no video stream in output")
    if a:
        add("audio_stream", "pass",
            {"codec": a.get("codec_name"), "channels": a.get("channels"),
             "sample_rate": a.get("sample_rate")})
    else:
        add("audio_stream", "fail", None, "no audio stream in output")

    # --- loudness ---
    loud = ebur128(output)
    if loud:
        lufs = loud.get("integrated_lufs")
        tp = loud.get("true_peak_dbtp")
        lufs_ok = lufs is not None and abs(lufs - LUFS_TARGET) <= LUFS_TOLERANCE
        tp_ok = tp is None or tp <= TRUE_PEAK_MAX_DBTP
        add("loudness",
            "pass" if (lufs_ok and tp_ok) else "fail",
            loud, f"target -{abs(LUFS_TARGET):.0f} LUFS ±{LUFS_TOLERANCE:.0f}, "
                  f"true peak ≤ {TRUE_PEAK_MAX_DBTP:+.1f} dBTP")
    else:
        add("loudness", "fail", None, "ebur128 produced no parseable numbers")

    # --- boundary audio pops + luma + filmstrips ---
    pop_ok = True
    for b in boundaries:
        if b <= 0 or (actual_dur and b >= actual_dur):
            continue
        score = audio_pop_score(output, b)
        if score.get("flag"):
            pop_ok = False
        add("audio_pop", "fail" if score.get("flag") else "pass", score,
            "impulse at cut = 30ms fade failure" if score.get("flag") else "")
    if not boundaries:
        add("audio_pop", "info", None, "no cut boundaries to check")

    luma_cuts: list[dict] = []
    for b in boundaries:
        if b <= 0 or (actual_dur and b >= actual_dur):
            continue
        d = luma_delta_across_cut(output, b)
        luma_cuts.append({"boundary_s": b, "mean_luma_delta": round(d, 2)})
    if luma_cuts:
        flagged = [c for c in luma_cuts if c["mean_luma_delta"] > LUMA_JUMP_DELTA]
        add("luma_continuity", "fail" if flagged else "pass", luma_cuts,
            "informational — inspect the filmstrip PNGs too")
    else:
        add("luma_continuity", "info", None, "no boundaries to sample")

    # --- cut edges must land on word boundaries (Hard Rule 6) ---
    if edl:
        ce = check_cut_edges(edl, output.parent)
        fails = [r for r in ce["results"] if r["status"] == "fail"]
        add("cut_edges_on_word_boundaries", "fail" if fails else "pass",
            ce["results"], "Rule 6: never cut inside a word")
    else:
        add("cut_edges_on_word_boundaries", "info", None, "no EDL to check against")

    # --- subtitles ---
    if edl and edl.get("subtitles"):
        srt = edl["subtitles"]
        srt_path = Path(srt)
        if not srt_path.is_absolute():
            srt_path = (output.parent / srt_path).resolve()
        if srt_path.exists():
            n_cues = sum(1 for ln in srt_path.read_text(encoding="utf-8", errors="ignore")
                         .splitlines() if re.match(r"^\d+$", ln.strip()))
            add("subtitles", "pass" if n_cues > 0 else "warn",
                {"file": str(srt_path), "cues": n_cues})
        else:
            add("subtitles", "fail", {"file": str(srt_path)}, "EDL references a missing SRT")
    else:
        add("subtitles", "info", None, "no subtitles in EDL")

    # --- filmstrips (first 2s, last 2s, midpoints, each boundary ±1.5s) ---
    # content_end: EDL total is authoritative; else the ffprobe estimate.
    content_end = total if (total and actual_dur and total < actual_dur) else (actual_dur or 0.0)
    dur = content_end
    sample_times: list[tuple[str, float]] = [("first", 1.0)]
    if dur > 1.5:
        sample_times.append(("last", max(0.0, dur - 1.5)))
        for i, m in enumerate(midpoints(dur)):
            sample_times.append((f"mid{i+1}", m))
        for i, b in enumerate(boundaries):
            if 0 < b < dur:
                sample_times.append((f"cut{i+1}", b))
    strips = render_filmstrips(output, sample_times, out_dir, duration=dur)
    add("filmstrips", "pass" if all("error" not in s for s in strips) else "warn",
        strips, "inspect PNGs for jump/flash/misaligned overlay/subtitle occlusion")

    result = {
        "output": str(output),
        "edl": str(edl_path) if edl_path else None,
        "verdict": verdict,
        "checks": checks,
    }
    return result, 0 if verdict == "pass" else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Self-eval sweep on a rendered video")
    ap.add_argument("--output", type=Path, required=True, help="Rendered video (final.mp4)")
    ap.add_argument("--edl", type=Path, default=None, help="edl.json (defines boundaries + total)")
    ap.add_argument("--boundaries", type=str, default=None,
                    help="Comma-separated cut times in output seconds (overrides EDL)")
    ap.add_argument("--duration", type=float, default=None,
                    help="Expected total duration in seconds (overrides EDL total)")
    ap.add_argument("--json", type=Path, default=None, help="Write pass/fail JSON here")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Filmstrip output dir (default: <output_parent>/verify)")
    args = ap.parse_args()

    output = args.output.resolve()
    if not output.exists():
        sys.exit(f"output not found: {output}")

    edl: dict | None = None
    if args.edl:
        edl_path = args.edl.resolve()
        if not edl_path.exists():
            sys.exit(f"edl not found: {edl_path}")
        edl = json.loads(edl_path.read_text())

    boundaries: list[float] = []
    total: float | None = args.duration
    if args.boundaries:
        boundaries = [float(x) for x in args.boundaries.split(",") if x.strip()]
    elif edl:
        boundaries, edl_total = edl_boundaries(edl)
        if total is None:
            total = edl_total
        if total is None:
            total = edl.get("total_duration_s")

    out_dir = (args.out_dir or (output.parent / "verify")).resolve()
    result, code = run_checks(output, edl, edl_path if args.edl else None,
                              boundaries, total, out_dir)

    print(json.dumps(result, indent=2))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"\nJSON → {args.json.resolve()}")
    print(f"\nverdict: {result['verdict']}")
    sys.exit(code)


if __name__ == "__main__":
    main()
