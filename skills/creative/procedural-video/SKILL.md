---
name: procedural-video
description: Render deterministic, local, square motion-graphics scenes with Python and FFmpeg, producing resumable clips and machine QC.
---

# Procedural video

Use this skill for Nous-style deterministic motion graphics: local assets, a square canvas, independently resumable scenes, and a stitched master. It is not a generative-video, 3D, cloud-render, or subjective-approval workflow.

1. Copy `templates/project.json` into the project, set a stable seed, and validate typography before render. Keep an optional soundtrack local and declare no more than one.
2. Run `python scripts/procedural_video.py all <project-dir>`. The renderer derives frame seeds from project seed + scene id + frame, reuses valid existing frames, encodes each scene with FFmpeg, stitches `output/master.mp4`, then optionally muxes the declared soundtrack.
3. Inspect `output/qc-report.json` and each scene's `contact-sheet.png`. Frozen/duplicate frames, missing frames, invalid square media, or overflow are failures for the production lane; vision review remains a human/model review after this mechanical evidence.

The executable renderer supplies manifest validation, timeline/easing, text measurement, camera transforms, radial masks, deterministic particles/flow, grain effects, scene resume, FFmpeg assembly, contact sheets, pixel diffs, duplicate hashes, FFprobe media probes, and a machine-readable QC report. See `references/qc-contract.md` for the evidence boundary.
