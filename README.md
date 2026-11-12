# Scan2Editable — 扫描版 PDF → Markdown / Word（多模态 AI）

把扫描 / 拍照得到的 PDF（没有文字层的那种）还原成**可编辑**的 Markdown 或 Word：
正文文字、题号、选项、**公式（LaTeX）**全部保留，并用多模态模型的**图片坐标**把插图精确裁出来，
插回正文中它原本出现的位置。

> 纯 Python 脚本，核心依赖只有 `Pillow` + `requests`。

---

## 1. 它是怎么工作的

```
scan.pdf
   │  ① 渲染：每页 → 300dpi PNG（PyMuPDF 或 pdftoppm）
   ▼
page_0001.png
   │  ② 版面理解：整页图交给多模态模型
   │      → markdown 正文（公式 LaTeX，插图处写 [[FIG:n]]）
   │      → figures[]：图号 / 图题 / 归一化 bbox(0-1000)
   ▼
   │  ③ 坐标精修（闭环纠偏）
   │      裁出局部 → 让模型在小图里重新框选 → 反馈 complete / only_target / missing_side
   │      → 迭代收敛；模型说某侧被切断就朝那一侧扩窗重框
   │      → 最后按墨迹投影收紧白边
   ▼
   │  ④ 校验 + 兜底
   │      把裁好的图回喂模型：「这里确实是图11-1-7 吗？」
   │      不通过 → 整页切成带重叠的小块重扫一遍，找到目标图号后重新精修
   ▼
   │  ⑤ 裁图：按最终 bbox 从高分辨率页面裁出 PNG（images/）
   ▼
output/scan.md  +  images/p001_01_图11-1-7.png ...
   │  ⑥ 可选：--docx 导出 Word（LaTeX 公式 → Word 原生公式 OMML）
   ▼
output/scan.docx
```

为什么需要 ③④ 两步：单次让模型在整页 A4 上直接回归 bbox，误差经常有页面宽度的
5%～15%（实测会整体偏移、甚至框到隔壁的文字段落）。局部裁剪 + 模型自评 + 回喂校验
能把误差压到接近像素级。

---

## 2. 安装

```bash
pip install -r requirements.txt          # Pillow + requests

# 可选，但强烈建议：
pip install PyMuPDF                      # 渲染更快，不装就用 poppler 的 pdftoppm
sudo apt install poppler-utils           # 提供 pdftoppm（Debian/Ubuntu）
sudo apt install tesseract-ocr tesseract-ocr-chi-sim   # 自动检测并转正被旋转的扫描页

# 导出 Word 二选一：
sudo apt install pandoc                  # 推荐：公式变成 Word 原生公式(OMML)，可编辑
pip install python-docx                  # 备选：公式以 LaTeX 文本保留
```

> 装不了系统包也可以：pandoc 有单文件绿色版，从
> <https://github.com/jgm/pandoc/releases> 下载 `pandoc-*-linux-amd64.tar.gz`，
> 解压后把 `bin/pandoc` 放进 `~/.local/bin` 即可，脚本按 `PATH` 自动发现。

## 3. 快速开始

```bash
export DEEPSEEK_API_KEY=sk-xxxxxx
python3 pdf2md_ai.py scan.pdf -o output
```

输出：

```
output/
├── scan.md                  # 正文 + 插图（[[FIG:n]] 已替换成图片）
├── images/                  # 裁出来的插图
│   ├── p001_01_图11-1-7.png
│   └── ...
├── debug/                   # --debug 时才有
│   ├── page_001_boxes.png   # 整页 + 所有 bbox 叠加，一眼就能看出框歪没歪
│   └── figures.json         # 每张图的 bbox / 校验结果 / 迭代轨迹
└── .cache/                  # 接口响应缓存，重复跑不重复花钱
```

跑挂了不要紧：`.cache/` 会记住每一次成功的模型响应，重跑只补没跑完的部分。

跑一页大概 3~5 分钟（10 张插图、几十次调用）。想快速验证脚本能不能跑通：

```bash
python3 pdf2md_ai.py scan.pdf -o output --pages 1 --refine 1 --no-verify
```

离线自测（不联网、不调接口）：

```bash
python3 test_pdf2md_ai.py
```

导出 Word：

```bash
python3 pdf2md_ai.py scan.pdf -o output --docx
```

只想快速看效果、不在意插图坐标：

```bash
python3 pdf2md_ai.py scan.pdf -o output --refine 0 --no-trim --no-verify
```

### 3.1 Web 可视化操作台

仓库自带一个本地 Web 界面：左侧逐页预览 PDF（滚轮向下把当前页向左丢出、向上翻回），
右侧配置导出参数，点一下就能跑完并下载 `.docx`。

```bash
# 首次构建前端（需要 Node 18+）
cd web && npm install && npm run build && cd ..

# 启动：http://127.0.0.1:8765
python3 server/app.py
```

开发前端时开两个终端，Vite 会把 `/api` 代理到 8765：

```bash
python3 server/app.py                 # 终端 1
cd web && npm run dev                 # 终端 2 → http://127.0.0.1:5173
```

界面里的配置会原样映射成 `pdf2md_ai.py` 的命令行参数（DPI、页码范围、精修轮次、
校验/去白边、调试框图、并发数等）；API Key 通过环境变量传给子进程，不会出现在命令行里。
上传的 PDF 与输出都写在 `output/web/` 下，任务日志和进度实时回显在右侧面板。

---

## 4. 模型 / 供应商

脚本内置常见视觉模型预设，用 `--provider` 选，用 `--model` 覆盖：

