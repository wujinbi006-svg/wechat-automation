"""OpenAI 兼容 Chat Completions 客户端。

只用标准库 urllib，不引入新依赖。任何 OpenAI 兼容端点都能用：
OpenAI / DeepSeek / 通义 / 本地 vLLM / Ollama(``/v1``)。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


class LLMError(RuntimeError):
    """调用大模型失败；调用方应把它当作「这条不回」，而不是崩溃。"""


_WS_RE = re.compile(r"[ \t\u3000]+")
_EDGE_QUOTES = "\"'“”‘’「」『』 \n\t"


class ChatLLM:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: float = 45.0, temperature: float = 0.8,
                 fallback_models: list[str] | None = None,
                 max_tokens: int = 0, reasoning_effort: str = ""):
        self.base_url = (base_url or os.getenv("AUTO_REPLY_LLM_BASE_URL")
                         or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.getenv("AUTO_REPLY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        primary = model or os.getenv("AUTO_REPLY_LLM_MODEL") or "deepseek-chat"
        extra = fallback_models or []
        if isinstance(extra, str):
            extra = [extra]
        # 免费路由池里的模型会偶发返回 null content；按顺序退到下一个模型，
        # 全部失败才让上层把这条判成「不回」。
        self.models = [m for m in [primary, *extra] if m]
        self.model = self.models[0]
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        # 显式 token 上限。0 表示沿用旧行为（按 max_chars 推算）。
        self.max_tokens = int(max_tokens or 0)
        # 推理强度。留空则不发送该参数。
        #
        # DeepSeek 的 flash / v4-pro 都是推理模型：思考与正文共用同一份
        # max_tokens 预算。实测只有 "none" 能真正关闭推理；minimal/low/medium/
        # high 都会产生大量 reasoning token，预算偏小时正文被挤成空串，
        # 接口返回 finish_reason=length + content=""，表现为「AI 不回复」。
        self.reasoning_effort = str(reasoning_effort or "").strip()
        self.last_error: str = ""
        self.attempts: list[dict] = []

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.models)

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 400,
                 model: str | None = None) -> str:
        if not self.configured:
            raise LLMError("LLM not configured: set AUTO_REPLY_LLM_BASE_URL / AUTO_REPLY_LLM_MODEL")
        payload = {
            "model": model or self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        # 仅在显式配置时发送：留空表示沿用服务端默认（通常是开推理）。
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise LLMError(f"HTTP {exc.code} from LLM: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都退化成「这条不回」
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response: {str(body)[:300]}") from exc

        # 内容为空时把「为什么空」写进错误里。推理模型最常见的空回复是
        # finish_reason=length——思考耗尽了 max_tokens，正文一个字都没写。
        # 只说「empty content」会让排查绕远路，所以这里带上完成原因与用量。
        if not str(text or "").strip():
            finish = choice.get("finish_reason")
            usage = body.get("usage") or {}
            detail = usage.get("completion_tokens_details") or {}
            raise LLMError(
                "empty content (finish_reason=%s, completion_tokens=%s, reasoning_tokens=%s,"
                " max_tokens=%s)%s" % (
                    finish,
                    usage.get("completion_tokens"),
                    detail.get("reasoning_tokens"),
                    int(max_tokens),
                    "；疑似推理耗尽预算，可调大 llm_max_tokens 或设 "
                    "llm_reasoning_effort=none" if finish == "length" else "",
                )
            )
        return str(text)

    def reply(self, system: str, messages: list[dict], *, max_chars: int = 300) -> str:
        """依次尝试模型列表；返回第一条非空回复。全部失败则抛 LLMError。"""
        # 优先用配置里显式给的 token 上限；没给才退回按字数推算的旧规则。
        # 旧规则（max_chars*3，下限 64、上限 800）对推理模型太紧——思考会
        # 吃光预算导致 content 为空，所以配置里通常会显式指定一个宽上限。
        max_tokens = self.max_tokens or max(64, min(800, max_chars * 3))
        self.attempts = []
        for name in self.models:
            try:
                text = sanitize_reply(
                    self.complete(system, messages, max_tokens=max_tokens, model=name),
                    max_chars=max_chars,
                )
            except LLMError as exc:
                self.attempts.append({"model": name, "result": "error", "detail": str(exc)[:200]})
                self.last_error = str(exc)[:200]
                continue
            if text:
                self.attempts.append({"model": name, "result": "ok"})
                return text
            self.attempts.append({"model": name, "result": "empty"})
            self.last_error = f"{name} returned empty content"
        raise LLMError(self.last_error or "all models returned empty content")


def sanitize_reply(raw: str, *, max_chars: int = 300) -> str:
    """把模型输出裁成一条可以直接发出去的微信文本。"""
    text = str(raw or "").strip()
    # 只取第一段有用的内容，避免模型附带解释或分行列表。
    if "\n" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = lines[0] if lines else ""
    text = text.strip(_EDGE_QUOTES)
    text = _WS_RE.sub(" ", text).strip()
    text = _strip_type_placeholders(text)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


# 免费路由模型会把历史里的消息类型标注当成内容照抄出来（例如 "[文本]"、
# "[图片]"），发出去就是一句莫名其妙的占位符。这里在最终出口处兜底：
# 整条都是占位符时直接判空（上层会跳过发送），句中夹带时把标记剥掉。
_TYPE_TAG = re.compile(
    r"\[\s*(?:文本|文字|图片|图像|语音|音频|视频|文件|链接|卡片|动画表情|表情|"
    r"转账|红包|位置|名片|引用|回复|系统消息|系统|消息|"
    r"text|image|img|voice|audio|video|file|link|card|sticker|emoji|transfer|"
    r"redpacket|location|quote|reply|system|msg|message)\s*\]",
    re.IGNORECASE,
)


def _strip_type_placeholders(text: str) -> str:
    """剥离模型误输出的消息类型占位符；整条都是占位符时返回空串。"""
    if not text:
        return text
    stripped = _TYPE_TAG.sub("", text)
    stripped = _WS_RE.sub(" ", stripped).strip(" -—·:：、,，")
    return stripped
