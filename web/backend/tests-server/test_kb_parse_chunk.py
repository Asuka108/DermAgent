# -*- coding: utf-8 -*-
"""KB 解析/清洗/切分单测：诚实失败、去噪计数、overlap 与 anchor 稳定。"""
import importlib.machinery
import http.server
import io
import json
import os
import sys
import threading
import types
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb import chunk, clean, parse  # noqa: E402


# —— 清洗 ——

def test_clean_drops_headers_pages_toc_and_refs():
    raw = "\n".join([
        "# 第一章 皮肤结构", "", "皮肤由表皮、真皮构成。", "",
        "临床皮肤病学", "12", "临床皮肤病学", "13", "临床皮肤病学",
        "一、概述 ........ 12", "第二章 湿疹", "", "湿疹表现为多形性皮损。",
        "参考文献", "[1] 某作者. 某论文. 2020.", "[2] 另一作者. 另一论文. 2021.",
        "# 第三章 感染", "", "感染性皮肤病需抗感染治疗。",
    ])
    out = clean.clean_markdown(raw)
    text = out["text"]
    assert "临床皮肤病学" not in text
    assert "........" not in text
    assert "某作者" not in text
    assert "第三章 感染" in text and "湿疹表现为多形性皮损" in text
    assert out["log"]["dropped_repeated_lines"] == 3
    assert out["log"]["dropped_page_numbers"] == 2
    assert out["log"]["dropped_toc_lines"] == 1
    assert out["log"]["dropped_reference_chars"] > 0


def test_clean_fixes_hyphen_and_images_keeps_tables():
    raw = ("# 图\n\nlong-\nterm therapy ![x](a.png) end\n\n"
           "| 药名 | 剂量 |\n| --- | --- |\n| 甲 | 1 |\n")
    out = clean.clean_markdown(raw)
    assert "longterm" in out["text"]
    assert "a.png" not in out["text"]
    assert "| 药名 |" in out["text"]
    assert out["log"]["dropped_images"] == 1
    assert out["log"]["fixed_hyphenations"] == 1


def test_clean_empty_input_reports_warning():
    out = clean.clean_markdown(" \n\n ### \n")
    assert out["text"] == ""
    assert out["log"]["warning"] == "empty_after_clean"


# —— 切分 ——

def test_chunk_sections_pages_and_ids():
    md = ("# 总论\n\n<!-- page: 2 -->\n皮肤屏障由角质层构成。\n\n"
          "## 湿疹\n\n<!-- pagebreak -->\n湿疹常见于屈侧。\n\n"
          "| 药 | 量 |\n| --- | --- |\n| 甲 | 1 |\n")
    chunks = chunk.chunk_markdown(md, "bk00000001", title="皮肤科学")
    assert all(c["chunk_id"].startswith("bk00000001-") for c in chunks)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    paths = [c["section_path"] for c in chunks]
    assert "总论 > 湿疹" in paths
    assert any(c["page"] == 2 for c in chunks)
    assert any(c["page"] == 3 for c in chunks)
    table = [c for c in chunks if c["is_table"]]
    assert table and "| 药 |" in table[0]["content"]
    assert table[0]["section_path"] == "总论 > 湿疹"
    assert all(c["book_title"] == "皮肤科学" for c in chunks)


def test_chunk_overlap_within_section_and_not_across():
    para1 = "甲" * 300
    para2 = "乙" * 300
    md = "# 一节\n\n" + para1 + "\n\n" + para2 + "\n\n# 二节\n\n" + "丙" * 300
    chunks = chunk.chunk_markdown(md, "bk1", overlap=60, target=450)
    first, second = chunks[0], chunks[1]
    assert first["content"].endswith("甲" * 60)
    assert second["content"].startswith("甲" * 60)
    assert len(second["content"]) <= 600
    assert not any(c["content"].startswith("丙" * 60) for c in chunks)
    assert all(len(c["content"]) <= 600 for c in chunks)


