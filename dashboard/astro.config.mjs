import { defineConfig } from 'astro/config';

// Static build served same-origin by the Falcon API from dashboard/dist/.
// Production CSP (set by the API): default-src 'self'; script-src 'self'; style-src 'self'; ...
// => every script and stylesheet must be an external file. `npm run check:csp` verifies dist/.
export default defineConfig({
  output: 'static',
  trailingSlash: 'always',
  compressHTML: true,
  devToolbar: { enabled: false },
  build: {
    format: 'directory',
    assets: '_astro',
    // Never emit <style> blocks into HTML (style-src 'self').
    inlineStylesheets: 'never',
  },
  vite: {
    build: {
      // Astro inlines processed <script>s below this size; 0 forces external files (script-src 'self').
      assetsInlineLimit: 0,
      sourcemap: false,
    },
  },
  // No dev proxy: the API serves dist/ itself (`make build-ui && make dev`), so the browser
  // talks to one origin and the Origin/CSRF checks are exercised exactly as in production.
});
