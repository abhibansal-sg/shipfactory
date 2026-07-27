import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const hostRoot = process.env.HERMES_MOBILE_PATH || "/Volumes/MainData/Developer/products/hermes-mobile";
const tailwindModule = resolve(hostRoot, "node_modules/@tailwindcss/vite/dist/index.mjs");
const { default: tailwindcss } = await import(pathToFileURL(tailwindModule).href);

// The checked-in harness intentionally keeps the same host-relative imports
// as the Hermes dashboard plugin. This resolver lets the browser lane point
// those imports at the installed host checkout without adding a workspace
// symlink or changing the product harness.
const hermesPrefix = "../../hermes-mobile/";
const hostImportResolver = {
  name: "w3e-hermes-host-imports",
  resolveId(source) {
    if (!source.startsWith(hermesPrefix)) return null;
    return resolve(hostRoot, source.slice(hermesPrefix.length));
  },
};

export default {
  root: resolve(here, ".."),
  plugins: [hostImportResolver, tailwindcss()],
  resolve: {
    alias: [
      { find: /^react$/, replacement: resolve(hostRoot, "node_modules/react/index.js") },
      { find: /^react-dom\/client$/, replacement: resolve(hostRoot, "node_modules/react-dom/client.js") },
    ],
  },
  server: {
    fs: { allow: [resolve(here, "../.."), hostRoot] },
  },
  build: {
    rollupOptions: { input: resolve(here, "../conformance-harness.html") },
  },
};
