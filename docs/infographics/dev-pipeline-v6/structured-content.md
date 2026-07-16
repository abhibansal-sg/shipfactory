# Structured content

## Title
ShipFactory dev-pipeline@6

## Subtitle
Sealed inputs → independent evidence → human approval

## Main progression

### Discover & specify
- EXPLORE — readonly repository map
- SPEC — typed task-spec
- SPEC ATTACK — adversarial review
- PLAN — executable plan
- PLAN ATTACK — adversarial review

### Build & prove
- BUILD — approved source paths only
- FACTORY COMMIT — journal expected identity, then atomic update-ref
- VERIFY — runtime evidence bundle
- CORRECTNESS — exact sealed inputs
- ADVERSARIAL — exact sealed inputs

### Decide & report
- REVIEW STORY — canonical operator view
- HUMAN APPROVAL — operator only
- NOTIFY — after decision

## Rework loops
- Spec attack → Spec
- Plan attack → Plan
- Correctness/adversarial review → Build
- Rejection → Build

## Trust-boundary callouts
- Git metadata ≠ authentication
- Recipe bytes rehashed at enqueue + apply
- Reviewer identity comes from durable successful runs
- App reuse binds instance + HEAD + workspace + process
- Review executor enforces strict MCP
- Agents never press approval

## Footer legend
- Blue: model work
- Violet: independent review
- Teal: Factory-owned deterministic control
- Amber: human-only gate
- Thin backward arrows: bounded rework cone
