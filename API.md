# MIA V2 动画绑定与动作库 API

## 服务地址

```text
http://agent-models-api.singularity-ai.com:8081
```

```bash
BASE='http://agent-models-api.singularity-ai.com:8081'
```

生成接口异步执行：提交资源 → 返回任务 ID → 查询状态 → 下载结果。模型与动画分别选择来源，允许文件和 URL 交叉组合。

## Web 三栏工作台

UI 模式启用后，直接在浏览器打开 `http://agent-models-api.singularity-ai.com:8081/`。当前开发 systemd 服务已配置 `MIA_UI=1`；前台替代运行可用 `CUDA_VISIBLE_DEVICES=4 GPU_ID=0 PORT=8081 .venv/bin/python api_server.py --ui`，也可设置 `MIA_UI=1`。不启用时仍是 API-only 模式。

当前常驻服务已在运行，直接访问即可。需要前台启动时先停止已有 `mia-v2-api.service`，避免重复占用 8081 和同一个任务目录；UI 与 API 共用一个 Worker，不应另外启动第二个服务。

界面使用本地 HTML/CSS/Three.js 和本地中英文字体，不依赖 Gradio、外部 CDN 或 Node 构建。桌面采用单屏三栏：左侧动画分类与动作卡片，中间 3D 模型，右侧上传、参数和「我的模型」；两侧独立滚动，导航保持紧凑。不提供移动端布局或面板切换。现代浏览器需支持 WebGL2。流程：

1. 上传或拖放 GLB；可先本地预览。明确选择「使用示例角色」才会把实际示例文件作为输入。
2. 按分类或中文 / 英文名称、描述查找动作；卡片悬停或点击「播放短视频」可快速预览源动作，不生成任务。详细 FBX 骨架预览仍保留。
3. 先选好模型和绑定参数，再点击动画卡片直接提交，无需再点生成按钮。没有模型时只记录动作；之后选择模型不会自动生成。上传或任务进行中重复点击不追加任务。「重新生成所选动作」用于重试或应用修改后的参数。查看真实上传进度和异步状态，推理阶段不显示虚构百分比。
4. UI 任务提供预览时，播放最终 GLB，暂停、拖动时间轴、调整倍率、旋转缩放视角，切换最终角色的真实骨架或关节点叠加；不提供预测关节等中间产物。
5. 直接下载 `animatable.glb` 和元数据 `manifest.json`，不提供 ZIP。任务未包含网页预览时明确提示，GLB 下载仍可用。页面刷新会重新查询上次任务，不会自动再次生成。
6. 上传的实际 GLB 文件自动保存至当前浏览器的 IndexedDB。刷新后在右侧「我的模型」点击即可载入，并作为当前生成输入；切换缓存模型不重复保存。删除只移除本机缓存，不删除服务端任务。

模型缓存按浏览器和站点来源隔离（包括端口），不是云端备份；清除站点数据或浏览器回收空间会移除文件。缓存配额不足或不可用时显示明确错误，当前模型仍可预览和提交生成。保存到浏览器不会自动上传给服务端，只有提交生成才上传文件。

示例角色读取 `data/examples/ninja.glb`；缺少示例资产时会明确提示，可以继续上传自己的模型。骨架源预览、上传的原始模型和已生成的绑定角色有独立标签，不把示例或源动作冒充生成结果。


## 1. 输入来源与互斥规则

| 资源 | 来源 | 规则 |
| --- | --- | --- |
| 模型 | `input_file` 上传 GLB，或 `input_url` | 必须且只能选择一种 |
| 动画 | `animation_file` 上传 FBX，或 `input_animation_url`，或 `animation_id` | 最多选择一种；不选则输出静态绑骨模型 |

因此同时支持：GLB 文件 + FBX 文件、GLB 文件 + FBX URL、GLB URL + FBX 文件、GLB URL + FBX URL，以及任一模型来源 + 库内动画 ID。