def test_chunk_anchor_stable_across_rechunk():
    md = "# 一节\n\n" + ("皮肤" * 150)
    a = chunk.chunk_markdown(md, "bk1")
    b = chunk.chunk_markdown("# 前置\n\n无关内容。\n\n" + md, "bk9")
    anchors_a = {c["anchor_hash"] for c in a}
    anchors_b = {c["anchor_hash"] for c in b}
    assert anchors_a <= anchors_b
    assert a[0]["chunk_id"] != b[-1]["chunk_id"]


def test_chunk_long_paragraph_split_under_max():
    md = "# 一节\n\n" + "皮肤" * 800
    chunks = chunk.chunk_markdown(md, "bk1")
    assert len(chunks) > 1
    assert all(len(c["content"]) <= 600 for c in chunks)


def test_chunk_long_table_repeats_header():
    rows = "\n".join(f"| 药{i} | {i} |" for i in range(120))
    md = "# 表\n\n| 药 | 量 |\n| --- | --- |\n" + rows
    chunks = chunk.chunk_markdown(md, "bk1")
    tables = [c for c in chunks if c["is_table"]]
    assert len(tables) > 1
    assert all(c["content"].startswith("| 药 | 量 |") for c in tables)
    assert all(len(c["content"]) <= 600 for c in tables)


def test_chunk_sentence_mode_packs_on_sentence_boundaries():
    body = "".join(f"第{i}条：皮肤瘙痒需保湿。" for i in range(1, 61))
    chunks = chunk.chunk_markdown("# 护理\n\n" + body, "bk1", mode="sentence",
                                  target=120, max_chars=200, overlap=0)
    assert len(chunks) > 1
    assert all(len(c["content"]) <= 200 for c in chunks)
    assert all(c["content"].rstrip().endswith("。") for c in chunks)
    joined = "".join(c["content"] for c in chunks)
    assert all(f"第{i}条" in joined for i in range(1, 61))


def test_chunk_sentence_mode_keeps_heading_break_and_meta():
    md = ("# 总论\n\n<!-- page: 2 -->\n皮肤屏障完整。湿疹多见。\n\n屈侧红斑。\n\n"
          "## 湿疹\n\n屈侧红斑。")
    chunks = chunk.chunk_markdown(md, "bk1", mode="sentence",
                                  target=30, max_chars=80, overlap=0)
    assert "总论 > 湿疹" in [c["section_path"] for c in chunks]
    body = [c for c in chunks if "皮肤屏障完整" in c["content"]][0]
    assert "\n\n" in body["content"]
    assert "屈侧红斑。" in body["content"]
    assert body["page"] == 2 and body["section_path"] == "总论"


def test_chunk_sentence_mode_hard_slices_overlong_sentence():
    long_sentence = "无标点" * 300
    chunks = chunk.chunk_markdown(long_sentence, "bk1", mode="sentence",
                                  target=100, max_chars=150, overlap=0)
    assert len(chunks) > 1
    assert all(len(c["content"]) <= 150 for c in chunks)
    assert "".join(c["content"] for c in chunks) == long_sentence


def test_chunk_sentence_mode_overlap_and_table_atomic():
    md = ("# 一节\n\n" + "甲" * 200 + "。\n\n" + "乙" * 200 + "。\n\n"
          "| 药 | 量 |\n| --- | --- |\n| 甲 | 1 |\n")
    chunks = chunk.chunk_markdown(md, "bk1", mode="sentence",
                                  target=120, max_chars=300, overlap=30)
    text_chunks = [c for c in chunks if not c["is_table"]]
    assert len(text_chunks) >= 2
    yi = [c for c in text_chunks if "乙" in c["content"]][0]
    prev = text_chunks[text_chunks.index(yi) - 1]
    assert yi["content"].startswith(prev["content"][-30:])
    tables = [c for c in chunks if c["is_table"]]
    assert tables and "| 药 |" in tables[0]["content"]


def test_chunk_paragraph_mode_does_not_merge():
    md = "# 一节\n\n短段一。\n\n短段二。\n\n短段三。"
    chunks = chunk.chunk_markdown(md, "bk1", mode="paragraph", overlap=0)
    assert [c["content"] for c in chunks] == ["一节", "短段一。", "短段二。", "短段三。"]


