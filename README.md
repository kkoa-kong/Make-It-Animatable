<div align="center">

# Make-It-Animatable: An Efficient Framework for Authoring Animation-Ready 3D Characters <br> (CVPR 2025 Highlight)

_**<a href="https://jasongzy.github.io">Zhiyang Guo</a>,
Jinxu Xiang,
Kai Ma,
Wengang Zhou,
Houqiang Li,
<a href="https://www.ran-zhang.com/">Ran Zhang</a>**_

<a href='https://arxiv.org/abs/2411.18197'><img src='https://img.shields.io/badge/arXiv-2411.18197-b31b1b.svg'></a>
<a href='https://jasongzy.github.io/Make-It-Animatable/'><img src='https://img.shields.io/badge/Project-Page-green'></a>
<a href='https://huggingface.co/spaces/jasongzy/Make-It-Animatable'><img src='https://img.shields.io/badge/%F0%9F%A4%97-Demo-orange'></a>

![teaser](assets/teaser.jpg)

</div>

## 🎉 News

- **MIA v2**
  - Upgraded to the Hunyuan3D 2.1 ShapeVAE backbone with retrained MIA models—larger capacity and better predictions! (only mesh inputs are supported).
  - Updated the architectures for joint, skinning-weight, and pose prediction, as long as the training pipeline.
  - Refined the demo app workflow—better skinning-weight post-processing, Blender integration, and global-transform restoration!
  - Try the [MIA v2 demo](https://huggingface.co/spaces/jasongzy/Make-It-Animatable-v2), or run it locally with `uv run app_v2.py`!

## Installation

```bash
git clone https://github.com/kkoa-kong/Make-It-Animatable --recursive --single-branch --branch feat/v2-async-api
cd Make-It-Animatable

uv sync
```

## Data Preparation

```bash
# hf auth login
hf download jasongzy/Mixamo \
  --repo-type dataset \
  --include 'bones*.fbx' 'animation/**' \
  --local-dir data/Mixamo
```

### For Demo

The demo also requires `bones.fbx` and `bones_vroid.fbx` from the gated Mixamo dataset. Request dataset access, run `hf auth login`, then download them using the Mixamo command above.


Download the pretrained models:

```bash
hf download jasongzy/Make-It-Animatable \
  --include 'output/best/new/**' 'output/best/v2/**' \
  --local-dir .
```

Download the example data:

```bash
hf download jasongzy/Make-It-Animatable \
  --include 'data/**' \
  --local-dir .
```

Download some tools:

```bash
wget https://github.com/facebookincubator/FBX2glTF/releases/download/v0.9.7/FBX2glTF-linux-x64 -O util/FBX2glTF
chmod +x util/FBX2glTF
```

### Asynchronous animation API

This fork maintains the API work on `feat/v2-async-api`, based on upstream `v2`. The original repository remains the `upstream` remote; `origin` points to [kkoa-kong/Make-It-Animatable](https://github.com/kkoa-kong/Make-It-Animatable).

The API listens on **8081**. Model and animation sources are selected independently: use a GLB upload (`input_file`) or model URL (`input_url`); use an animation FBX upload (`animation_file`), animation URL (`input_animation_url`), or unique local `animation_id`. Sources for the same resource are mutually exclusive, but file/URL combinations across the two resources are supported. JSON requests provide URLs/IDs; multipart requests provide file fields and an `options` JSON text field. Model URLs also support FBX, OBJ, STL, PLY and self-contained glTF; model FBX is converted to GLB first. Files with external dependencies must embed their assets. See [API.md](API.md) for the public base URL, all four combinations and parameter reference.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | `200` when models are ready, otherwise `503` |
| `GET /info` | Worker state, supported formats and parameters |
| `POST /generate` | JSON or multipart request; returns `202` after uploaded files are validated and persisted, before inference |
| `GET /jobs/{job_id}` | `queued`, `running`, `succeeded` or `failed`; failures include `error` and `error_code`; successful jobs provide `result_url`, `manifest_url` and optional `preview_url` |
| `GET /jobs/{job_id}/result` | Direct `model/gltf-binary` attachment named `animatable.glb`; `409` before success, `404` for unknown jobs, `410` if the result was removed |
| `GET /jobs/{job_id}/manifest` | Persisted `manifest.json` as `application/json` |
| `GET /jobs/{job_id}/preview` | Same final GLB, inline as `animatable_preview.glb`; only exposed in UI mode when the task manifest includes preview |
| `GET /jobs/{job_id}/artifacts` | `job_id`, `animation`, `options`, and `items` with direct `name`, `size`, `media_type`, `url`; only final GLB, optional preview entry and manifest |
| `GET /animations` | Paginated deterministic name/keyword/description search returning unique Motion IDs |
| `GET /animations/categories` | Keyword-derived UI groups, counts and classification version |
| `GET /animations/{motion_id}` | Original metadata and pinned dataset revision |
| `GET /animations/{motion_id}/file` | Original pure animation FBX |

One GPU job runs at a time; additional submissions queue without holding the HTTP connection open. Invalid request parameters return `422`; unavailable workers return `503`. Download and inference errors occur asynchronously and are reported in the job state.

```bash
curl -sS http://127.0.0.1:8081/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url": "https://huggingface.co/jasongzy/Make-It-Animatable/resolve/main/data/examples/ninja.glb",
    "input_animation_url": "https://raw.githubusercontent.com/mrdoob/three.js/dev/examples/models/fbx/Samba%20Dancing.fbx"
  }'

# Use the job_id returned above:
curl -sS http://127.0.0.1:8081/jobs/JOB_ID
curl --fail http://127.0.0.1:8081/jobs/JOB_ID/result -o animatable.glb
curl --fail http://127.0.0.1:8081/jobs/JOB_ID/manifest -o manifest.json
```

For extensionless mesh URLs, supply `input_format`, for example `"glb"` or `"fbx"`. Animation URLs must identify an `.fbx` file. Uploads accept GLB v2 models and binary Mixamo-compatible animation FBX. The default upload/download limit is 100 MiB per input, configurable with `MIA_MAX_INPUT_BYTES`; multipart bodies are bounded to twice that limit plus 64 KiB and JSON/options to 64 KiB. `MIA_DOWNLOAD_TIMEOUT` configures the download socket timeout in seconds (default 30). Upload transfer itself is not asynchronous: the task is accepted only after both uploaded files, if present, have been persisted.

Job metadata and requests persist in SQLite under `MIA_JOBS_DIR`, defaulting to `.cache/api-jobs`. Successful tasks persist the final `animatable.glb` and `manifest.json`, not a ZIP. Blender exports the final GLB directly after rigging, retargeting and animation baking; the API does not generate debug/intermediate visualizations or export a final FBX and convert it. In UI mode, the optional `animatable_preview.glb` entry is an inline route to the same GLB bytes, not a second export, file copy or duplicated storage. Non-UI mode exposes no generated preview. The manifest's `artifacts` list records `animatable.glb`, optional `animatable_preview.glb`, and `manifest.json`; library animation metadata and request options remain available. `result_url` and `manifest_url` are `null` until success, and `preview_url` is also `null` whenever preview is unavailable. Results remain downloadable repeatedly and survive service restarts. Queued jobs resume on startup; interrupted running jobs become failed rather than silently being retried. There is no automatic result expiration: retain this directory during deployment changes and manage its disk usage explicitly. Old ZIP-only results must be regenerated; there is no ZIP compatibility shim or `/jobs/{job_id}/artifacts/{name}` endpoint.

#### Local Mixamo motion library

`data/mixamo/` contains the original `animation_motion/`, `animation_motion_prompts.json`, `animation_motion_frames.json`, and `animation_bones.json` from `Linzhan/Mixamo-Animations-Characters`, plus the deterministic derived `index.json` and `validation_report.json`. It is separate from the existing `data/Mixamo/` skeleton templates. English source descriptions remain untouched; Chinese display translations are stored separately in `web/motion-index.zh-CN.json`.

Prepare the resource library with pinned upstream inventory, resumable bounded downloads, file-size checks, LFS SHA-256/Git blob SHA-1 verification, duplicate/missing validation and atomic index publication:

```bash
.venv/bin/python prepare_mixamo.py --destination data/mixamo \
  --revision 7f134bc734557b1d9516cb476d25ea1f577e45c1 --workers 8
```

On this host, use the existing TUN DNS gateway in a process-specific mount namespace, without changing global DNS:

```bash
sudo systemd-run --unit=mia-mixamo-prepare --wait --pipe --collect \
  --property=BindReadOnlyPaths=/data/Make-It-Animatable/mia-v2-api.resolv.conf:/etc/resolv.conf \
  --property=WorkingDirectory=/data/Make-It-Animatable \
  /data/Make-It-Animatable/.venv/bin/python /data/Make-It-Animatable/prepare_mixamo.py \
  --destination /data/Make-It-Animatable/data/mixamo \
  --revision 7f134bc734557b1d9516cb476d25ea1f577e45c1 --workers 8
```

The original FBX preparation uses the official Hugging Face source; no equivalent Aliyun mirror is configured. It does not download character meshes, Y Bot, videos or textures. The validated source library has **2,317 FBX files and three untouched JSONs**, totaling **1,371,030,958 bytes**, with no missing files, duplicate IDs or duplicate file contents. There are 362 duplicate display-name groups, so generation accepts only the unique `animation_id`, never an animation name. Search includes original English metadata and the separately translated Chinese display fields; it is deterministic text matching, not vector semantic search.

`MIA_ANIMATIONS_DIR` selects the resource root, defaulting to `data/mixamo`. Query the library, select a returned `motion_id`, then submit it as `animation_id` alongside either model source. It is mutually exclusive with uploaded/URL animations. Unknown IDs return `404`; a missing/invalid catalog returns `503` for library-dependent calls without disabling external animation inputs. The generated manifest records the selected original metadata and dataset revision. Source metadata contains frame counts and bone names, not FPS or a full bone hierarchy.

The library files are ignored by Git and excluded from the image; retain them on disk and mount the complete directory read-only in Docker.

Prepare the fast card previews separately:

```bash
.venv/bin/python prepare_mixamo_previews.py --destination data/mixamo --workers 8
```

The pinned HF dataset contains **no GIF**; `animation_motion_render/` provides four MP4 views per motion. This command downloads only the exact filename-stem-matched `v000.mp4`, stored as `previews/<motion_id>.mp4`. It prefers `https://hf-mirror.com` only after the pinned render inventory matches official paths, sizes and hashes, and falls back to official HF if that verification fails. Existing downloads are integrity-checked and interrupted transfers resume. This host has **2,317 verified previews**, totaling **105,453,156 bytes**, with zero missing mappings; details are in `preview_validation_report.json`. Mount `previews/` together with the original motion library. Preview requests are local: the browser never contacts HF or parses FBX just to show a card.

`web/motion-index.zh-CN.json` ships with the code and uses the original Motion IDs and pinned dataset/revision. The API loads it at startup, exposes `prompt_zh` / `description_zh`, and retains original `prompt` / `description` and manifest fields. A missing display file falls back to English; invalid provenance or IDs fail catalog loading explicitly. Rebuild local font subsets with `.venv/bin/python prepare_ui_fonts.py` after editing the display index. Backend/index changes require a worker restart during your deployment window; do not start a second GPU worker. Static UI changes require a browser reload. These changes do not authorize an automatic production restart.

#### Web animation studio

Enable the optional HTML/Three.js interface on the same worker and port:

```bash
CUDA_VISIBLE_DEVICES=4 GPU_ID=0 PORT=8081 .venv/bin/python api_server.py --ui
# Equivalent environment configuration:
MIA_UI=1 CUDA_VISIBLE_DEVICES=4 GPU_ID=0 PORT=8081 .venv/bin/python api_server.py
```

Open `http://agent-models-api.singularity-ai.com:8081/` through your existing access path, or `http://127.0.0.1:8081/` on the host. Without `--ui` or `MIA_UI=1`, the command remains API-only. The supplied development systemd unit now enables `MIA_UI=1`; this does not launch a second GPU worker or Gradio.

The development service is already running; access its URL directly. Stop `mia-v2-api.service` before using a foreground command so two workers do not compete for the same port and job directory.

The studio uses a midnight-blue three-column layout: motion categories/search and two-column video cards on the left, the main 3D viewport in the center, model upload/options/history on the right. Both sidebars scroll independently; narrower screens reflow the panels. Grid/list controls switch card presentation, Cmd/Ctrl+K focuses motion search, and the “My models” header opens the browser cache section. The stage supports fullscreen and displays real model statistics over a blue-lit grid floor. The supplied reference artwork is cropped/compressed under `web/art/`; the interface remains real HTML and Three.js rather than a screenshot. Choose a GLB model, adjust parameters, then click an animation card to submit immediately. Without a model, clicking only records the motion; selecting a model later does not submit automatically. Double-clicks and clicks during upload/queued/running tasks do not append jobs. The separate generation button is for retrying or applying changed parameters. Actual upload progress and task states are shown without fabricated inference percentages. Reloading reconnects the last saved task without submitting another job.

The binding panel's collapsed advanced form exposes the existing API options: rest-pose adjustment (`No`, `T-pose`, `A-pose`, `大-pose`), ignored body parts, skinning-weight correction, restoration of global transforms, reset to predicted rest pose, and animation retargeting. Mesh normals are shown as fixed on because the API accepts only `true`. Ignored parts are sent as an empty list when rest-pose adjustment is `No`. Values apply to the next generation, whether submitted by an animation card or the generation button; changing controls does not modify an existing job. “Restore advanced defaults” resets only the advanced form, leaving basic in-place/no-fingers choices unchanged. Refreshing the page restores parameter defaults.

The header and modern browser favicon share `web/art/brand-mark.svg`, a transparent gradient-filled vector mark with a continuous silhouette and no bitmap scaling seams. `web/favicon.ico`, linked through `/ui/favicon.ico` as a compatibility fallback, contains transparent 16/24/32/48/64/128/256-pixel variants rendered from that SVG. Interface line icons remain inline SVGs with transparent canvases; button and panel backgrounds are intentional CSS styling.

Cards load video metadata only when visible and show duration from the actual preview file. Preview videos use a cool dark-background display filter; the original server files and downloads are unchanged. Hover or keyboard focus plays the source MP4; leaving pauses it. Reduced-motion users get an explicit play/pause button instead of automatic playback. Short-video controls never submit a job or replace the central model view; the detailed FBX skeleton preview loads the real source motion in the center. Source videos demonstrate the upstream character, not your generated character. Missing/broken videos are explicit and do not prevent generation.

Uploaded GLB files are automatically stored as actual blobs in browser IndexedDB, not just filenames or remote URLs. The right-hand “My models” list remains after reload; selecting a saved model reloads that GLB as the current generation input without creating another history entry. Delete controls remove only the local cached copy. Storage is scoped to the browser and origin (including port); clearing site data or browser eviction removes it. If saving fails (for example, quota or disabled storage), an explicit error is shown while the current upload remains usable. Files are sent to the API only when generation is submitted.

Successful tasks expose the final animated `animatable.glb` and `manifest.json`, with an optional same-file inline preview in UI mode. The studio follows the returned `preview_url` or preview item's direct URL; it never substitutes the source motion or an intermediate visualization. Playback, pause, seek, speed, orbit/zoom and reset use the real Three.js scene and animation clips, including skeleton/joint overlays of the final model. Tasks without preview still offer the GLB download and explicitly indicate that no web preview is available. Requires a modern WebGL2 browser; if 3D initialization fails, generation and downloads remain available with an explicit preview error.

All browser dependencies are served locally under `/ui/`: pinned Three.js 0.180.0 and only required loader/control modules, fetched from the HTTPS npmmirror registry with npm SHA-512 integrity validation, plus licensed Noto Sans CJK SC UI/Latin font subsets. No Node build step or runtime external CDN is needed. Licenses and source/checksum manifests are included. If UI literals change, the optional font rebuild helper is `.venv/bin/python prepare_ui_fonts.py`, using the installed Ubuntu `fonts-noto-cjk` source and existing `fontTools`/Brotli environment.

The optional demo loads the real `data/examples/ninja.glb` when installed; it is labelled as an ungenerated example and is not submitted until explicitly selected. A deployment without example assets can still upload its own models. Frontend styling does not change raw motion descriptions: category labels are deterministic keyword groups (`keyword-v1`), not official Mixamo labels or LLM classification.

#### Local frontend preview with the remote API

Use Node.js 20.19+ (20.x) or 22.12+ and install the development dependencies:

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:5173/`. The development server serves local `web/` files, updates CSS live and reloads the page for HTML/JavaScript changes. It preserves `/ui/` asset paths and uses the vendored Three.js modules; production still needs no Node runtime or build step. The server binds only to localhost and fails rather than silently changing ports if 5173 is occupied.

By default, API requests go to `http://agent-models-api.singularity-ai.com:8081`. Override the target for another deployment or an SSH tunnel:

```bash
MIA_API_TARGET=http://127.0.0.1:18000 npm run dev
# In a separate terminal, if the remote API is not directly reachable:
ssh -N -L 18000:127.0.0.1:8081 USER@SERVER
```

The proxy forwards `/health`, `/info`, `/animations`, `/generate`, `/jobs`, API documentation and `/ui/demo`, including uploads, video byte ranges and generated artifact downloads. Other `/ui/` resources stay local. This avoids browser CORS changes; existing frontend same-origin URL checks remain enabled. Deployment-specific login or gateway credentials are not configured automatically.

Generation uses the real remote GPU and persists jobs on that server; local preview is not a sandbox. Preview verification can use health, catalog, videos and the demo without submitting a job. Browser model caches and last-job state are separate from the deployed UI because they are scoped to the local origin. Stop the development server with Ctrl-C; changing `MIA_API_TARGET` requires restarting it.


#### Temporary host service

Use the existing Python 3.11 `.venv`, V2 checkpoints, Hunyuan VAE cache and `data/Mixamo/bones.fbx`. Start directly for foreground development:

```bash
CUDA_VISIBLE_DEVICES=4 GPU_ID=0 PORT=8081 .venv/bin/python api_server.py
```

The supplied `mia-v2-api.service` is configured for this host's `/data/Make-It-Animatable` path and **physical GPU 4**, exposed as logical CUDA device 0. It runs without Docker, restarts on process failure and starts at boot:

```bash
sudo systemctl link /data/Make-It-Animatable/mia-v2-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now mia-v2-api.service
journalctl -u mia-v2-api.service -f
```

Optional environment overrides go in `/etc/default/mia-v2-api`; restart the service after changing them. Do not run the Gradio app at the same time: it also uses port 8081. From a development machine, replace `127.0.0.1` in the API examples with the server address, or use an SSH tunnel.

This host has an existing TUN fake-IP gateway. The unit mounts `mia-v2-api.resolv.conf` only inside the service, using its existing DNS server `10.251.1.1` without changing host DNS. `MIA_TRUSTED_EGRESS_HOSTS` explicitly permits the example hosts (`huggingface.co`, `us.aws.cdn.hf.co`, `raw.githubusercontent.com`) to resolve through the gateway's `198.18.0.0/15` range. Add only trusted asset/CDN hostnames, including redirect destinations, if your storage uses this gateway. The exception never permits literal fake-IP URLs or other internal IP ranges; arbitrary hosts still require public IP addresses, and HTTPS certificates remain verified. On hosts without this gateway, remove the unit's DNS bind mount and trusted-egress environment setting; foreground commands do not inherit the unit's DNS configuration.

The API has no authentication. Restrict network access to your development clients or use an SSH tunnel; do not expose it unrestricted to the public internet.

#### Container deployment and cutover

The API image excludes model weights. Mount the three MIA v2 checkpoints at `/models/mia-v2` and the Hunyuan3D VAE files (`hunyuan3d-vae-v2-1/config.yaml` and `model.fp16.ckpt`) under `/models/hunyuan/tencent/Hunyuan3D-2.1/`:

```bash
./prepare_api_wheels.sh /data/mia-build-wheels-aliyun
docker buildx build --load --build-context wheelhouse=/data/mia-build-wheels-aliyun \
  -f Dockerfile.api -t mia-v2-api:dev .

# Stop the temporary worker before handing its GPU and port to Docker:
sudo systemctl disable --now mia-v2-api.service
docker run --rm --gpus '"device=4"' -p 8081:8081 \
  -v "$PWD/output/best/v2:/models/mia-v2:ro" \
  -v "$HOME/.cache/hy3dgen:/models/hunyuan:ro" \
  -v "$PWD/data/mixamo:/models/mixamo:ro" \
  -v "$PWD/.cache/api-jobs:/var/lib/mia-v2/jobs" \
  mia-v2-api:dev
```
Container deployment is API-only by default (`MIA_UI=0`): do not pass `--ui` or `MIA_UI=1` in the deployment command. The separately documented host UI is a development option, not the container startup mode.

The supported process entry point is `python api_server.py`, not `uvicorn api_server:app`: model initialization and Blender operators must run on the process main thread. SIGTERM stops new submissions, waits for the active task while continuing to service its Blender export queue, and leaves queued jobs for the next startup. After the HTTP/job threads finish, the main thread disables Blender add-ons and unregisters startup scripts in reverse registration order before Python finalization. This prevents bpy 4.3's Python-backed UI class destructors from accessing an already finalized interpreter. Script cleanup uses bpy 4.3's internal module registry; revalidate startup, animation generation and SIGTERM shutdown when upgrading bpy.

Shutdown verification must include a real animation/retargeting request, not only an idle worker: idle shutdown alone does not exercise registered Blender UI classes. Blender may still print `Not freed memory blocks` during native teardown; that diagnostic is not suppressed and must not be confused with a clean memory-leak audit. Require exit code 0, valid generated FBX/GLB artifacts and released GPU allocations.


The Docker example assumes ordinary public DNS and outbound connectivity. If the container also uses a fake-IP gateway, configure its DNS and trusted-egress hostnames explicitly rather than disabling IP checks.

The image contains an immutable snapshot of the API modules, `web/` assets and the `ninja.glb` UI demo; editing the host checkout does not update an existing image. Rebuild and recreate the container after code changes. Python dependencies are installed before copying business code, so unchanged dependency pins and wheelhouse inputs can reuse the dependency layer. The complete motion library and model weights remain external read-only mounts.

The host prepares the 110 pinned runtime wheels in `api-offline-requirements.txt`, excluding notebook and training-only dependencies. Preparation requires the existing Linux x86_64 Python 3.11 `.venv`, `curl` with HTTP/2 support, and `uv` for source-only wheel builds. Downloads use curl HTTP/2 and default to Aliyun PyPI; override with `MIA_PIP_INDEX_URL`. Specialized CUDA wheel sources remain available. Matching cached wheels are CRC-checked and reused; downloaded wheels are checked against published SHA-256 values when supplied by their index. `SHA256SUMS` records the prepared artifacts.

The upstream `bpy` 4.3.0 CPython 3.11 wheel incorrectly declares a CPython 3.9 tag internally. Preparation corrects only `WHEEL` and its `RECORD` entry so `pip check` can validate it; all binary members remain unchanged, and the original archive is retained under `wheelhouse/upstream/`. The Dockerfile also installs Blender's required `libxkbcommon0`.

PyTorch/CUDA are supplied by the base image. Python dependencies install with networking disabled and `--no-index`, followed by `pip check`; a missing wheel fails the build. Base-image pulls and apt installation still require network access; apt uses the Aliyun Ubuntu mirror. Model weights and the host virtual environment are not copied into the image.

### For Training

Download the training dataset:

```bash
hf download jasongzy/Mixamo \
  --repo-type dataset \
  --include 'bones*.fbx' 'animation/**' 'animation_extra/**' 'character_refined/**' 'character_rabit_refined/**' \
  --local-dir data/Mixamo
```

Download the [pretrained](https://github.com/1zb/3DShape2VecSet#balloon-sampling) weights of AE (**only for training from scratch**):

```bash
hf download jasongzy/Make-It-Animatable \
  --include 'output/ae/**' \
  --local-dir .
```

<details>

<summary>About the online pairing of FBX files</summary>

During training, we do online pairing of characters and animations (skeletal motion sequences) using Blender to reduce the storage size of dataset while ensuring the diversity of training samples.
This basically includes the following steps (implemented using Blender's `bpy` API):

- Sample a character and a motion sequence, apply the motion to the character to create animated sequences, and optionally do retargeting for better poses.
- Sample some frames from the animated sequences to get the training samples of the current iteration.
- Extract the necessary ground-truth data (blend weights, bone positions, poses, etc.) for all sampled frames.
- Sample some points from the surface of the animated mesh as the input shape representation.

So if you want to get rid of the Blender dependency during training, you can iterate through the entire dataset in advance and store the results (may take a lot of storage space).

</details>

## Demo for Inference

```bash
uv run app_v2.py
```

The app listens on port `8081` on all interfaces.

An online demo is also available at [Hugging Face Spaces](https://huggingface.co/spaces/jasongzy/Make-It-Animatable).

## Training

```bash
bash train.sh
```

Tips:

- For the flexibility and efficiency during training, the models for predicting weights, bones, and poses are trained separately. If you wish to train them jointly and share decoder parameters across them, just modify the `train.sh` script by enabling all `--predict_*` flags.
- The hyperparameters in `train.sh` are set for training on A100 80G GPU. To reduce the VRAM usage, consider decreasing the `--batch_size` and `--sample_frames` values.

## Differences to Paper

- The dual-quaternion representation adopted in learning pose-to-rest transformations is replaced by a more effective rotation representation [ortho6d](https://openaccess.thecvf.com/content_CVPR_2019/html/Zhou_On_the_Continuity_of_Rotation_Representations_in_Neural_Networks_CVPR_2019_paper.html) along with the joint positions.
- Some samples from the [3DBiCar Dataset](https://gaplab.cuhk.edu.cn/projects/RaBit/dataset.html) proposed in [RaBit](https://gaplab.cuhk.edu.cn/projects/RaBit/) (rigged by [Mixamo](https://www.mixamo.com/)) are included in training to improve the generalization to characters with unusual head-body ratio.

## Acknowledgement

- [1zb/3DShape2VecSet](https://github.com/1zb/3DShape2VecSet)
- [Adobe Mixamo](https://www.mixamo.com/)
- [3DGS Render Blender Addon by KIRI Engine](https://github.com/Kiri-Innovation/3dgs-render-blender-addon)

## Citation

```bibtex
@inproceedings{Guo_2025_CVPR,
    author={Guo, Zhiyang and Xiang, Jinxu and Ma, Kai and Zhou, Wengang and Li, Houqiang and Zhang, Ran},
    title={Make-It-Animatable: An Efficient Framework for Authoring Animation-Ready 3D Characters},
    booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
    year={2025},
}
```

[![Star History Chart](https://api.star-history.com/svg?repos=jasongzy/Make-It-Animatable&type=Date)](https://star-history.com/#jasongzy/Make-It-Animatable&Date)
