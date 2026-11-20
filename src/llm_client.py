"""OpenAI 兼容的 LLM 客户端。openai 库按需导入，避免无密钥环境下 import 报错。"""
from __future__ import annotations
import time


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
            timeout=float(self.cfg.get("timeout", 120)),
            max_retries=0,  # 重试由本类统一处理，避免库内重试与本类叠加放大等待
        )
        self._max_attempts = int(self.cfg.get("max_attempts", 3))
        self._retry_delay = float(self.cfg.get("retry_delay", 1.5))

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """判定异常是否是可重试的瞬态错误。

        山大 :4000 网关有个坑：当它连不上上游模型时，会把「连接失败」错误地
        包装成 401 auth_error，报错里的 key 其实完全有效（同样的 key 换个时刻
        就能 200）。所以不能只看状态码 401 就判定密钥失效——必须看错误内容：
        - "All connection attempts failed" / "connection" / "timeout" / 超时类
          → 上游抖动，可重试；
        - 明确的 "invalid api key" / "authentication" → 真密钥问题，不重试。
        """
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        # 真·密钥失效优先判定（即使消息里带了 connection 字样也不重试，
        # 因为密钥错误重试 3 次只会白白浪费时间）——用明确的凭据类关键词。
        if any(k in msg for k in ("invalid api key", "incorrect api key",
                                  "invalid_api_key", "api key not found",
                                  "authentication token", "permission denied")):
            return False
        # 网关把连接失败伪装成 401：按错误正文判定
        if "authentication" in name or "401" in msg:
            if any(k in msg for k in ("connection", "attempts failed", "timeout",
                                      "timed out", "temporarily", "unavailable",
                                      "overload", "busy")):
                return True
            return False
        # 超时 / 连接中断 / 限流 / 服务端错误：明确的瞬态错误（类名或正文）
        if any(k in name for k in ("timeout", "connection", "ratelimit", "apistatus",
                                   "internalserver")):
            return True
        if any(k in msg for k in ("timed out", "timeout", "rate limit", "429",
                                  "temporarily", "unavailable", "overload", "busy",
                                  "500", "502", "503", "504")):
            return True
        return False

    def complete(self, system: str, user: str, temperature: float | None = None) -> str:
        t = temperature if temperature is not None else self.cfg.get("temperature", 0.2)
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.get("model", "gpt-4o-mini"),
                    temperature=t,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self._max_attempts - 1 and self._is_transient(e):
                    time.sleep(self._retry_delay * (attempt + 1))
                    continue
                raise
        raise last  # pragma: no cover - 循环内必定 return 或 raise
