#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf2md_ai.py 的离线自测：不联网、不调用任何 AI 接口，只验证纯函数与本地裁图链路。

    python3 test_pdf2md_ai.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

import pdf2md_ai as M


class TestCoords(unittest.TestCase):
    def test_clamp_box_sorts_and_clamps(self):
        self.assertEqual(M.clamp_box([900, 1500, -20, 100]), [0, 100, 900, 1000])
        self.assertEqual(M.clamp_box([300, 400, 100, 200]), [100, 200, 300, 400])

    def test_norm_px_roundtrip(self):
        size = (1000, 500)
        px = M.norm_to_px([100, 200, 600, 800], size)
        self.assertEqual(px, (100, 100, 600, 400))
        self.assertEqual(M.px_to_norm(px, size), [100, 200, 600, 800])

    def test_expand_box(self):
        self.assertEqual(M.expand_box([100, 100, 200, 200], 0.5), [50, 50, 250, 250])
        # 不能越界
        self.assertEqual(M.expand_box([10, 10, 20, 20], 1.0)[0], 0)

    def test_parse_pages(self):
        self.assertEqual(M.parse_pages("1,3-5"), [1, 3, 4, 5])
        self.assertIsNone(M.parse_pages(None))


class TestJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(M.parse_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(M.parse_json('```json\n{"a": [1,2]}\n```'), {"a": [1, 2]})

    def test_prose_wrapped(self):
        self.assertEqual(M.parse_json('好的，结果是：\n{"a": 1}\n以上。'), {"a": 1})

    def test_trailing_comma(self):
        self.assertEqual(M.parse_json('{"a": [1,2,],}'), {"a": [1, 2]})

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            M.parse_json("没有 JSON")


class TestTiles(unittest.TestCase):
    def test_split_tiles_covers_page(self):
        tiles = M.split_tiles(2, 3, 0.12)
        self.assertEqual(len(tiles), 6)
        self.assertTrue(all(0 <= t[0] < t[2] <= 1000 for t in tiles))
        # 全部小块的并集应覆盖整页
        self.assertLessEqual(min(t[0] for t in tiles), 0)
        self.assertGreaterEqual(max(t[2] for t in tiles), 1000)

    def test_norm_label(self):
        self.assertEqual(M._norm_label("图 11-1-7"), M._norm_label("图11-1-7"))

    def test_match_prefers_unclipped(self):
        dets = [
            {"label": "图11-1-7", "bbox": [0, 0, 100, 100], "clipped": True},
            {"label": "图11-1-7", "bbox": [10, 20, 40, 60], "clipped": False},
        ]
        self.assertEqual(M.match_tile_detection("图11-1-7", dets)["bbox"], [10, 20, 40, 60])

    def test_match_none(self):
        self.assertIsNone(M.match_tile_detection("图11-9-9", []))

    def test_match_subfigure_by_prefix(self):
        dets = [{"label": "图11-1-14", "bbox": [500, 800, 900, 950], "clipped": False}]
        self.assertIsNotNone(M.match_tile_detection("图11-1-14甲", dets))


class TestGuards(unittest.TestCase):
    def test_reject_degenerate(self):
        self.assertFalse(M.FigureLocator._plausible([100, 100, 110, 300], [50, 50, 250, 250]))

    def test_accept_legit_shrink(self):
        # 初值框过大（文字段落），模型收到真实图形上 —— 必须放行
        self.assertTrue(M.FigureLocator._plausible([585, 398, 657, 450], [530, 300, 670, 415]))

    def test_reject_blowup(self):
        self.assertFalse(M.FigureLocator._plausible([0, 0, 900, 900], [400, 400, 500, 500]))

    def test_grow_side(self):
        self.assertEqual(M.FigureLocator._grow_side([100, 100, 200, 200], "left", 0.5)[0], 50)
        self.assertEqual(M.FigureLocator._grow_side([100, 100, 200, 200], "bottom", 0.5)[3], 250)

    def test_verify_ok(self):
        self.assertTrue(M.verify_ok({"contains_target": True}, "图11-1-7"))
        # 目标图号出现在 labels 里 → 视为通过（模型只是嫌图题被切了）
        self.assertTrue(M.verify_ok({"contains_target": False, "labels": ["图 11-1-7"]}, "图11-1-7"))
        self.assertFalse(M.verify_ok({"contains_target": False, "labels": ["正文"]}, "图11-1-7"))
        self.assertFalse(M.verify_ok({}, "图11-1-7"))


class TestTrim(unittest.TestCase):
    def test_trim_removes_white_border(self):
        im = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(im).rectangle([300, 300, 600, 600], fill="black")
        out = M.trim_to_ink(im, [200, 200, 700, 700])
        self.assertLess(out[0], 320)
        self.assertGreater(out[2], 580)
        self.assertLess(out[2] - out[0], 420)

    def test_trim_keeps_box_when_empty(self):
        im = Image.new("RGB", (400, 400), "white")
        self.assertEqual(M.trim_to_ink(im, [100, 100, 300, 300]), [100, 100, 300, 300])


class TestMarkdown(unittest.TestCase):
    def test_options_become_list(self):
        md = "题干。\nA. 甲\nB. 乙\nC. 丙\nD. 丁\n下一段。"
        out = M.normalize_markdown(md)
        self.assertIn("- A. 甲", out)
        self.assertIn("- D. 丁", out)
        self.assertIn("下一段。", out)

    def test_single_option_not_listed(self):
        self.assertNotIn("- A. 甲", M.normalize_markdown("A. 甲"))

    def _page(self, figs):
        return M.PageResult(page=1, markdown="前文 [[FIG:1]] 后文 [[FIG:2]] 结束。", figures=figs)

    def test_assemble_replaces_placeholders(self):
        figs = [M.Figure(id=1, label="图1", caption="图题一", image_path="images/a.png"),
                M.Figure(id=2, label="图2", caption="图题二", image_path="images/b.png")]
        md = M.assemble_markdown([self._page(figs)], None)
        self.assertIn("![图题一](images/a.png)", md)
        self.assertIn("![图题二](images/b.png)", md)
        self.assertNotIn("[[FIG:", md)

    def test_assemble_appends_unreferenced_figure(self):
        figs = [M.Figure(id=1, label="图1", caption="图题一", image_path="images/a.png"),
                M.Figure(id=9, label="图9", caption="图题九", image_path="images/z.png")]
        md = M.assemble_markdown([self._page(figs)], None)
        self.assertIn("![图题九](images/z.png)", md)

    def test_assemble_drops_unknown_placeholder(self):
        figs = [M.Figure(id=1, label="图1", caption="图题一", image_path="images/a.png"),
                M.Figure(id=2, label="图2", caption="图题二", image_path="images/b.png")]
        md = M.assemble_markdown([self._page(figs)], "标题")
        self.assertTrue(md.startswith("# 标题"))
        self.assertNotIn("[[FIG:", md)


class TestFigurePipelineOffline(unittest.TestCase):
    """不调用模型，直接验证「bbox → 裁图 → 写文件」这一段。"""

    def test_crop_and_record(self):
        tmp = tempfile.mkdtemp()
        try:
            page = Image.new("RGB", (1000, 1400), "white")
            ImageDraw.Draw(page).rectangle([100, 200, 400, 500], fill="black")
            path = os.path.join(tmp, "page_0001.png")
            page.save(path)

            fig = M.Figure(id=1, label="图11-1-7", caption="图11-1-7", bbox=[80, 130, 420, 380])
            px = M.norm_to_px(fig.bbox, page.size)
            crop = page.crop(px)
            out = os.path.join(tmp, "p001_01_图11-1-7.png")
            crop.save(out)
            self.assertTrue(os.path.isfile(out))
            self.assertGreater(crop.width, 100)
            # 裁出来的图必须含有那块黑色矩形
            self.assertIsNotNone(M.image_ink_bbox(crop))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDocxExport(unittest.TestCase):
    """Word 导出回归测试。

    这里锁住的是一个真实踩过的坑：pandoc 解析 markdown 里的相对图片路径时是相对
    「当前工作目录」的，不是相对 md 文件所在目录。早期实现直接把 md 路径传给 pandoc，
    结果图片被静默丢弃，导出一个只有文字没有插图的 docx。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.images = os.path.join(self.tmp, "images")
        os.makedirs(self.images, exist_ok=True)
        Image.new("RGB", (60, 40), "black").save(os.path.join(self.images, "a.png"))
        Image.new("RGB", (60, 40), "gray").save(os.path.join(self.images, "b.png"))
        self.md = os.path.join(self.tmp, "doc.md")
        with open(self.md, "w", encoding="utf-8") as fh:
            fh.write("# 标题\n\n$a^2+b^2=c^2$\n\n"
                     "![图1](images/a.png)\n\n![图2](images/b.png)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_counts(self):
        self.assertEqual(M._count_markdown_images(self.md), 2)

    @unittest.skipUnless(shutil.which("pandoc"), "未安装 pandoc，跳过")
    def test_pandoc_embeds_images_and_formulas(self):
        out = os.path.join(self.tmp, "doc.docx")
        # 故意从一个「不相关的工作目录」调用，确保实现不依赖 CWD
        old = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            ok = M.export_docx(self.md, out, self.images)
        finally:
            os.chdir(old)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(out))
        # 插图必须全部嵌入，公式必须是 Word 原生 OMML
        self.assertEqual(M._count_docx_images(out), 2)
        self.assertGreaterEqual(M._count_docx_equations(out), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
