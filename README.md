# upd-to-cvat-xml

Export IDAH datasets stored in [UPD](https://github.com/idah-ai/upd-sdk-python)
files into [CVAT 1.1](https://opencv.github.io/cvat/docs/manual/advanced/xml_format/)
annotation packages. The output format is chosen per dataset `modality`:

- **`idah-video`** → *CVAT for video 1.1*. Each entry is one video → **one CVAT
  task folder** per entry, with `<track>`s.
- **`idah-image`** → *CVAT for images 1.1*. The whole dataset is **one task**,
  with one `<image>` per entry.

```
# idah-video
cvat-export/<dataset>/<entry>_<media-id>/annotations.xml
                                         images/frame_000000.PNG   # --with-images

# idah-image
cvat-export/<dataset>/annotations.xml
                      images/<name>.jpg                            # --with-images
```

Supported shapes: bounding-box, polygon (both); plus ellipse and line
(→ CVAT `polyline`) for images.

---

## Requirements

- **Python ≥ 3.14**
- **ffmpeg / ffprobe** on `PATH`. CVAT XML uses absolute pixel coordinates, so
  every video/image is probed with `ffprobe` for its dimensions (and frame
  count, for video) — required even without `--with-images`. (`brew install
  ffmpeg` / `apt-get install ffmpeg`.)
- The [`upd`](https://github.com/idah-ai/upd-sdk-python) SDK (pulled from its
  public GitHub repo over HTTPS).

## Installation

The `upd` SDK is not on PyPI — it is pulled directly from its public GitHub
repo over HTTPS, which is declared as a dependency in `pyproject.toml`. From a
fresh virtual environment:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install .                 # installs this package + upd from git (HTTPS)
```

This resolves `upd @ git+https://github.com/idah-ai/upd-sdk-python.git`. If you
have SSH set up and prefer it, install the SDK that way first, then this
package:

```bash
pip install "upd @ git+ssh://git@github.com/idah-ai/upd-sdk-python.git"
pip install .
```

### Local development (editable, side-by-side)

If you have both repos checked out next to each other and want to hack on the
SDK at the same time, install it editable instead of from git:

```bash
pip install -e ../upd-sdk-python   # editable SDK checkout
pip install -e .                   # this package, editable
```

## Usage

```bash
# annotations only
upd-to-cvat --upd idah-export.upd --output cvat-export

# also extract every frame as PNG (needs ffmpeg)
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images

# video: emit only IDAH keyframes, let CVAT interpolate between them
upd-to-cvat --upd idah-export.upd --output cvat-export --keyframes-only

# limit to one dataset
upd-to-cvat --upd idah-export.upd --output cvat-export --dataset <dataset-id>
```

Equivalent module form: `python -m upd_to_cvat --upd …`.

| Flag              | Description                                                            |
| ----------------- | --------------------------------------------------------------------- |
| `--upd`           | Input UPD file (required).                                            |
| `--output`        | Output root directory (default `cvat-export`).                        |
| `--with-images`   | Video: extract frames as `images/frame_%06d.PNG`. Images: copy the source images into `images/`. (Requires `ffmpeg` for video.) |
| `--keyframes-only`| Video: emit only the IDAH keyframes (each `keyframe="1"`) and let CVAT interpolate between them, instead of materialising every frame. Much smaller files. Bboxes are identical (both interpolate linearly); **polygons differ** — CVAT's polygon interpolation is not flubber, so in-between shapes won't match the frontend. No effect on images. |
| `--dataset`       | Optional dataset-id filter.                                           |

## How the mapping works

### idah-video → CVAT for video 1.1

IDAH `idah-video` annotations are stored as
`shape_args = {start, end, frames:[{frame, angle, points}]}` with **normalised
`[0, 1]`** points and `annotation = {"category": <label>}`. These map onto CVAT
`<track>`s:

- each annotation → one `<track>` (`label` = category),
- **every** frame in `[start, end]` is emitted as a `<box>` / `<polygon>`
  (denormalised to absolute pixels using the probed video size). The shape at
  each frame is computed with the [interpolation helper](#per-frame-interpolation)
  — bbox linear, polygon via flubber — so the exported geometry matches the
  frontend rather than relying on CVAT's own interpolation. Original IDAH
  keyframes are flagged `keyframe="1"`, interpolated frames `keyframe="0"`,
- a trailing `outside="1"` shape terminates the track (unless it runs to the
  last video frame).

Shape types: `idah-video:bounding-box` → `<box>`, `idah-video:polygon` →
`<polygon>`. Unknown shape types are skipped with a warning.

### idah-image → CVAT for images 1.1

Each `idah-image` annotation is a single shape (no `frames`); `shape_args`
holds normalised points (and an `angle` in **radians** for box/ellipse). The
whole dataset becomes one task with one `<image>` per entry, denormalised to
each image's probed size:

| IDAH shape_type           | CVAT element | notes                                            |
| ------------------------- | ------------ | ------------------------------------------------ |
| `idah-image:bounding-box` | `<box>`      | `angle` (rad) → `rotation` (deg)                 |
| `idah-image:polygon`      | `<polygon>`  |                                                  |
| `idah-image:ellipse`      | `<ellipse>`  | `points = [[cx,cy],[rx,ry]]`; `angle` → rotation |
| `idah-image:circle`       | `<ellipse>`  | treated as ellipse with equal radii (untested)   |
| `idah-image:line`         | `<polyline>` | CVAT has no "line"                               |

The annotation `category` stores the value *id* (e.g. `"car"`); it is mapped to
the CVAT label name (e.g. `"Car"`) via the dataset's `Labeling-Configuration`.

## Programmatic use

```python
from upd_to_cvat import run

run("idah-export.upd", "cvat-export", with_images=False)
```

## Per-frame interpolation

IDAH stores only sparse **keyframes** (`shape_args["frames"]`). To get the shape
at any frame, use the interpolation helper:

```python
from upd_to_cvat.interpolation import shape_at, iter_frames, kind_of

kind = kind_of(ann.shape_type)               # "bounding-box" | "polygon"

# one frame (None if outside [start, end])
pts, angle = shape_at(ann.shape_args, frame=42, kind=kind)

# every frame in [start, end]
for frame, pts, angle in iter_frames(ann.shape_args, kind=kind):
    ...
```

Points stay in IDAH's normalised `[0, 1]` space; exact keyframes return their
original (un-resampled) points. `angle` is in **radians** (linearly
interpolated for bboxes, `0` for polygons), matching the frontend's
`getInterpolatedFrame`.

- **bounding-box** — linear interpolation of the 4 corners between keyframes.
- **polygon** — morphed with [`pyflubber`](https://pypi.org/project/pyflubber/),
  a Python port of the **flubber** algorithm used in the frontend, so server-
  and client-side interpolation agree. It resamples / point-matches rings whose
  vertex counts differ (ours vary, ~63–67) before interpolating.

> **Note on `pyflubber` + numpy:** pyflubber's `get_area()` uses `np.cross()` on
> 2-D vectors, which numpy 2.0 removed (and Python ≥ 3.14 requires numpy ≥ 2).
> `interpolation.py` overrides that one function with a 2-D-safe equivalent at
> import time — no global numpy monkeypatching, no fork needed.
