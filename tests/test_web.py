"""FastAPI 网页的端到端测试（使用 TestClient，不启动真实端口）。"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import ResultStore


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


OLD_CSV = """段号,里程起点,里程终点,起点桩号
K1,0,500,1000
K2,500,900,1500
K3,900,1000,1850
K4,1200,1600,2100
"""

NEW_CSV = """段号,里程起点,里程终点,起点桩号
N1,0,400,2000
N2,400,850,3000
N3,850,950,5000
N4,1000,1050,2300
N5,1200,1600,6000
"""


def post(client, old=OLD_CSV, new=NEW_CSV, start="1000", end="2499"):
    return client.post(
        "/analyse",
        data={"query_start": start, "query_end": end},
        files={
            "old_file": ("old.csv", old.encode("utf-8"), "text/csv"),
            "new_file": ("new.csv", new.encode("utf-8"), "text/csv"),
        },
        follow_redirects=True,
    )


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "铁路桩号换版核查" in resp.text
    assert 'name="old_file"' in resp.text


def test_health(client):
    assert client.get("/healthz").text == "ok"


def test_analyse_and_download(client):
    resp = post(client)
    assert resp.status_code == 200
    assert "可迁移" in resp.text
    # 从页面中拿到下载链接
    marker = '/download/'
    assert marker in resp.text
    token = resp.text.split(marker, 1)[1].split('"', 1)[0]

    dl = client.get(f"/download/{token}")
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    text = dl.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["旧号起", "旧号止", "旧号个数", "新号起", "新号止", "旧段号", "新段号"]
    # 只含可迁移区间；重号（旧/新）、缺号、新表未覆盖均不导出
    assert rows[1:] == [
        ["1000", "1299", "300", "2000", "2299", "K1", "N1"],
        ["1350", "1399", "50", "2350", "2399", "K1", "N1"],
        ["1400", "1499", "100", "3000", "3099", "K1", "N2"],
        ["1500", "1849", "350", "3100", "3449", "K2", "N2"],
        ["2100", "2499", "400", "6000", "6399", "K4", "N5"],
    ]


def test_download_unknown_token(client):
    resp = client.get("/download/does-not-exist")
    assert resp.status_code == 404
    assert "失效" in resp.text


def test_analyse_reports_errors(client):
    resp = post(client, start="abc")
    assert resp.status_code == 400
    assert "整数" in resp.text

    resp = post(client, start="2000", end="1000")
    assert resp.status_code == 400
    assert "不能大于" in resp.text

    resp = post(client, old="段号,里程起点\nK1,0\n")
    assert resp.status_code == 400
    assert "里程终点" in resp.text


def test_result_lists_uncovered_and_missing(client):
    resp = post(client, start="1800", end="2100")
    assert resp.status_code == 200
    assert "新表未覆盖" in resp.text
    assert "缺号" in resp.text
    assert "N3" in resp.text and "K4" in resp.text


def test_duplicate_statuses_reported(client):
    resp = post(client, start="1000", end="2499")
    assert resp.status_code == 200
    assert "重号（新表）" in resp.text
    assert "重号（旧表）" in resp.text
    assert "K1" in resp.text and "K3" in resp.text and "N4" in resp.text


def test_xlsx_upload(client):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["段号", "里程起点", "里程终点", "起点桩号"])
    ws.append(["K1", 0, 500, 1000])
    buf = io.BytesIO()
    wb.save(buf)
    resp = client.post(
        "/analyse",
        data={"query_start": "1000", "query_end": "1499"},
        files={
            "old_file": (
                "old.xlsx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            "new_file": (
                "new.xlsx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "可迁移旧号" in resp.text


def test_big_range_response_is_fast(client):
    old = "段号,里程起点,里程终点,起点桩号\nK1,0,100000000,0\n"
    new = "段号,里程起点,里程终点,起点桩号\nN1,0,100000000,5\n"
    import time

    started = time.perf_counter()
    resp = post(client, old=old, new=new, start="0", end="99999999")
    elapsed = time.perf_counter() - started
    assert resp.status_code == 200
    assert elapsed < 5, f"长区间网页核查过慢：{elapsed:.2f}s"
    # 1 亿个旧号也只输出少量区间，页面不会逐米展开
    assert len(resp.text) < 500_000


def test_empty_upload_rejected(client):
    resp = client.post(
        "/analyse",
        data={"query_start": "0", "query_end": "10"},
        files={
            "old_file": ("old.csv", b"", "text/csv"),
            "new_file": ("new.csv", NEW_CSV.encode("utf-8"), "text/csv"),
        },
    )
    assert resp.status_code == 400
    assert "空文件" in resp.text


def test_store_expiry():
    store = ResultStore(ttl_seconds=-1)
    item = store.put("a,b\r\n", "x.csv", {})
    assert store.get(item.token) is None
    store2 = ResultStore(ttl_seconds=60)
    item2 = store2.put("a,b\r\n", "x.csv", {})
    assert store2.get(item2.token) is item2
