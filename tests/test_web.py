"""FastAPI 网页的端到端测试（使用 TestClient，不启动真实端口）。"""

from __future__ import annotations

import csv
import io
import threading
import time

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


# ---------------------------------------------------------------------------
# 结果页换范围复查
# ---------------------------------------------------------------------------


def _extract(resp, prefix):
    marker = prefix
    assert marker in resp.text, resp.text[:500]
    return resp.text.split(marker, 1)[1].split('"', 1)[0]


def _session_id(resp):
    return _extract(resp, '/recheck/')


def _download(client, resp):
    token = _extract(resp, '/download/')
    return client.get(f"/download/{token}")


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_recheck_matches_separate_upload(client):
    first = post(client, start="1000", end="2499")
    sid = _session_id(first)

    for start, end in [("1800", "2100"), ("1000", "1099"), ("0", "9999"), ("1000", "2499")]:
        rechecked = client.post(
            f"/recheck/{sid}",
            data={"query_start": start, "query_end": end},
        )
        standalone = post(client, start=start, end=end)
        assert rechecked.status_code == standalone.status_code == 200
        # 页面结果表与 CSV 均与“单独上传同一份表、同一范围”一致
        assert _result_table(rechecked.text) == _result_table(standalone.text)
        assert _csv_rows(_download(client, rechecked)) == _csv_rows(
            _download(client, standalone)
        )


def _result_table(html):
    import re as _re

    match = _re.search(r'<table class="result-table">(.*?)</table>', html, _re.S)
    assert match
    return " ".join(match.group(1).split())


def test_recheck_range_is_actually_applied(client):
    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    rechecked = client.post(
        f"/recheck/{sid}", data={"query_start": "1500", "query_end": "1849"}
    )
    assert rechecked.status_code == 200
    assert "旧号 1500 – 1849" in rechecked.text
    rows = _csv_rows(_download(client, rechecked))
    assert rows[1:] == [["1500", "1849", "350", "3100", "3449", "K2", "N2"]]


def test_each_download_link_keeps_its_own_result(client):
    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    first_rows = _csv_rows(_download(client, first))

    second = client.post(
        f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1099"}
    )
    second_rows = _csv_rows(_download(client, second))
    assert second_rows[1:] == [["1000", "1099", "100", "2000", "2099", "K1", "N1"]]

    # 两次结果互不覆盖，各链接仍指向各自范围
    assert first_rows == _csv_rows(_download(client, first))
    assert first_rows != second_rows


def test_recheck_reuses_session_without_reupload(client, monkeypatch):
    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    from app import analysis

    def boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("复查不应重新解析上传文件")

    monkeypatch.setattr(analysis, "parse_table_bytes", boom)
    monkeypatch.setattr("app.main.parse_table_bytes", boom)
    resp = client.post(
        f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1499"}
    )
    assert resp.status_code == 200


def test_recheck_unknown_or_expired_session_asks_reupload(client):
    resp = client.post(
        "/recheck/no-such-session", data={"query_start": "0", "query_end": "10"}
    )
    assert resp.status_code == 404
    assert "重新上传" in resp.text or "重新核查" in resp.text


def test_recheck_invalid_range_shows_reason_and_allows_retry(client):
    first = post(client, start="1000", end="2499")
    sid = _session_id(first)

    bad = client.post(f"/recheck/{sid}", data={"query_start": "abc", "query_end": "10"})
    assert bad.status_code == 400
    assert "整数" in bad.text
    assert f'/recheck/{sid}' in bad.text  # 表单仍可再次提交

    inverted = client.post(
        f"/recheck/{sid}", data={"query_start": "2000", "query_end": "1000"}
    )
    assert inverted.status_code == 400
    assert "不能大于" in inverted.text

    # 同一会话修正后即可继续
    ok = client.post(
        f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1099"}
    )
    assert ok.status_code == 200


def test_session_expiry_reclaims_recheck_data(monkeypatch):
    from app import main as main_module

    store = ResultStore(ttl_seconds=60)
    monkeypatch.setattr(main_module, "store", store)
    with TestClient(app) as c:
        first = post(c, start="1000", end="2499")
        sid = _session_id(first)
        token = first.text.split("/download/", 1)[1].split('"', 1)[0]
        assert c.get(f"/download/{token}").status_code == 200

        # 会话过期：复查入口与下载链接一并失效
        store._ttl = -1
        expired = c.post(
            f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1099"}
        )
        assert expired.status_code == 404
        dl = c.get(f"/download/{token}")
        assert dl.status_code == 404
        assert "失效" in dl.text


def test_recheck_failure_shows_reason_and_retry_succeeds(client):
    from app import main as main_module

    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    session = main_module.store.get_session(sid).session

    real = session.boundary_index
    failing = {"on": True}

    class SometimesFailing:
        def boundaries_for(self, seg):
            if failing["on"]:
                raise RuntimeError("模拟磁盘索引损坏")
            return real.boundaries_for(seg)

    session.boundary_index = SometimesFailing()
    session._ready.clear()

    failed = client.post(
        f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1499"}
    )
    assert failed.status_code == 400
    assert "模拟磁盘索引损坏" in failed.text
    assert "K1" in failed.text
    assert f'/recheck/{sid}' in failed.text  # 可直接再次提交

    failing["on"] = False
    ok = client.post(
        f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1099"}
    )
    assert ok.status_code == 200
    assert _csv_rows(_download(client, ok))[1:] == [
        ["1000", "1099", "100", "2000", "2099", "K1", "N1"]
    ]


def test_concurrent_recheck_failure_shares_one_conclusion(client):
    from app import main as main_module

    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    session = main_module.store.get_session(sid).session
    calls = []

    class AlwaysFailing:
        def boundaries_for(self, seg):
            calls.append(seg.segment_no)
            time.sleep(0.15)
            raise RuntimeError("并发准备失败")

    session.boundary_index = AlwaysFailing()
    session._ready.clear()

    barrier = threading.Barrier(5)
    responses = []

    def worker():
        barrier.wait()
        responses.append(
            client.post(
                f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1499"}
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(responses) == 5
    assert all(r.status_code == 400 for r in responses)
    assert len({r.text for r in responses}) == 1  # 同一成败结论与同一原因
    assert calls.count("K1") == 1  # 只准备一次


def test_concurrent_recheck_first_hit_prepares_once(client):
    """并发复查同一旧段：边界只准备一次，所有结果一致。"""
    from app import main as main_module

    first = post(client, start="1000", end="2499")
    sid = _session_id(first)
    session = main_module.store.get_session(sid).session

    counting_calls = []
    real = session.boundary_index

    class Counting:
        def boundaries_for(self, seg):
            counting_calls.append(seg.segment_no)
            return real.boundaries_for(seg)

    session.boundary_index = Counting()
    session._ready.clear()
    session._prepared_count = 0

    results = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        resp = client.post(
            f"/recheck/{sid}", data={"query_start": "1000", "query_end": "1499"}
        )
        results.append((resp.status_code, _result_table(resp.text)))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(code == 200 for code, _ in results)
    assert len({table for _, table in results}) == 1
    # K1 首次命中只准备一次
    assert counting_calls.count("K1") == 1
    assert session.prepared_count == 1

