"""Bounded Kimi JSON client using only the standard library; no automatic retries."""
from __future__ import annotations

import getpass
import json
import os
import re
import time
import urllib.error
import urllib.request
import warnings

MODEL = "kimi-k2.6"
BASE_URLS = ("https://api.moonshot.cn/v1", "https://api.moonshot.ai/v1")

class KimiError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_api_key(prompt=False):
    if prompt:
        # Fail rather than falling back to a terminal that echoes the secret.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Moonshot API key (hidden): ")
    else:
        key = os.environ.get("MOONSHOT_API_KEY", "")
    if not key.strip():
        raise KimiError("Set MOONSHOT_API_KEY or use --prompt-key; no key is saved.")
    return key.strip()


class KimiClient:
    def __init__(self, api_key, *, base_url=BASE_URLS[0], timeout=60):
        if base_url not in BASE_URLS:
            raise ValueError("Choose an official Moonshot endpoint explicitly.")
        if not api_key.strip():
            raise ValueError("API key is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._api_key, self.base_url, self.timeout = api_key.strip(), base_url, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def complete(self, messages, *, max_tokens=128):
        if not 1 <= max_tokens <= 256:
            raise ValueError("This short-answer example allows 1–256 output tokens")
        payload = {"model": MODEL, "messages": messages, "stream": False,
                   "thinking": {"type": "disabled"},
                   "max_completion_tokens": max_tokens,
                   "response_format": {"type": "json_object"}}
        # K2.6 supplies its own sampling defaults. Do not send vLLM seed/template fields.
        request = urllib.request.Request(self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key})
        start = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(8192).decode("utf-8", errors="replace")
            detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", detail.replace(self._api_key, "[REDACTED]"))
            raise KimiError(f"Kimi HTTP {error.code}: {detail[:500]}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise KimiError("Kimi connection failed or timed out; no automatic retry was made.") from None
        try:
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise KimiError("Kimi returned no text completion.") from None
        return {"request": payload, "content": content,
                "finish_reason": choice.get("finish_reason"), "usage": raw.get("usage", {}),
                "response_id": raw.get("id"), "model": raw.get("model"),
                "latency_ms": round((time.perf_counter() - start) * 1000, 3)}
