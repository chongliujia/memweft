"""Single-request Kimi client for bounded multistep evaluation actions."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from handoff_app.kimi_client import KimiClient, KimiError, MODEL  # noqa: E402


class RateLimited(KimiError):
    """HTTP 429; the caller owns any recorded retry and global backoff."""

    status_code = 429


class KimiResponseError(KimiError):
    """An HTTP response arrived, but no usable completion can be executed.

    Only accounting fields and the credential-free request are retained. This
    permits persistence before stopping without saving arbitrary provider bodies.
    """

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response
        self.usage = response.get('usage')


class BoundedKimiClient(KimiClient):
    """Keep the example's transport restrictions with a 1,024-token budget."""

    def complete(self, messages, *, max_tokens=1024):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1024:
            raise ValueError("Multistep actions allow 1–1024 output tokens")
        payload = {
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._api_key},
        )
        start = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(8192).decode("utf-8", errors="replace")
            detail = detail.replace(self._api_key, "[REDACTED]")
            detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", detail)
            detail = re.sub(r"[\x00-\x1f\x7f]", " ", detail)
            error_type = RateLimited if error.code == 429 else KimiError
            raise error_type(f"Kimi HTTP {error.code}: {detail[:500]}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise KimiError(
                "Kimi connection failed or timed out; no automatic retry was made."
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise KimiError("Kimi returned an invalid JSON response.") from None
        try:
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            metadata = raw if isinstance(raw, dict) else {}
            def safe_text(value):
                if not isinstance(value, str):
                    return None
                value = value.replace(self._api_key, '[REDACTED]')
                value = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', value)
                return re.sub(r'[\x00-\x1f\x7f]', ' ', value)[:256]
            usage = metadata.get('usage')
            if isinstance(usage, dict):
                # Missing/invalid quantities stay unknown, never coerced to zero.
                usage = {k: v if type(v) is int else None for k in
                         ('prompt_tokens', 'completion_tokens', 'total_tokens')
                         for v in [usage.get(k)]}
            else:
                usage = None
            choices = metadata.get('choices')
            first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            response = {'request': payload, 'content': None, 'usage': usage,
                        'response_id': safe_text(metadata.get('id')),
                        'model': safe_text(metadata.get('model')),
                        'finish_reason': safe_text(first.get('finish_reason')),
                        'latency_ms': round((time.perf_counter() - start) * 1000, 3),
                        'response_error': 'missing_or_empty_completion'}
            raise KimiResponseError('Kimi returned no text completion.', response) from None
        return {
            "request": payload,
            "content": content,
            "finish_reason": choice.get("finish_reason"),
            "usage": raw.get("usage", {}),
            "response_id": raw.get("id"),
            "model": raw.get("model"),
            "latency_ms": round((time.perf_counter() - start) * 1000, 3),
        }
