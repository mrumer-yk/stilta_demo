from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_MODEL = "gemini-3.1-pro-preview"


@dataclass
class GeminiResult:
    text: str | None
    error: str | None = None
    model: str = DEFAULT_MODEL


class GeminiClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        mode = "gemini" if self.enabled else "local_agentic"
        return {
            "provider": "optional_gemini",
            "model": self.model,
            "api_key_present": self.enabled,
            "required": False,
            "mode": mode,
            "default_workflow": "local agentic workflow runs when Gemini is not configured",
        }

    def generate(self, system: str, prompt: str, max_tokens: int = 900) -> GeminiResult:
        if not self.enabled:
            return GeminiResult(None, "GEMINI_API_KEY is not set", self.model)

        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model)}:generateContent"
        )
        url = f"{endpoint}?key={urllib.parse.quote(self.api_key)}"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"{system.strip()}\n\n"
                                "Return concise, source-grounded text. Do not invent citations.\n\n"
                                f"{prompt.strip()}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
            },
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            return GeminiResult(None, f"Gemini HTTP {exc.code}: {detail[:500]}", self.model)
        except Exception as exc:
            return GeminiResult(None, str(exc), self.model)

        try:
            candidates = body.get("candidates", [])
            parts = candidates[0]["content"].get("parts", [])
            text = "\n".join(part.get("text", "") for part in parts).strip()
            return GeminiResult(text or None, None if text else "Gemini returned no text", self.model)
        except Exception as exc:
            return GeminiResult(None, f"Unexpected Gemini response: {exc}", self.model)


def gemini_client() -> GeminiClient:
    return GeminiClient()
