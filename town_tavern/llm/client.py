"""DeepSeek 调用封装。

使用 OpenAI 兼容 SDK 接入 DeepSeek。提供两类调用:
- chat_text:返回自由文本(用于对话回复这类叙事场景)
- chat_json:强制返回 JSON,并用 Pydantic 模型校验,失败自动重试一次

设计原则:LLM 负责想象,程序负责裁决。所有需要影响游戏状态的输出
都必须走 chat_json,落到结构化模型里。
"""
import json
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    LLM_JSON_RETRIES,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """LLM 调用或解析失败时抛出。"""


class LLMClient:
    """DeepSeek 客户端封装。"""

    def __init__(self):
        if not DEEPSEEK_API_KEY:
            raise LLMError(
                "未检测到 DEEPSEEK_API_KEY 环境变量。请先执行:\n"
                "  export DEEPSEEK_API_KEY=你的key"
            )
        # 延迟导入,避免未安装 openai 时影响其他模块
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMError("缺少 openai 依赖,请先 pip install -r requirements.txt") from e

        self._client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            timeout=LLM_TIMEOUT,
        )
        self.model = DEEPSEEK_MODEL

    # ------------------------------------------------------------------
    # 自由文本
    # ------------------------------------------------------------------
    def chat_text(self, system_prompt: str, user_prompt: str,
                  temperature: float = LLM_TEMPERATURE) -> str:
        """返回纯文本回复。"""
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()

    # ------------------------------------------------------------------
    # 结构化 JSON(带校验重试)
    # ------------------------------------------------------------------
    def chat_json(self, system_prompt: str, user_prompt: str,
                  schema: Type[T], temperature: float = 0.4) -> T:
        """要求模型返回 JSON,并用给定 Pydantic 模型校验。

        解析/校验失败时,把错误信息回灌给模型重试,最多 LLM_JSON_RETRIES 次。
        裁决类调用默认用较低温度,减少胡乱发挥。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt
                + "\n\n严格只输出一个 JSON 对象,不要任何额外解释、不要 markdown 代码块。",
            },
        ]

        last_error = ""
        for attempt in range(LLM_JSON_RETRIES + 1):
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            try:
                data = self._extract_json(raw)
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                # 把模型的错误输出与错误原因回灌,引导其修正
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"上面的输出无法被解析为目标结构,错误如下:\n{last_error}\n"
                        "请严格按要求重新输出一个合法 JSON 对象。"
                    ),
                })

        raise LLMError(f"JSON 校验在重试后仍失败: {last_error}")

    @staticmethod
    def _extract_json(raw: str) -> dict:
        """从模型输出中提取 JSON 对象(容错去除 markdown 围栏)。"""
        text = raw.strip()
        if text.startswith("```"):
            # 去掉 ```json ... ``` 围栏
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        # 截取第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        return json.loads(text)
