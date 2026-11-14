from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class RemoteError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(str(body))
        self.status = status
        self.body = body


class ProductClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, path, value=None):
        body = None if value is None else json.dumps(value, allow_nan=False).encode()
        request = Request(self.base_url + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise RemoteError(error.code, json.load(error)) from error

    def snapshot(self, as_of):
        return self.request("/snapshot?as_of=" + as_of)

    def policy(self):
        return self.request("/policy")

    def publish(self, key, artifact, revision):
        return self.request("/artifacts", {"request_key": key, "artifact": artifact, "policy_revision": revision})

    def artifact(self, identifier):
        return self.request("/artifacts/" + identifier)
