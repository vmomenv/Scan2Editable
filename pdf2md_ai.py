#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2md_ai.py —— 用多模态大模型把（扫描版）PDF 还原成可编辑的 Markdown / Word

核心流程
--------
  1. 渲染      : PDF 每一页渲染成高分辨率 PNG（优先 PyMuPDF，回退 poppler 的 pdftoppm）
  2. 版面理解  : 把整页图片交给多模态模型，要求返回
                   - markdown 正文（公式用 LaTeX，插图位置用 [[FIG:n]] 占位）
                   - figures[]：每张插图的图号、图题、归一化 bbox(0-1000)
  3. 精修坐标  : 用「裁剪 → 让模型在局部图里重新框选 → 反馈 complete/only_target」的
                 闭环迭代，把粗略 bbox 收敛到紧贴图形+图题（--refine）
  4. 裁图      : 按收敛后的 bbox 从高分辨率页面裁出插图，可选按墨迹投影再收紧（--trim）
  5. 组装      : 把 [[FIG:n]] 替换成 Markdown 图片引用，输出 .md + images/
  6. 导出      : 可选转 .docx（优先 pandoc：LaTeX 公式会变成 Word 原生公式 OMML）

依赖：Pillow + requests（必需）；PyMuPDF / poppler / pandoc 都是可选的加速器。

示例
----
  export DEEPSEEK_API_KEY=sk-xxx
  python3 pdf2md_ai.py scan.pdf -o output

  # 换供应商 / 模型
  python3 pdf2md_ai.py scan.pdf -o output --provider openai --model gpt-4o
  python3 pdf2md_ai.py scan.pdf -o output --provider dashscope --model qwen-vl-max

  # 只要 Markdown，跳过插图精修（省钱快速）
  python3 pdf2md_ai.py scan.pdf -o output --refine 0 --no-trim

  # 同时导出 Word
  python3 pdf2md_ai.py scan.pdf -o output --docx
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Sequence

# ----------------------------------------------------------------------------
# 依赖检查
# ----------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 Pillow：  pip install pillow")

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 requests： pip install requests")


LOG_PREFIX = "[pdf2md]"
QUIET = False


def log(msg: str, level: str = "info") -> None:
    if QUIET and level not in ("warn", "error"):
        return
    mark = {"info": "·", "warn": "!", "error": "×", "ok": "✓", "step": "▶"}.get(level, "·")
    stream = sys.stderr if level in ("warn", "error") else sys.stdout
    print(f"{LOG_PREFIX} {mark} {msg}", file=stream, flush=True)


# ============================================================================
# 1. 供应商配置
# ============================================================================
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "api": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "api_key_env": ["DEEPSEEK_API_KEY"],
        "note": "deepseek-flash 支持图片输入，返回 0-1000 归一化坐标",
    },
    "openai": {
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key_env": ["OPENAI_API_KEY"],
    },
    "dashscope": {
        "api": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max-latest",
        "api_key_env": ["DASHSCOPE_API_KEY"],
    },
    "zhipu": {
        "api": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-plus",
        "api_key_env": ["ZHIPUAI_API_KEY", "ZHIPU_API_KEY"],
    },
    "moonshot": {
        "api": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k-vision-preview",
        "api_key_env": ["MOONSHOT_API_KEY"],
    },
    "gemini": {
        "api": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
        "api_key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    "anthropic": {
        "api": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
        "api_key_env": ["ANTHROPIC_API_KEY"],
    },
    "openai-compatible": {
        "api": "openai",
        "base_url": "http://localhost:8000/v1",
        "model": "",
        "api_key_env": ["AI_API_KEY"],
    },
}

DSH_CREDENTIALS = os.path.expanduser("~/.dsh/.credentials.yaml")


def _read_dsh_key() -> str | None:
    """本机 DSH 客户端把 DEEPSEEK_API_KEY 存在 ~/.dsh/.credentials.yaml 里，作为兜底。"""
    if not os.path.isfile(DSH_CREDENTIALS):
        return None
    try:
        import yaml  # PyYAML 是可选依赖
        data = yaml.safe_load(open(DSH_CREDENTIALS, encoding="utf-8")) or {}
        return (data.get("refs") or {}).get("DEEPSEEK_API_KEY")
    except Exception:
        # 没有 PyYAML 时用最朴素的解析，避免为此多一个硬依赖
        try:
            text = open(DSH_CREDENTIALS, encoding="utf-8").read()
            m = re.search(r"^\s*DEEPSEEK_API_KEY:\s*(\S+)\s*$", text, re.M)
            return m.group(1) if m else None
        except Exception:
            return None


def resolve_config(args: argparse.Namespace) -> dict[str, Any]:
    preset = dict(PROVIDERS.get(args.provider, PROVIDERS["openai-compatible"]))

    base_url = args.base_url or os.environ.get("AI_BASE_URL") or preset["base_url"]
    model = args.model or os.environ.get("AI_MODEL") or preset["model"]
    api = preset["api"]

    api_key = args.api_key or os.environ.get("AI_API_KEY") or ""
    if not api_key:
        for env in preset.get("api_key_env", []):
            if os.environ.get(env):
                api_key = os.environ[env]
                break
    if not api_key and args.provider == "deepseek":
        api_key = _read_dsh_key() or ""
        if api_key:
            log("使用 ~/.dsh/.credentials.yaml 中的 DEEPSEEK_API_KEY", "warn")

    if not api_key:
        raise SystemExit(
            "没有找到 API Key。请用 --api-key 传入，或设置环境变量 "
            f"{' / '.join(preset.get('api_key_env') or ['AI_API_KEY'])}。"
        )
    if not model:
        raise SystemExit("没有指定模型，请用 --model 或环境变量 AI_MODEL。")

    return {"api": api, "base_url": base_url.rstrip("/"), "model": model,
            "api_key": api_key, "timeout": args.timeout, "retries": args.retries,
            "max_tokens": args.max_tokens}


