# upd-to-cvat-xml

Convert IDAH datasets stored in [UPD](https://github.com/idah-ai/upd-sdk-python)
files into [CVAT 1.1](https://opencv.github.io/cvat/docs/manual/advanced/xml_format/)
annotation packages. The output format is selected automatically per dataset
`modality`:

- **`idah-video`** → *CVAT for video 1.1*: one CVAT task folder per video entry,
  with annotation tracks.
- **`idah-image`** → *CVAT for images 1.1*: the whole dataset as a single task,
  with one image per entry.

```
# idah-video
cvat-export/<dataset>/<entry>_<media-id>/annotations.xml
                                         images/frame_000000.PNG   # --with-images

# idah-image
cvat-export/<dataset>/annotations.xml
                      images/<name>.jpg                            # --with-images
```

Supported shapes: bounding box and polygon (video and images); plus ellipse,
circle, and line for images.

---

## Requirements

- **Python ≥ 3.14**
- **ffmpeg / ffprobe** available on `PATH`. CVAT XML uses absolute pixel
  coordinates, so every video and image is probed for its dimensions (and frame
  count, for video) — required even without `--with-images`.
  (`brew install ffmpeg` / `apt-get install ffmpeg`.)

## Installation

The [`upd`](https://github.com/idah-ai/upd-sdk-python) SDK is not on PyPI; it is
declared as a dependency and pulled from its public GitHub repository over HTTPS.
From a fresh virtual environment:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install .                 # installs this package + the upd SDK from git
```

## Usage

```bash
# annotations only
upd-to-cvat --upd idah-export.upd --output cvat-export

# also extract every video frame as PNG (needs ffmpeg)
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images

# limit to a single dataset
upd-to-cvat --upd idah-export.upd --output cvat-export --dataset <dataset-id>
```

| Flag              | Description                                                            |
| ----------------- | --------------------------------------------------------------------- |
| `--upd`           | Input UPD file (required).                                            |
| `--output`        | Output root directory (default `cvat-export`).                        |
| `--with-images`   | Video: extract frames as `images/frame_%06d.PNG`. Images: copy the source images into `images/`. (Requires `ffmpeg` for video.) |
| `--no-clamp`      | Keep raw coordinates instead of clamping each shape to the media bounds. By default shapes are clipped to `[0, width] × [0, height]`, since normalised IDAH points can drift slightly outside `[0, 1]`. |
| `--dataset`       | Optional dataset-id filter.                                          |

Equivalent module form: `python -m upd_to_cvat --upd …`.

### Programmatic use

```python
from upd_to_cvat import run

run("idah-export.upd", "cvat-export", with_images=False)
```

---

## Viewer

The [`viewer/`](viewer/) directory contains a small web application for quickly
previewing the generated CVAT exports before delivery. It lists every exported
dataset in a sidebar and provides a frame-by-frame player that overlays the
annotations on the exported images (or on a blank canvas for annotation-only
exports).

```bash
cd viewer
npm install
npm run dev          # opens at http://localhost:5180/
```

The dev server automatically discovers every `annotations.xml` under
`../cvat-export/` and serves it alongside its images.

**Features**

- Supports both export formats (per-image shapes and per-frame tracks), with
  linear interpolation of track boxes between keyframes.
- Renders boxes (including rotation), polygons, polylines, points, and ellipses,
  coloured by the label colours from the export metadata.
- Playback controls: step, play/pause, scrubber, jump to ends, and a 1×–5× speed
  selector that respects the video's native frame rate. Keyboard shortcuts:
  `←`/`→` step, `Space` play/pause, `Home`/`End` first/last frame.
- Canvas zoom (mouse wheel or toolbar), pan (click and drag), and fit-to-frame.
- Per-label counts for the current frame and task metadata in the side panel.

See [`viewer/README.md`](viewer/README.md) for full details.
