"""构造带真值的合成视频：20 句、逐句 TTS、记录每句精确起始时间。

    .\\.venv/Scripts/python.exe scripts/build_synthetic_video.py --out <目录>

输出：
    narration.wav / narration.mp4
    ground_truth.json  {"sentences": [{"index","text","key","start","end"}]}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SENTENCES = [
    ("项目代号北极星，启动于二零二五年三月。", "北极星"),
    ("系统每天处理大约三千次请求。", "三千"),
    ("核心模块叫 PagedAttention，负责显存分页。", "PagedAttention"),
    ("推理引擎是 vLLM，版本号零点六点三。", "vLLM"),
    ("缓存块大小固定为十六，可以调整。", "十六"),
    ("调度器按块回收，延迟下降百分之四十。", "百分之四十"),
    ("部署机器有十二个核心，内存十六 GB。", "十二个核心"),
    ("模型文件放在 E 盘的 models 目录。", "models"),
    ("转写使用 whisper，模型是 small。", "small"),
    ("术语偏置可以显著提升专名识别。", "术语偏置"),
    ("日志保留三十天，之后自动清理。", "三十天"),
    ("接口前缀是 api，所有路由都带它。", "api"),
    ("只读模式使用 8443 端口。", "8443"),
    ("令牌长度至少三十二个字符。", "三十二"),
    ("失败十次后会冷却五分钟。", "五分钟"),
    ("证书有效期三百九十七天。", "三百九十七"),
    ("产物登记后可以获得下载链接。", "下载链接"),
    ("对比功能展示两个版本的差异。", "对比"),
    ("时间线按时间倒序展示最近活动。", "时间线"),
    ("本周目标是完成全部验收。", "本周目标"),
]


def synth_sentence(text: str, target: Path) -> float:
    """用 SAPI 合成一句，返回时长（秒）。

    中文必须走 **UTF-8 BOM 文件**：经 PowerShell stdin 传中文会按 GBK 解码，
    结果是把乱码音节念出来（实测：英文术语还在、中文全成生僻字）。
    """

    script_path = target.with_suffix(".ps1")
    text_path = target.with_suffix(".txt")

    text_path.write_text(text, encoding="utf-8-sig")
    script_path.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$synth.SelectVoice('Microsoft Huihui Desktop')\n"
        "$synth.Rate = 0\n"
        f"$synth.SetOutputToWaveFile('{target}')\n"
        f"$text = Get-Content -Raw -Encoding UTF8 '{text_path}'\n"
        "$synth.Speak($text)\n"
        "$synth.Dispose()\n",
        encoding="utf-8-sig",
    )

    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    if completed.returncode != 0 or not target.is_file():
        raise SystemExit(f"TTS 失败：{completed.stderr[-300:]}")

    with wave.open(str(target), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def concat_wavs(parts: list[Path], target: Path, *, gap_seconds: float = 0.4) -> list[float]:
    """拼接 wav（同参数），句间插静音；返回每句的起始时间。"""

    params = None
    frames: list[bytes] = []
    starts: list[float] = []
    cursor = 0.0

    for part in parts:
        with wave.open(str(part), "rb") as handle:
            current = handle.getparams()

            if params is None:
                params = current

            elif (
                current.nchannels, current.sampwidth, current.framerate
            ) != (params.nchannels, params.sampwidth, params.framerate):
                raise SystemExit("TTS 输出参数不一致")

            data = handle.readframes(handle.getnframes())
            duration = handle.getnframes() / float(handle.getframerate())

        starts.append(round(cursor, 3))
        frames.append(data)
        cursor += duration

        silence = b"\x00" * int(params.framerate * gap_seconds * params.sampwidth)
        frames.append(silence)
        cursor += gap_seconds

    with wave.open(str(target), "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        out.writeframes(b"".join(frames))

    return starts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构造带真值的合成视频")
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-video", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as folder:
        parts = []

        for index, (text, _key) in enumerate(SENTENCES):
            part = Path(folder) / f"s{index:02d}.wav"

            synth_sentence(text, part)
            parts.append(part)

        starts = concat_wavs(parts, out_dir / "narration.wav")

    with wave.open(str(out_dir / "narration.wav"), "rb") as handle:
        total = handle.getnframes() / float(handle.getframerate())

    truth = {
        "total_seconds": round(total, 2),
        "sentences": [
            {
                "index": index,
                "text": text,
                "key": key,
                "start": starts[index],
            }
            for index, (text, key) in enumerate(SENTENCES)
        ],
    }
    (out_dir / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not args.no_video:
        video = out_dir / "narration.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s=640x360:d={total:.2f}",
                "-i", str(out_dir / "narration.wav"),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest", str(video),
            ],
            check=False,
            capture_output=True,
        )

        if not video.is_file():
            raise SystemExit("mux mp4 失败")

    print(f"句子 {len(SENTENCES)} 条，总时长 {total:.1f} s")
    print(f"输出：{out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