def test_chunk_book_mode_crosses_sections():
    md = ("# 一节\n\n" + "甲" * 200 + "。\n\n# 二节\n\n" + "乙" * 200 + "。")
    section = chunk.chunk_markdown(md, "bk1", mode="section",
                                   target=300, max_chars=600, overlap=0)
    book = chunk.chunk_markdown(md, "bk1", mode="book",
                                target=300, max_chars=600, overlap=0)
    assert len(section) == 2
    assert len(book) == 1
    assert book[0]["section_path"] == "一节"
    assert "甲" in book[0]["content"] and "乙" in book[0]["content"]


def test_chunk_invalid_mode_and_overlap_rejected():
    md = "# 节\n\n正文。"
    for kwargs in ({"mode": "bogus"}, {"overlap": -1}, {"overlap": 301},
                   {"overlap": 450}):
        with pytest.raises(ValueError):
            chunk.chunk_markdown(md, "bk1", **kwargs)


def test_chunk_overlap_shrinks_to_fit_hard_max():
    md = "甲" * 560 + "。\n\n" + "乙" * 560 + "。"
    chunks = chunk.chunk_markdown(md, "bk1", mode="sentence",
                                  target=450, max_chars=600, overlap=100)
    assert len(chunks) == 2
    room = 600 - len(chunks[0]["content"]) - 1
    assert 0 < room < 100
    assert len(chunks[1]["content"]) == 600
    assert chunks[1]["content"].startswith(chunks[0]["content"][-room:])
    assert chunks[1]["content"].endswith("乙" * 560 + "。")


# —— MinerU 适配 ——

class _Result:
    def markdown(self):
        return "# 标题\n\n正文内容。"

    def to_json(self):
        return {"version": "4.1", "pages": 1}


def _inject_mineru(monkeypatch, parse_fn=None, fallback_cls=None):
    mod = types.ModuleType("mineru")
    mod.__spec__ = importlib.machinery.ModuleSpec("mineru", loader=None)
    if parse_fn is not None:
        parser = types.ModuleType("mineru.parser")
        parser.__spec__ = importlib.machinery.ModuleSpec("mineru.parser", loader=None)
        parser.parse = parse_fn
        mod.parser = parser
        monkeypatch.setitem(sys.modules, "mineru.parser", parser)
    if fallback_cls is not None:
        mod.MinerU = fallback_cls
    monkeypatch.setitem(sys.modules, "mineru", mod)
    parse.reset()


