// Parse a CVAT 1.1 annotations.xml into a frame-oriented model that both the
// image export format (<image> elements) and the video/track export format
// (<track> elements) can be rendered from uniformly.

const SHAPE_TAGS = ['box', 'polygon', 'polyline', 'points', 'ellipse', 'cuboid'];

function attr(el, name, fallback = null) {
  const v = el.getAttribute(name);
  return v === null ? fallback : v;
}

function num(el, name, fallback = 0) {
  const v = el.getAttribute(name);
  return v === null || v === '' ? fallback : parseFloat(v);
}

function parseAttributes(el) {
  const out = {};
  for (const a of el.querySelectorAll(':scope > attribute')) {
    out[a.getAttribute('name')] = a.textContent;
  }
  return out;
}

function parsePoints(str) {
  if (!str) return [];
  return str
    .trim()
    .split(';')
    .filter(Boolean)
    .map((pair) => {
      const [x, y] = pair.split(',').map(Number);
      return { x, y };
    });
}

// Normalise a single shape element into a plain object used by the renderer.
function readShape(el, { label }) {
  const tag = el.tagName.toLowerCase();
  const base = {
    type: tag,
    label,
    occluded: attr(el, 'occluded') === '1',
    outside: attr(el, 'outside') === '1',
    keyframe: attr(el, 'keyframe') === '1',
    frame: el.hasAttribute('frame') ? parseInt(el.getAttribute('frame'), 10) : null,
    zOrder: num(el, 'z_order', 0),
    rotation: num(el, 'rotation', 0),
    attributes: parseAttributes(el),
  };

  if (tag === 'box') {
    base.xtl = num(el, 'xtl');
    base.ytl = num(el, 'ytl');
    base.xbr = num(el, 'xbr');
    base.ybr = num(el, 'ybr');
  } else if (tag === 'ellipse') {
    base.cx = num(el, 'cx');
    base.cy = num(el, 'cy');
    base.rx = num(el, 'rx');
    base.ry = num(el, 'ry');
  } else {
    base.points = parsePoints(attr(el, 'points', ''));
  }
  return base;
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

// Linearly interpolate a box between two keyframes.
function interpolateBox(a, b, f) {
  if (b.frame === a.frame) return { ...a };
  const t = (f - a.frame) / (b.frame - a.frame);
  return {
    ...a,
    frame: f,
    xtl: lerp(a.xtl, b.xtl, t),
    ytl: lerp(a.ytl, b.ytl, t),
    xbr: lerp(a.xbr, b.xbr, t),
    ybr: lerp(a.ybr, b.ybr, t),
    rotation: lerp(a.rotation, b.rotation, t),
  };
}

// Resolve a track's visible shape at frame `f`, interpolating boxes between
// keyframes and carrying the last keyframe forward for other shape types.
function shapeAtFrame(shapes, f) {
  let prev = null;
  let next = null;
  for (const s of shapes) {
    if (s.frame === f) return s.outside ? null : s;
    if (s.frame < f && (!prev || s.frame > prev.frame)) prev = s;
    if (s.frame > f && (!next || s.frame < next.frame)) next = s;
  }
  if (!prev) return null;
  if (prev.outside) return null;
  if (prev.type === 'box' && next && !next.outside) return interpolateBox(prev, next, f);
  return prev;
}

/**
 * @returns {{
 *   mode: 'image' | 'track',
 *   task: object,
 *   labelColors: Record<string,string>,
 *   width: number, height: number,
 *   frameCount: number,
 *   frameName: (i:number)=>string|null,   // image basename for a frame, if any
 *   shapesForFrame: (i:number)=>object[],
 * }}
 */
export function parseCvatXml(xmlText) {
  const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
  const err = doc.querySelector('parsererror');
  if (err) throw new Error('Invalid XML: ' + err.textContent);

  const meta = doc.querySelector('meta > task') || doc.querySelector('meta');
  const task = {};
  if (meta) {
    for (const key of ['id', 'name', 'size', 'mode', 'start_frame', 'stop_frame', 'subset']) {
      const node = meta.querySelector(`:scope > ${key}`);
      if (node) task[key] = node.textContent;
    }
  }

  const labelColors = {};
  for (const label of doc.querySelectorAll('meta label')) {
    const name = label.querySelector(':scope > name')?.textContent;
    const color = label.querySelector(':scope > color')?.textContent;
    if (name) labelColors[name] = color || null;
  }

  const origWidth = parseFloat(doc.querySelector('meta original_size > width')?.textContent || '0');
  const origHeight = parseFloat(doc.querySelector('meta original_size > height')?.textContent || '0');

  const imageEls = [...doc.querySelectorAll('annotations > image')];
  const trackEls = [...doc.querySelectorAll('annotations > track')];

  if (imageEls.length > 0) {
    // ---- Image export format: each <image> is a frame ----
    const frames = imageEls
      .map((img) => {
        const shapes = [];
        for (const tag of SHAPE_TAGS) {
          for (const el of img.querySelectorAll(`:scope > ${tag}`)) {
            shapes.push(readShape(el, { label: attr(el, 'label', '') }));
          }
        }
        return {
          id: parseInt(attr(img, 'id', '0'), 10),
          name: attr(img, 'name', ''),
          width: num(img, 'width'),
          height: num(img, 'height'),
          shapes,
        };
      })
      .sort((a, b) => a.id - b.id);

    return {
      mode: 'image',
      task,
      labelColors,
      width: frames[0]?.width || origWidth || 1920,
      height: frames[0]?.height || origHeight || 1080,
      frameCount: frames.length,
      frameName: (i) => frames[i]?.name ?? null,
      shapesForFrame: (i) => frames[i]?.shapes ?? [],
    };
  }

  // ---- Track export format: shapes are spread across frames ----
  const tracks = trackEls.map((tr) => {
    const label = attr(tr, 'label', '');
    const shapes = [];
    for (const tag of SHAPE_TAGS) {
      for (const el of tr.querySelectorAll(`:scope > ${tag}`)) {
        const s = readShape(el, { label });
        if (s.frame !== null && s.frame >= 0) shapes.push(s);
      }
    }
    shapes.sort((a, b) => a.frame - b.frame);
    return { id: attr(tr, 'id', ''), label, shapes };
  });

  let maxFrame = parseInt(task.stop_frame ?? '', 10);
  if (Number.isNaN(maxFrame)) {
    maxFrame = 0;
    for (const t of tracks) for (const s of t.shapes) if (s.frame > maxFrame) maxFrame = s.frame;
  }
  const frameCount = maxFrame + 1;

  return {
    mode: 'track',
    task,
    labelColors,
    width: origWidth || 1920,
    height: origHeight || 1080,
    frameCount,
    frameName: (i) => `frame_${String(i).padStart(6, '0')}`,
    shapesForFrame: (i) => {
      const out = [];
      for (const t of tracks) {
        const s = shapeAtFrame(t.shapes, i);
        if (s) out.push({ ...s, trackId: t.id });
      }
      return out;
    },
  };
}

// Deterministic fallback color when a label has no color in the metadata.
export function colorForLabel(label, labelColors) {
  const c = labelColors?.[label];
  if (c) return c;
  let h = 0;
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) % 360;
  return `hsl(${h}, 70%, 55%)`;
}
