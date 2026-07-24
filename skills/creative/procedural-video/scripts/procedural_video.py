#!/usr/bin/env python3
"""Deterministic, local, square motion-graphics renderer for procedural-video."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    canvas = data.get("canvas", {})
    width, height, fps = canvas.get("width"), canvas.get("height"), canvas.get("fps")
    if not isinstance(width, int) or width < 16 or width != height:
        raise ValueError("canvas must be square and at least 16 pixels")
    if not isinstance(fps, int) or not 1 <= fps <= 60:
        raise ValueError("canvas.fps must be an integer from 1 through 60")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("manifest needs one or more scenes")
    ids: set[str] = set()
    for scene in scenes:
        ident = scene.get("id") if isinstance(scene, dict) else None
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("scene ids must be unique nonempty strings")
        ids.add(ident)
        if not isinstance(scene.get("frames"), int) or scene["frames"] < 2:
            raise ValueError(f"scene {ident} needs at least two frames")
    if data.get("soundtrack") is not None and not isinstance(data["soundtrack"], str):
        raise ValueError("soundtrack must be one local relative filename")
    return data


def seed_for(project_seed: Any, scene_id: str, frame: int) -> int:
    raw = f"{project_seed}|{scene_id}|{frame}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def ease(name: str, t: float) -> float:
    t = min(1.0, max(0.0, t))
    if name == "linear": return t
    if name == "in-out-sine": return -(math.cos(math.pi * t) - 1) / 2
    if name == "out-cubic": return 1 - (1 - t) ** 3
    raise ValueError(f"unsupported easing {name!r}")


def measure_text(text: str, font: ImageFont.ImageFont, max_width: int) -> tuple[int, int]:
    bounds = font.getbbox(text)
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width > max_width:
        raise ValueError(f"text overflow: {width}px exceeds {max_width}px")
    return width, height


def timeline(scene: dict[str, Any], frame: int) -> float:
    return ease(scene.get("easing", "in-out-sine"), frame / (scene["frames"] - 1))


def radial_mask(size: int, center: tuple[float, float], radius: float) -> np.ndarray:
    y, x = np.ogrid[:size, :size]
    distance = np.hypot(x - center[0], y - center[1])
    return np.clip(1.0 - distance / max(radius, 1.0), 0.0, 1.0)


def flow_particles(draw: ImageDraw.ImageDraw, size: int, count: int, seed: int, progress: float) -> None:
    rng = random.Random(seed)
    for _ in range(count):
        x0, y0 = rng.random() * size, rng.random() * size
        angle = rng.random() * math.tau + progress * math.tau
        length = 3 + rng.random() * (size / 12)
        x1, y1 = x0 + math.cos(angle) * length, y0 + math.sin(angle) * length
        alpha = int(55 + 120 * rng.random())
        draw.line((x0, y0, x1, y1), fill=(255, 255, 255, alpha), width=1)


def render_frame(manifest: dict[str, Any], scene: dict[str, Any], frame: int) -> Image.Image:
    size = manifest["canvas"]["width"]
    progress = timeline(scene, frame)
    background = tuple(scene.get("background", [14, 21, 39]))
    image = Image.new("RGBA", (size, size), (*background, 255))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    flow_particles(draw, size, int(scene.get("particles", 18)),
                   seed_for(manifest.get("seed", 0), scene["id"], frame), progress)
    # Camera/transform: the animated disc moves through a masked local coordinate space.
    camera_x = (progress - 0.5) * size * float(scene.get("camera_pan", 0.08))
    cx, cy = size * (0.25 + 0.5 * progress) - camera_x, size * 0.54
    radius = size * (0.10 + 0.12 * ease("out-cubic", progress))
    mask = (radial_mask(size, (cx, cy), radius) * 210).astype(np.uint8)
    color = tuple(scene.get("accent", [52, 211, 153]))
    accent = Image.new("RGBA", image.size, (*color, 0)); accent.putalpha(Image.fromarray(mask))
    image.alpha_composite(accent)
    image.alpha_composite(overlay)
    text = scene.get("text", "")
    if text:
        font = ImageFont.load_default()
        tw, th = measure_text(text, font, int(size * 0.82))
        x = (size - tw) // 2
        y = int(size * 0.16 + (1 - progress) * size * 0.04)
        text_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(text_layer).text((x, y), text, font=font, fill=(255, 255, 255, int(255 * progress)))
        image.alpha_composite(text_layer)
    # Reusable effect: deterministic fine grain is applied after the compositing pass.
    noise = np.random.default_rng(seed_for(manifest.get("seed", 0), scene["id"], frame)).integers(-2, 3, (size, size, 1), dtype=np.int16)
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    return Image.fromarray(np.clip(pixels + noise, 0, 255).astype(np.uint8), "RGB")


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _encode_frames(frames: Path, fps: int, output: Path) -> None:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames / "frame_%04d.png"),
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)])


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            check=True, text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def render(project: Path) -> dict[str, Any]:
    manifest = load_manifest(project / "project.json")
    output = project / "output"; scenes_root = output / "scenes"; scenes_root.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for scene in manifest["scenes"]:
        frames = scenes_root / scene["id"] / "frames"; frames.mkdir(parents=True, exist_ok=True)
        for frame in range(scene["frames"]):
            target = frames / f"frame_{frame + 1:04d}.png"
            if target.exists():
                try:
                    with Image.open(target) as old:
                        if old.size == (manifest["canvas"]["width"],) * 2: continue
                except OSError:
                    pass
            render_frame(manifest, scene, frame).save(target)
        clip = scenes_root / scene["id"] / "clip.mp4"; _encode_frames(frames, manifest["canvas"]["fps"], clip); clips.append(clip)
    concat = output / "clips.txt"
    concat.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clips), encoding="utf-8")
    master = output / "master.mp4"
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(master)])
    soundtrack = manifest.get("soundtrack")
    if soundtrack:
        source = (project / soundtrack).resolve()
        if not source.is_file() or project.resolve() not in source.parents:
            raise ValueError("soundtrack must be one local file inside the project")
        muxed = output / "master-with-audio.mp4"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(master), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy", "-c:a", "aac", str(muxed)])
        muxed.replace(master)
    return {"manifest": manifest, "master": master, "clips": clips}


def contact_sheet(frames: list[Path], target: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in frames]
    width, height = images[0].size; columns = min(4, len(images)); rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (width * columns, height * rows), "black")
    for index, image in enumerate(images): sheet.paste(image, ((index % columns) * width, (index // columns) * height))
    sheet.save(target)


def qc(project: Path) -> dict[str, Any]:
    manifest = load_manifest(project / "project.json"); output = project / "output"; scenes: list[dict[str, Any]] = []
    for scene in manifest["scenes"]:
        paths = sorted((output / "scenes" / scene["id"] / "frames").glob("*.png"))
        if len(paths) != scene["frames"]: raise ValueError(f"scene {scene['id']} has incomplete frames")
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        diffs = []
        for left, right in zip(paths, paths[1:]):
            diff = ImageChops.difference(Image.open(left).convert("RGB"), Image.open(right).convert("RGB"))
            diffs.append(float(np.asarray(diff, dtype=np.float32).mean()))
        sheet = output / "scenes" / scene["id"] / "contact-sheet.png"; contact_sheet(paths, sheet)
        scenes.append({"id": scene["id"], "frame_count": len(paths), "duplicate_frames": sum(a == b for a, b in zip(hashes, hashes[1:])), "frame_diff_mean": diffs, "contact_sheet": str(sheet.relative_to(project))})
    master = output / "master.mp4"; report = {"schema": "procedural-video.qc/v1", "scenes": scenes, "master": {"path": str(master.relative_to(project)), "probe": _probe(master)}}
    (output / "qc-report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("render", "qc", "all")); parser.add_argument("project", type=Path)
    args = parser.parse_args()
    if args.command in {"render", "all"}: render(args.project)
    if args.command in {"qc", "all"}: qc(args.project)


if __name__ == "__main__": main()