# ============================================================================
# 2. 多模态客户端
# ============================================================================
class VLMClient:
    """极简多模态客户端，支持 OpenAI Chat Completions 与 Anthropic Messages 两种协议。"""

    def __init__(self, cfg: dict[str, Any]):
        self.api = cfg["api"]
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self.api_key = cfg["api_key"]
        self.timeout = cfg["timeout"]
        self.retries = cfg["retries"]
        self.max_tokens = cfg["max_tokens"]
        self.session = requests.Session()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self._anthropic_tools: list[dict] = []
        self._fatal: str = ""

    # -- 底层 HTTP ---------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.api == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    hint = ""
                    if r.status_code == 429 and "concurrency" in r.text:
                        hint = "（账户并发额度用满，把 --workers 调小可降低并发）"
                    raise RuntimeError(f"HTTP {r.status_code}{hint}: {r.text[:300]}")
                if r.status_code >= 400:
                    self._fatal = f"API 返回 {r.status_code}: {r.text[:600]}"
                    raise RuntimeError(self._fatal)
                return r.json()
            except Exception as exc:  # 网络抖动 / 限流 → 退避重试
                last_err = exc
                if attempt < self.retries:
                    wait = min(2 ** attempt, 20)
                    log(f"请求失败（{exc}），{wait}s 后重试 {attempt}/{self.retries - 1}", "warn")
                    time.sleep(wait)
        raise RuntimeError(f"请求重试全部失败: {last_err}")

    # -- 统一 chat 接口 ----------------------------------------------------
    def chat(self, prompt: str, images: Sequence[bytes] = (), max_tokens: int | None = None,
             temperature: float = 0.0) -> str:
        max_tokens = max_tokens or self.max_tokens
        if self.api == "anthropic":
            content: list[dict] = []
            for raw in images:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png",
                               "data": base64.b64encode(raw).decode()},
                })
            content.append({"type": "text", "text": prompt})
            payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                       "messages": [{"role": "user", "content": content}]}
            if self._anthropic_tools:
                payload["tools"] = self._anthropic_tools
            data = self._post("/v1/messages", payload)
            self._track(data)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        content = []
        for raw in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()},
            })
        content.append({"type": "text", "text": prompt})
        payload = {"model": self.model, "messages": [{"role": "user", "content": content}],
                   "max_tokens": max_tokens, "temperature": temperature}
        data = self._post("/chat/completions", payload)
        self._track(data)
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            log("模型输出被 max_tokens 截断，建议调大 --max-tokens", "warn")
        return choice.get("message", {}).get("content") or ""

    def _track(self, data: dict) -> None:
        u = data.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += u.get("prompt_tokens") or u.get("input_tokens") or 0
        self.usage["completion_tokens"] += u.get("completion_tokens") or u.get("output_tokens") or 0


# ============================================================================
# 3. PDF 渲染
# ============================================================================
def render_pdf(pdf_path: str, outdir: str, dpi: int, pages: Sequence[int] | None = None) -> list[str]:
    """把 PDF 渲染为 PNG，返回按页序排列的文件路径列表。"""
    os.makedirs(outdir, exist_ok=True)

    # 方案 A：PyMuPDF（快、纯 Python）
    try:
        import fitz  # type: ignore

        doc = fitz.open(pdf_path)
        wanted = list(pages) if pages else list(range(1, doc.page_count + 1))
        paths = []
        for pno in wanted:
            page = doc[pno - 1]
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
            path = os.path.join(outdir, f"page_{pno:04d}.png")
            pix.save(path)
            paths.append(path)
        doc.close()
        log(f"PyMuPDF 渲染 {len(paths)} 页 @ {dpi}dpi", "ok")
        return paths
    except ImportError:
        pass

    # 方案 B：poppler 的 pdftoppm
    if not shutil.which("pdftoppm"):
        raise SystemExit("渲染 PDF 需要 PyMuPDF 或 poppler-utils(pdftoppm)：\n"
                         "  pip install PyMuPDF    或    sudo apt install poppler-utils")
    total = _pdf_page_count(pdf_path)
    wanted = list(pages) if pages else list(range(1, total + 1))
    paths = []
    for pno in wanted:
        base = os.path.join(outdir, f"page_{pno:04d}")
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(pno), "-l", str(pno),
                        "-singlefile", pdf_path, base], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        path = base + ".png"
        _normalise_orientation(path, pno)
        paths.append(path)
    log(f"pdftoppm 渲染 {len(paths)} 页 @ {dpi}dpi", "ok")
    return paths


def _normalise_orientation(png_path: str, page_no: int, min_conf: float = 0.6) -> None:
    """把竖排的亚洲文字自动转正。

    关键点：页面被整体旋转 180° 时，单个汉字看上去仍然是正常的汉字，只有把它们
    连起来看才知道是倒的。多模态模型看着倒排的文字去回归坐标会整体跑偏，所以这里
    先用 tesseract 的竖排模型逐块投票，判定整页是否倒置，是就转 180°。
    """
    if not shutil.which("tesseract"):
        log("未安装 tesseract，跳过页面方向检测（若 PDF 页面被旋转过，坐标会整体偏移）", "warn")
        return
    try:
        from PIL import Image
        img = Image.open(png_path)
        w, h = img.size
        votes = {"upright": 0, "inverted": 0}
        # 取上/中/下三块带，分别用竖排中文模型识别并读出置信度
        for i, frac in enumerate((0.05, 0.4, 0.75)):
            band = img.crop((0, int(h * frac), w, int(h * (frac + 0.22)))).convert("L")
            tmp = png_path + f".band{i}.png"
            band.save(tmp)
            try:
                out = subprocess.run(
                    ["tesseract", tmp, "stdout", "--psm", "6", "-l", "chi_sim_vert",
                     "tsv"], capture_output=True, text=True, timeout=120).stdout
            except Exception:
                os.remove(tmp)
                continue
            os.remove(tmp)
            confs = []
            for line in out.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) >= 11 and cols[11].strip():
                    try:
                        confs.append(float(cols[10]))
                    except ValueError:
                        pass
            if not confs:
                continue
            avg = sum(confs) / len(confs)
            if avg < min_conf:
                continue
            # 该带宽度下能识别出足够多字，认为整页方向正确
            if len(confs) >= 8:
                votes["upright"] += 1
            else:
                votes["inverted"] += 1
        if votes["inverted"] > votes["upright"]:
            img.rotate(180, expand=True).save(png_path)
            log(f"第 {page_no} 页检测为倒置（{votes}），已旋转 180°", "warn")
        else:
            log(f"第 {page_no} 页方向检测：{votes or '无有效投票'}", "info")
    except Exception as exc:
        log(f"页面方向检测失败（{exc}），按原样使用", "warn")


