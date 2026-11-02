# -*- coding: utf-8 -*-
"""MinerU 4.x 解析适配：懒加载 + 能力探测，缺包/接口不符如实失败（不伪造文本）。

三方格式（pdf/docx/html/epub/pptx/xlsx）走 MinerU；txt/md 直接读取，
省掉一次模型调用，也避免纯文本被 OCR 误伤。
"""
import http.client
import importlib.metadata as metadata
import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import config

_TXT_ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")
_sdk_lock = threading.Lock()
_sdk = None
_sdk_error = ""
_cloud_lock = threading.Lock()
_cloud = None
_IMAGE_BYTES_MAX = 64 * 1024 * 1024


class ParseError(Exception):
    """code 直接作为 kb_books.error_code 落库。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = str(code)[:48]
        self.detail = str(detail or "")[:300]


def _cloud_selected() -> bool:
    mode = config.mineru_mode()
    return mode == "cloud" or (mode == "auto" and bool(config.mineru_token()))


def available() -> dict:
    """只做安装/凭据探测，不 import 重型依赖（health 接口可用）。"""
    if _cloud_selected():
        if not config.mineru_token():
            return {"ok": False, "version": "", "mode": "cloud", "state": "token_missing",
                    "detail": "token_missing（设置 DERM_MINERU_TOKEN）"}
        return {"ok": True, "version": "cloud-v4", "mode": "cloud", "state": "ready",
                "detail": f"mineru cloud {config.mineru_base_url()}"}
    if importlib.util.find_spec("mineru") is None:
        return {"ok": False, "version": "", "mode": "local", "state": "not_installed",
                "detail": "not_installed"}
    try:
        version = metadata.version("mineru")
    except Exception:  # noqa: BLE001
        version = ""
    return {"ok": True, "version": version, "mode": "local", "state": "ready",
            "detail": f"mineru {version}".strip() + "（接口在首次解析时探测）"}


def _result_markdown(result) -> str:
    value = getattr(result, "markdown", None)
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001
            value = ""
    if not isinstance(value, str) or not value.strip():
        value = ""
    return value


def _result_json(result):
    value = getattr(result, "to_json", None)
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001
            value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def _markdown_from_sources(md: str, raw: dict) -> str:
    if md.strip():
        return md
    for key in ("markdown", "md", "md_content", "content"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


class _Sdk4:
    def __init__(self, parse_fn, version: str):
        self._parse = parse_fn
        self.version = version

    def parse(self, path: str, tier: str, ocr_mode: str, page_range: str) -> dict:
        try:
            result = self._parse(path, tier=tier, ocr_mode=ocr_mode, page_range=page_range)
        except TypeError:  # 参数集在 4.x 小版本间有差异：退回最小调用
            result = self._parse(path)
        md = _result_markdown(result)
        raw = _result_json(result)
        md = _markdown_from_sources(md, raw)
        if not md.strip():
            raise ParseError("empty_text", "MinerU 未产出正文")
        return {"markdown": md, "raw_json": raw, "engine": "mineru",
                "version": self.version, "tier": tier}


class _Sdk3:
    """MinerU 3.x 兜底：仅有类入口时尽力探测 parse/run。"""

    def __init__(self, cls, version: str):
        self._cls = cls
        self.version = version

    def parse(self, path: str, tier: str, ocr_mode: str, page_range: str) -> dict:
        try:
            instance = self._cls()
        except TypeError:
            instance = self._cls(path)
        method = getattr(instance, "parse", None) or getattr(instance, "run", None)
        if not callable(method):
            raise ParseError("mineru_unavailable", "mineru 3.x 无可调用 parse/run")
        try:
            result = method(path)
        except TypeError:
            result = method()
        md = _result_markdown(result)
        raw = _result_json(result)
        md = _markdown_from_sources(md, raw)
        if not md.strip():
            raise ParseError("empty_text", "MinerU 未产出正文")
        return {"markdown": md, "raw_json": raw, "engine": "mineru",
                "version": self.version, "tier": tier}


class _HttpTransport:
    """云端 API 的唯一网络出口；测试注入同名方法的假实现即可离线覆盖。"""

    def _context(self):
        try:
            import certifi  # noqa: PLC0415
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001 — 缺 certifi 时退回系统 CA
            return ssl.create_default_context()

    def _open(self, req, timeout):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=self._context())
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", "replace")
            code = "mineru_auth_failed" if exc.code in (401, 403) else "mineru_api_error"
            raise ParseError(code, f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ParseError("mineru_service_unavailable", str(exc)[:200]) from exc

    def request_json(self, method: str, url: str, payload=None, token: str = "",
                     timeout: float = 60) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._open(req, timeout) as resp:
            body = resp.read()
        try:
            out = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ParseError("mineru_api_error", f"非 JSON 响应: {body[:120]!r}") from exc
        return out if isinstance(out, dict) else {"data": out}

    def put_bytes(self, url: str, path: str, timeout: float = 600) -> None:
        with open(path, "rb") as fh:
            blob = fh.read()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ParseError("mineru_api_error", "无效的上传链接")
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            # OSS 预签名 URL 的签名不含 Content-Type；urllib 会给 data 请求补默认头，
            # 导致 SignatureDoesNotMatch，因此这里用 http.client 发裸 PUT。
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(
                    parts.hostname, parts.port or 443, timeout=timeout,
                    context=self._context())
            else:
                conn = http.client.HTTPConnection(
                    parts.hostname, parts.port or 80, timeout=timeout)
            try:
                conn.request("PUT", target, body=blob)
                resp = conn.getresponse()
                body = resp.read()[:400].decode("utf-8", "replace")
            finally:
                conn.close()
        except ParseError:
            raise
        except (OSError, TimeoutError, socket.timeout) as exc:
            raise ParseError("mineru_service_unavailable", str(exc)[:200]) from exc
        if resp.status >= 300:
            code = "mineru_auth_failed" if resp.status in (401, 403) else "mineru_api_error"
            raise ParseError(code, f"HTTP {resp.status}: {body}")

    def download(self, url: str, dest: str, timeout: float = 900) -> None:
        req = urllib.request.Request(url, method="GET")
        with self._open(req, timeout) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)


class CloudParser:
    """MinerU 云端 Precision API：申请上传链接 → PUT → 轮询 batch → 解 zip。"""

    def __init__(self, token: str, base_url: str, model_version: str = "vlm",
                 language: str = "ch", poll: float = 5.0, timeout: float = 1800.0,
                 transport=None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.language = language
        self.poll = max(0.0, float(poll))
        self.timeout = max(1.0, float(timeout))
        self.transport = transport or _HttpTransport()

    @property
    def key(self):
        return (self.token, self.base_url, self.model_version, self.language)

    def _api(self, method: str, path: str, payload=None, timeout: float = 60) -> dict:
        try:
            raw = self.transport.request_json(method, self.base_url + path,
                                             payload=payload, token=self.token,
                                             timeout=timeout)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 — 网络层异常如实失败
            raise ParseError("mineru_service_unavailable", str(exc)[:200]) from exc
        if int(raw.get("code") or 0) != 0:
            raise ParseError("mineru_api_error",
                             f"code={raw.get('code')} msg={str(raw.get('msg'))[:160]}")
        return raw

    def _create_upload(self, path: str, ocr_mode: str, page_range: str) -> tuple:
        payload = {
            "files": [{"name": os.path.basename(path), "data_id": "derm-kb"}],
            "model_version": self.model_version,
            "is_ocr": str(ocr_mode).lower() not in ("off", "none", "false", "0"),
            "enable_formula": True,
            "enable_table": True,
            "language": self.language,
        }
        if page_range and str(page_range).lower() != "all":
            payload["page_ranges"] = str(page_range)
        data = (self._api("POST", "/file-urls/batch", payload).get("data") or {})
        batch_id = str(data.get("batch_id") or "")
        urls = data.get("file_urls") or []
        if not batch_id or not urls:
            raise ParseError("mineru_api_error", "未返回 batch_id/file_urls")
        return batch_id, urls

    def _wait(self, batch_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        state = "pending"
        while True:
            data = (self._api("GET", f"/extract-results/batch/{batch_id}").get("data") or {})
            rows = data.get("extract_result") or data.get("extract_results") or []
            row = rows[0] if rows else {}
            state = str(row.get("state") or "").lower()
            if state == "done" and row.get("full_zip_url"):
                return row
            if state == "failed":
                raise ParseError("mineru_parse_failed",
                                 str(row.get("err_msg") or "云端解析失败")[:300])
            if time.monotonic() >= deadline:
                raise ParseError("mineru_timeout", f"等待超时（state={state or 'unknown'}）")
            time.sleep(self.poll)

    def _unpack(self, zip_url: str) -> dict:
        fd, tmp = tempfile.mkstemp(prefix="derm-mineru-", suffix=".zip")
        os.close(fd)
        try:
            self.transport.download(zip_url, tmp, timeout=900)
            with zipfile.ZipFile(tmp) as zf:
                names = zf.namelist()
                mds = [n for n in names if n.lower().endswith(".md")]
                if not mds:
                    raise ParseError("empty_text", "云端结果无 markdown")
                pick = next((n for n in mds if n.endswith("full.md")), mds[0])
                markdown = zf.read(pick).decode("utf-8", "replace")
                raw = {}
                lists = [n for n in names if n.lower().endswith("content_list.json")]
                if lists:
                    try:
                        raw = {"content_list": json.loads(zf.read(lists[0]).decode("utf-8"))}
                    except ValueError:
                        raw = {}
                images, total = {}, 0
                for name in names:
                    idx = name.find("images/")
                    if idx < 0 or name.endswith("/"):
                        continue
                    blob = zf.read(name)
                    total += len(blob)
                    if total > _IMAGE_BYTES_MAX:
                        break
                    images[name[idx:]] = blob
        except zipfile.BadZipFile as exc:
            raise ParseError("mineru_api_error", f"结果包损坏: {exc}") from exc
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if not markdown.strip():
            raise ParseError("empty_text", "MinerU 云端未产出正文")
        return {"markdown": markdown, "raw_json": raw, "images": images,
                "engine": "mineru_cloud", "version": "v4"}

    def parse(self, path: str, tier: str, ocr_mode: str, page_range: str) -> dict:
        batch_id, urls = self._create_upload(path, ocr_mode, page_range)
        try:
            self.transport.put_bytes(urls[0], path, timeout=600)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ParseError("mineru_service_unavailable", f"上传失败: {exc}"[:200]) from exc
        row = self._wait(batch_id)
        out = self._unpack(str(row.get("full_zip_url")))
        out["tier"] = tier
        return out


def _new_cloud(token: str) -> CloudParser:
    return CloudParser(
        token, config.mineru_base_url(),
        model_version=config.mineru_model_version(),
        language=config.mineru_language(),
        poll=config.mineru_poll_seconds(),
        timeout=config.mineru_timeout_seconds())


def _cloud_parser():
    global _cloud
    token = config.mineru_token()
    if not token:
        raise ParseError("mineru_unavailable", "token_missing（设置 DERM_MINERU_TOKEN）")
    key = (token, config.mineru_base_url(), config.mineru_model_version(),
           config.mineru_language())
    with _cloud_lock:
        if _cloud is None or _cloud.key != key:
            _cloud = _new_cloud(token)
        return _cloud


def _build_sdk():
    try:
        from mineru.parser import parse  # noqa: PLC0415 — 懒加载，未装时可降级
    except Exception:  # noqa: BLE001
        parse = None
    version = ""
    try:
        version = metadata.version("mineru")
    except Exception:  # noqa: BLE001
        pass
    if callable(parse):
        return _Sdk4(parse, version)
    try:
        from mineru import MinerU  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ParseError("mineru_unavailable", f"SDK 接口不可用: {exc}") from exc
    if MinerU is not None:
        return _Sdk3(MinerU, version)
    raise ParseError("mineru_unavailable", "SDK 接口不可用")


def _sdk_or_raise():
    global _sdk, _sdk_error
    with _sdk_lock:
        if _sdk is None and not _sdk_error:
            if importlib.util.find_spec("mineru") is None:
                _sdk_error = "not_installed"
            else:
                try:
                    _sdk = _build_sdk()
                except ParseError as exc:
                    _sdk_error = f"{exc.code}: {exc.detail}"
                except Exception as exc:  # noqa: BLE001
                    _sdk_error = f"load_failed: {exc}"
        if _sdk is None:
            raise ParseError("mineru_unavailable", _sdk_error or "not_installed")
        return _sdk


def reset() -> None:
    """测试注入/重装后强制重新探测。"""
    global _sdk, _sdk_error, _cloud
    with _sdk_lock:
        _sdk = None
        _sdk_error = ""
    with _cloud_lock:
        _cloud = None


def _decode(data: bytes) -> str:
    for encoding in _TXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_direct(path: str) -> dict:
    if not os.path.isfile(path):
        raise ParseError("corrupt_file", "文件不存在")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ParseError("corrupt_file", str(exc)) from exc
    text = _decode(data)
    if not text.strip():
        raise ParseError("empty_text", "文件无正文")
    return {"markdown": text, "raw_json": {}, "engine": "direct",
            "version": "", "tier": "direct"}


def parse_document(path: str, tier: str = "standard", ocr_mode: str = "auto",
                   page_range: str = "all") -> dict:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in config.supported_formats():
        raise ParseError("format_unsupported", f"不支持的格式: {ext or 'unknown'}")
    if ext in config.direct_formats():
        return read_direct(path)
    if not os.path.isfile(path):
        raise ParseError("corrupt_file", "文件不存在")
    try:
        if _cloud_selected():
            return _cloud_parser().parse(path, tier=tier, ocr_mode=ocr_mode,
                                         page_range=page_range)
        return _sdk_or_raise().parse(path, tier=tier, ocr_mode=ocr_mode,
                                     page_range=page_range)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 — 解析崩溃如实落 failed
        raise ParseError("parse_failed", f"{exc.__class__.__name__}: {exc}") from exc
