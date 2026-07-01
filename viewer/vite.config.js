import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { createReadStream, existsSync, readdirSync, statSync } from 'node:fs';
import { extname, join, posix, relative, resolve } from 'node:path';

// Where the CVAT exports live (../cvat-export relative to this config).
const EXPORT_ROOT = resolve(import.meta.dirname, '..', 'cvat-export');

const MIME = {
  '.xml': 'application/xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.json': 'application/json',
};

function walkAnnotations(dir, acc = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walkAnnotations(full, acc);
    else if (entry.name === 'annotations.xml') acc.push(full);
  }
  return acc;
}

function discoverDatasets() {
  if (!existsSync(EXPORT_ROOT)) return [];
  return walkAnnotations(EXPORT_ROOT)
    .sort()
    .map((xmlPath) => {
      const dsDir = resolve(xmlPath, '..');
      const relDir = posix.normalize(relative(EXPORT_ROOT, dsDir).split(/[\\/]/).join('/'));
      const imagesDir = join(dsDir, 'images');
      const hasImages = existsSync(imagesDir) && statSync(imagesDir).isDirectory();
      const imageCount = hasImages
        ? readdirSync(imagesDir).filter((f) => !f.startsWith('.')).length
        : 0;
      const parts = relDir.split('/');
      return {
        id: relDir,
        group: parts.length > 1 ? parts[0] : '(root)',
        name: parts[parts.length - 1],
        xml: `/cvat/${relDir}/annotations.xml`,
        imagesDir: hasImages ? `/cvat/${relDir}/images` : null,
        imageCount,
      };
    });
}

/** Dev middleware: dataset discovery + static serving of the export tree. */
function cvatDataPlugin() {
  return {
    name: 'cvat-data',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = decodeURIComponent((req.url || '').split('?')[0]);

        if (url === '/api/datasets') {
          res.setHeader('Content-Type', 'application/json');
          res.setHeader('Cache-Control', 'no-store');
          res.end(JSON.stringify(discoverDatasets()));
          return;
        }

        if (url.startsWith('/cvat/')) {
          const rel = url.slice('/cvat/'.length);
          const filePath = resolve(EXPORT_ROOT, rel);
          // Prevent path traversal outside the export root.
          if (!filePath.startsWith(EXPORT_ROOT) || !existsSync(filePath)) {
            res.statusCode = 404;
            res.end('Not found');
            return;
          }
          res.setHeader('Content-Type', MIME[extname(filePath).toLowerCase()] || 'application/octet-stream');
          createReadStream(filePath).pipe(res);
          return;
        }

        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [svelte(), cvatDataPlugin()],
  server: { port: 5180, open: true },
});
