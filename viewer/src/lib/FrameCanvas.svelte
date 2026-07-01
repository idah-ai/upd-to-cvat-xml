<script>
  import { colorForLabel } from './cvat.js';

  let { model, imagesDir, frame, showLabels = true, showFill = false } = $props();

  let canvas;
  let wrap;
  let displayImg = $state(null); // last successfully decoded image being shown
  let imgStatus = $state('none'); // none | loading | ok | error

  // View transform: image is drawn at (tx, ty) with `scale` px per image unit.
  let scale = $state(1);
  let tx = $state(0);
  let ty = $state(0);
  let fitScale = $state(1); // scale that fits the whole frame in the viewport
  let fitted = false; // becomes true once the current model has been fit
  let dragging = $state(false);

  // Cache decoded images so scrubbing/replaying is instant and flicker-free.
  const imgCache = new Map();
  let reqSeq = 0;

  const frameUrl = (i) => {
    if (!imagesDir || i < 0 || i >= model.frameCount) return null;
    const name = model.frameName(i);
    return name ? `${imagesDir}/${name}.PNG` : null;
  };

  // Warm the cache for an upcoming frame without touching what's on screen.
  function prefetch(i) {
    const url = frameUrl(i);
    if (!url || imgCache.has(url)) return;
    const img = new Image();
    img.onload = () => imgCache.set(url, img);
    img.src = url;
  }

  // Drop cached images when switching datasets.
  $effect(() => {
    void imagesDir;
    imgCache.clear();
  });

  // Mark the view as needing a fit whenever the dataset changes. The draw
  // effect below performs the actual fit (must not read other view state here,
  // or zooming would re-trigger this and snap the view back to fit).
  $effect(() => {
    void model;
    void imagesDir;
    fitted = false;
  });

  // Resolve the image for the current frame, keeping the previous one visible
  // while the next decodes so playback doesn't flash a blank background.
  $effect(() => {
    const url = frameUrl(frame);
    if (!url) {
      displayImg = null;
      imgStatus = 'none';
      return;
    }
    const cached = imgCache.get(url);
    if (cached) {
      displayImg = cached;
      imgStatus = 'ok';
      for (let k = 1; k <= 6; k++) prefetch(frame + k);
      return;
    }
    const token = ++reqSeq;
    imgStatus = displayImg ? 'ok' : 'loading';
    const image = new Image();
    image.onload = () => {
      imgCache.set(url, image);
      if (token === reqSeq) {
        displayImg = image;
        imgStatus = 'ok';
      }
    };
    image.onerror = () => {
      // A real 404 (e.g. frames past the exported image range): clear the stale
      // frame and show the note. Valid images never reach here, so playback's
      // decode gap still keeps the previous frame and stays flicker-free.
      if (token === reqSeq) {
        displayImg = null;
        imgStatus = 'error';
      }
    };
    image.src = url;
    for (let k = 1; k <= 6; k++) prefetch(frame + k);
  });

  const shapes = $derived(model.shapesForFrame(frame));

  // Redraw whenever anything visual changes.
  $effect(() => {
    void frame;
    void displayImg;
    void showLabels;
    void showFill;
    void shapes;
    void scale;
    void tx;
    void ty;
    draw();
  });

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function fit() {
    if (!wrap || !model) return;
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    if (cw <= 0 || ch <= 0) return;
    const fs = Math.min(cw / model.width, ch / model.height) || 1;
    fitScale = fs;
    scale = fs;
    tx = (cw - model.width * fs) / 2;
    ty = (ch - model.height * fs) / 2;
    fitted = true;
  }

  // Zoom keeping the image point under (px, py) fixed on screen.
  function zoomAt(px, py, factor) {
    const ns = clamp(scale * factor, fitScale * 0.2, fitScale * 40);
    const k = ns / scale;
    tx = px - (px - tx) * k;
    ty = py - (py - ty) * k;
    scale = ns;
  }

  function zoomButton(factor) {
    zoomAt(wrap.clientWidth / 2, wrap.clientHeight / 2, factor);
  }

  function onWheel(e) {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-e.deltaY * 0.0015);
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor);
  }

  function onPointerDown(e) {
    if (e.button !== 0) return;
    dragging = true;
    canvas.setPointerCapture(e.pointerId);
    canvas.dataset.lastX = e.clientX;
    canvas.dataset.lastY = e.clientY;
  }
  function onPointerMove(e) {
    if (!dragging) return;
    tx += e.clientX - +canvas.dataset.lastX;
    ty += e.clientY - +canvas.dataset.lastY;
    canvas.dataset.lastX = e.clientX;
    canvas.dataset.lastY = e.clientY;
  }
  function onPointerUp(e) {
    dragging = false;
    try {
      canvas.releasePointerCapture(e.pointerId);
    } catch {}
  }

  function draw() {
    if (!canvas || !wrap) return;
    const dpr = window.devicePixelRatio || 1;
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    if (cw <= 0 || ch <= 0) return;
    if (!fitted) fit();

    const W = Math.round(cw * dpr);
    const H = Math.round(ch * dpr);
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;
    canvas.style.width = cw + 'px';
    canvas.style.height = ch + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch); // transparent → checkerboard shows through

    const iw = model.width * scale;
    const ih = model.height * scale;
    if (displayImg) {
      ctx.drawImage(displayImg, tx, ty, iw, ih);
    } else {
      ctx.fillStyle = '#11151c';
      ctx.fillRect(tx, ty, iw, ih);
    }
    ctx.strokeStyle = '#2a3140';
    ctx.lineWidth = 1;
    ctx.strokeRect(tx, ty, iw, ih);

    for (const shape of shapes) drawShape(ctx, shape);
  }

  function drawShape(ctx, shape) {
    const color = colorForLabel(shape.label, model.labelColors);
    const SX = (x) => x * scale + tx;
    const SY = (y) => y * scale + ty;
    ctx.save();
    ctx.lineWidth = 1;
    ctx.strokeStyle = color;
    ctx.fillStyle = toRgba(color, 0.12);
    ctx.setLineDash(shape.occluded ? [6, 4] : []);

    let labelAnchor = null;

    if (shape.type === 'box') {
      const x = SX(shape.xtl);
      const y = SY(shape.ytl);
      const w = (shape.xbr - shape.xtl) * scale;
      const h = (shape.ybr - shape.ytl) * scale;
      if (shape.rotation) {
        const cx = x + w / 2;
        const cy = y + h / 2;
        ctx.translate(cx, cy);
        ctx.rotate((shape.rotation * Math.PI) / 180);
        ctx.translate(-cx, -cy);
      }
      if (showFill) ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      labelAnchor = { x, y };
    } else if (shape.type === 'ellipse') {
      ctx.beginPath();
      ctx.ellipse(SX(shape.cx), SY(shape.cy), shape.rx * scale, shape.ry * scale, 0, 0, Math.PI * 2);
      if (showFill) ctx.fill();
      ctx.stroke();
      labelAnchor = { x: SX(shape.cx - shape.rx), y: SY(shape.cy - shape.ry) };
    } else if (shape.points?.length) {
      if (shape.type === 'points') {
        for (const p of shape.points) {
          ctx.beginPath();
          ctx.arc(SX(p.x), SY(p.y), 3, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
      } else {
        ctx.beginPath();
        shape.points.forEach((p, i) => {
          if (i === 0) ctx.moveTo(SX(p.x), SY(p.y));
          else ctx.lineTo(SX(p.x), SY(p.y));
        });
        if (shape.type === 'polygon') {
          ctx.closePath();
          if (showFill) ctx.fill();
        }
        ctx.stroke();
      }
      labelAnchor = { x: SX(shape.points[0].x), y: SY(shape.points[0].y) };
    }

    ctx.restore();

    if (showLabels && labelAnchor) drawLabel(ctx, labelAnchor.x, labelAnchor.y, shape.label, color);
  }

  function drawLabel(ctx, x, y, text, color) {
    ctx.save();
    ctx.font = '11px system-ui, sans-serif';
    const padding = 4;
    const w = ctx.measureText(text).width + padding * 2;
    const h = 15;
    const ty2 = y - h < 0 ? y : y - h;
    ctx.fillStyle = color;
    ctx.fillRect(x, ty2, w, h);
    ctx.fillStyle = '#0b0e13';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x + padding, ty2 + h / 2 + 0.5);
    ctx.restore();
  }

  function toRgba(color, alpha) {
    if (color.startsWith('#')) {
      const hex = color.slice(1);
      const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
      const r = parseInt(full.slice(0, 2), 16);
      const g = parseInt(full.slice(2, 4), 16);
      const b = parseInt(full.slice(4, 6), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    if (color.startsWith('hsl(')) return color.replace('hsl(', 'hsla(').replace(')', `, ${alpha})`);
    return color;
  }

  // Non-passive wheel listener so we can preventDefault the page scroll.
  $effect(() => {
    if (!canvas) return;
    const h = (e) => onWheel(e);
    canvas.addEventListener('wheel', h, { passive: false });
    return () => canvas.removeEventListener('wheel', h);
  });

  // Refit-if-needed and redraw on container resize.
  $effect(() => {
    if (!wrap) return;
    const ro = new ResizeObserver(() => {
      if (!fitted) fit();
      draw();
    });
    ro.observe(wrap);
    return () => ro.disconnect();
  });

  const zoomPct = $derived(Math.round((scale / fitScale) * 100));
</script>

<div class="canvas-wrap" bind:this={wrap}>
  <canvas
    bind:this={canvas}
    class:dragging
    ondblclick={fit}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
  ></canvas>

  <div class="zoom-toolbar">
    <button onclick={() => zoomButton(1 / 1.25)} title="Zoom out">−</button>
    <button class="pct" onclick={fit} title="Fit to view (double-click canvas)">{zoomPct}%</button>
    <button onclick={() => zoomButton(1.25)} title="Zoom in">+</button>
    <button class="fit" onclick={fit} title="Fit to view">Fit</button>
  </div>

  {#if imgStatus === 'error'}
    <div class="img-note">image missing — showing annotations only</div>
  {:else if imgStatus === 'none'}
    <div class="img-note">no images in this dataset — annotations only</div>
  {/if}
</div>

<style>
  .canvas-wrap {
    position: relative;
    flex: 1;
    min-height: 0;
    overflow: hidden;
    background:
      linear-gradient(45deg, #0d1016 25%, transparent 25%),
      linear-gradient(-45deg, #0d1016 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #0d1016 75%),
      linear-gradient(-45deg, transparent 75%, #0d1016 75%);
    background-size: 20px 20px;
    background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
    background-color: #12161d;
  }
  canvas {
    display: block;
    cursor: grab;
    touch-action: none;
  }
  canvas.dragging {
    cursor: grabbing;
  }
  .zoom-toolbar {
    position: absolute;
    top: 10px;
    right: 10px;
    display: flex;
    gap: 4px;
    background: rgba(10, 13, 19, 0.82);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 4px;
    backdrop-filter: blur(4px);
  }
  .zoom-toolbar button {
    background: transparent;
    border: none;
    color: var(--text);
    cursor: pointer;
    border-radius: 5px;
    height: 24px;
    min-width: 26px;
    font-size: 14px;
    line-height: 1;
  }
  .zoom-toolbar button:hover {
    background: var(--hover);
  }
  .zoom-toolbar .pct {
    min-width: 48px;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .zoom-toolbar .fit {
    font-size: 12px;
    padding: 0 8px;
  }
  .img-note {
    position: absolute;
    bottom: 10px;
    left: 50%;
    transform: translateX(-50%);
    font-size: 12px;
    color: #93a1b5;
    background: rgba(10, 13, 19, 0.8);
    padding: 4px 10px;
    border-radius: 6px;
    pointer-events: none;
  }
</style>