- 上传模型只支持 GLB v2；上传动画支持 Blender 可读取的二进制 Mixamo 兼容 FBX。
- 模型 URL 还支持 FBX、OBJ、STL、PLY、自包含 glTF；模型 FBX 下载后先转换为 GLB。
- 不接受 `animation_name` 作为生成参数。名称、关键词、描述仅用于查询候选，再使用唯一 Motion ID 绑定。
- `input_file` 与 `input_url` 同时存在、或多个动画来源同时存在，均返回 `422`，不会隐式覆盖。
- URL 指定的资源必须由服务器访问得到。外部纹理等依赖须嵌入资源文件，接口不会下载整个资源目录。

## 2. 健康检查与服务信息

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 模型就绪返回 `200`，否则 `503` |
| `GET /info` | Worker 状态、GPU、运行任务数、输入格式、上传格式、动作库状态与动作数量 |

```bash
curl -sS -i "$BASE/health"
curl -sS "$BASE/info"
```

健康响应：

```json
{"status":"ok","model":"mia-v2"}
```

单 Worker 同时执行一个任务，其他任务进入队列。

## 3. 动作库查询

本地库来自 `Linzhan/Mixamo-Animations-Characters`，固定 revision：

```text
7f134bc734557b1d9516cb476d25ea1f577e45c1
```

包含 2,317 个纯骨骼 FBX 动画。原始名称、描述、Motion ID 和帧数直接复用数据集 JSON。中文名称和描述保存在独立展示索引 `web/motion-index.zh-CN.json`，不覆盖原始元数据；分类仍按原始英文关键词确定。固定版本的完整文件清单没有 GIF，但有每动作四个视角的 MP4；卡片使用一个现成视角，不需要实时解析 FBX。

### 查询动作列表

```http
GET /animations?q=walking&limit=20&offset=0
```

| 参数 | 说明 |
| --- | --- |
| `q` | 按文件名、原始英文名称/描述和独立中文展示名称/描述检索；所有归一化关键词都须匹配 |
| `name` | 原始动作名称的归一化精确匹配；可能返回多个同名动作 |
| `category` | 可选的关键词分组 ID；省略、空值或 `all` 为全部，未知值返回 `422` |
| `limit` | 每页条数，默认 20，范围 1–100 |
| `offset` | 分页起点，默认 0，须非负 |

```bash
curl -sS -G "$BASE/animations" \
  --data-urlencode 'q=step back cautiously' \
  --data-urlencode 'limit=20'

curl -sS -G "$BASE/animations" \
  --data-urlencode 'name=Agreeing'

curl -sS -G "$BASE/animations" \
  --data-urlencode 'q=公文包' \
  --data-urlencode 'limit=12'
```

响应结构为 `{total, limit, offset, items}`。每个动作记录包含：

```json
{
  "motion_id": "c9c91ad5-b96c-11e4-a802-0aaa78deedf9",
  "filename": "Agreeing.fbx",
  "prompt": "Agreeing",
  "description": "Step Back Cautiously Agreeing",
  "frames": 142,
  "relative_path": "animation_motion/Agreeing.fbx",
  "sha256": "<文件SHA-256>"
}
```

将返回记录的 `motion_id` 填入生成请求的 `animation_id`。原始动作名称存在 362 个重名分组，但 Motion ID 没有重复。

检索是确定性的中英文文本匹配：大小写、Unicode 和分隔符归一化，名称优先于描述，排序稳定。中文来自展示索引，不通过在线翻译或向量语义检索。`name` 仍精确匹配原始英文名称；生成仍只接受 Motion ID。

### 分类接口

```http
GET /animations/categories
```

返回 `{total, classification, items}`，每个分组为 `{id, label, count}`。当前规则版本 `keyword-v1`，ID 为 `dance`、`fitness`、`combat`、`locomotion`、`acrobatics`、`gestures`、`idle`、`other`。每个动作只归入一个主要分组，数量合计覆盖完整动作库。

