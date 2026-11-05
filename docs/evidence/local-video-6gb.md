# 本地视频生成可行性：RTX 4050 Laptop 6 GB + CogVideoX-2b

> 日期：2026-11-05
> 目的：在 Seedance 成本偏高的情况下，验证「本地能否生成可用视频」
> 结论：**能跑，且显存真的压在 6 GB 内；但速度和质量只适合做草稿/兜底，不适合量产。**

## 1. 环境

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4050 Laptop，**6141 MiB** |
| 驱动 | 617.14 |
| Python | 3.12.7（`E:\AppData\Local\Programs\Python\Python312`） |
| PyTorch | 2.5.1+cu118（机器上原本就有，CUDA 可用） |
| diffusers | 0.33.1（0.42 需要 torch≥2.6，降到 0.33.1） |
| 模型 | `ZhipuAI/CogVideoX-2b`，**12.83 GB**（ModelScope） |
| 运行目录 | `E:\AppData\models\CogVideoX-2b` |

踩到的坑：DSH 环境变量里 `HTTP_PROXY=127.0.0.1:7890` 指向一个已关闭的代理，
Python/urllib 全被它拦住。清掉代理环境变量后单连接 **6.7 MB/s**，并行下载 6 线程
稳定 **5.3 MB/s**，13.78 GB 约 43 分钟下完。

## 2. 实测结果

| 参数 | 耗时 | 输出 | 质量 |
|---|---|---|---|
| 13 帧 / 8 步 / 480×320 | 140 s | 2.0 s @8fps | ❌ 只有蓝色渐变，步数太少 |
| **33 帧 / 25 步 / 480×320** | **187 s** | **4.1 s @8fps** | ✅ 鲸鱼跃出水面 + 水花，可辨认 |

GPU 采样（`nvidia-smi`，5 s 一次，共 52 个采样）：

| 指标 | 值 |
|---|---|
| 峰值显存 | **5689 MiB**（物理 6 GB 内，没爆） |
| GPU 利用率峰值 | 100% |
| 模型加载 | 55 s |

> 说明：torch 自己报的 `max_memory_allocated` 是 9.59 GB，包含了 CPU offload
> 的统计口径；以 `nvidia-smi` 的 5689 MiB 为准。

## 3. 外推（按实测线性估算）

| 目标 | 预估耗时 |
|---|---|
| 49 帧（6 s）/ 25 步 / 480×320 | ~5 min |
| 49 帧 / 25 步 / 720×480 | ~10 min |
| 49 帧 / 50 步 / 720×480（官方推荐步数） | ~20 min |

## 4. 与 Seedance / 便宜 API 的取舍

| 方案 | 单条成本 | 画质 | 时长/分辨率 | 适用 |
|---|---|---|---|---|
| Seedance 2.0 | ~$0.72 / 5 s | 高 | 5–10 s，720p+ | 定稿 |
| Kling 2.6 | ~$0.18 / 5 s | 中上 | 5 s | 迭代 |
| **本地 CogVideoX-2b** | **¥0（电费）** | 中低 | 4 s / 480×320（可到 720×480） | 草稿、离线、兜底 |

**我的判断**：本地生成适合做「提示词迭代」和「断网兜底」，不适合当 Seedance
的平替 —— 20 分钟一条 720p、没有音频，和 $0.18 的 Kling 比没有竞争力。
但如果目标是**证明平台能接本地推理后端**（external_job 的 provider 之一），
这条链路已经跑通，是有叙事价值的。

## 5. 复现命令

```powershell
# 下载模型（脚本已处理死代理）
E:\AppData\Local\Programs\Python\Python312\python.exe E:\AppData\dl_cogvideo.py E:\AppData\models\CogVideoX-2b

# 生成
E:\AppData\Local\Programs\Python\Python312\python.exe E:\AppData\gen_video.py `
  --prompt "a humpback whale breaches in a calm blue ocean at sunrise, spray and sunlight, cinematic, detailed" `
  --out E:\AppData\out\whale.mp4 --frames 33 --steps 25 --width 480 --height 320
```

产物：`docs/evidence/local-cogvideox-6gb.mp4`、`local-cogvideox-6gb-frame.png`。

## 6. 下一步建议

1. 若要更好画质：49 帧 / 720×480 / 25 步，约 10 分钟，跑一次看值不值。
2. 若要接进平台：把本地推理包成 `external_job` 的一个 provider（提交 → 轮询 →
   工件），成本为 0，正好用来演示成本闸门。
3. 若要量产：仍建议用 Kling 2.6（$0.18/5s）或 Wan 2.2。
