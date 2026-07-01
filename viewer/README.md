# CVAT Export Viewer

A small Svelte 5 + Vite app to quickly preview the CVAT 1.1 exports under
`../cvat-export/`. Datasets are listed in the left sidebar; the main area is a
frame-by-frame player that overlays annotations on the exported images (or on a
blank canvas for annotations-only datasets).

## Run

```bash
cd viewer
npm install
npm run dev
```

Opens at http://localhost:5180/.

## What it does

- **Discovery** — a Vite dev-server middleware (`vite.config.js`) scans
  `../cvat-export` for every `annotations.xml`, exposes them at `/api/datasets`,
  and serves the XML + images under `/cvat/...`.
- **Both export formats** — image format (`<image>` with shapes) and
  video/track format (`<track>` with per-frame shapes). Track boxes are linearly
  interpolated between keyframes; other shapes carry the last keyframe forward.
- **Shapes** — boxes (incl. rotation), polygons, polylines, points, ellipses,
  colored by the label colors from the export metadata.

## Navigation

- Sidebar — pick a dataset (grouped by top-level folder).
- Player — ◀ ▶ step, ▶/⏸ play, scrubber, ⏮/⏭ jump to ends. Any manual frame
  change (buttons, keys, or scrubber) pauses playback. The frame counter is
  0-indexed (`0 / N-1`) to match the "Frame N" label in the right panel.
- A spinner badge appears while the current frame's image is still decoding.
- Speed — 1×–5× selector. 1× plays at the video's native frame rate (parsed
  from the task name, e.g. `..._30fps_...`, defaulting to 30 fps).
- Playback buffers: it won't advance to a frame until that frame's image has
  decoded, so annotations never run ahead of the picture. It stops on the last
  frame; pressing play again from the end restarts from the beginning. A shared
  image cache ([src/lib/imageCache.js](src/lib/imageCache.js)) backs both the
  renderer and this gating.
- Keyboard — `←`/`→` step, `Space` play/pause, `Home`/`End` first/last frame.
- Toggles — `Labels` and `Fill`; the right panel lists per-label counts for the
  current frame plus task metadata.

## Canvas (zoom / pan)

- **Zoom** — mouse wheel (zooms toward the cursor) or the `−` / `+` buttons in
  the canvas toolbar. The toolbar shows the current zoom relative to fit.
- **Pan** — click and drag the canvas.
- **Fit** — the `Fit` button, the `%` readout, or double-click the canvas resets
  the view to fit the frame. Switching datasets refits automatically.
