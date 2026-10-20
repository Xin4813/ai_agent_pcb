"""OpenAI 兼容的 LLM 客户端。openai 库按需导入，避免无密钥环境下 import 报错。"""
from __future__ import annotations


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("llm", {})
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "未安装 openai 库，请先 `pip install openai`。或使用 --mock 离线模式。"
            ) from e
        self._client = OpenAI(
            base_url=self.cfg.get("base_url", "https://api.openai.com/v1"),
            api_key=self.cfg.get("api_key", ""),
        )

    def complete(self, system: str, user: str, temperature: float | None = None) -> str:
        t = temperature if temperature is not None else self.cfg.get("temperature", 0.2)
        resp = self._client.chat.completions.create(
            model=self.cfg.get("model", "gpt-4o-mini"),
            temperature=t,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""
