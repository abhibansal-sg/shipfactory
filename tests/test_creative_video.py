"""Focused, port-free SF-20 capability tests."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest
import yaml

from shipfactory.artifacts import ArtifactValidationError, _validate_document
from shipfactory.artifact_contracts import artifact_output_contract
from shipfactory.recipes.loader import RecipeError, bind_parameters, load_library, validate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "creative-video" / "project.json"
RENDERER = ROOT / "skills" / "creative" / "procedural-video" / "scripts" / "procedural_video.py"
RECIPE = ROOT / "recipes" / "creative-video@1.yaml"


def _recipe() -> dict:
    return load_library(ROOT / "recipes", persist=False).get("creative-video@1").document


def _tone(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)


def _render_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"; project.mkdir(); shutil.copy(FIXTURE, project / "project.json"); _tone(project / "tone.wav")
    subprocess.run([sys.executable, str(RENDERER), "all", str(project)], check=True, capture_output=True, text=True)
    return project


def test_procedural_skill_contract_and_resources_are_actionable():
    text = (RENDERER.parents[1] / "SKILL.md").read_text()
    assert text.startswith("---\nname: procedural-video")
    for required in ("deterministic", "resumable", "FFmpeg", "QC", "references/qc-contract.md"):
        assert required in text
    assert (RENDERER.parents[1] / "templates" / "project.json").is_file()
    assert (RENDERER.parents[1] / "references" / "qc-contract.md").is_file()
    assert RENDERER.stat().st_mode & 0o111


def test_tiny_fixture_renders_resumably_with_media_and_qc(tmp_path: Path):
    project = _render_project(tmp_path)
    first = project / "output" / "scenes" / "opening" / "frames" / "frame_0001.png"
    original_hash = hashlib.sha256(first.read_bytes()).hexdigest(); original_mtime = first.stat().st_mtime_ns
    subprocess.run([sys.executable, str(RENDERER), "all", str(project)], check=True, capture_output=True, text=True)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == original_hash
    assert first.stat().st_mtime_ns == original_mtime
    report = json.loads((project / "output" / "qc-report.json").read_text())
    assert report["schema"] == "procedural-video.qc/v1"
    assert (project / "output" / "master.mp4").stat().st_size > 0
    assert all(item["duplicate_frames"] == 0 and any(value > 0 for value in item["frame_diff_mean"]) for item in report["scenes"])
    video_stream = next(item for item in report["master"]["probe"]["streams"] if item["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == (64, 64)
    assert sum(item["codec_type"] == "audio" for item in report["master"]["probe"]["streams"]) == 1


def test_renderer_subsystems_are_deterministic_and_reject_overflow():
    spec = importlib.util.spec_from_file_location("procedural_video", RENDERER); assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.seed_for("seed", "scene", 1) == module.seed_for("seed", "scene", 1)
    assert module.ease("in-out-sine", .5) == pytest.approx(.5)
    with pytest.raises(ValueError, match="overflow"):
        module.measure_text("too wide", module.ImageFont.load_default(), 1)
    mask = module.radial_mask(16, (8, 8), 4)
    assert mask.shape == (16, 16) and mask[8, 8] > mask[0, 0]


def test_orchestration_contract_routes_independent_lanes_and_stage_handoffs():
    skill = (ROOT / "skills" / "creative" / "video-production" / "SKILL.md").read_text()
    routing = (ROOT / "skills" / "creative" / "video-production" / "references" / "lane-selection.md").read_text()
    for stage in ("reference study", "treatment/timeline", "lane selection", "styleframes", "scene production", "machine verification", "vision review", "picture lock/master", "delivery"):
        assert stage in skill
    for capability in ("social-video-deep-study", "procedural-video", "ascii-video", "tldraw-offline", "HyperFrames", "ai-video-production", "ai-music-production"):
        assert capability in skill or capability in routing
    assert "Choose `procedural-video` by default" in routing and "Do not route that work to Kling" in routing


def test_creative_recipe_topology_artifacts_and_human_gate():
    recipe = _recipe(); steps = recipe["steps"]
    assert [step["id"] for step in steps] == ["research", "treatment", "styleframes", "motion-build", "machine-verify", "vision-review", "adversarial-review", "master", "approval", "notify"]
    assert len([step for step in steps if step["id"] == "motion-build"]) == 1
    by_id = {step["id"]: step for step in steps}
    assert by_id["approval"]["primitive"] == "approval_gate" and by_id["approval"]["params"]["approvers"] == ["${operator_approver}"]
    assert by_id["notify"]["needs"] == ["approval"] and by_id["notify"]["params"]["target"] == "${notify_target}"
    assert {item["kind"] for step in steps for item in step["outputs"]} >= {"reference-study", "treatment", "styleframes", "scene-manifest", "draft-media", "qc-report", "contact-sheet", "vision-verdict", "video-review-story", "master-media"}
    invalid = copy.deepcopy(recipe); invalid["steps"][-2]["primitive"] = "agent_task"
    with pytest.raises(RecipeError): validate(invalid)


def test_video_artifact_contract_binds_typed_media_without_substitution():
    document = {"schema": "shipfactory.draft-media/v1", "artifact_type": "draft-media", "title": "draft", "content": {"scene": "intro"}, "media": {"path": ".shipfactory-output/draft.mp4", "sha256": "a" * 64, "bytes": 12, "mime_type": "video/mp4", "width": 64, "height": 64, "duration_seconds": 1.2}}
    _validate_document(document, kind="draft-media", schema="shipfactory.draft-media/v1")
    document["media"]["width"] = 63
    with pytest.raises(ArtifactValidationError, match="square"):
        _validate_document(document, kind="draft-media", schema="shipfactory.draft-media/v1")
    assert "hash-bound" in artifact_output_contract("shipfactory.master-media/v1")


def test_ratified_seat_model_policy_and_bounded_review_targets():
    expected = {
        "research": ("video-researcher", "codex", "gpt-5.6-terra", "medium"),
        "treatment": ("video-creative-director", "codex", "gpt-5.6-sol", "high"),
        "styleframes": ("video-styleframe-designer", "codex", "gpt-5.6-sol", "high"),
        "motion-build": ("video-motion-designer", "codex", "gpt-5.6-sol", "high"),
        "machine-verify": ("video-machine-verifier", "codex", "gpt-5.6-luna", "medium"),
        "vision-review": ("video-vision-reviewer", "codex", "gpt-5.6-sol", "high"),
        "adversarial-review": ("video-adversarial-reviewer", "claude", "claude-opus-4-8", "high"),
        "master": ("video-render-engineer", "codex", "gpt-5.6-terra", "high"),
    }
    for step in _recipe()["steps"]:
        if step["id"] not in expected: continue
        seat, executor, model, effort = expected[step["id"]]
        instructions = step["params"]["instructions"]
        parameter = step["params"]["seat"][2:-1]
        policy = _recipe()["parameters"][parameter]
        assert policy == {"type": "enum", "required": False, "default": seat, "values": [seat]}
        assert f"{executor} / {model} / {effort}" in instructions
    bound = bind_parameters(
        load_library(ROOT / "recipes", persist=False).get("creative-video@1"),
        {"brief": "fixture"},
    )
    assert bound["motion_designer_seat"] == "video-motion-designer"
    assert bound["operator_approver"] == "operator"
    with pytest.raises(RecipeError, match="wrong type"):
        bind_parameters(
            load_library(ROOT / "recipes", persist=False).get("creative-video@1"),
            {"brief": "fixture", "motion_designer_seat": "fallback"},
        )
    vision = next(step for step in _recipe()["steps"] if step["id"] == "vision-review")
    assert "only upstream styleframes or motion-build" in vision["params"]["instructions"]


def test_install_document_is_active_profile_only_and_dogfood_is_separate():
    document = (ROOT / "docs" / "creative-video-install.md").read_text()
    assert "ACTIVE_PROFILE" in document and "source identity receipt" in document
    assert "Run the same commands again" in document and "gateway restart" in document
    assert "other profile" in document and "separate Linear-backed Factory flight" in document
    assert "15–20 second" in document
