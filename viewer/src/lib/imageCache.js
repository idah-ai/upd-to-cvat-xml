// Shared image cache. Used by the canvas renderer (to draw the decoded frame)
// and by the playback loop (to gate advancing until a frame's image has
// actually decoded, so annotations never run ahead of the picture).
//
// Plain module + subscribe/notify: consumers that need to react to load
// completion (the renderer) subscribe; the playback loop just polls isReady().

const cache = new Map(); // url -> { status: 'loading' | 'ok' | 'error', img }
const listeners = new Set();

function notify() {
  for (const fn of listeners) fn();
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function loadImage(url) {
  if (!url || cache.has(url)) return;
  const entry = { status: 'loading', img: null };
  cache.set(url, entry);
  const img = new Image();
  img.onload = () => {
    entry.status = 'ok';
    entry.img = img;
    notify();
  };
  img.onerror = () => {
    entry.status = 'error';
    notify();
  };
  img.src = url;
}

export function getImage(url) {
  const e = cache.get(url);
  return e && e.status === 'ok' ? e.img : null;
}

export function imageStatus(url) {
  return cache.get(url)?.status ?? 'none';
}

// Ready = decoded or known-missing (i.e. not still loading). An unknown URL is
// not ready — the caller should kick off loadImage() for it.
export function isReady(url) {
  const e = cache.get(url);
  return !!e && e.status !== 'loading';
}

// No notify(): callers clear on a dataset switch, which already re-runs the
// renderer's frame effect. Notifying here would mutate subscriber state during
// that effect (Svelte flags it as unsafe).
export function clearImages() {
  cache.clear();
}