```bash
curl -sS "$BASE/animations/categories"
curl -sS -G "$BASE/animations" \
  --data-urlencode 'category=dance' \
  --data-urlencode 'limit=20'
```

动作列表和详情额外提供 `category_id`、`category_label`。分组依据原始英文关键词，名称和描述不改写；这些不是 Mixamo 官方分类，也不是 LLM 语义分类。生成仍只接受唯一 `animation_id`，不接受分类或名字作为绑定选择。

列表和详情还提供 `prompt_zh`、`description_zh`（展示译文存在时），以及 `preview_status`（`ready` / `missing`）和 `preview_url`（可用时为同源 `/animations/{motion_id}/preview`，否则 `null`）。中文索引绑定原始 dataset/revision 和 Motion ID，部署启动时加载；缺少索引回退英文，索引格式、来源或 ID 错误会明确使动作库不可用。原始英文、FBX 路径与生成 manifest 不受展示译文影响。

### 快速视频预览与部署

```bash
# 已准备 FBX 和原始 index.json 后，额外准备卡片短视频：
.venv/bin/python prepare_mixamo_previews.py --destination data/mixamo --workers 8

# 验证卡片视频；支持浏览器 Range 请求：
curl --fail -sS "$BASE/animations/$ANIMATION_ID/preview" -o motion-preview.mp4
```

预览接口返回 `video/mp4`；未知 ID、未安装视频或越界资源返回 `404`。短视频以 `<motion_id>.mp4` 保存于 `MIA_ANIMATIONS_DIR/previews/`，部署时连同 FBX 目录挂载。下载脚本优先验证 `hf-mirror.com` 与官方固定版本清单的路径、大小和哈希完全一致，再校验本地文件；镜像不可用或不匹配时回退官方 HF。准备报告为 `preview_validation_report.json`。本机已下载并校验 2,317 个视频，总计 105,453,156 字节。

卡片仅为可见视频读取元数据，悬停/键盘聚焦播放，离开暂停；减少动态效果偏好下仅手动播放。快速预览和骨架按钮不提交任务，快速预览不切换中央模型。视频展示的是数据集源角色，不是用户模型生成结果；缺失时明确提示并保留骨架入口。

中文索引随代码发布；修改译文后运行 `.venv/bin/python prepare_ui_fonts.py` 更新字体覆盖。部署新后端或索引后需在部署窗口重启现有 Worker，再刷新浏览器；不要另外启动同端口/任务目录的 GPU Worker。仅静态 HTML/CSS/JS 更新可刷新页面生效。

### 指定动作详情与文件

```bash
ANIMATION_ID='c9c91ad5-b96c-11e4-a802-0aaa78deedf9'

curl -sS "$BASE/animations/$ANIMATION_ID"
curl --fail -sS "$BASE/animations/$ANIMATION_ID/file" -o Agreeing.fbx
```

详情接口额外返回 `dataset` 和 `revision`。未知 ID 返回 `404`；文件丢失返回 `410`；动作库未加载时查询或库内 ID 绑定返回 `503`。接口不暴露服务器绝对路径。

`animation_bones.json` 仅提供骨骼名称列表，不是完整骨骼父子层级；帧数元数据不含 FPS，不能据此直接推算时长。

## 4. 提交生成任务

```http
POST /generate
```

支持两种请求格式：

- `application/json`：模型 URL + 动画 URL 或库内 ID。
- `multipart/form-data`：`input_file`、`animation_file` 为可选文件字段，`options` 为 JSON 文本字段。URL、ID 和生成参数都放入 `options`，不是单独的表单字段。

使用 `curl -F` 时不要手动设置 Content-Type；curl 会生成正确的 multipart boundary。

### 示例 A：GLB 文件 + FBX 文件

```bash
curl -sS -i "$BASE/generate" \
  -F 'input_file=@./character.glb' \
  -F 'animation_file=@./motion.fbx' \
  -F 'options={"input_retarget":true,"input_inplace":true}'
```

