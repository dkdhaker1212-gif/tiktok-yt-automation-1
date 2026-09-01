"""ffmpeg edit pass: blur baked-in overlays + light de-dup transform.

Runs AFTER download, BEFORE upload. Everything is opt-in via channels.yaml
(`edit:` block). If ffmpeg errors, the pipeline falls back to the original file
so a bad filter can never drop a slot.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EditOptions:
    enabled: bool = True
    blur_watermark: bool = True          # blur the two TikTok watermark zones
    blur_regions: list = field(default_factory=list)  # extra [x%,y%,w%,h%] boxes
    zoom_crop_pct: float = 3.0           # crop N% off each edge, scale back (subtle zoom)
    speed: float = 1.03                 # 1.0 = off; retimes video + audio
    saturation: float = 1.05
    contrast: float = 1.03
    hflip: bool = False                 # mirror -- strong de-dup, changes framing
    fade_seconds: float = 0.25

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "EditOptions":
        cfg = cfg or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        return cls(**{k: v for k, v in cfg.items() if k in known})


def _probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(out.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    dur = st.get("duration") or data.get("format", {}).get("duration") or 0
    return {"w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
            "duration": float(dur or 0)}


# TikTok watermark alternates between two zones. Values are fractions of W/H.
_TT_ZONES = [
    (0.030, 0.780, 0.360, 0.090),   # lower-left
    (0.610, 0.070, 0.360, 0.090),   # upper-right
]


def _blur_chain(w: int, h: int, opts: EditOptions) -> list[str]:
    zones: list[tuple[float, float, float, float]] = []
    if opts.blur_watermark:
        zones += _TT_ZONES
    for r in opts.blur_regions or []:
        if len(r) == 4:
            zones.append((r[0] / 100, r[1] / 100, r[2] / 100, r[3] / 100))

    steps: list[str] = []
    src = "0:v"
    for i, (fx, fy, fw, fh) in enumerate(zones):
        x, y = int(fx * w), int(fy * h)
        bw, bh = int(fw * w), int(fh * h)
        x = max(0, min(x, w - 8)); y = max(0, min(y, h - 8))
        bw = max(8, min(bw, w - x)); bh = max(8, min(bh, h - y))
        steps.append(
            f"[{src}]split=2[b{i}a][b{i}b];"
            f"[b{i}b]crop={bw}:{bh}:{x}:{y},boxblur=18:2[b{i}c];"
            f"[b{i}a][b{i}c]overlay={x}:{y}[wm{i}]"
        )
        src = f"wm{i}"
    return steps, src


def process(in_path: str, out_path: str,
            cfg: Optional[dict] = None) -> str:
    """Return out_path on success, or in_path unchanged if editing is off/failed."""
    opts = EditOptions.from_cfg(cfg)
    if not opts.enabled:
        return in_path

    meta = _probe(in_path)
    w, h = meta["w"], meta["h"]
    if not (w and h):
        print("[video_editor] could not probe size; skipping edit")
        return in_path

    chain, last = _blur_chain(w, h, opts)
    vf = list(chain)

    # de-dup transform on the (possibly blurred) stream
    tail = []
    if opts.zoom_crop_pct and opts.zoom_crop_pct > 0:
        keep = 1 - (opts.zoom_crop_pct / 100.0)
        tail.append(f"crop=iw*{keep:.4f}:ih*{keep:.4f},scale={w}:{h}")
    eqs = []
    if opts.saturation and opts.saturation != 1.0:
        eqs.append(f"saturation={opts.saturation}")
    if opts.contrast and opts.contrast != 1.0:
        eqs.append(f"contrast={opts.contrast}")
    if eqs:
        tail.append("eq=" + ":".join(eqs))
    if opts.hflip:
        tail.append("hflip")
    if opts.speed and opts.speed != 1.0:
        tail.append(f"setpts=PTS/{opts.speed}")
    if opts.fade_seconds and meta["duration"] > 2 * opts.fade_seconds + 0.5:
        d = meta["duration"] / (opts.speed or 1.0)
        tail.append(f"fade=t=in:st=0:d={opts.fade_seconds}")
        tail.append(f"fade=t=out:st={max(0, d - opts.fade_seconds):.3f}:d={opts.fade_seconds}")
    tail.append("format=yuv420p")

    tail_str = ",".join(tail)
    if vf:
        filter_complex = ";".join(vf) + f";[{last}]{tail_str}[v]"
    else:
        filter_complex = f"[0:v]{tail_str}[v]"

    # audio: retime to match if speed != 1
    a_filter = []
    if opts.speed and opts.speed != 1.0:
        s = opts.speed
        # atempo accepts 0.5-2.0; our speeds are ~1.03 so one stage is enough
        a_filter = ["-filter:a", f"atempo={s}"]
        amap = []
    else:
        amap = []

    cmd = [
        "ffmpeg", "-y", "-i", in_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        *a_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ]
    print("[video_editor] " + " ".join(shlex.quote(c) for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
            print("[video_editor] ffmpeg failed; using original:\n" + r.stderr[-1500:])
            return in_path
    except Exception as exc:  # noqa: BLE001
        print(f"[video_editor] exception ({exc}); using original")
        return in_path
    return out_path
