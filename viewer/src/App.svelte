<script>
  import { untrack } from 'svelte';
  import { parseCvatXml, colorForLabel } from './lib/cvat.js';
  import { isReady, loadImage } from './lib/imageCache.js';
  import FrameCanvas from './lib/FrameCanvas.svelte';

  let datasets = $state([]);
  let selected = $state(null);
  let model = $state(null);
  let loading = $state(false);
  let error = $state(null);

  let frame = $state(0);
  let playing = $state(false);
  let speed = $state(1); // playback speed multiplier (1–5×)
  let showLabels = $state(true);
  let showFill = $state(false);

  // Native frame rate for 1× playback. CVAT exports don't record fps, so parse
  // it from the task name when present (e.g. "..._30fps_...") and default to 30.
  const fps = $derived.by(() => {
    const m = model?.task?.name?.match(/(\d+)\s*fps/i);
    return m ? Number(m[1]) : 30;
  });

  // Group datasets by their top-level folder for the sidebar.
  const groups = $derived.by(() => {
    const map = new Map();
    for (const d of datasets) {
      if (!map.has(d.group)) map.set(d.group, []);
      map.get(d.group).push(d);
    }
    return [...map.entries()];
  });

  const currentShapes = $derived(model ? model.shapesForFrame(frame) : []);

  // Count shapes per label on the current frame for the info panel.
  const labelCounts = $derived.by(() => {
    const counts = new Map();
    for (const s of currentShapes) counts.set(s.label, (counts.get(s.label) || 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  });

  async function loadDatasets() {
    const res = await fetch('/api/datasets');
    datasets = await res.json();
    if (datasets.length && !selected) select(datasets[0]);
  }

  async function select(ds) {
    if (selected?.id === ds.id) return;
    selected = ds;
    model = null;
    error = null;
    loading = true;
    playing = false;
    frame = 0;
    try {
      const res = await fetch(ds.xml);
      if (!res.ok) throw new Error(`Failed to load ${ds.xml} (${res.status})`);
      const text = await res.text();
      model = parseCvatXml(text);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function clampFrame(i) {
    if (!model) return 0;
    return Math.max(0, Math.min(model.frameCount - 1, i));
  }
  function pause() {
    playing = false;
  }
  // Any manual frame change (buttons, keys, scrubber) stops playback.
  function goto(i) {
    pause();
    frame = clampFrame(i);
  }
  function step(delta) {
    goto(frame + delta);
  }
  // Toggle playback; if starting from the last frame, restart from the top.
  function togglePlay() {
    if (!model) return;
    if (!playing && frame >= model.frameCount - 1) frame = 0;
    playing = !playing;
  }

  // Playback loop. Advances one frame per frame-duration, but never moves on
  // until the next frame's image has decoded — so annotations can't run ahead
  // of the picture. Playback effectively "buffers" while a frame is loading.
  $effect(() => {
    if (!playing || !model) return;
    const count = model.frameCount;
    const dur = 1000 / Math.max(1, fps * speed); // ms per displayed frame
    const dir = selected?.imagesDir;
    const imageCount = dir ? selected.imageCount : 0;
    const urlFor = (i) =>
      dir && i >= 0 && i < count ? `${dir}/${model.frameName(i)}.PNG` : null;

    let nextDue = performance.now() + dur;
    const id = setInterval(() => {
      const now = performance.now();
      if (now < nextDue) return;

      const cur = untrack(() => frame);
      if (cur >= count - 1) {
        playing = false; // stop at the end
        return;
      }
      const next = cur + 1;

      // Gate on the image only for frames that actually have one exported.
      if (next < imageCount && !isReady(urlFor(next))) {
        loadImage(urlFor(next));
        for (let k = 2; k <= 15; k++) loadImage(urlFor(next + k - 1));
        return; // hold here (don't advance, don't reset the clock) until ready
      }

      frame = next;
      for (let k = 1; k <= 15; k++) loadImage(urlFor(next + k));
      nextDue = now + dur;
    }, 1000 / 120);
    return () => clearInterval(id);
  });

  function onKey(e) {
    if (!model) return;
    if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
    else if (e.key === 'Home') { goto(0); }
    else if (e.key === 'End') { goto(model.frameCount - 1); }
    else if (e.key === ' ') { togglePlay(); e.preventDefault(); }
  }

  $effect(() => {
    loadDatasets();
  });
</script>

<svelte:window on:keydown={onKey} />

<div class="app">
  <aside class="sidebar">
    <div class="sidebar-head">
      <h1>CVAT Exports</h1>
      <span class="count">{datasets.length} datasets</span>
    </div>
    <div class="sidebar-list">
      {#each groups as [group, items] (group)}
        <div class="group">
          <div class="group-label">{group}</div>
          {#each items as ds (ds.id)}
            <button
              class="ds"
              class:active={selected?.id === ds.id}
              onclick={() => select(ds)}
              title={ds.id}
            >
              <span class="ds-name">{ds.name}</span>
              <span class="ds-meta">
                {ds.imageCount ? `${ds.imageCount} imgs` : 'xml only'}
              </span>
            </button>
          {/each}
        </div>
      {/each}
      {#if datasets.length === 0}
        <p class="empty">No datasets found under <code>cvat-export/</code>.</p>
      {/if}
    </div>
  </aside>

  <main class="main">
    {#if loading}
      <div class="center muted">Loading…</div>
    {:else if error}
      <div class="center error">{error}</div>
    {:else if model}
      <div class="topbar">
        <div class="title">
          <strong>{selected.name}</strong>
          <span class="badge">{model.mode}</span>
          <span class="dims">{model.width}×{model.height}</span>
        </div>
        <div class="toggles">
          <label><input type="checkbox" bind:checked={showLabels} /> Labels</label>
          <label><input type="checkbox" bind:checked={showFill} /> Fill</label>
        </div>
      </div>

      <FrameCanvas {model} imagesDir={selected.imagesDir} {frame} {showLabels} {showFill} />

      <div class="controls">
        <button class="nav" onclick={() => goto(0)} title="First (Home)">⏮</button>
        <button class="nav" onclick={() => step(-1)} title="Prev (←)">◀</button>
        <button class="nav play" onclick={togglePlay} title="Play/Pause (Space)">
          {playing ? '⏸' : '▶'}
        </button>
        <button class="nav" onclick={() => step(1)} title="Next (→)">▶</button>
        <button class="nav" onclick={() => goto(model.frameCount - 1)} title="Last (End)">⏭</button>

        <input
          class="scrub"
          type="range"
          min="0"
          max={model.frameCount - 1}
          bind:value={frame}
          oninput={pause}
        />
        <select class="speed" bind:value={speed} title="Playback speed">
          {#each [1, 2, 3, 4, 5] as s (s)}
            <option value={s}>{s}×</option>
          {/each}
        </select>
        <span class="frame-count">
          {frame} / {model.frameCount - 1}
        </span>
      </div>
    {:else}
      <div class="center muted">Select a dataset</div>
    {/if}
  </main>

  {#if model}
    <aside class="inspector">
      <h2>Frame {frame}</h2>
      <div class="stat">{currentShapes.length} shape{currentShapes.length === 1 ? '' : 's'}</div>
      <div class="legend">
        {#each labelCounts as [label, n] (label)}
          <div class="legend-row">
            <span class="swatch" style:background={colorForLabel(label, model.labelColors)}></span>
            <span class="legend-name">{label}</span>
            <span class="legend-count">{n}</span>
          </div>
        {/each}
        {#if labelCounts.length === 0}
          <div class="muted small">No annotations on this frame</div>
        {/if}
      </div>

      <h3>Task</h3>
      <dl class="meta">
        {#if model.task.name}<dt>Name</dt><dd title={model.task.name}>{model.task.name}</dd>{/if}
        {#if model.task.id}<dt>ID</dt><dd>{model.task.id}</dd>{/if}
        {#if model.task.mode}<dt>Mode</dt><dd>{model.task.mode}</dd>{/if}
        <dt>Frames</dt><dd>{model.frameCount}</dd>
      </dl>
    </aside>
  {/if}
</div>

<style>
  .app {
    display: grid;
    grid-template-columns: 260px 1fr 240px;
    height: 100vh;
    overflow: hidden;
  }

  /* Sidebar */
  .sidebar {
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    background: var(--panel);
    min-height: 0;
  }
  .sidebar-head {
    padding: 16px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-head h1 {
    font-size: 15px;
    margin: 0;
  }
  .count {
    font-size: 11px;
    color: var(--muted);
  }
  .sidebar-list {
    overflow-y: auto;
    padding: 8px;
    flex: 1;
  }
  .group-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    padding: 10px 8px 4px;
  }
  .ds {
    width: 100%;
    text-align: left;
    background: transparent;
    border: none;
    color: var(--text);
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .ds:hover {
    background: var(--hover);
  }
  .ds.active {
    background: var(--accent-dim);
    box-shadow: inset 2px 0 0 var(--accent);
  }
  .ds-name {
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ds-meta {
    font-size: 11px;
    color: var(--muted);
  }
  .empty {
    color: var(--muted);
    font-size: 13px;
    padding: 12px;
  }

  /* Main */
  .main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    gap: 12px;
  }
  .title {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .title strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    background: var(--accent-dim);
    color: var(--accent);
    padding: 2px 7px;
    border-radius: 999px;
  }
  .dims {
    font-size: 12px;
    color: var(--muted);
  }
  .toggles {
    display: flex;
    gap: 14px;
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  .toggles label {
    display: flex;
    align-items: center;
    gap: 5px;
    cursor: pointer;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    background: var(--panel);
  }
  .nav {
    background: var(--hover);
    border: 1px solid var(--border);
    color: var(--text);
    width: 34px;
    height: 30px;
    border-radius: 7px;
    cursor: pointer;
    font-size: 13px;
  }
  .nav:hover {
    background: var(--accent-dim);
  }
  .nav.play {
    background: var(--accent);
    color: #06121f;
    border-color: var(--accent);
    font-weight: 700;
  }
  .scrub {
    flex: 1;
    accent-color: var(--accent);
  }
  .speed {
    background: var(--hover);
    border: 1px solid var(--border);
    color: var(--text);
    height: 30px;
    border-radius: 7px;
    padding: 0 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .frame-count {
    font-variant-numeric: tabular-nums;
    font-size: 13px;
    color: var(--muted);
    min-width: 90px;
    text-align: right;
  }

  /* Inspector */
  .inspector {
    border-left: 1px solid var(--border);
    background: var(--panel);
    padding: 16px;
    overflow-y: auto;
    min-height: 0;
  }
  .inspector h2 {
    font-size: 14px;
    margin: 0 0 2px;
  }
  .inspector h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    margin: 20px 0 8px;
  }
  .stat {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 12px;
  }
  .legend-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    font-size: 12px;
  }
  .swatch {
    width: 12px;
    height: 12px;
    border-radius: 3px;
    flex-shrink: 0;
  }
  .legend-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .legend-count {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .meta {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 10px;
    font-size: 12px;
    margin: 0;
  }
  .meta dt {
    color: var(--muted);
  }
  .meta dd {
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .center {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .muted {
    color: var(--muted);
  }
  .small {
    font-size: 12px;
  }
  .error {
    color: #ff8080;
    padding: 20px;
    text-align: center;
  }
</style>