def test_parse_missing_mineru_is_honest(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mineru", None)
    parse.reset()
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        parse.parse_document(str(path))
    except parse.ParseError as exc:
        assert exc.code == "mineru_unavailable"
    else:
        raise AssertionError("expected ParseError")
    finally:
        parse.reset()


def test_parse_direct_txt(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("皮肤瘙痒三天。", encoding="utf-8")
    out = parse.parse_document(str(path))
    assert out["engine"] == "direct" and "瘙痒" in out["markdown"]


def test_parse_unsupported_and_missing(tmp_path):
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(str(tmp_path / "a.xyz"))
    assert exc.value.code == "format_unsupported"
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(str(tmp_path / "missing.pdf"))
    assert exc.value.code == "corrupt_file"


def test_parse_mineru4_success_and_kwargs(monkeypatch, tmp_path):
    seen = {}

    def fake_parse(path, **kwargs):
        seen.update(kwargs)
        return _Result()

    _inject_mineru(monkeypatch, parse_fn=fake_parse)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        out = parse.parse_document(str(path))
        assert out["engine"] == "mineru"
        assert "正文内容" in out["markdown"]
        assert seen == {"tier": "standard", "ocr_mode": "auto", "page_range": "all"}
    finally:
        parse.reset()


def test_parse_mineru4_typeerror_falls_back(monkeypatch, tmp_path):
    calls = []

    def fake_parse(path, **kwargs):
        calls.append(kwargs)
        if kwargs:
            raise TypeError("unexpected keyword")
        return _Result()

    _inject_mineru(monkeypatch, parse_fn=fake_parse)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        out = parse.parse_document(str(path))
        assert out["markdown"]
        assert calls == [{"tier": "standard", "ocr_mode": "auto", "page_range": "all"}, {}]
    finally:
        parse.reset()


def test_parse_mineru_empty_text(monkeypatch, tmp_path):
    class Empty:
        def markdown(self):
            return ""

        def to_json(self):
            return {}

    _inject_mineru(monkeypatch, parse_fn=lambda path, **kw: Empty())
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        with pytest.raises(parse.ParseError) as exc:
            parse.parse_document(str(path))
        assert exc.value.code == "empty_text"
    finally:
        parse.reset()


def test_parse_mineru_bad_api(monkeypatch, tmp_path):
    _inject_mineru(monkeypatch, parse_fn=None)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        with pytest.raises(parse.ParseError) as exc:
            parse.parse_document(str(path))
        assert exc.value.code == "mineru_unavailable"
    finally:
        parse.reset()


# —— MinerU 云端 Precision API（假网络，无真实调用） ——

def _zip_bytes(md="# 云端标题\n\n正文。", content_list=None, images=None, with_md=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_md:
            zf.writestr("result/full.md", md)
        zf.writestr("result/content_list.json",
                    json.dumps(content_list if content_list is not None else [{"type": "text"}]))
        for name, blob in (images or {"images/p1.jpg": b"\xff\xd8\xff"}).items():
            zf.writestr("result/" + name, blob)
    return buf.getvalue()


class _FakeCloudTransport:
    def __init__(self, states=("running", "done"), code=0, err="", zip_bytes=None):
        self.states = list(states)
        self.code = code
        self.err = err
        self.zip_bytes = zip_bytes if zip_bytes is not None else _zip_bytes()
        self.calls = []
        self.polls = 0

    def request_json(self, method, url, payload=None, token="", timeout=60):
        self.calls.append({"method": method, "url": url, "payload": payload, "token": token})
        if self.code != 0:
            return {"code": self.code, "msg": self.err or "bad"}
        if url.endswith("/file-urls/batch"):
            return {"code": 0, "data": {"batch_id": "b1",
                                        "file_urls": ["https://upload.example/put"]}}
        if "/extract-results/batch/" in url:
            state = self.states[min(self.polls, len(self.states) - 1)]
            self.polls += 1
            row = {"state": state, "err_msg": self.err}
            if state == "done":
                row["full_zip_url"] = "https://zip.example/out.zip"
            return {"code": 0, "data": {"extract_result": [row]}}
        raise AssertionError(f"unexpected url {url}")

    def put_bytes(self, url, path, timeout=600):
        self.calls.append({"method": "PUT", "url": url,
                           "size": os.path.getsize(path), "path": path})

    def download(self, url, dest, timeout=900):
        self.calls.append({"method": "DOWNLOAD", "url": url})
        with open(dest, "wb") as fh:
            fh.write(self.zip_bytes)


def _cloud_parser(transport, **kw):
    return parse.CloudParser("tok-123", "https://mineru.net/api/v4", poll=0,
                             timeout=kw.pop("timeout", 5), transport=transport, **kw)


def test_cloud_parse_uploads_polls_and_unpacks(tmp_path):
    transport = _FakeCloudTransport()
    parser = _cloud_parser(transport)
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    out = parser.parse(str(path), tier="standard", ocr_mode="auto", page_range="all")
    assert out["engine"] == "mineru_cloud" and out["version"] == "v4"
    assert "云端标题" in out["markdown"]
    assert out["raw_json"]["content_list"] == [{"type": "text"}]
    assert out["images"]["images/p1.jpg"].startswith(b"\xff\xd8")
    create = transport.calls[0]
    assert create["method"] == "POST" and create["token"] == "tok-123"
    assert create["payload"]["model_version"] == "vlm"
    assert create["payload"]["language"] == "ch"
    assert create["payload"]["is_ocr"] is True
    assert create["payload"]["enable_table"] is True
    assert "page_ranges" not in create["payload"]
    assert any(c["method"] == "PUT" and c["url"] == "https://upload.example/put"
               for c in transport.calls)
    assert transport.polls == 2


def test_cloud_page_range_and_ocr_off(tmp_path):
    transport = _FakeCloudTransport(states=("done",))
    parser = _cloud_parser(transport)
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    parser.parse(str(path), tier="standard", ocr_mode="off", page_range="1-20")
    payload = transport.calls[0]["payload"]
    assert payload["page_ranges"] == "1-20" and payload["is_ocr"] is False


def test_cloud_api_error_is_honest(tmp_path):
    parser = _cloud_parser(_FakeCloudTransport(code=-1, err="quota"))
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(parse.ParseError) as exc:
        parser.parse(str(path), tier="standard", ocr_mode="auto", page_range="all")
    assert exc.value.code == "mineru_api_error" and "quota" in exc.value.detail


def test_cloud_state_failed_is_honest(tmp_path):
    parser = _cloud_parser(_FakeCloudTransport(states=("failed",), err="加密文件"))
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(parse.ParseError) as exc:
        parser.parse(str(path), tier="standard", ocr_mode="auto", page_range="all")
    assert exc.value.code == "mineru_parse_failed" and "加密" in exc.value.detail


def test_cloud_timeout_is_retryable(tmp_path):
    parser = _cloud_parser(_FakeCloudTransport(states=("running",)), timeout=1)
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(parse.ParseError) as exc:
        parser.parse(str(path), tier="standard", ocr_mode="auto", page_range="all")
    assert exc.value.code == "mineru_timeout"


def test_cloud_zip_without_markdown_is_empty_text(tmp_path):
    parser = _cloud_parser(_FakeCloudTransport(
        states=("done",), zip_bytes=_zip_bytes(with_md=False)))
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(parse.ParseError) as exc:
        parser.parse(str(path), tier="standard", ocr_mode="auto", page_range="all")
    assert exc.value.code == "empty_text"


def test_parse_document_routes_cloud_when_mode_cloud(monkeypatch, tmp_path):
    transport = _FakeCloudTransport(states=("done",))
    monkeypatch.setenv("DERM_KB_MINERU_MODE", "cloud")
    monkeypatch.setenv("DERM_MINERU_TOKEN", "tok-123")
    monkeypatch.setattr(parse, "_new_cloud", lambda token: _cloud_parser(transport))
    parse.reset()
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        out = parse.parse_document(str(path))
        assert out["engine"] == "mineru_cloud"
        assert parse.available()["mode"] == "cloud" and parse.available()["ok"] is True
    finally:
        parse.reset()


def test_parse_document_cloud_without_token_is_honest(monkeypatch, tmp_path):
    monkeypatch.setenv("DERM_KB_MINERU_MODE", "cloud")
    monkeypatch.delenv("DERM_MINERU_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_TOKEN", raising=False)
    parse.reset()
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4")
    try:
        assert parse.available()["ok"] is False
        with pytest.raises(parse.ParseError) as exc:
            parse.parse_document(str(path))
        assert exc.value.code == "mineru_unavailable" and "token_missing" in exc.value.detail
    finally:
        parse.reset()


def test_http_transport_put_sends_no_default_content_type(tmp_path):
    """OSS 预签名 URL 不含 Content-Type，携带默认头会 SignatureDoesNotMatch。"""
    captured = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or 0)
            captured["headers"] = dict(self.headers)
            captured["body"] = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4 signed")
    try:
        parse._HttpTransport().put_bytes(
            f"http://127.0.0.1:{server.server_port}/upload?sign=abc",
            str(path), timeout=5)
    finally:
        worker.join(5)
        server.server_close()
    assert captured["body"] == b"%PDF-1.4 signed"
    assert "Content-Type" not in captured["headers"]
    assert captured["headers"].get("Host")