| `--provider` | 默认模型 | 需要的环境变量 |
|---|---|---|
| `deepseek`（默认） | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| `openai` | `gpt-4o` | `OPENAI_API_KEY` |
| `dashscope` | `qwen-vl-max-latest` | `DASHSCOPE_API_KEY` |
| `zhipu` | `glm-4v-plus` | `ZHIPUAI_API_KEY` |
| `moonshot` | `moonshot-v1-8k-vision-preview` | `MOONSHOT_API_KEY` |
| `gemini` | `gemini-2.0-flash` | `GEMINI_API_KEY` |
| `anthropic` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` |
| `openai-compatible` | 自定义 | `AI_API_KEY` |

任意 OpenAI 兼容端点也可以直接指：

```bash
python3 pdf2md_ai.py scan.pdf -o output \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-VL-7B-Instruct \
  --api-key EMPTY
```

> 本机上如果配置了 DSH 客户端，脚本会兜底读取 `~/.dsh/.credentials.yaml` 里的
> `DEEPSEEK_API_KEY`，所以 DeepSeek 场景通常不需要手动 export。

## 5. 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --out` | 输出目录（默认 `output`） |
| `--dpi` | 渲染分辨率，扫描件建议 300（默认） |
| `--pages 1,3-5` | 只处理指定页 |
| `--refine N` | 坐标精修迭代上限，默认 3；`0` = 只用首轮坐标 |
| `--no-trim` | 关掉按墨迹收紧白边 |
| `--no-verify` | 关掉回喂校验 + 分块兜底（更快更省 token） |
| `--pad` | 裁图额外留白，占页面短边比例（默认 0.004） |
| `--min-figure-px` | 丢弃小于该像素的"插图"（默认 64） |
| `--workers` | 插图精修并发数（默认 6） |
| `--debug` | 输出带框图 + 迭代轨迹 JSON |
| `--docx` | 同时导出 Word |
| `--no-cache` | 忽略缓存强制重新请求 |
| `--keep-pages` | 保留渲染出的整页 PNG |
| `-q, --quiet` | 只打警告 |

---

## 6. 精度与调参

* **先开 `--debug` 跑一页**，打开 `debug/page_001_boxes.png` 看框准不准，这是最快的验收方式。
* 框整体偏移 / 框到文字上 → 提高 `--refine`（3~5），并确认 `--verify` 是开着的。
* 图被切掉边缘 → 把 `--pad` 调到 `0.01`，或关掉 `--no-trim`（默认就是开的，别加它）。
* 整页文字识别错乱 → 提高 `--dpi` 到 400；超长页面/超多插图可以调大 `--max-tokens`。
* 只想省钱 → `--refine 0 --no-trim --no-verify`，body 文本依然准，只是插图框是首轮粗框。
* 模型输出被截断（日志会提示）→ 调大 `--max-tokens`。

## 7. 实测效果（仓库里的 `scan.pdf`）

`scan.pdf` 是一页被**旋转 180°** 的扫描件（`Page rot: 180`，无文字层，纯 CCITT 图像）。
用默认参数跑：

```bash
python3 pdf2md_ai.py scan.pdf -o output --debug --docx
```

结果：

* 正文约 **1150 字**，题号 8–14、选项 A–D、填空下划线全部还原，公式转成 LaTeX
  （`$F_1$`、`$l_1$`、`$AOC$`、`$M$` …）；
* **插图全部裁出**（图 11-1-7 ~ 图 11-1-14，含甲/乙/丙子图），每张都带自己的图题，
  并通过回喂校验；个别图会先判否、再由分块兜底救回来（日志里标 `recovered`）；
* 多栏阅读顺序正确（先左栏再右栏），插图插回了它原本出现的位置；
* `--docx` 产出的 Word 里公式是 **Word 原生公式**（OMML，可直接编辑），插图全部内嵌；
* 整页约 40~50 次接口调用、3 万上下输入 token。

> 模型存在随机性：同一页两次运行，个别插图的判定路径会不同（有时直接 verified，
> 有时走一次 recovered），最终框位置基本一致。这也是为什么要开 `--verify`。

验收方式：打开 `output/debug/page_001_boxes.png`，一眼就能看出每个 bbox 贴不贴。

## 8. 开发中踩到并已修掉的两个坑

记录下来，因为它们都会**静默出错**、不报异常：

1. **pandoc 的相对图片路径是按「当前工作目录」解析的**，不是按 md 文件所在目录。
   早期实现直接把 md 路径交给 pandoc，图片会被悄悄丢弃、只留一行 `[WARNING]`，
   最后得到一个有公式却没有插图的 docx。现在传绝对 `--resource-path` 并切到 md 目录，
   导出后还会清点内嵌图片数 ×公式数并打日志（`test_pdf2md_ai.py::TestDocxExport` 锁住了这个行为）。

2. **页面被旋转 180° 时，单个汉字看着仍是正的**，只有连起来读才知道是倒的。
   模型看着倒排文字回归坐标会整体偏移 5%~15%。所以在渲染阶段就用 tesseract 的
   `chi_sim_vert` 逐带投票判定方向并自动转正。

## 9. 已知边界

* 表格会被还原成 Markdown 表格，但复杂的合并单元格只做近似还原。
* 手写体、严重倾斜 / 透印的扫描件，识别率取决于模型本身。
* 插图裁图精度受模型坐标能力限制；脚本用"精修 + 校验 + 分块兜底"尽可能逼近像素级，
  但仍建议对 `figures.json` 里 `status != verified` 的插图人工复核。
* 页面方向检测依赖 `tesseract`；没装的话，遇到被旋转过的 PDF 坐标会整体偏移
  （日志里会给出警告）。
* 单张图内部如果还有更细的子图（如"图丙"里再分左右），不会自动拆得更细——
  可在 `figures.json` 里手动改 bbox 后重跑；主体裁图会被缓存复用，
  改一处不会重新请求整页。