### 示例 B：GLB 文件 + FBX URL

```bash
curl -sS -i "$BASE/generate" \
  -F 'input_file=@./character.glb' \
  -F 'options={"input_animation_url":"https://raw.githubusercontent.com/mrdoob/three.js/dev/examples/models/fbx/Samba%20Dancing.fbx"}'
```

### 示例 C：GLB URL + FBX 文件

```bash
curl -sS -i "$BASE/generate" \
  -F 'animation_file=@./motion.fbx' \
  -F 'options={"input_url":"https://huggingface.co/jasongzy/Make-It-Animatable/resolve/main/data/examples/ninja.glb"}'
```

### 示例 D：GLB URL + FBX URL

```bash
curl -sS -i "$BASE/generate" \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url":"https://huggingface.co/jasongzy/Make-It-Animatable/resolve/main/data/examples/ninja.glb",
    "input_animation_url":"https://raw.githubusercontent.com/mrdoob/three.js/dev/examples/models/fbx/Samba%20Dancing.fbx"
  }'
```

### 示例 E：模型 + 库内唯一 ID

上传 GLB 时：

```bash
curl -sS -i "$BASE/generate" \
  -F 'input_file=@./character.glb' \
  -F 'options={"animation_id":"c9c91ad5-b96c-11e4-a802-0aaa78deedf9"}'
```

使用 GLB URL 时：

```bash
curl -sS -i "$BASE/generate" \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url":"https://huggingface.co/jasongzy/Make-It-Animatable/resolve/main/data/examples/ninja.glb",
    "animation_id":"c9c91ad5-b96c-11e4-a802-0aaa78deedf9"
  }'
```

### JSON / options 参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `input_url` | string | `null` | 模型 HTTP(S) URL；与上传字段 `input_file` 二选一 |
| `input_format` | string | `null` | URL 路径无扩展名时必填，例如 `"glb"`；有扩展名时须一致；GLB 上传只能省略或填 `"glb"` |
| `input_animation_url` | string | `null` | 非空的 Mixamo 兼容动画 FBX URL，路径须标识 `.fbx` 文件 |
| `animation_id` | UUID string | `null` | 库内唯一 Motion ID；与动画 URL、动画文件互斥 |
| `input_no_fingers` | boolean | `false` | 启用无手指处理 |
| `input_rest_pose` | string | `"No"` | `"T-pose"`、`"A-pose"`、`"大-pose"`、`"No"` |
| `input_rest_parts` | string[] | `[]` | 姿态调整时忽略的部位：`"Fingers"`、`"Arms"`、`"Legs"`、`"Head"` |
| `input_normal` | boolean | `true` | 当前只接受 `true`，使用网格法线 |
| `input_bw_fix` | boolean | `true` | 启用蒙皮权重修正 |
| `input_restore_global` | boolean | `false` | 恢复输入模型的全局变换 |
| `input_reset_to_rest` | boolean | `true` | 重置到预测的静止姿态 |
| `input_retarget` | boolean | `true` | 应用动画时启用重定向 |
| `input_inplace` | boolean | `true` | 应用动画时启用原地运动 |

未知参数（包括 `animation_name`）返回 `422`。JSON/options 文本上限 64 KiB。默认每个上传或下载文件上限 100 MiB，由 `MIA_MAX_INPUT_BYTES` 配置；multipart 整体上限为两份文件上限加 64 KiB 表单开销。

上传本身仍需等待文件传输完成。服务完成校验并将文件保存到持久化任务目录后，才返回 `202`；后续下载、推理和烘焙异步执行。

### 提交响应

HTTP `202 Accepted`，响应示意：

```json
{
  "job_id":"<任务ID>",
  "status":"queued",
  "created_at":"<UTC时间>",
  "updated_at":"<UTC时间>",
  "error":null,
  "error_code":null,
  "status_url":"/jobs/<任务ID>",
  "result_url":null,
  "manifest_url":null,
  "preview_url":null
}
```

