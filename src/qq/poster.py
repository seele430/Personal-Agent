"""QQ 海报管线：文字需求 → LLM 写自包含 HTML → 无头 Edge 截图成 PNG。

边界（写死）：

- 只渲染 LLM 生成的**静态自包含 HTML**（提示词里禁外部资源、禁脚本联网）；
- 渲染用本机 Edge 无头截图，超时 30 秒，产物必须真实落盘且非空；
- 产物放 ``data/posters/<message_id>/``（poster.html + poster.png），可追溯；
- 任何一步失败抛 `PosterError`（中文短句），绝不发空白图。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from pathlib import Path


RENDER_TIMEOUT_SECONDS = 30.0
POSTER_WIDTH = 750
POSTER_HEIGHT = 1100

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

PROMPT_RULES = (
    "生成一张手机端竖版海报，输出一个完整、自包含的 HTML 文件（只要 HTML 本身，不要任何解释）。\n"
    "硬性要求：\n"
    "1. 所有样式写在 <style> 里；禁止引用任何外部资源（字体、图片、脚本、CDN 一律禁止）。\n"
    f"2. 版面固定 {POSTER_WIDTH}x{POSTER_HEIGHT} 像素：html、body 无外边距、无滚动条、内容不得溢出。\n"
    "3. 色彩鲜艳、对比强、排版有层次：主标题和关键数字要醒目，适当用色块分区。\n"
    "4. 数据可视化必须用纯 CSS 实现（禁止 JS 图表库）："
    "占比/构成/汇总对比类数据优先用饼图（conic-gradient 画扇区，配图例）；"
    "趋势/逐年演变类数据用柱状图或条形图（div + 宽度百分比）；"
    "用户指定了图表类型时必须遵从。\n"
    "5. 涉及数据/统计时，底部用小字标注来源与年份；凭记忆给出、无法核实的数字必须标注「未联网核实」。\n"
    "6. 字体只用系统常见中文字体（如 Microsoft YaHei）。\n"
)


class PosterError(RuntimeError):
    """海报生成失败（中文短句）。"""


def data_root() -> Path:
    return Path(
        os.getenv("PERSONAL_AGENT_DATA_DIR")
        or Path(__file__).resolve().parents[2] / "data"
    )


def build_poster_prompt(topic: str) -> str:
    return f"{PROMPT_RULES}海报主题与要求：{topic}\n"


def extract_html(text: str) -> str:
    """从模型输出里抠出纯 HTML：模型常带「这是……」开场白和 ```html 围栏，直接渲染会漏字。

    优先取围栏内的 HTML；否则取 <!DOCTYPE/<html> 到 </html> 的区间；都没有返回空串。
    """

    fenced = re.search(
        r"```(?:html)?\s*((?:<!DOCTYPE|<html)[\s\S]*?</html>)\s*```",
        text or "",
        re.IGNORECASE,
    )

    if fenced:
        return fenced.group(1).strip()

    bare = re.search(
        r"((?:<!DOCTYPE[^>]*>|<html)[\s\S]*?</html>)", text or "", re.IGNORECASE
    )

    return bare.group(1).strip() if bare else ""


def find_edge() -> str:
    """找本机 Edge；找不到返回空串。"""

    found = shutil.which("msedge")

    if found:
        return found

    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    return ""


def render_png(html_path: Path, png_path: Path, *, timeout: float = RENDER_TIMEOUT_SECONDS) -> Path:
    """无头 Edge 把 HTML 截成 PNG；失败抛 PosterError。"""

    edge = find_edge()

    if not edge:
        raise PosterError("找不到 Edge 浏览器，无法渲染海报")

    command = [
        edge,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={POSTER_WIDTH},{POSTER_HEIGHT}",
        f"--screenshot={png_path}",
        Path(html_path).resolve().as_uri(),
    ]

    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise PosterError("Edge 渲染超时") from None
    except OSError as error:
        raise PosterError(f"Edge 启动失败：{error}") from None

    if not png_path.is_file() or png_path.stat().st_size == 0:
        raise PosterError("Edge 没有产出图片")

    return png_path


def make_poster(
    topic: str,
    message_id: str,
    *,
    provider,
    root: Path | None = None,
    renderer=render_png,
) -> Path:
    """全链路：LLM 写 HTML → 渲染 PNG；返回 PNG 路径。"""

    if not getattr(provider, "configured", False):
        raise PosterError("未配置外部 Provider")

    out_dir = (root or data_root()) / "posters" / str(message_id or int(time.time()))
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = provider.submit(
            build_poster_prompt(topic), artifact_name="poster.html", out_dir=out_dir
        )
    except Exception as error:  # noqa: BLE001 - 外部失败要回给用户
        raise PosterError(f"模型生成失败：{error}") from None

    html_path = Path(result.get("artifact") or out_dir / "poster.html")
    raw = html_path.read_text(encoding="utf-8", errors="replace") if html_path.is_file() else ""
    cleaned = extract_html(raw)

    if not cleaned:
        raise PosterError("模型没产出有效的 HTML")

    if cleaned != raw.strip():
        html_path.write_text(cleaned, encoding="utf-8")

    return renderer(html_path, out_dir / "poster.png")
