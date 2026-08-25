"""錄製 / 重放 HTTP 回應。

錄製：包住真實 session，把 (method, url, 正規化 params) -> 回應存成 JSON。
重放：完全離線，依同一把鍵回傳錄下的內容；找不到就記成 miss。

鍵刻意含 method 與排序後的 params，讓「兩個 branch 送出的請求集合是否相同」
本身也成為可觀測的結果——develop 若要了 main 沒要過的東西，會顯示成 miss。
"""
from __future__ import annotations
import json, hashlib, os, threading, time


def _key(method: str, url: str, params, data=None) -> str:
    norm = {
        "m": method.upper(),
        "u": url,
        "p": sorted((str(k), str(v)) for k, v in (params or {}).items()),
        "d": sorted((str(k), str(v)) for k, v in (data or {}).items()),
    }
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


class _Resp:
    def __init__(self, rec):
        self.status_code = rec["status"]
        self.text = rec["text"]
        self._json = rec.get("json")
        self._err = rec.get("error")
        self.encoding = rec.get("encoding")

    @property
    def content(self):
        return self.text.encode(self.encoding or "utf-8", errors="replace")

    def raise_for_status(self):
        if self._err:
            import requests
            raise requests.HTTPError(self._err)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class RecordingSession:
    def __init__(self, real, path, min_interval=1.1):
        self._real = real
        self._path = path
        self._tape = {}
        self._lock = threading.Lock()
        self._min = min_interval
        self._last = 0.0
        self.n = 0
        if os.path.exists(path):
            self._tape = json.load(open(path, encoding="utf-8"))

    def _pace(self, url):
        if "twse.com.tw" not in url:
            return
        with self._lock:
            wait = self._min - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _do(self, method, url, params=None, data=None, **kw):
        k = _key(method, url, params, data)
        if k in self._tape:
            return _Resp(self._tape[k])
        self._pace(url)
        self.n += 1
        fn = getattr(self._real, method.lower())
        kwargs = dict(timeout=kw.get("timeout", 30), verify=kw.get("verify", False))
        if params is not None:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        r = fn(url, **kwargs)
        rec = {"status": r.status_code, "text": r.text, "encoding": r.encoding}
        try:
            rec["json"] = r.json()
        except Exception:
            rec["json"] = None
        if r.status_code >= 400:
            rec["error"] = f"{r.status_code}"
        self._tape[k] = rec
        self.save()
        return _Resp(rec)

    def get(self, url, params=None, **kw):
        return self._do("GET", url, params, None, **kw)

    def post(self, url, data=None, params=None, **kw):
        return self._do("POST", url, params, data, **kw)

    def save(self):
        tmp = self._path + ".tmp"
        json.dump(self._tape, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, self._path)


class ReplaySession:
    def __init__(self, path):
        self._tape = json.load(open(path, encoding="utf-8"))
        self.misses = []
        self.hits = 0

    def _do(self, method, url, params=None, data=None, **kw):
        k = _key(method, url, params, data)
        if k not in self._tape:
            self.misses.append({"method": method, "url": url,
                                "params": dict(params or {}), "data": dict(data or {})})
            import requests
            raise requests.ConnectionError(f"TAPE MISS {method} {url}")
        self.hits += 1
        return _Resp(self._tape[k])

    def get(self, url, params=None, **kw):
        return self._do("GET", url, params, None, **kw)

    def post(self, url, data=None, params=None, **kw):
        return self._do("POST", url, params, data, **kw)
