"""FastAPI 网页入口。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .analysis import (
    STATUS_LABELS,
    InputError,
    analyse_migration,
    parse_table_bytes,
    result_to_csv,
    table_warnings,
    validate_table,
)
from .store import ResultStore

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

app = FastAPI(title="铁路桩号换版核查", version=__version__)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = ResultStore()


@app.exception_handler(413)
async def payload_too_large(request: Request, exc) -> PlainTextResponse:
    return PlainTextResponse("上传内容超过限制（20 MB），请拆分后重试。", status_code=413)


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """在解析 multipart 之前就拒绝过大的请求体。"""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_UPLOAD_BYTES * 2 + 64 * 1024:
        return PlainTextResponse(
            "上传内容超过限制（每份文件 20 MB），请拆分后重试。", status_code=413
        )
    return await call_next(request)


def _safe_filename(name: str | None, fallback: str) -> str:
    raw = (name or "").strip() or fallback
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\r\n\t]", " ", raw)
    return raw[:120]


def _parse_query_number(value: str, field_label: str) -> int:
    text = (value or "").strip().replace(",", "").replace("，", "")
    if not text:
        raise InputError(f"请填写{field_label}。")
    if not re.fullmatch(r"[+-]?\d+", text):
        raise InputError(f"{field_label}必须是整数米（当前为 {value!r}）。")
    return int(text)


async def _read_upload(upload: UploadFile, label: str) -> tuple[bytes, str]:
    filename = _safe_filename(upload.filename, f"{label}.csv")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise InputError(f"{label}文件 {filename} 超过 20 MB 限制。")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise InputError(f"请上传{label}文件（{filename} 是空文件）。")
    return data, filename


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": __version__,
            "status_labels": STATUS_LABELS,
        },
    )


@app.post("/analyse", response_class=HTMLResponse)
async def analyse(
    request: Request,
    old_file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入写法
    new_file: UploadFile = File(...),  # noqa: B008
    query_start: str = Form(...),
    query_end: str = Form(...),
):
    form_values = {
        "query_start": query_start,
        "query_end": query_end,
    }
    try:
        old_bytes, old_name = await _read_upload(old_file, "旧版桩号表")
        new_bytes, new_name = await _read_upload(new_file, "新版桩号表")
        start = _parse_query_number(query_start, "旧号起")
        end = _parse_query_number(query_end, "旧号止")

        def run_analysis():
            old_table = parse_table_bytes(old_bytes, old_name, label="旧版桩号表")
            new_table = parse_table_bytes(new_bytes, new_name, label="新版桩号表")
            for table in (old_table, new_table):
                errors = validate_table(table)
                if errors:
                    raise InputError("数据校验未通过：\n" + "\n".join(errors))
            return old_table, new_table, analyse_migration(old_table, new_table, start, end)

        # 解析与核查是 CPU 密集型，放到线程池，避免阻塞事件循环。
        old_table, new_table, result = await run_in_threadpool(run_analysis)
    except InputError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "status_labels": STATUS_LABELS,
                "error": str(exc),
                "form_values": form_values,
                "old_filename": _safe_filename(old_file.filename, ""),
                "new_filename": _safe_filename(new_file.filename, ""),
            },
            status_code=400,
        )

    csv_text = result_to_csv(result)
    stamp = f"{old_name.rsplit('.', 1)[0]}_to_{new_name.rsplit('.', 1)[0]}"
    stamp = re.sub(r"[^\w一-鿿.-]+", "_", stamp)[:80] or "migration"
    download_name = f"{stamp}_{start}-{end}.csv"
    stored = store.put(
        csv_text=csv_text,
        filename=download_name,
        summary={
            "query_start": start,
            "query_end": end,
            "migratable_rows": len(result.migratable_rows),
        },
    )

    warnings = table_warnings(old_table) + table_warnings(new_table)
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "version": __version__,
            "status_labels": STATUS_LABELS,
            "result": result,
            "rows": [r.as_dict() for r in result.rows],
            "summary": result.summary_rows(),
            "warnings": warnings,
            "old_name": old_name,
            "new_name": new_name,
            "old_segment_count": len(old_table),
            "new_segment_count": len(new_table),
            "download_token": stored.token,
            "download_name": stored.filename,
            "migratable_interval_count": len(result.migratable_rows),
            "migratable_total": sum(r.count for r in result.migratable_rows),
            "total_queried": result.total_queried,
        },
    )


@app.get("/download/{token}")
async def download(request: Request, token: str):
    item = store.get(token)
    if item is None:
        return templates.TemplateResponse(
            request,
            "expired.html",
            {"version": __version__},
            status_code=404,
        )
    filename = item.filename
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "migration.csv"
    disposition = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=item.csv_text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@app.get("/samples/{name}")
async def sample(name: str):
    allowed = {
        "old_v1.csv": "旧版桩号表示例.csv",
        "new_v1.csv": "新版桩号表示例.csv",
    }
    filename = allowed.get(name)
    if filename is None:
        return PlainTextResponse("not found", status_code=404)
    path = BASE_DIR.parent / "samples" / name
    if not path.exists():
        return PlainTextResponse("sample missing", status_code=404)
    # 示例表带有“缺号/重号/未覆盖”等故意设置的问题，便于演示。
    return Response(
        content=path.read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse(url="/static/favicon.svg", status_code=307)
