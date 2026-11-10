"""DeepSeek思考模式的协议适配；不增加或修改任何分析提示词。"""

import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai import BaseModel
from pydantic import Field


_LOG_LOCK = threading.Lock()


class InvalidDeepSeekResponse(ValueError):
    """接口没有返回可供代理继续分析的正常响应。"""


def _log(path, event, **details):
    if path is None:
        return
    record = {"time": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
              "event": event, **details}
    with _LOG_LOCK:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _response_summary(response):
    """只记录结构与统计，不写入请求头、原始正文、图片或思考内容。"""
    raw = response.content
    result = {
        "http_status": response.status_code,
        "request_id": response.headers.get("x-request-id") or response.headers.get("x-ds-request-id"),
        "content_type": response.headers.get("content-type"),
        "response_bytes": len(raw),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        data = response.json()
    except ValueError:
        result["json_type"] = "invalid_json"
        return result
    result["json_type"] = "null" if data is None else type(data).__name__
    if isinstance(data, dict):
        result["top_level_keys"] = sorted(data)
        choices = data.get("choices")
        result["choices_count"] = len(choices) if isinstance(choices, list) else None
        if isinstance(choices, list):
            result["choices"] = []
            for choice in choices:
                if not isinstance(choice, dict):
                    result["choices"].append({"type": type(choice).__name__})
                    continue
                message = choice.get("message")
                item = {"finish_reason": choice.get("finish_reason"),
                        "message_type": type(message).__name__}
                if isinstance(message, dict):
                    content, tools = message.get("content"), message.get("tool_calls")
                    item.update({"content_length": len(content) if isinstance(content, (str, list)) else None,
                                 "tool_calls_count": len(tools) if isinstance(tools, list) else 0,
                                 "has_reasoning_content": "reasoning_content" in message})
                result["choices"].append(item)
        usage = data.get("usage")
        if isinstance(usage, dict):
            result["token_usage"] = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
    return result


def _response_hook(path):
    def record(response):
        response.read()
        _log(path, "http_response", **_response_summary(response))
    return record


def _async_response_hook(path):
    async def record(response):
        await response.aread()
        _log(path, "http_response", **_response_summary(response))
    return record


class DeepSeekChatOpenAI(ChatOpenAI):
    """保留工具调用历史所需的reasoning_content，继续使用官方代理代码。"""

    diagnostics_path: Path | None = Field(default=None, exclude=True)
    invalid_response_retries: int = Field(default=1, ge=0)
    invalid_response_retry_delay: float = Field(default=2, ge=0)

    def __init__(self, **data):
        path = data.get("diagnostics_path")
        if path is not None:
            path = Path(path)
            sync_client = data.get("http_client")
            if sync_client is None:
                sync_client = httpx.Client(follow_redirects=True)
                data["http_client"] = sync_client
            sync_client.event_hooks["response"].append(_response_hook(path))
            async_client = data.get("http_async_client")
            if async_client is None:
                async_client = httpx.AsyncClient(follow_redirects=True)
                data["http_async_client"] = async_client
            async_client.event_hooks["response"].append(_async_response_hook(path))
        super().__init__(**data)

    def _diagnostic(self, event, **details):
        _log(self.diagnostics_path, event, **details)

    def close(self):
        """每个样本结束后释放连接，避免300个样本积累HTTP客户端。"""
        self.root_client.close()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.root_async_client.close())
        else:
            loop.create_task(self.root_async_client.close())

    async def aclose(self):
        self.root_client.close()
        await self.root_async_client.close()

    def _failure(self, error, attempt):
        self._diagnostic(
            "request_error", error_type=type(error).__name__, attempt=attempt + 1,
            traceback=[{"file": f.filename, "line": f.lineno, "function": f.name}
                       for f in traceback.extract_tb(error.__traceback__)],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        for attempt in range(self.invalid_response_retries + 1):
            try:
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except (InvalidDeepSeekResponse, json.JSONDecodeError) as error:
                self._failure(error, attempt)
                if attempt == self.invalid_response_retries:
                    if isinstance(error, json.JSONDecodeError):
                        raise InvalidDeepSeekResponse("DeepSeek返回空正文或无法解析的JSON，未生成有效结果") from error
                    raise
                self._diagnostic("invalid_response_retry", retry_number=attempt + 1)
                time.sleep(self.invalid_response_retry_delay)
            except Exception as error:
                self._failure(error, attempt)
                raise

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        for attempt in range(self.invalid_response_retries + 1):
            try:
                return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except (InvalidDeepSeekResponse, json.JSONDecodeError) as error:
                self._failure(error, attempt)
                if attempt == self.invalid_response_retries:
                    if isinstance(error, json.JSONDecodeError):
                        raise InvalidDeepSeekResponse("DeepSeek返回空正文或无法解析的JSON，未生成有效结果") from error
                    raise
                self._diagnostic("invalid_response_retry", retry_number=attempt + 1)
                await asyncio.sleep(self.invalid_response_retry_delay)
            except Exception as error:
                self._failure(error, attempt)
                raise

    def _create_chat_result(self, response, generation_info=None):
        if not isinstance(response, (dict, BaseModel)):
            raise InvalidDeepSeekResponse(
                f"DeepSeek未返回正常响应对象（收到{type(response).__name__}），本次未生成有效结果"
            )
        raw = response if isinstance(response, dict) else response.model_dump()
        choices = raw.get("choices")
        if raw.get("error") or not isinstance(choices, list) or not choices:
            raise InvalidDeepSeekResponse("DeepSeek响应缺少有效choices，不能作为分析报告")
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise InvalidDeepSeekResponse("DeepSeek响应中的message结构异常")
            finish = choice.get("finish_reason")
            if finish == "length":
                raise ValueError(
                    "DeepSeek输出达到max_tokens上限，结果不完整，未作为成功样本评分。"
                    "请检查输出长度设置后再开始新的实验配置。"
                )
            if finish == "content_filter":
                raise ValueError("DeepSeek响应被内容过滤，未生成可评分结果")
            if finish in ("insufficient_system_resource", "aborted"):
                raise InvalidDeepSeekResponse(f"DeepSeek请求未正常完成：{finish}")
            message = choice["message"]
            # 工具调用可以正常返回空content，不能把它误判为空响应。
            content = message.get("content")
            has_content = bool(content.strip()) if isinstance(content, str) else bool(content)
            if not has_content and not message.get("tool_calls"):
                raise InvalidDeepSeekResponse("DeepSeek未返回报告内容或工具调用")
        result = super()._create_chat_result(response, generation_info)
        for choice, generation in zip(raw["choices"], result.generations, strict=True):
            message = choice["message"]
            if "reasoning_content" in message:
                generation.message.additional_kwargs["reasoning_content"] = message[
                    "reasoning_content"
                ]
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if payload.get("stream") or "messages" not in payload:
            raise ValueError("本适配器使用非流式Chat Completions接口")
        for original, outgoing in zip(messages, payload["messages"], strict=True):
            if isinstance(original, AIMessage):
                if "reasoning_content" in original.additional_kwargs:
                    outgoing["reasoning_content"] = original.additional_kwargs[
                        "reasoning_content"
                    ]
        return payload