def _pdf_page_count(pdf_path: str) -> int:
    try:
        import fitz  # type: ignore
        d = fitz.open(pdf_path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        pass
    if shutil.which("pdfinfo"):
        out = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        if m:
            return int(m.group(1))
    raise SystemExit("无法确定 PDF 页数，请安装 PyMuPDF 或 poppler-utils")


# ============================================================================
# 4. 提示词
# ============================================================================
PAGE_PROMPT = """你是一台高精度的「扫描件版面还原引擎」。输入是一页扫描文档的整页图片。

请完成两件事：

【一】把整页内容还原成 Markdown，严格遵守：
1. 按人类阅读顺序输出。多栏排版请「先读完左栏再读右栏」，不要左右交错。
2. 逐字还原原文，不要翻译、不要概括、不要补充原文没有的内容；无法辨认的字用 `□` 表示。
3. 数学公式、物理量、化学式一律用 LaTeX：行内用 $...$，独立成行用 $$...$$。
   下标写 $F_1$、$l_1$、$M_O$，上标写 $v^2$，分数写 $\\frac{a}{b}$，根号写 $\\sqrt{x}$，
   向量写 $\\vec{F}$，单位与中文照原样保留。
4. 选择题选项写成列表：`A. ...` 每项一行；题号保留为 `**1.**` 或 `1.`。
5. 填空的下划线写成 `______`。
6. 表格还原成 Markdown 表格；标题用 `#`/`##`/`###`。
7. 插图位置必须插入占位符 `[[FIG:n]]`（n 与下面 figures 数组里的 id 对应），
   占位符单独占一行，放在正文中该图实际出现的位置。
8. 页眉、页脚、页码可以不输出。

【二】找出这一页上所有的插图（示意图、照片、表格图片等），给出精确外接矩形：
   - bbox = [x0, y0, x1, y1]，归一化到整页的 0-1000 整数，原点在左上角，x 向右，y 向下。
   - 框要**紧贴图形本身**，并包含图形正下方的图题文字（例如「图11-1-7」）。
     不要把周围的正文段落框进去，也不要切断图形任何一部分。
   - label 用页面上的图号原文（如「图11-1-7」）；子图用「图11-1-14甲」这种写法。

只输出一个 JSON 对象，不要 Markdown 代码围栏，不要任何解释文字：
{
  "markdown": "……正文，插图处写 [[FIG:1]] ……",
  "figures": [
    {"id": 1, "label": "图11-1-7", "caption": "图题原文", "bbox": [x0, y0, x1, y1],
     "kind": "diagram|photo|table", "description": "一句话描述图上画了什么"}
  ]
}
若本页没有任何插图，figures 返回空数组。"""


def refine_prompt(label: str, caption: str, w: int, h: int) -> str:
    cap = f"（图题文字「{caption}」通常在图形的正下方）" if caption else ""
    return f"""这是从一页扫描文档上裁下的一块区域，尺寸 {w}px × {h}px。
请在本区域内**精确框选**插图「{label}」{cap}。

要求：
- 矩形紧贴图形本身 + 它正下方的图题文字，不要包含周围正文段落，也不要切断图形的任何部分。
- 坐标归一化到「本裁剪图」自身：0-1000 的整数，原点左上角，x 向右，y 向下。
- complete：矩形是否完整包含了整张图（没有切断）。如果图形超出了本裁剪区域的边界，
  说明它在这个小图里本来就不完整，此时 complete 填 false。
- only_target：矩形内是否只有这一张图和它的图题（没有混进别的内容）。
- caption_visible：图题文字是否已经包含在矩形内。
- missing_side：如果不完整，指出被切掉的是哪一边，取值为 left/right/top/bottom/none；
  caption_visible 为 false 时通常填 bottom。

只输出 JSON：
{{"bbox": [x0, y0, x1, y1], "complete": true, "only_target": true, "caption_visible": true,
 "missing_side": "none", "note": "简述框里有什么"}}"""


# ============================================================================
# 5. 工具函数
# ============================================================================
def parse_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍代码围栏、前后解释文字、尾随逗号）。"""
    if not text:
        raise ValueError("模型返回空内容")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"输出中没有 JSON 对象: {text[:200]!r}")
    frag = s[start:end + 1]
    try:
        return json.loads(frag)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", frag)  # 去掉尾随逗号
        return json.loads(fixed)


def clamp_box(box: Iterable[float], lo: float = 0.0, hi: float = 1000.0) -> list[int]:
    vals = [int(round(float(v))) for v in box]
    x0, y0, x1, y1 = vals
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return [int(max(lo, min(hi, x0))), int(max(lo, min(hi, y0))),
            int(max(lo, min(hi, x1))), int(max(lo, min(hi, y1)))]


def norm_to_px(box: Sequence[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    w, h = size
    x0, y0, x1, y1 = box
    return (int(round(x0 / 1000 * w)), int(round(y0 / 1000 * h)),
            int(round(x1 / 1000 * w)), int(round(y1 / 1000 * h)))


def px_to_norm(px: Sequence[float], size: tuple[int, int]) -> list[int]:
    w, h = size
    return clamp_box([px[0] / w * 1000, px[1] / h * 1000, px[2] / w * 1000, px[3] / h * 1000])


def expand_box(box: Sequence[float], frac: float, limit: float = 1000.0) -> list[int]:
    x0, y0, x1, y1 = box
    pw, ph = (x1 - x0) * frac, (y1 - y0) * frac
    return clamp_box([x0 - pw, y0 - ph, x1 + pw, y1 + ph], 0, limit)


def safe_name(text: str, fallback: str = "fig") -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", (text or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s or fallback)[:60]


def png_bytes(img: "Image.Image") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def image_ink_bbox(img: "Image.Image", thr: int = 250) -> tuple[int, int, int, int] | None:
    """用行/列投影求图内墨迹（非白像素）的紧致外接框。纯 Pillow 实现，不依赖 numpy。"""
    gray = img.convert("L")
    w, h = gray.size
    if w < 3 or h < 3:
        return None
    cols = list(gray.resize((w, 1), Image.BOX).getdata())
    rows = list(gray.resize((1, h), Image.BOX).getdata())
    xs = [i for i, v in enumerate(cols) if v < thr]
    ys = [i for i, v in enumerate(rows) if v < thr]
    if not xs or not ys:
        return None
    return (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1)


# ============================================================================
# 6. 插图精修（核心：模型闭环纠偏）
# ============================================================================
class FigureLocator:
    def __init__(self, client: VLMClient, page_img: "Image.Image", page_no: int,
                 iterations: int, verbose: bool = True):
        self.client = client
        self.page = page_img
        self.size = page_img.size
        self.page_no = page_no
        self.iterations = max(0, iterations)
        self.verbose = verbose

    def _crop_window(self, box: Sequence[float], frac: float) -> tuple["Image.Image", list[int], float]:
        """按归一化 box 外扩 frac 裁剪，返回（图, 窗口归一化 box, 分辨率倍率）。"""
        win = expand_box(box, frac)
        px = norm_to_px(win, self.size)
        px = (max(0, px[0]), max(0, px[1]), min(self.size[0], px[2]), min(self.size[1], px[3]))
        if px[2] - px[0] < 8 or px[3] - px[1] < 8:
            px = (max(0, px[0]), max(0, px[1]), px[0] + 8, px[1] + 8)
        sub = self.page.crop(px)
        # 小图放大后模型更容易看清细节，坐标仍是归一化的，不影响映射
        scale = 1.0
        if max(sub.size) < 720:
            scale = min(2.5, 900 / max(1, max(sub.size)))
            sub = sub.resize((int(sub.width * scale), int(sub.height * scale)), Image.LANCZOS)
        return sub, win, scale

    def refine(self, label: str, caption: str, seed: Sequence[float]) -> tuple[list[int], list[dict]]:
        """迭代精修 seed bbox，返回（最终 bbox, 迭代轨迹）。iterations=0 时原样返回。"""
        seed = clamp_box(seed)
        cur = list(seed)
        trace: list[dict] = [{"iter": 0, "bbox": list(cur), "stage": "seed"}]
        if self.iterations == 0:
            return cur, trace

        frac = 0.30
        for k in range(1, self.iterations + 1):
            sub, win, _ = self._crop_window(cur, frac)
            try:
                raw = self.client.chat(refine_prompt(label, caption, *sub.size), [png_bytes(sub)],
                                       max_tokens=min(self.client.max_tokens, 16000))
                data = parse_json(raw)
                local = clamp_box(data["bbox"])
            except Exception as exc:
                if self.verbose:
                    log(f"  第{k}轮精修失败（{exc}），沿用当前框", "warn")
                break

            # 局部坐标 → 整页归一化坐标
            nx0 = win[0] + local[0] / 1000 * (win[2] - win[0])
            ny0 = win[1] + local[1] / 1000 * (win[3] - win[1])
            nx1 = win[0] + local[2] / 1000 * (win[2] - win[0])
            ny1 = win[1] + local[3] / 1000 * (win[3] - win[1])
            new = clamp_box([nx0, ny0, nx1, ny1])

            if not self._plausible(new, cur):
                trace.append({"iter": k, "bbox": list(cur), "rejected": list(new),
                              "note": "候选框尺寸不合理，已拒绝"})
                if self.verbose:
                    log(f"  第{k}轮候选 {new} 尺寸不合理（相对 {cur}），沿用当前框", "warn")
                break

            delta = max(abs(new[i] - cur[i]) for i in range(4))
            complete = bool(data.get("complete"))
            side = str(data.get("missing_side") or "none").lower()
            trace.append({"iter": k, "bbox": list(new), "complete": complete,
                          "only_target": bool(data.get("only_target")),
                          "caption_visible": bool(data.get("caption_visible")),
                          "missing_side": side, "note": str(data.get("note", ""))[:120]})
            cur = new
            if self.verbose:
                log(f"  第{k}轮 → {cur}  Δ={delta}  complete={complete} "
                    f"only_target={data.get('only_target')} missing={side}")

            if delta <= 3 and complete and data.get("only_target"):
                break
            # 模型说这一侧被切掉了 → 下一轮把窗口朝那一侧多张开一些，给它重新框的机会
            if not complete and side in ("left", "right", "top", "bottom"):
                frac = 0.55
                cur = self._grow_side(cur, side, 0.25)
                trace.append({"iter": k, "bbox": list(cur), "stage": f"grow-{side}"})
                if self.verbose:
                    log(f"    ↳ 图形在 {side} 侧被切断，向该侧外扩到 {cur}")
            else:
                frac = 0.12

        # 收尾：退化框回退到初始候选；图题仍缺失则向下补一小段
        if not self._plausible(cur, seed, allow_equal=True):
            trace.append({"iter": "fallback", "bbox": list(seed), "note": "精修结果退化，回退初始框"})
            if self.verbose:
                log(f"  精修结果 {cur} 退化，回退到初始框 {seed}", "warn")
            cur = list(seed)

        if trace[-1].get("caption_visible") is False:
            x0, y0, x1, y1 = cur
            pad = max(6, int((y1 - y0) * 0.18))
            cur = clamp_box([x0, y0, x1, min(1000, y1 + pad)])
            trace.append({"iter": "caption-pad", "bbox": list(cur)})
            if self.verbose:
                log(f"  图题缺失 → 向下补齐到 {cur}")
        return cur, trace

    @staticmethod
    def _plausible(new: Sequence[float], ref: Sequence[float], allow_equal: bool = True) -> bool:
        """拒绝退化/暴涨的候选框。

        注意：只拦「退化成一条线」和「暴涨好几倍」这两种明显误检。
        模型把过大的初始框收小到真实图形是正常行为，不能拦。
        """
        nw, nh = new[2] - new[0], new[3] - new[1]
        if nw < 18 or nh < 18:          # 归一化 0-1000 下小于 1.8% 边长，基本是误检
            return False
        rw, rh = ref[2] - ref[0], ref[3] - ref[1]
        if rw > 0 and rh > 0:
            ratio = (nw * nh) / (rw * rh)
            if ratio > 8.0:
                return False
        return True

    @staticmethod
    def _grow_side(box: Sequence[float], side: str, frac: float) -> list[int]:
        x0, y0, x1, y1 = box
        dx, dy = (x1 - x0) * frac, (y1 - y0) * frac
        if side == "left":
            x0 -= dx
        elif side == "right":
            x1 += dx
        elif side == "top":
            y0 -= dy
        elif side == "bottom":
            y1 += dy
        return clamp_box([x0, y0, x1, y1])


def trim_to_ink(page_img: "Image.Image", box: Sequence[float], margin: float = 0.02) -> list[int]:
    """在 box 外扩一点点的窗口内，按墨迹投影把多余白边收紧。"""
    size = page_img.size
    win = expand_box(box, margin)
    px = norm_to_px(win, size)
    px = (max(0, px[0]), max(0, px[1]), min(size[0], px[2]), min(size[1], px[3]))
    if px[2] - px[0] < 8 or px[3] - px[1] < 8:
        return list(box)
    sub = page_img.crop(px)
    ink = image_ink_bbox(sub)
    if not ink:
        return list(box)
    tightened = px_to_norm([px[0] + ink[0], px[1] + ink[1], px[0] + ink[2], px[1] + ink[3]], size)
    # 收紧后不能比原框小太多（避免把浅色内容误删）
    w0, h0 = box[2] - box[0], box[3] - box[1]
    w1, h1 = tightened[2] - tightened[0], tightened[3] - tightened[1]
    if w0 and h0 and (w1 * h1) < 0.35 * (w0 * h0):
        return list(box)
    # 单边最多内收 25%，避免把细线条/浅色图形切掉
    for i, (a, b) in enumerate(((0, 2), (1, 3))):
        span = box[b] - box[a]
        if tightened[a] - box[a] > 0.25 * span:      # 左/上边内收过多
            tightened[a] = int(box[a] + 0.25 * span)
        if box[b] - tightened[b] > 0.25 * span:      # 右/下边内收过多
            tightened[b] = int(box[b] - 0.25 * span)
    return clamp_box(tightened)


# ============================================================================
# 6b. 裁剪区域校验 + 分块兜底搜索
# ============================================================================
VERIFY_PROMPT = """这是从一页扫描文档上裁出来的一块区域。请如实回答：
1) 这块区域里有没有插图（示意图/照片）？区域里的图题文字原文是什么（逐字抄写，没有就留空）？
2) 这块区域是否**完整**包含图号为「{label}」的那张插图（含它下方的图题）？