`202` 不是生成完成。URL 字段为相对路径，客户端应拼接 Base URL。`result_url`、`manifest_url` 在成功前为 `null`；`preview_url` 在成功前或预览不可用时为 `null`。

## 5. 查询任务与下载结果

```bash
JOB_ID='替换为提交响应的任务ID'
curl -sS "$BASE/jobs/$JOB_ID"

# 状态为 succeeded 后下载：
curl --fail -sS "$BASE/jobs/$JOB_ID/result" -o animatable.glb
curl --fail -sS "$BASE/jobs/$JOB_ID/manifest" -o manifest.json
```

| 任务状态 | 说明 |
| --- | --- |
| `queued` | 排队等待 |
| `running` | 正在下载、转换、推理或烘焙 |
| `succeeded` | 可下载成功结果 |
| `failed` | 查看 `error`、`error_code` |

建议每 2–5 秒查询一次；不是服务端强制限制。

结果直接为 `model/gltf-binary`，以 `Content-Disposition: attachment; filename="animatable.glb"` 下载；不是 ZIP。Blender 在核心骨架预测、蒙皮、姿态处理、重定向和烘焙后直接导出最终 GLB，不先导出最终 FBX 再转换。选择动画时 GLB 包含烘焙动画，不选动画时为静态绑骨模型。API 不生成调试、中间关节、权重或姿态可视化导出。

`GET /jobs/{job_id}/manifest` 返回持久化的 `manifest.json`，类型为 `application/json`。库内 ID 请求的 manifest 记录原始动作元数据、Motion ID、SHA-256 和数据集 revision；外部动画来源的 `animation` 字段为 `null`，不生成虚构的库内 ID。`options` 保留本次生成参数，`artifacts` 记录最终产物名：`animatable.glb`、可选的 `animatable_preview.glb`、`manifest.json`。

结果可重复下载。任务状态、排队任务和上传文件持久化保存；正常停止时等待当前任务完成，排队任务在启动后恢复。被进程异常退出中断的运行中任务标记为失败，不自动重试。上传输入在任务终止后清理，成功 GLB 和 manifest 保留并跨服务重启可用，无自动过期机制。部署时保留 `MIA_JOBS_DIR`（默认 `.cache/api-jobs`）并自行管理磁盘。旧的 ZIP-only 成功结果必须重新生成；不会读取旧 ZIP 提取产物，也不提供兼容 shim。

权重可视化参数 `input_bw_vis_bone` 已从新请求契约移除，提交该参数返回 `422`。升级前持久化的排队请求在执行时移除这个展示专用字段，仍可继续生成；不重新启用中间可视化。

### 成功响应与直接下载入口

成功任务的 URL 字段示意（UI 模式且该任务包含预览）：

```json
{
  "job_id":"<任务ID>",
  "status":"succeeded",
  "result_url":"/jobs/<任务ID>/result",
  "manifest_url":"/jobs/<任务ID>/manifest",
  "preview_url":"/jobs/<任务ID>/preview"
}
```

| 接口 | 成功响应 |
| --- | --- |
| `GET /jobs/{job_id}/result` | 最终 `animatable.glb`，`model/gltf-binary`，attachment 下载 |
| `GET /jobs/{job_id}/manifest` | 元数据 `manifest.json`，`application/json` |
| `GET /jobs/{job_id}/preview` | 同一个 GLB，`model/gltf-binary`，inline 文件名 `animatable_preview.glb`；仅 UI 模式且该任务 manifest 含预览时可用 |
| `GET /jobs/{job_id}/artifacts` | `job_id`、manifest 中的 `animation`、`options` 和直接 URL 清单 `items` |

预览入口与下载入口返回相同 GLB 字节，不进行二次导出、复制或重复存储。非 UI 模式不暴露生成预览，即使磁盘上的任务曾在 UI 模式生成；非 UI 任务的 `preview_url` 为 `null`。成功任务缺少预览时也为 `null`，但仍可正常下载最终 GLB。最终模型的骨架和关节显示由浏览器基于 GLB 内实际骨骼绘制，不依赖额外的预测关节文件。

