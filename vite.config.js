import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const webRoot = fileURLToPath(new URL('./web/', import.meta.url));
const apiTarget = process.env.MIA_API_TARGET || 'http://agent-models-api.singularity-ai.com:8081';

export default defineConfig({
  root: webRoot,
  plugins: [{
    name: 'mia-ui-paths',
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (request.url?.startsWith('/ui/') && !/^\/ui\/demo(?:\?|$)/.test(request.url)) {
          request.url = request.url.slice('/ui'.length);
        }
        next();
      });
    },
  }],
  resolve: {
    alias: [
      { find: /^three$/, replacement: `${webRoot}vendor/three/build/three.module.min.js` },
      { find: /^three\/addons\//, replacement: `${webRoot}vendor/three/examples/jsm/` },
    ],
  },
  optimizeDeps: { noDiscovery: true },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    open: false,
    proxy: {
      '^/(?:health|info|animations|generate|jobs|docs|redoc|openapi\\.json)(?:/|\\?|$)': {
        target: apiTarget,
        changeOrigin: true,
      },
      '^/ui/demo(?:\\?|$)': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