只输出 JSON：
{{"has_figure": true, "labels": ["抄写看到的图题，如 图11-1-12"], "contains_target": true,
 "complete": true, "note": "简述区域内容"}}"""

TILE_PROMPT = """这是从一页扫描文档（可能是双栏排版）上裁下的一块区域，尺寸 {w}px × {h}px。
请找出**本区域内**出现的所有插图（示意图、照片、表格图片），不要找区域外的。

对每张图给出：
- label：图号原文（例如「图11-1-7」）；若该图没有可见图号，用一句话描述它的位置，如「右上角的手推车图」
- caption：图形下方图题文字的原文（没有就留空）
- bbox：[x0,y0,x1,y1]，归一化到**本裁剪图**的 0-1000 整数，原点左上角，x 向右，y 向下，
  紧贴图形本身并包含它下方的图题文字

只输出 JSON：{{"figures":[{{"label":"...","caption":"...","bbox":[x0,y0,x1,y1]}}]}}"""


def _norm_label(text: str) -> str:
    return re.sub(r"[\s_·、。]", "", str(text or "")).lower()


def split_tiles(cols: int = 2, rows: int = 3, overlap: float = 0.12) -> list[list[float]]:
    """把整页切成带重叠的小块，返回每块的归一化窗口 [x0,y0,x1,y1]。"""
    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0 = c / cols * 1000
            y0 = r / rows * 1000
            x1 = (c + 1) / cols * 1000
            y1 = (r + 1) / rows * 1000
            dx, dy = (x1 - x0) * overlap, (y1 - y0) * overlap
            tiles.append([max(0.0, x0 - dx), max(0.0, y0 - dy),
                          min(1000.0, x1 + dx), min(1000.0, y1 + dy)])
    return tiles


def verify_crop(client: VLMClient, page_img: "Image.Image", bbox: Sequence[float],
                label: str) -> dict:
    """把裁出来的区域回喂给模型，确认里面确实是目标插图。"""
    px = norm_to_px(bbox, page_img.size)
    px = (max(0, px[0]), max(0, px[1]), min(page_img.size[0], px[2]), min(page_img.size[1], px[3]))
    if px[2] - px[0] < 8 or px[3] - px[1] < 8:
        return {"contains_target": False, "note": "裁图区域过小"}
    sub = page_img.crop(px)
    if max(sub.size) < 700:
        s = min(2.0, 800 / max(1, max(sub.size)))
        sub = sub.resize((int(sub.width * s), int(sub.height * s)), Image.LANCZOS)
    try:
        data = parse_json(client.chat(VERIFY_PROMPT.format(label=label), [png_bytes(sub)],
                                      max_tokens=min(client.max_tokens, 12000)))
        return data
    except Exception as exc:
        log(f"  校验失败（{exc}），按通过处理", "warn")
        return {"contains_target": True, "note": f"verify error: {exc}"}


def verify_ok(verdict: dict, label: str) -> bool:
    """判断校验结论算不算通过。

    模型经常因为「图题被裁掉一点点」就报 contains_target=false，但 labels 里其实
    清楚写着目标图号——这种情况按通过处理，避免无谓地触发昂贵的分块兜底。
    """
    if verdict.get("contains_target"):
        return True
    want = _norm_label(label)
    seen = _norm_label("".join(verdict.get("labels") or []))
    return bool(want and seen and want in seen)


def tiled_figure_search(client: VLMClient, page_img: "Image.Image", page_no: int,
                        cache_dir: str | None, use_cache: bool = True) -> list[dict]:
    """分块扫描整页，得到一批更可靠的局部检测结果（整页归一化坐标）。"""
    key = cache_key(CACHE_VERSION, "tiles", client.model, str(page_no),
                    hashlib.sha256(png_bytes(page_img)).hexdigest())
    if use_cache:
        hit = load_cache(cache_dir, key)
        if hit:
            return hit.get("figures", [])

    tiles = split_tiles()
    results: list[dict] = []

    def scan(tile: Sequence[float]) -> list[dict]:
        px = norm_to_px(tile, page_img.size)
        px = (max(0, px[0]), max(0, px[1]), min(page_img.size[0], px[2]), min(page_img.size[1], px[3]))
        sub = page_img.crop(px)
        if sub.width < 8 or sub.height < 8:
            return []
        try:
            data = parse_json(client.chat(TILE_PROMPT.format(w=sub.width, h=sub.height),
                                          [png_bytes(sub)],
                                          max_tokens=min(client.max_tokens, 20000)))
        except Exception as exc:
            log(f"  分块检测失败（{exc}）", "warn")
            return []
        found = []
        for item in data.get("figures") or []:
            if not isinstance(item, dict):
                continue
            try:
                lb = clamp_box(item.get("bbox") or [0, 0, 0, 0])
            except Exception:
                continue
            if lb[2] - lb[0] < 4 or lb[3] - lb[1] < 4:
                continue
            # 局部 → 整页归一化
            gx0 = tile[0] + lb[0] / 1000 * (tile[2] - tile[0])
            gy0 = tile[1] + lb[1] / 1000 * (tile[3] - tile[1])
            gx1 = tile[0] + lb[2] / 1000 * (tile[2] - tile[0])
            gy1 = tile[1] + lb[3] / 1000 * (tile[3] - tile[1])
            box = clamp_box([gx0, gy0, gx1, gy1])
            # 贴着分块边界的检测很可能是被切开的，降权但保留
            border = min(abs(box[0] - tile[0]), abs(box[1] - tile[1]),
                         abs(box[2] - tile[2]), abs(box[3] - tile[3]))
            found.append({"label": str(item.get("label") or ""),
                          "caption": str(item.get("caption") or ""),
                          "bbox": box, "clipped": border < 6})
        return found

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for found in pool.map(scan, tiles):
            results.extend(found)

    save_cache(cache_dir, key, {"figures": results})
    return results


def match_tile_detection(label: str, detections: Sequence[dict]) -> dict | None:
    """在分块检测结果里找目标图号；优先不贴边的、面积大的。"""
    want = _norm_label(label)
    if not want:
        return None

    def rank(d: dict) -> tuple:
        # 不贴边的优先（贴边说明图被分块切开了），其次取面积大的
        area = (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1])
        return (0 if d.get("clipped") else 1, area)

    exact = [d for d in detections if _norm_label(d["label"]) == want]
    if not exact:
        # 允许「图11-1-14甲」与「图11-1-14」互相匹配
        base = re.sub(r"(甲|乙|丙|丁|戊|己)$", "", want)
        if base != want and len(base) >= 4:
            exact = [d for d in detections
                     if _norm_label(d["label"]).startswith(base) and _norm_label(d["label"]) != want]
    if not exact:
        return None
    return max(exact, key=rank)


# ============================================================================
# 7. 单页处理
# ============================================================================
@dataclass
class Figure:
    id: int
    label: str
    caption: str = ""
    bbox: list[int] = field(default_factory=list)
    kind: str = "diagram"
    description: str = ""
    placeholder: str = ""
    image_path: str = ""
    trace: list[dict] = field(default_factory=list)
    status: str = "seed"
    verify: dict = field(default_factory=dict)


@dataclass
class PageResult:
    page: int
    markdown: str
    figures: list[Figure]
    raw: dict = field(default_factory=dict)
    image_path: str = ""


def cache_key(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:16]


# 提示词/算法一改就该失效，避免旧缓存污染结果
CACHE_VERSION = "v7"


def load_cache(cache_dir: str | None, key: str) -> dict | None:
    if not cache_dir:
        return None
    path = os.path.join(cache_dir, key + ".json")
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return None
    return None


def save_cache(cache_dir: str | None, key: str, data: dict) -> None:
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, key + ".json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def analyse_page(client: VLMClient, page_img: "Image.Image", page_no: int, cache_dir: str | None,
                 use_cache: bool = True) -> dict:
    """调用多模态模型做整页版面理解（带磁盘缓存）。"""
    key = cache_key(CACHE_VERSION, "page", client.model, str(page_no),
                    hashlib.sha256(png_bytes(page_img)).hexdigest())
    if use_cache:
        hit = load_cache(cache_dir, key)
        if hit:
            log(f"第 {page_no} 页命中缓存", "ok")
            return hit

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            raw = client.chat(PAGE_PROMPT, [png_bytes(page_img)],
                              max_tokens=client.max_tokens if attempt == 0 else min(client.max_tokens * 2, 128000))
            data = parse_json(raw)
            save_cache(cache_dir, key, data)
            return data
        except Exception as exc:
            last_exc = exc
            log(f"第 {page_no} 页版面解析失败（{exc}）", "warn")
    raise RuntimeError(f"第 {page_no} 页版面解析失败: {last_exc}")


def analyse_page_text_only(client: VLMClient, page_img: "Image.Image", page_no: int,
                           cache_dir: str | None, use_cache: bool = True) -> dict:
    """降级路径：只要正文，不要插图坐标。用于整页多模态请求被拒的情况。"""
    key = cache_key(CACHE_VERSION, "page-text", client.model, str(page_no),
                    hashlib.sha256(png_bytes(page_img)).hexdigest())
    if use_cache:
        hit = load_cache(cache_dir, key)
        if hit:
            log(f"第 {page_no} 页纯文本模式命中缓存", "ok")
            return hit
    prompt = ("请把这页扫描文档的内容原样转写成 Markdown。数学公式用 LaTeX（行内 $...$，独立 $$...$$）。"
              "逐字还原，不要翻译或概括，不要输出插图位置占位符。只输出 Markdown 正文，不要任何解释。")
    raw = client.chat(prompt, [png_bytes(page_img)], max_tokens=client.max_tokens)
    data = {"markdown": raw.strip(), "figures": []}
    save_cache(cache_dir, key, data)
    return data


def build_figures(data: dict) -> list[Figure]:
    figs: list[Figure] = []
    for i, item in enumerate(data.get("figures") or [], start=1):
        if not isinstance(item, dict):
            continue
        try:
            bbox = clamp_box(item.get("bbox") or [0, 0, 0, 0])
        except Exception:
            continue
        if bbox[2] - bbox[0] < 4 or bbox[3] - bbox[1] < 4:
            log(f"忽略无效 bbox 的插图：{item.get('label')}", "warn")
            continue
        fid = int(item.get("id") or i)
        figs.append(Figure(
            id=fid,
            label=str(item.get("label") or f"图{fid}"),
            caption=str(item.get("caption") or item.get("label") or ""),
            bbox=bbox,
            kind=str(item.get("kind") or "diagram"),
            description=str(item.get("description") or ""),
            placeholder=str(item.get("placeholder") or f"[[FIG:{fid}]]"),
        ))
    return figs


def process_page(client: VLMClient, page_img_path: str, page_no: int, args, cache_dir: str | None,
                 images_dir: str, debug_dir: str) -> PageResult:
    page_img = Image.open(page_img_path).convert("RGB")

    try:
        data = analyse_page(client, page_img, page_no, cache_dir, use_cache=not args.no_cache)
    except RuntimeError as exc:
        # 模型拒绝整页请求（上下文超限 / 服务端限制）→ 降级成「只要正文、不要插图」再来一次
        log(f"第 {page_no} 页整页解析失败（{exc}），降级为纯文本模式重试", "warn")
        data = analyse_page_text_only(client, page_img, page_no, cache_dir, use_cache=not args.no_cache)
    figs = build_figures(data)
    markdown = str(data.get("markdown") or "")
    log(f"第 {page_no} 页：正文 {len(markdown)} 字，插图 {len(figs)} 张", "ok")

    locator = FigureLocator(client, page_img, page_no, iterations=args.refine,
                            verbose=not args.quiet)

    def fig_cache_key(fig: Figure, seed: Sequence[float]) -> str:
        return cache_key(CACHE_VERSION, "fig", client.model, fig.label, fig.caption,
                         ",".join(map(str, seed)), str(args.refine), str(args.trim),
                         str(args.verify))

    def polish(fig: Figure, seed: Sequence[float], tag: str = "") -> None:
        """精修 + 收紧，就地更新 fig.bbox / fig.trace。"""
        fig.bbox, trace = locator.refine(fig.label, fig.caption, seed)
        fig.trace.extend(trace)
        if not FigureLocator._plausible(fig.bbox, seed):
            fig.trace.append({"stage": "fallback-seed", "bbox": list(seed)})
            fig.bbox = list(seed)
        if args.trim:
            trimmed = trim_to_ink(page_img, fig.bbox)
            if trimmed != fig.bbox:
                fig.trace.append({"stage": "trim", "bbox": trimmed})
                fig.bbox = trimmed
        if tag:
            fig.trace.append({"stage": tag, "bbox": list(fig.bbox)})

    def prepare(fig: Figure) -> Figure:
        seed = list(fig.bbox)
        key = fig_cache_key(fig, seed)
        cached = None if args.no_cache else load_cache(cache_dir, key)
        if cached:
            fig.bbox = list(cached["bbox"])
            fig.trace = cached.get("trace", [])
            fig.status = cached.get("status", "cached")
            fig.verify = cached.get("verify", {})
            return fig
        polish(fig, seed)
        fig.status = "seed"
        if args.verify:
            fig.verify = verify_crop(client, page_img, fig.bbox, fig.label)
            fig.status = "verified" if verify_ok(fig.verify, fig.label) else "unverified"
            if not args.quiet:
                log(f"  校验 {fig.label}: contains_target={fig.verify.get('contains_target')} "
                    f"labels={fig.verify.get('labels')} → {fig.status}")
        save_cache(cache_dir, key, {"bbox": fig.bbox, "trace": fig.trace,
                                    "status": fig.status, "verify": fig.verify})
        return fig

    if figs:
        log(f"第 {page_no} 页：精修 {len(figs)} 张插图的坐标…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            figs = list(pool.map(prepare, figs))

    # 校验没通过的 → 用分块扫描整页找一次，再局部精修（这是从错误初值里爬出来的兜底路径）
    failed = [f for f in figs if args.verify and not verify_ok(f.verify, f.label)]
    if failed:
        log(f"第 {page_no} 页：{len(failed)} 张插图校验未通过，启动分块搜索兜底："
            f"{[f.label for f in failed]}", "warn")
        detections = tiled_figure_search(client, page_img, page_no, cache_dir, use_cache=not args.no_cache)
        log(f"  分块搜索得到 {len(detections)} 个候选")

        def recover(fig: Figure) -> Figure:
            match = match_tile_detection(fig.label, detections)
            if not match:
                log(f"  {fig.label} 在分块结果里没找到，保留原框（置信度低）", "warn")
                return fig
            fig.trace.append({"stage": "tile-recovery", "bbox": list(match["bbox"]),
                              "from_label": match.get("label"), "clipped": match.get("clipped")})
            polish(fig, match["bbox"], tag="after-tile-recovery")
            v = verify_crop(client, page_img, fig.bbox, fig.label)
            fig.verify = v
            fig.status = "recovered" if verify_ok(v, fig.label) else "unverified"
            log(f"  {fig.label} 兜底后 bbox={fig.bbox} status={fig.status}",
                "ok" if fig.status == "recovered" else "warn")
            save_cache(cache_dir, fig_cache_key(fig, match["bbox"]),
                       {"bbox": fig.bbox, "trace": fig.trace, "status": fig.status, "verify": fig.verify})
            return fig

        # 只对失败的图做兜底，别把已经通过的一起重跑
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            recovered = list(pool.map(recover, failed))
        by_label = {f.label: f for f in recovered}
        figs = [by_label.get(f.label, f) for f in figs]

    uncertain = [f.label for f in figs if f.status == "unverified"]
    if uncertain:
        log(f"第 {page_no} 页仍有存疑插图（已按最佳猜测裁图，建议人工核对）：{uncertain}", "warn")

    # 裁图
    for fig in figs:
        px = norm_to_px(fig.bbox, page_img.size)
        pad = int(round(args.pad * min(page_img.size)))
        px = (max(0, px[0] - pad), max(0, px[1] - pad),
              min(page_img.size[0], px[2] + pad), min(page_img.size[1], px[3] + pad))
        crop = page_img.crop(px)
        fname = f"p{page_no:03d}_{fig.id:02d}_{safe_name(fig.label)}.png"
        out = os.path.join(images_dir, fname)
        crop.save(out, "PNG", optimize=True)
        fig.image_path = os.path.join(os.path.basename(images_dir), fname)
        log(f"  裁出 {fname}  {crop.width}×{crop.height}px  bbox={fig.bbox}  [{fig.status}]")

    if args.debug:
        draw_debug(page_img, figs, os.path.join(debug_dir, f"page_{page_no:03d}_boxes.png"))

    return PageResult(page=page_no, markdown=markdown, figures=figs, raw=data, image_path=page_img_path)


def draw_debug(page_img: "Image.Image", figs: Sequence[Figure], out_path: str) -> None:
    im = page_img.copy()
    d = ImageDraw.Draw(im)
    colors = [(255, 0, 0), (0, 150, 0), (0, 0, 255), (255, 120, 0),
              (180, 0, 180), (0, 170, 170), (120, 80, 0), (255, 0, 120)]
    w, h = im.size
    for i, fig in enumerate(figs):
        x0, y0, x1, y1 = norm_to_px(fig.bbox, (w, h))
        c = colors[i % len(colors)]
        d.rectangle([x0, y0, x1, y1], outline=c, width=max(3, w // 250))
        d.text((x0 + 4, y0 + 3), fig.label, fill=c)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path)
    log(f"调试叠加图：{out_path}")


# ============================================================================
# 8. 组装 Markdown
# ============================================================================
FIG_TOKEN = re.compile(r"\[\[\s*FIG\s*[:_]?\s*(\d+)\s*\]\]", re.I)
# 连续的 "A. …" "B. …" 选项行：Markdown 里会被并成一整段，统一转成无序列表
OPTION_LINE = re.compile(r"^\s*([A-Ha-h])[.、．]\s+\S")


def normalize_markdown(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        if len(run) >= 2:
            out.append("")
            out.extend("- " + ln.strip() for ln in run)
            out.append("")
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if OPTION_LINE.match(line):
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    text = "\n".join(out)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


def assemble_markdown(pages: Sequence[PageResult], title: str | None = None) -> str:
    chunks: list[str] = []
    if title:
        chunks.append(f"# {title}\n")

    for pr in pages:
        body = pr.markdown.strip()
        used: set[int] = set()

        def repl(m: re.Match) -> str:
            fid = int(m.group(1))
            fig = next((f for f in pr.figures if f.id == fid), None)
            if fig is None:
                log(f"第 {pr.page} 页正文引用了不存在的 [[FIG:{fid}]]", "warn")
                return ""
            used.add(fig.id)
            alt = fig.caption or fig.label
            return f"\n\n![{alt}]({fig.image_path})\n\n*{alt}*\n\n"

        body = FIG_TOKEN.sub(repl, body)

        # 模型忘了放占位符的插图，补在页尾并提醒
        missing = [f for f in pr.figures if f.id not in used]
        if missing:
            log(f"第 {pr.page} 页有 {len(missing)} 张插图未被正文引用，已追加到页尾", "warn")
            for fig in missing:
                alt = fig.caption or fig.label
                body += f"\n\n![{alt}]({fig.image_path})\n\n*{alt}*\n\n"

        if len(pages) > 1:
            chunks.append(f"<!-- page {pr.page} -->\n\n{body}")
        else:
            chunks.append(body)

    text = "\n\n".join(c.strip() for c in chunks if c.strip())
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip() + "\n"
    return normalize_markdown(text)


# ============================================================================
# 9. 导出 Word
# ============================================================================
def _count_docx_images(docx_path: str) -> int:
    import zipfile
    try:
        with zipfile.ZipFile(docx_path) as z:
            return len([n for n in z.namelist() if n.startswith("word/media/")])
    except Exception:
        return 0


def _count_docx_equations(docx_path: str) -> int:
    import zipfile
    try:
        with zipfile.ZipFile(docx_path) as z:
            return z.read("word/document.xml").decode("utf-8").count("<m:oMath")
    except Exception:
        return 0


def _count_markdown_images(md_path: str) -> int:
    try:
        with open(md_path, encoding="utf-8") as fh:
            return len(re.findall(r"!\[[^\]]*\]\([^)]+\)", fh.read()))
    except Exception:
        return 0


def export_docx(md_path: str, docx_path: str, images_dir: str) -> bool:
    """优先 pandoc（LaTeX 公式 → Word 原生 OMML）；否则用 python-docx 兜底。"""
    md_path = os.path.abspath(md_path)
    docx_path = os.path.abspath(docx_path)
    images_dir = os.path.abspath(images_dir)
    expected = _count_markdown_images(md_path)

    if shutil.which("pandoc"):
        # 关键：pandoc 解析 markdown 里的相对图片路径时相对「当前工作目录」，
        # 而不是相对 md 文件所在目录。搞错了图片会被静默丢弃、只留一行 WARNING，
        # 最后拿到一个没有插图的 docx。所以这里传绝对 resource-path 并切到 md 目录。
        cmd = ["pandoc", os.path.basename(md_path), "-o", docx_path,
               "--resource-path", images_dir, "--from", "markdown+tex_math_dollars",
               "--toc", "--standalone"]
        log("pandoc: " + " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(md_path))
        if r.returncode == 0 and os.path.isfile(docx_path):
            embedded = _count_docx_images(docx_path)
            if expected and embedded < expected:
                log(f"pandoc 只嵌入了 {embedded}/{expected} 张插图，图片资源可能没找全", "warn")
            log(f"已生成 Word：{docx_path}"
                f"（原生公式 {_count_docx_equations(docx_path)} 个，插图 {embedded} 张）", "ok")
            return True
        log(f"pandoc 失败：{(r.stderr or r.stdout)[:400]}", "warn")

    try:
        import docx  # type: ignore
        from docx.shared import Inches  # type: ignore
    except ImportError:
        log("没有 pandoc，也没有 python-docx。安装其一即可导出 Word：\n"
            "   sudo apt install pandoc        # 推荐：公式转成 Word 原生公式\n"
            "   pip install python-docx        # 备选：公式保留为 LaTeX 文本", "error")
        return False

    log("使用 python-docx 兜底导出（公式以 LaTeX 文本保留，Word 里可用「公式→LaTeX」转换）", "warn")
    doc = docx.Document()
    base = os.path.dirname(os.path.abspath(md_path))
    with open(md_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            doc.add_heading(m.group(2).strip(), level=len(m.group(1)))
            continue
        img = re.match(r"^!\[[^\]]*\]\(([^)]+)\)$", stripped)
        if img:
            path = img.group(1)
            if not os.path.isabs(path):
                path = os.path.join(base, path)
            if os.path.isfile(path):
                try:
                    doc.add_picture(path, width=Inches(5.2))
                    continue
                except Exception as exc:
                    log(f"插图写入失败 {path}: {exc}", "warn")
        if stripped.startswith(("- ", "* ")):
            doc.add_paragraph(stripped[2:], style="List Bullet")
        else:
            doc.add_paragraph(stripped)
    doc.save(docx_path)
    log(f"已生成 Word：{docx_path}", "ok")
    return True


# ============================================================================
# 10. CLI
# ============================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="用多模态模型把（扫描）PDF 转成 Markdown / Word，并精确裁出插图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="供应商预设：" + ", ".join(PROVIDERS) + "\n"
               "环境变量：AI_BASE_URL / AI_API_KEY / AI_MODEL（以及各供应商的 *_API_KEY）")
    p.add_argument("pdf", help="输入 PDF 路径")
    p.add_argument("-o", "--out", default="output", help="输出目录（默认 output）")
    p.add_argument("--provider", default=os.environ.get("AI_PROVIDER", "deepseek"),
                   choices=sorted(PROVIDERS), help="API 供应商预设（默认 deepseek）")
    p.add_argument("--base-url", help="覆盖 API base url")
    p.add_argument("--api-key", help="覆盖 API Key")
    p.add_argument("--model", help="覆盖模型名")
    p.add_argument("--api", choices=["openai", "anthropic"], help="强制协议类型")

    g = p.add_argument_group("渲染")
    g.add_argument("--dpi", type=int, default=300, help="渲染分辨率，扫描件建议 300（默认）")
    g.add_argument("--pages", help="只处理指定页，如 1,3-5")

    g = p.add_argument_group("插图定位")
    g.add_argument("--refine", type=int, default=3,
                   help="模型闭环精修坐标的迭代上限，0=只用首次坐标（默认 3）")
    g.add_argument("--trim", dest="trim", action="store_true", default=True,
                   help="按墨迹投影收紧多余白边（默认开）")
    g.add_argument("--no-trim", dest="trim", action="store_false")
    g.add_argument("--pad", type=float, default=0.004,
                   help="裁图时四周额外留白，占页面短边比例（默认 0.004）")
    g.add_argument("--min-figure-px", type=int, default=64,
                   help="小于该像素的插图丢弃（默认 64）")
    g.add_argument("--workers", type=int, default=6, help="插图精修并发数（默认 6）")
    g.add_argument("--debug", action="store_true", help="输出带框图/迭代轨迹，便于人工校验")
    g.add_argument("--verify", dest="verify", action="store_true", default=True,
                   help="把裁好的图回喂模型校验，不过则用分块搜索兜底（默认开）")
    g.add_argument("--no-verify", dest="verify", action="store_false",
                   help="跳过校验，只跑精修（更快更省）")

    g = p.add_argument_group("接口与输出")
    g.add_argument("--max-tokens", type=int, default=32000, help="单次请求最大输出 token")
    g.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数")
    g.add_argument("--retries", type=int, default=3, help="失败重试次数")
    g.add_argument("--cache-dir", help="缓存目录（默认 <out>/.cache）")
    g.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新请求")
    g.add_argument("--title", help="Markdown 一级标题")
    g.add_argument("--docx", action="store_true", help="同时导出 Word")
    g.add_argument("--keep-pages", action="store_true", help="保留渲染出的整页 PNG")
    g.add_argument("-q", "--quiet", action="store_true")
    return p


def parse_pages(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    global QUIET
    QUIET = bool(args.quiet)

    if not os.path.isfile(args.pdf):
        raise SystemExit(f"找不到输入文件：{args.pdf}")

    cfg = resolve_config(args)
    if args.api:
        cfg["api"] = args.api
    client = VLMClient(cfg)

    out_dir = os.path.abspath(args.out)
    images_dir = os.path.join(out_dir, "images")
    debug_dir = os.path.join(out_dir, "debug")
    pages_dir = os.path.join(out_dir, "pages")
    cache_dir = args.cache_dir or os.path.join(out_dir, ".cache")
    for d in (out_dir, images_dir, pages_dir):
        os.makedirs(d, exist_ok=True)

    log(f"模型 {cfg['model']} @ {cfg['base_url']}（协议 {cfg['api']}）", "step")
    wanted = parse_pages(args.pages)

    log("① 渲染 PDF → PNG", "step")
    page_paths = render_pdf(args.pdf, pages_dir, args.dpi, wanted)

    log("② 版面理解 + 插图定位", "step")
    results: list[PageResult] = []
    for idx, path in enumerate(page_paths):
        page_no = wanted[idx] if wanted else idx + 1
        log(f"处理第 {page_no} 页 …", "step")
        try:
            results.append(process_page(client, path, page_no, args, cache_dir, images_dir, debug_dir))
        except Exception as exc:
            log(f"第 {page_no} 页失败：{exc}", "error")
            raise

    # 丢弃过小的"插图"
    for pr in results:
        size = Image.open(pr.image_path).size
        keep = []
        for fig in pr.figures:
            px = norm_to_px(fig.bbox, size)
            if (px[2] - px[0]) < args.min_figure_px or (px[3] - px[1]) < args.min_figure_px:
                log(f"丢弃过小的插图 {fig.label}（{px[2]-px[0]}×{px[3]-px[1]}px）", "warn")
                if fig.image_path and os.path.isfile(os.path.join(out_dir, fig.image_path)):
                    os.remove(os.path.join(out_dir, fig.image_path))
                continue
            keep.append(fig)
        pr.figures = keep

    log("③ 组装 Markdown", "step")
    title = args.title
    if title is None:
        title = os.path.splitext(os.path.basename(args.pdf))[0]
    md = assemble_markdown(results, title)
    md_path = os.path.join(out_dir, safe_name(os.path.splitext(os.path.basename(args.pdf))[0]) + ".md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    if args.debug:
        meta = {"pdf": os.path.abspath(args.pdf), "model": cfg["model"], "dpi": args.dpi,
                "pages": [{"page": pr.page,
                           "figures": [asdict(f) for f in pr.figures]} for pr in results]}
        with open(os.path.join(debug_dir, "figures.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

    if args.docx:
        log("④ 导出 Word", "step")
        export_docx(md_path, os.path.join(out_dir, os.path.splitext(os.path.basename(md_path))[0] + ".docx"),
                    images_dir)

    if not args.keep_pages:
        shutil.rmtree(pages_dir, ignore_errors=True)

    total_figs = sum(len(p.figures) for p in results)
    log(f"完成！{len(results)} 页 / {total_figs} 张插图 / API 调用 {client.usage['calls']} 次 "
        f"(输入 {client.usage['prompt_tokens']} tokens, 输出 {client.usage['completion_tokens']} tokens)", "ok")
    log(f"Markdown: {md_path}", "ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
