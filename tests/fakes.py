"""Minimal fake of requests.Session for testing HTTP integrations offline."""
import json as jsonlib


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else jsonlib.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """`router(method, url, params, json)` returns a FakeResponse."""

    def __init__(self, router):
        self.router = router
        self.calls = []
        self.headers = {}
        self.auth = None

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        return self.router(method, url, params, json)

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params, timeout=timeout)

    def post(self, url, json=None, timeout=None):
        return self.request("POST", url, json=json, timeout=timeout)
