# Installing the creative-video skills after landing

This repository is the only source of the two skill packages. Install only after the ShipFactory change is landed and the operator has chosen the active Hermes profile; this implementation flight does not install anything, restart a gateway, or create a production flight.

Set `ACTIVE_PROFILE` to the one target profile directory, not a shared profiles parent. From the landed repository root, copy exactly the two source-controlled packages and write a source identity receipt. The receipt makes a later install auditable and allows the same release to be installed again safely.

```bash
ACTIVE_PROFILE="/absolute/path/to/one/hermes/profile"
REPO_ROOT="$(git rev-parse --show-toplevel)"
SKILLS_ROOT="$ACTIVE_PROFILE/skills/creative"
mkdir -p "$SKILLS_ROOT"
for SKILL in procedural-video video-production; do
  rm -rf "$SKILLS_ROOT/$SKILL.tmp"
  cp -R "$REPO_ROOT/skills/creative/$SKILL" "$SKILLS_ROOT/$SKILL.tmp"
  rm -rf "$SKILLS_ROOT/$SKILL"
  mv "$SKILLS_ROOT/$SKILL.tmp" "$SKILLS_ROOT/$SKILL"
done
git -C "$REPO_ROOT" rev-parse HEAD > "$SKILLS_ROOT/.shipfactory-creative-video-source"
```

Run the same commands again to reconcile only those two packages in that one profile. Do not run a gateway restart, do not invoke `hermes update`, and do not write another profile. Audit correspondence with `git -C "$REPO_ROOT" rev-parse HEAD` and the recorded `.shipfactory-creative-video-source`; if they differ, reinstall from the landed source before use.

## First dogfood is a separate flight

After installation, create one separate Linear-backed Factory flight for a 15–20 second ShipFactory launch film. That future flight starts from `creative-video@1`, has its own operator approval, and must not be created or run as part of SF-20. There is no Factory-owned work-decomposition store and no runtime Linear integration in this capability.