列表中的每项包含实际产物 `name`、字节数 `size`、`media_type` 和相对 `url`，名称仅为 `animatable.glb`、可选的 `animatable_preview.glb`、`manifest.json`。客户端应使用返回的 `url`，不要拼接旧的 `/artifacts/{name}` 路径；旧的 `GET /jobs/{job_id}/artifacts/{name}` 接口已删除。

清单结构示意（`size` 为响应中的实际字节数；预览项与最终 GLB 大小相同）：

```json
{
  "job_id":"<任务ID>",
  "animation":null,
  "options":{"input_retarget":true,"input_inplace":true},
  "items":[
    {"name":"animatable.glb","size":123456,"media_type":"model/gltf-binary","url":"/jobs/<任务ID>/result"},
    {"name":"animatable_preview.glb","size":123456,"media_type":"model/gltf-binary","url":"/jobs/<任务ID>/preview"},
    {"name":"manifest.json","size":1024,"media_type":"application/json","url":"/jobs/<任务ID>/manifest"}
  ]
}
```

```bash
curl -sS "$BASE/jobs/$JOB_ID/artifacts"
# 仅当 preview_url 非 null 或清单有预览项时：
curl --fail -sS "$BASE/jobs/$JOB_ID/preview" -o animatable_preview.glb
```

未知任务返回 `404`、未成功任务返回 `409`、已移除的成功文件返回 `410`。未启用 UI 或该任务不含预览时，预览接口返回 `404`。播放时长来自 GLB 内实际 AnimationClip，不从原始帧数推算。


## 6. 错误说明

| HTTP 状态码 | 场景 |
| --- | --- |
| `200` | 正常查询、健康检查或文件下载 |
| `202` | 任务已接收 |
| `400` | 不合法的 multipart 格式或请求头 |
| `404` | 动作 ID 或任务不存在 |
| `409` | 任务没有成功结果，包括排队、运行中和失败 |
| `410` | 动作文件或成功任务结果已被移除 |
| `413` | 请求体或上传文件超过限制 |
| `415` | 请求 Content-Type 不支持 |
| `422` | 参数、互斥规则、上传文件格式校验失败 |
| `503` | Worker 未就绪或停止中；需要的动作库不可用 |

异步失败的 `error_code` 是任务错误分类，不是查询接口的 HTTP 状态码：`400` 下载地址无效或不安全，`422` 内容或推理参数错误，`500` 内部错误或运行中任务被重启中断，`502` 远程资源无法连接或读取。

## 7. 部署和资源说明

- 资源位于项目 `data/mixamo/`：纯动画 `animation_motion/`、三份原始 JSON、派生 `index.json` 和 `validation_report.json`。
- 下载范围不含角色 Mesh、Y Bot、视频或贴图。验收为 2,317 个 FBX + 3 个原始 JSON，共 1,371,030,958 字节；全部通过上游哈希与大小校验，缺失和重复 ID 为 0。
- `MIA_ANIMATIONS_DIR` 配置资源根目录。Docker 必须只读挂载完整目录，见 README；动画资源不打入镜像，也不提交到 Git。
- 服务会检查下载地址及重定向目标，默认拒绝内网、回环和链路本地地址。当前 TUN 假 IP 网关上的资源域名及重定向域名须配置在 `MIA_TRUSTED_EGRESS_HOSTS`。
- 文件上传不经过 URL 下载校验，不要求把开发电脑域名加入资源下载白名单。
- 当前没有应用层鉴权，依赖现有访问限制。反向代理若限制请求大小或上传超时，需要匹配所需文件大小；本次未更改反向代理配置。
- 服务器本机已经验证四种文件/URL 组合、两种模型来源 + 库内 ID、互斥拒绝、排队双文件上传重启恢复，以及 Blender 重新导入检查。开发电脑到上述域名入口尚未实测。
