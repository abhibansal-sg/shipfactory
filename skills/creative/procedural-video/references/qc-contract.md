# QC contract

`qc-report.json` is the renderer-produced `procedural-video.qc/v1` evidence. It records every scene's frame count, adjacent SHA-256 duplicate count, per-adjacent-frame mean pixel differences, contact-sheet path, and an FFprobe JSON result for the stitched master. A nonzero duplicate count or zero frame-difference value is a machine-review finding, not a subjective approval decision.

The V1 renderer accepts only square manifests, local files, independently encoded scene clips, and zero or one soundtrack. It never downloads assets or opens a network listener.
