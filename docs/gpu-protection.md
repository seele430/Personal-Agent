# GPU 保护（2026-11-08 蓝屏后加固）

> 适用：本机 RTX 4050 Laptop / **6 GB 显存**。所有涉及 GPU 的操作按本文执行，
> 违反其中任何一条都视为缺陷。

---

## 1. 事故时间线

| 时间 | 事件 |
|---|---|
| 13:43 | 机器 **bugcheck `0x116 VIDEO_TDR_FAILURE`**（显卡驱动超时未恢复） |
| 13:43–14:04 | 系统不可用 |
| 14:04 | 重启完成，恢复服务 |
| 当时在跑 | **ComfyUI 出图**（SD1.5，512×512）**+ 视频转写**同时进行 |

**原因判断（收窄）**：蓝屏与 ComfyUI 出图同期发生，**显存压力是疑似诱因，确切根因未完全定位**。
当时 ComfyUI 占着 torch CUDA 上下文 + SD1.5 权重 + 采样，视频链路虽在 CPU 上跑，
但桌面合成仍要显存；这与 TDR（驱动超时未恢复）的发生时机吻合，但**没有驱动/硬件日志作为定论依据**。
无论根因是否最终确认，下面四条保护限制都继续生效。

**结论：这台机器的 GPU 只允许"一次一件事"，且必须留余量。**

---

## 2. 四条硬限制

### ① ComfyUI 只能用安全脚本启动

```powershell
# 启动（先检查空闲显存 ≥ 3 GB，不满足直接拒绝）
comfy-agent\scripts\start-comfyui-safe.ps1

# 用完就停（不常驻）
comfy-agent\scripts\start-comfyui-safe.ps1 -Stop
```

- **禁止**再用 `run_nvidia_gpu.bat` / 手工 `python main.py`；
- 启动参数固定 `--lowvram --reserve-vram 1`（给桌面留 1 GB）；
- 拿不到 `nvidia-smi` 信息时**拒绝启动**（不给"也许没事"留口子）。

### ② 出图参数硬上限（`src/tools/comfy_tools.py`）

| 限制 | 值 | 行为 |
|---|---|---|
| `steps` | **≤ 30**（默认 20） | 超出 → 中文 `ValueError`，不发请求 |
| 输入图边长 | 任一边 > **768** → 缩到 **512** | 工作流自动插入 `ImageScale`；读不到尺寸按大图处理 |
| 空闲显存 | **< 2500 MB** | 读 `/system_stats` 预检 → 中文拒绝，**不发 `/prompt`** |
| batch | **1** | 工作流无 batch 维度，`SaveImage` 只出单张 |

### ③ 一次一张，用完即停

- 禁止串行连跑多张：同进程内第二次调用会立即被拒（`已有出图任务在跑`）；
- 出图结束就 `-Stop` 关服务，不常驻后台吃显存。

### ④ 超时收紧

`PA_COMFY_TIMEOUT` 默认从 180 s 改为 **120 s**——跑不完就报错，不无限占着显存。

---

## 3. 其他 GPU 使用（已核查）

| 组件 | 状态 | 证据 |
|---|---|---|
| faster-whisper | **CPU** | `src/video/asr.py:169` `device: str = "cpu"`，`compute_type="int8"` |
| ffmpeg | **无 hwaccel** | `src/video/audio.py`、`keyframes.py` 只调用 `ffmpeg` 默认软解 |
| PA 其余代码 | 无 torch/cuda 引用 | `grep -i "cuda\|torch" src/` 只命中 comfy 工具本身 |

---

## 4. 自检清单（改 GPU 相关代码前过一遍）

```powershell
# 1) 是否用安全脚本启动
Get-Content $env:TEMP\pa-comfy.pid

# 2) 当前空闲显存
nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits

# 3) 工具约束是否还在
.\.venv\Scripts\python.exe -m unittest tests.unit.test_comfy_tools -q

# 4) 错误路径（服务未起时必须是中文提示）
.\.venv\Scripts\python.exe scripts\verify_comfy_tool.py
```

**红线**：不要为了"快一点"把 `steps` 上限、显存预检、`batch=1`、`--lowvram` 任何一项去掉。
