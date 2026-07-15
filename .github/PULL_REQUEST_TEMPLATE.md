## What does this PR do?

<!-- Describe the change clearly. What problem does it solve? Why is this approach the right one? -->

## Type of Change

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 🔒 Security / control-plane hardening
- [ ] 📝 Documentation update
- [ ] ✅ Tests (adding or improving test coverage)
- [ ] ♻️ Refactor (no behavior change)
- [ ] 🧪 Recipe (new pipeline version — published recipes are immutable)

## Changes Made

<!-- List the specific changes. Include file paths for code changes. -->

-

## How to Test

<!-- Steps to verify. Include the exact test command and both full-suite run counts. -->

1. `bash -c 'ulimit -n 4096; <hermes-venv-python> -m pytest tests/ -q'`
2.

## Risk & Rollback

<!-- What could break? How is it reverted? Control-plane changes require adversarial-suite evidence. -->

## Checklist

- [ ] Full suite green ×2 consecutively (counts pasted above)
- [ ] No published recipe bytes changed
- [ ] Spec §15/§17 updated if any module signature or primitive changed
- [ ] New findings landed in AGENTS.md in this same PR
- [ ] No secrets, tokens, or private paths
