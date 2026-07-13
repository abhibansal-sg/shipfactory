import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import tailwindcss from "../../hermes-mobile/node_modules/@tailwindcss/vite/dist/index.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const hostRoot = resolve(here, "../../hermes-mobile");

export default {
  root: here,
  plugins: [tailwindcss()],
  resolve: {
    alias: [
      { find: /^react$/, replacement: resolve(hostRoot, "node_modules/react/index.js") },
      { find: /^react-dom\/client$/, replacement: resolve(hostRoot, "node_modules/react-dom/client.js") },
    ],
  },
  server: {
    fs: { allow: [resolve(here, "../..") ] },
  },
};
