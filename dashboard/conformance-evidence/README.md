# Factory tab visual conformance evidence

The harness mounts `dashboard/dist/index.js` unchanged, imports the Factory
stylesheet, and imports the host dashboard's `web/src/index.css` through the
host Tailwind Vite plugin. API reads are deterministic fixtures; component
rendering, tabs, drawer behavior, state chips, and layout are the real bundle.

Run it from the Factory repository root:

```sh
/Volumes/MainData/Developer/products/hermes-mobile/node_modules/.bin/vite \
  --config dashboard/conformance-harness.vite.mjs \
  --host 127.0.0.1 --port 4179
```

Review these routes at a 1440 × 1000 viewport:

- `http://127.0.0.1:4179/conformance-harness.html`
- `http://127.0.0.1:4179/conformance-harness.html?view=instances&drawer=open`
- `http://127.0.0.1:4179/conformance-harness.html?view=costs`

Captured evidence:

- `factory-waiting.png` — populated waiting-gate cards and actions.
- `factory-instance-drawer.png` — instance table context plus the full drawer.
- `factory-costs.png` — daily and per-instance data-card rollups.
- `live-factory.png` — the rebuilt plugin mounted at `/factory` in Hermes.
- `live-kanban.png` — `/kanban` at the same viewport and active host theme.
