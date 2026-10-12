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
| `GET /jobs/{job_id}` | `queued`, `running`, `succeeded` or `failed`; failures include `error` and `error_code` |
| `GET /jobs/{job_id}/result` | Downloads the successful ZIP; `409` before success, `404` for unknown jobs, `410` if the result was removed |
| `GET /animations` | Paginated deterministic name/keyword/description search returning unique Motion IDs |
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
curl --fail http://127.0.0.1:8081/jobs/JOB_ID/result -o mia-v2-result.zip
```

For extensionless mesh URLs, supply `input_format`, for example `"glb"` or `"fbx"`. Animation URLs must identify an `.fbx` file. Uploads accept GLB v2 models and binary Mixamo-compatible animation FBX. The default upload/download limit is 100 MiB per input, configurable with `MIA_MAX_INPUT_BYTES`; multipart bodies are bounded to twice that limit plus 64 KiB and JSON/options to 64 KiB. `MIA_DOWNLOAD_TIMEOUT` configures the download socket timeout in seconds (default 30). Upload transfer itself is not asynchronous: the task is accepted only after both uploaded files, if present, have been persisted.

Job metadata and requests persist in SQLite under `MIA_JOBS_DIR`, defaulting to `.cache/api-jobs`. Successful ZIPs include `animatable.fbx`, available previews/intermediate outputs and `manifest.json`. Results remain downloadable repeatedly and survive service restarts. Queued jobs resume on startup; interrupted running jobs become failed rather than silently being retried. There is no automatic result expiration: retain this directory during deployment changes and manage its disk usage explicitly.

#### Local Mixamo motion library

`data/mixamo/` contains only `animation_motion/`, `animation_motion_prompts.json`, `animation_motion_frames.json`, and `animation_bones.json` from `Linzhan/Mixamo-Animations-Characters`, plus the deterministic derived `index.json` and `validation_report.json`. It is separate from the existing `data/Mixamo/` skeleton templates. Preserve the original descriptions; do not classify or rewrite them with an LLM.

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

These dataset-specific files use their official Hugging Face source; no equivalent Aliyun mirror is configured. No character meshes, Y Bot, renders, videos or textures are downloaded. The validated library has **2,317 FBX files and three untouched JSONs**, totaling **1,371,030,958 bytes**, with no missing files, duplicate IDs or duplicate file contents. There are 362 duplicate display-name groups, so generation accepts only the unique `animation_id`, never an animation name. Search can use names and original English descriptions; it is deterministic text matching, not translated or vector semantic search.

`MIA_ANIMATIONS_DIR` selects the resource root, defaulting to `data/mixamo`. Query the library, select a returned `motion_id`, then submit it as `animation_id` alongside either model source. It is mutually exclusive with uploaded/URL animations. Unknown IDs return `404`; a missing/invalid catalog returns `503` for library-dependent calls without disabling external animation inputs. The generated manifest records the selected original metadata and dataset revision. Source metadata contains frame counts and bone names, not FPS or a full bone hierarchy.

The library files are ignored by Git and excluded from the image; retain them on disk and mount the complete directory read-only in Docker.

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
./prepare_api_wheels.sh /data/mia-build-wheels
docker buildx build --load --build-context wheelhouse=/data/mia-build-wheels \
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

The Docker example assumes ordinary public DNS and outbound connectivity. If the container also uses a fake-IP gateway, configure its DNS and trusted-egress hostnames explicitly rather than disabling IP checks.

The host prepares wheels using the pinned, locally validated Python 3.11 versions in `api-offline-requirements.txt`; this requires the existing `.venv` interpreter and `uv`. Ordinary packages default to Aliyun PyPI; override with `MIA_PIP_INDEX_URL` when preparing wheels. Specialized CUDA wheel sources remain available. PyTorch/CUDA are supplied by the base image. The image installs Python dependencies with networking disabled and `--no-index`, then runs `pip check`; a missing wheel fails the build. Base-image pulls and apt installation still require network access. Model weights and the host virtual environment are not copied into the image.

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
