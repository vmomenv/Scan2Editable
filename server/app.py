#!/usr/bin/env python3
"""Scan2Editable 本地 Web 后端。

用标准库实现，不引入额外依赖：
  - 上传 PDF
  - 创建转换任务（排队、单并发执行 pdf2md_ai.py）
  - 轮询任务状态 / 日志 / 进度
  - 取消任务、下载 Markdown / Word
  - 生产模式下直接托管 web/dist

运行：
    python3 server/app.py            # http://127.0.0.1:8765
    python3 server/app.py --port 9000
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "pdf2md_ai.py"
WEB_DIST = ROOT / "web" / "dist"
DATA = ROOT / "output" / "web"
UPLOADS = DATA / "uploads"
JOBS_DIR = DATA / "jobs"

for d in (UPLOADS, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
MAX_LOGS = 3000
MAX_UPLOAD = 400 * 1024 * 1024
LOG_RE = re.compile(r"^\[pdf2md\]\s+(.)\s?(.*)$")
MARKS = {"·": "info", "!": "warn", "×": "error", "✓": "ok", "▶": "step"}
PAGE_RE = re.compile(r"处理第\s*(\d+)\s*页")


def now_ms() -> int:
    return int(time.time() * 1000)


def snapshot(job: dict) -> dict:
    with LOCK:
        logs = list(job["logs"])
        jobdir = JOBS_DIR / job["id"]
        outputs = {"md": None, "docx": None}
        for suffix in ("docx", "md"):
            files = sorted(jobdir.glob(f"*.{suffix}")) if jobdir.is_dir() else []
            if files:
                f = files[0]
                outputs[suffix] = {
                    "name": f.name,
                    "size": f.stat().st_size,
                    "url": f"/api/jobs/{job['id']}/download?kind={suffix}",
                }
        return {
            "id": job["id"],
            "name": job["name"],
            "status": job["status"],
            "progress": job["progress"],
            "currentPage": job["currentPage"],
            "totalPages": job["totalPages"],
            "createdAt": job["createdAt"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "error": job["error"],
            "logs": logs,
            "outputs": outputs,
            "options": job["options"],
        }


def append_log(job: dict, level: str, text: str) -> None:
    with LOCK:
        job["logs"].append({"t": now_ms(), "level": level, "text": text})
        if len(job["logs"]) > MAX_LOGS:
            del job["logs"][: len(job["logs"]) - MAX_LOGS]
        m = PAGE_RE.search(text)
        if m and job["totalPages"]:
            page = int(m.group(1))
            job["currentPage"] = page
            job["progress"] = min(0.97, page / job["totalPages"])


def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    jobdir = JOBS_DIR / job_id
    pdf = jobdir / "source.pdf"
    opts = job["options"]

    with LOCK:
        job["status"] = "running"
        job["startedAt"] = now_ms()
    append_log(job, "step", f"开始处理：{job['name']}")

    cmd = [
        sys.executable,
        str(SCRIPT),
        str(pdf),
        "-o",
        str(jobdir),
        "--provider",
        str(opts.get("provider") or "deepseek"),
        "--dpi",
        str(int(opts.get("dpi") or 300)),
        "--refine",
        str(int(opts.get("refine", 3))),
        "--workers",
        str(int(opts.get("workers") or 6)),
    ]
    if opts.get("pages"):
        cmd += ["--pages", str(opts["pages"])]
    if opts.get("title"):
        cmd += ["--title", str(opts["title"])]
    cmd += ["--trim"] if opts.get("trim", True) else ["--no-trim"]
    cmd += ["--verify"] if opts.get("verify", True) else ["--no-verify"]
    if opts.get("debug"):
        cmd.append("--debug")
    if opts.get("keepPages"):
        cmd.append("--keep-pages")
    if opts.get("noCache"):
        cmd.append("--no-cache")
    if opts.get("exportDocx", True):
        cmd.append("--docx")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if opts.get("apiKey"):
        env["AI_API_KEY"] = str(opts["apiKey"])
    if opts.get("baseUrl"):
        env["AI_BASE_URL"] = str(opts["baseUrl"])
    if opts.get("model"):
        env["AI_MODEL"] = str(opts["model"])

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with LOCK:
        job["proc"] = proc

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        m = LOG_RE.match(line)
        if m:
            level = MARKS.get(m.group(1), "info")
            text = m.group(2)
        else:
            level = "error" if ("Traceback" in line or "Error" in line) else "info"
            text = line
        append_log(job, level, text)

    code = proc.wait()
    with LOCK:
        job["proc"] = None
        job["finishedAt"] = now_ms()
        job["returncode"] = code

    if job.get("canceled"):
        with LOCK:
            job["status"] = "canceled"
            job["error"] = "已取消"
        append_log(job, "warn", "任务已取消")
        return

    if code == 0:
        with LOCK:
            job["status"] = "success"
            job["progress"] = 1.0
        append_log(job, "ok", "转换完成")
    else:
        with LOCK:
            job["status"] = "error"
            job["error"] = f"进程退出码 {code}"
        append_log(job, "error", f"转换失败，进程退出码 {code}")


def worker() -> None:
    while True:
        job_id = TASK_QUEUE.get()
        try:
            if JOBS.get(job_id, {}).get("canceled"):
                continue
            run_job(job_id)
        except Exception as exc:  # noqa: BLE001
            job = JOBS.get(job_id)
            if job is not None:
                with LOCK:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["finishedAt"] = now_ms()
                append_log(job, "error", f"后端异常：{exc}")
        finally:
            TASK_QUEUE.task_done()


threading.Thread(target=worker, daemon=True, name="converter").start()


class Handler(BaseHTTPRequestHandler):
    server_version = "Scan2Editable/1.0"

    # ---------- helpers ----------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_body(self, limit: int = MAX_UPLOAD) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._error(400, "请求体为空")
            return None
        if length > limit:
            self._error(413, "文件过大")
            return None
        return self.rfile.read(length)

    def _read_json(self) -> dict | None:
        raw = self._read_body(2 * 1024 * 1024)
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "JSON 解析失败")
            return None
        if not isinstance(data, dict):
            self._error(400, "JSON 必须是对象")
            return None
        return data

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[web] %s\n" % (fmt % args))

    # ---------- routes ----------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/health":
            self._json({"ok": True, "script": SCRIPT.is_file(), "version": 1})
            return

        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                self._error(404, "任务不存在")
                return
            self._json(snapshot(job))
            return

        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/download", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                self._error(404, "任务不存在")
                return
            kind = (query.get("kind") or ["docx"])[0]
            if kind not in ("docx", "md"):
                self._error(400, "只支持 docx / md")
                return
            files = sorted((JOBS_DIR / job["id"]).glob(f"*.{kind}"))
            if not files:
                self._error(404, "文件还没生成")
                return
            self._send_file(files[0], download=True)
            return

        if path.startswith("/api/"):
            self._error(404, "接口不存在")
            return

        # 静态资源：web/dist（生产模式）
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/upload":
            self._handle_upload(query)
            return

        if path == "/api/jobs":
            self._handle_create_job()
            return

        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path)
        if m:
            self._handle_cancel(m.group(1))
            return

        self._error(404, "接口不存在")

    # ---------- handlers ----------
    def _handle_upload(self, query: dict) -> None:
        name = (query.get("name") or ["document.pdf"])[0]
        name = os.path.basename(name) or "document.pdf"
        body = self._read_body()
        if body is None:
            return
        if not body.startswith(b"%PDF"):
            self._error(400, "这不是一个有效的 PDF 文件")
            return
        file_id = uuid.uuid4().hex[:12]
        folder = UPLOADS / file_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "source.pdf"
        target.write_bytes(body)
        (folder / "meta.json").write_text(
            json.dumps({"name": name, "size": len(body)}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._json({"fileId": file_id, "name": name, "size": len(body)})

    def _handle_create_job(self) -> None:
        data = self._read_json()
        if data is None:
            return
        file_id = str(data.get("fileId") or "")
        src = UPLOADS / file_id / "source.pdf"
        if not file_id or not src.is_file():
            self._error(404, "上传的文件不存在，请重新选择 PDF")
            return

        meta_path = UPLOADS / file_id / "meta.json"
        try:
            name = json.loads(meta_path.read_text(encoding="utf-8")).get("name") or "document.pdf"
        except Exception:  # noqa: BLE001
            name = "document.pdf"

        options = data.get("options") or {}
        if not isinstance(options, dict):
            self._error(400, "options 必须是对象")
            return

        job_id = uuid.uuid4().hex[:12]
        jobdir = JOBS_DIR / job_id
        jobdir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, jobdir / "source.pdf")

        job = {
            "id": job_id,
            "name": name,
            "status": "queued",
            "progress": 0.0,
            "currentPage": 0,
            "totalPages": max(0, int(data.get("totalPages") or 0)),
            "createdAt": now_ms(),
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "logs": [],
            "options": options,
            "proc": None,
            "canceled": False,
            "returncode": None,
        }
        with LOCK:
            JOBS[job_id] = job
        TASK_QUEUE.put(job_id)
        self._json({"jobId": job_id}, 201)

    def _handle_cancel(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self._error(404, "任务不存在")
            return
        with LOCK:
            job["canceled"] = True
            proc = job.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                proc.terminate()
        elif job["status"] == "queued":
            with LOCK:
                job["status"] = "canceled"
                job["error"] = "已取消"
        self._json({"ok": True})

    # ---------- static ----------
    def _serve_static(self, path: str) -> None:
        if not WEB_DIST.is_dir():
            self._json(
                {
                    "error": "前端还没构建",
                    "hint": "cd web && npm install && npm run build  或开发模式运行 npm run dev",
                },
                503,
            )
            return
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIST / rel).resolve()
        try:
            target.relative_to(WEB_DIST.resolve())
        except ValueError:
            self._error(403, "非法路径")
            return
        if not target.is_file():
            target = WEB_DIST / "index.html"
        self._send_file(target)

    def _send_file(self, target: Path, download: bool = False) -> None:
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{_quote(target.name)}",
            )
        self.end_headers()
        with open(target, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name)


def main() -> int:
    port = 8765
    host = "127.0.0.1"
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Scan2Editable server → http://{host}:{port}")
    print(f"  前端目录：{WEB_DIST}")
    if not WEB_DIST.is_dir():
        print("  ! 尚未构建前端，开发时请另开终端运行：cd web && npm run dev")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
