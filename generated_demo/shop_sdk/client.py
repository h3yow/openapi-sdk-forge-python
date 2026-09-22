"""由 OpenAPI 契约自动生成的 HTTP 客户端，仅依赖标准库。"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import models


class ApiError(Exception):
    """服务端返回 4xx/5xx。"""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f'HTTP {status_code}: {body!r}')


@dataclass
class _Response:
    status_code: int
    data: Any


class ApiClient:
    """同步 HTTP 客户端。"""

    def __init__(self, base_url: str, *, timeout: float = 10.0,
                 headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _request(self, method: str, path: str, *,
                 path_params: dict[str, Any] | None = None,
                 query_params: dict[str, Any] | None = None,
                 body: Any = None,
                 content_type: str = 'application/json',
                 headers: dict[str, str] | None = None):
        for key, value in (path_params or {}).items():
            path = path.replace('{' + key + '}',
                              urllib.parse.quote(str(value), safe=''))
        url = self.base_url + path
        query = {k: v for k, v in (query_params or {}).items()
                 if v is not None}
        if query:
            url += '?' + urllib.parse.urlencode(query, doseq=True)
        data = None
        req_headers = dict(self.headers)
        req_headers.update(headers or {})
        if body is not None:
            data = _json.dumps(self._plain(body)).encode('utf-8')
            req_headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=data,
                                     headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode('utf-8')
                status = resp.status
        except urllib.error.HTTPError as exc:
            text = exc.read().decode('utf-8', errors='replace')
            raise ApiError(exc.code, self._maybe_json(text)) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f'请求失败: {exc}') from exc
        return _Response(status, self._maybe_json(text))

    @staticmethod
    def _maybe_json(text: str):
        if not text:
            return None
        try:
            return _json.loads(text)
        except ValueError:
            return text

    @staticmethod
    def _plain(value: Any) -> Any:
        if hasattr(value, '__dict__'):
            return {k: ApiClient._plain(v) for k, v in value.__dict__.items()
                    if v is not None}
        if isinstance(value, list):
            return [ApiClient._plain(v) for v in value]
        if isinstance(value, dict):
            return {k: ApiClient._plain(v) for k, v in value.items()}
        return value

class _ItemsApi:
    def list_items_items_get(self, tag: str | None = None) -> _Response:
        """GET /items"""
        path_params = None
        query_params = {
            'tag': tag,
        }
        return self._request('GET', '/items', path_params=path_params, query_params=query_params)

    def create_item_items_post(self, body: "ItemCreate") -> _Response:
        """POST /items"""
        path_params = None
        query_params = None
        return self._request('POST', '/items', path_params=path_params, query_params=query_params, body=body, content_type='application/json')

    def get_item_items__item_id__get(self, item_id: int) -> _Response:
        """GET /items/{item_id}"""
        path_params = {
            'item_id': item_id,
        }
        query_params = None
        return self._request('GET', '/items/{item_id}', path_params=path_params, query_params=query_params)

    def patch_item_items__item_id__patch(self, item_id: int, body: "ItemPatch") -> _Response:
        """PATCH /items/{item_id}"""
        path_params = {
            'item_id': item_id,
        }
        query_params = None
        return self._request('PATCH', '/items/{item_id}', path_params=path_params, query_params=query_params, body=body, content_type='application/json')

    def delete_item_items__item_id__delete(self, item_id: int) -> _Response:
        """DELETE /items/{item_id}"""
        path_params = {
            'item_id': item_id,
        }
        query_params = None
        return self._request('DELETE', '/items/{item_id}', path_params=path_params, query_params=query_params)



class Client(_ItemsApi, ApiClient):
    """组合全部接口分组的客户端。"""
