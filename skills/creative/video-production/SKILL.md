---
name: video-production
description: Orchestrate a governed creative-video delivery by selecting independent production lanes and verifying explicit handoffs.
---

# Video production

Use this orchestration skill to coordinate rather than duplicate the independent skills it routes to. Apply `references/lane-selection.md` before production and use `templates/treatment-checklist.md` for the treatment handoff.

| Stage | Checkable handoff | Completion criterion |
| --- | --- | --- |
| reference study | reference-study | references and availability decisions recorded |
| treatment/timeline | treatment | duration, scene beats, assets, soundtrack decision |
| lane selection | lane decision | deterministic Nous motion defaults to `procedural-video`, not Kling |
| styleframes | square styleframes | composition and typography constraints declared |
| scene production | scene-manifest + draft-media | one worker owns internal scene fan-out and resumable clips |
| machine verification | qc-report + contact-sheet | FFprobe, diffs, and duplicate-frame findings recorded |
| vision review | vision-verdict | only upstream styleframes or motion-build may be reworked |
| picture lock/master | master-media | reviewed draft is stitched/muxed to the square master |
| delivery | approved master notice | human operator approval has occurred; notification follows it |

Conditional routing is explicit: call `social-video-deep-study`, `procedural-video`, `ascii-video`, `tldraw-offline`, installed `HyperFrames`, the `ai-video-production` generative lane, or `ai-music-production` only when their independent capability is available and selected. This skill contains no copied instructions from those packages and never makes subjective approval automatic.
