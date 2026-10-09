#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动评分模块
负责调用多种 AI 接口并提取分数。
"""

import base64
import json
import re
import time
from typing import Mapping

import requests

# 支持思考模式的模型列表（模型名关键词匹配）
_THINKING_MODEL_KEYWORDS = ["mimo", "qwen3", "deepseek-r1", "qwq", "thinking"]

# 明确禁用思考模式时使用的模型名标记
_THINKING_OFF_MARKERS = ["no-think", "nothink", "thinking-off", "non-thinking"]

# ── 全局请求策略（超时 / 失败重试次数） ──────────────────────────────
# 下面的常量只是默认值；运行时可用 configure_request_policy() 修改，
# 上层 GUI 的「API 配置 → 请求设置」即调用它，并随 config.json 持久化。
API_TIMEOUT_SECONDS = 60      # 默认单次请求超时（秒）
API_MAX_RETRIES = 1           # 默认失败重试次数（不含首次请求）
API_TIMEOUT_MIN, API_TIMEOUT_MAX = 5, 600
API_RETRIES_MIN, API_RETRIES_MAX = 0, 5

_REQUEST_POLICY = {
    "timeout_seconds": API_TIMEOUT_SECONDS,
    "max_retries": API_MAX_RETRIES,
}

# 重试间隔基数（第 1 次重试等待 1s，第 2 次 2s……）
_API_RETRY_BACKOFF_SECONDS = 1.0


def configure_request_policy(timeout_seconds=None, max_retries=None) -> dict:
    """设置全局 API 请求策略，返回生效后的完整策略。

    :param timeout_seconds: 单次请求超时秒数（5~600）；None 表示不修改
    :param max_retries: 失败重试次数（0~5，不含首次请求）；None 表示不修改
    """
    if timeout_seconds is not None:
        try:
            seconds = int(round(float(timeout_seconds)))
        except (TypeError, ValueError):
            seconds = API_TIMEOUT_SECONDS
        _REQUEST_POLICY["timeout_seconds"] = max(API_TIMEOUT_MIN, min(API_TIMEOUT_MAX, seconds))
    if max_retries is not None:
        try:
            retries = int(round(float(max_retries)))
        except (TypeError, ValueError):
            retries = API_MAX_RETRIES
        _REQUEST_POLICY["max_retries"] = max(API_RETRIES_MIN, min(API_RETRIES_MAX, retries))
    return get_request_policy()


def get_request_policy() -> dict:
    """读取当前全局请求策略（``timeout_seconds`` / ``max_retries``）。"""
    return dict(_REQUEST_POLICY)

# 服务端拒绝 thinking 参数时返回的错误特征串（小写匹配）
_UNSUPPORTED_THINKING_MARKERS = [
    "unsupportedparamserror",
    "does not support parameters",
    "unsupported parameter",
    "unsupported_params",
    "drop_params",
]

# 开启思考模式时依次尝试的参数写法（按兼容性从高到低）。
# 实测：LiteLLM 网关会拒绝顶层 "thinking"/"reasoning_effort" 键（报 UnsupportedParamsError），
# 但接受 vLLM 原生的 "enable_thinking"，以及放在 "extra_body" 里的嵌套写法。
_THINKING_PARAM_STYLES = [
    ("enable_thinking", {"enable_thinking": True}),
    ("extra_body.thinking", {"extra_body": {"thinking": {"type": "enabled"}}}),
    ("thinking", {"thinking": {"type": "enabled"}}),
]

# 关闭思考模式时的候选写法
_THINKING_OFF_STYLES = [
    ("enable_thinking=false", {"enable_thinking": False}),
    ("extra_body.thinking=disabled", {"extra_body": {"thinking": {"type": "disabled"}}}),
    ("thinking=disabled", {"thinking": {"type": "disabled"}}),
]


def _supports_thinking(model_name: str) -> bool:
    """判断模型是否可能支持思考模式（仅凭模型名推测，可能被服务端拒绝，故需配合降级重试）"""
    name_lower = (model_name or "").lower()
    if any(marker in name_lower for marker in _THINKING_OFF_MARKERS):
        return False
    return any(kw in name_lower for kw in _THINKING_MODEL_KEYWORDS)


def _apply_thinking_param(payload: dict, style_index: int, enabled: bool) -> str:
    """按给定序号往 payload 写入思考参数，返回使用的写法名。"""
    styles = _THINKING_PARAM_STYLES if enabled else _THINKING_OFF_STYLES
    name, params = styles[style_index % len(styles)]
    payload.update(params)
    return name


def _should_send_thinking_error_only(resp) -> bool:
    """响应失败且错误信息明确指向思考参数（用于最后的兜底重试判断）。"""
    return (
        not resp.ok
        and _is_unsupported_thinking_error(resp.status_code, resp.text)
    )


def _is_unsupported_thinking_error(status_code: int, body: str) -> bool:
    """
    判断一个 4xx 响应是否是「服务端不认识 thinking 参数」导致的。

    典型报错（LiteLLM 网关）：
        litellm.UnsupportedParamsError: openai does not support parameters: ['thinking']
    """
    if status_code not in (400, 422):
        return False
    body_lower = (body or "").lower()
    if "thinking" not in body_lower:
        return False
    return any(marker in body_lower for marker in _UNSUPPORTED_THINKING_MARKERS)


# 系统级提示：强制 AI 输出结构化结果，便于自动提取分数和调试。
# 由系统自动追加到每次评分请求中，用户无需手动维护。
FINAL_SCORE_INSTRUCTION = (
    "\n\n---\n"
    "【重要】评分指令：\n"
    "你必须严格按照以下格式输出，不要有任何例外：\n\n"
    "第一行：最终得分：X分（X为整数，这是唯一的得分依据）\n"
    "第二行：===反馈开始===\n"
    "第三行起：你的评分分析、扣分原因等调试信息\n"
    "最后一行：===反馈结束===\n\n"
    "示例：\n"
    "最终得分：4分\n"
    "===反馈开始===\n"
    "第1题得2分，第2题得1分，第3题得1分。\n"
    "===反馈结束===\n\n"
    "规则：\n"
    "- 第一行必须是「最终得分：X分」，X为整数\n"
    "- 禁止在第一行之前输出任何内容\n"
    "- 反馈信息中禁止输出emoji表情符号\n"
    "- 空白卷直接输出：最终得分：0分\n"
    "- 你只能依据学生实际写下的内容评分，不得补充或编写答案后评分\n"
    "- 截图中可能包含答题卡自带的印刷题目文字。你必须严格区分印刷文字和学生手写内容。只有学生手写的部分（通常为笔迹，与印刷字体明显不同）才是答案。印刷的题目文字、括号、下划线、横线等不是学生答案，不得作为评分依据\n"
    "- 如果截图中左侧有蓝色标签标注「空1」「空2」等编号，说明每个区域对应一个答案空位。你必须严格按照编号顺序识别每个空位的答案，即使某个空位为空也不能跳过，否则会导致后续空位答案错位"
)


# 接口类型选项 -> 是否使用 Responses API
_API_TYPE_AUTO = "auto"
_API_TYPE_CHAT = "chat"
_API_TYPE_RESPONSES = "responses"

_API_TYPE_CHOICES = {
    _API_TYPE_AUTO: "自动判断",
    _API_TYPE_CHAT: "Chat Completions",
    _API_TYPE_RESPONSES: "Responses API",
}


def normalize_api_type(api_type: str | None) -> str:
    """把界面上的接口类型取值规整为内部常量，非法值回落到自动判断。"""
    if not api_type:
        return _API_TYPE_AUTO
    normalized = str(api_type).strip().lower()
    # 容忍直接写 URL 后缀或英文值的各种写法
    if normalized in ("chat", "chat_completions", "chat-completions", "chatcompletion", "openai"):
        return _API_TYPE_CHAT
    if normalized in ("response", "responses", "responses_api", "responses-api", "responsesapi"):
        return _API_TYPE_RESPONSES
    return _API_TYPE_AUTO


def _is_responses_api_endpoint(base_url: str, api_type: str | None = None) -> bool:
    """判断是否走 Responses API。

    ``api_type`` 为显式设置时优先生效（自动/关闭用Chat Completions，开启用Responses API）；
    为自动判断时，沿用按域名识别的历史行为（火山引擎方舟等）。
    """
    resolved = normalize_api_type(api_type)
    if resolved == _API_TYPE_CHAT:
        return False
    if resolved == _API_TYPE_RESPONSES:
        return True
    return "volces.com" in (base_url or "").lower()


def _is_mimo_endpoint(base_url: str) -> bool:
    """检测是否为小米 MiMo 平台"""
    return "xiaomimimo.com" in base_url


def _build_auth_headers(
    api_key: str,
    base_url: str = "",
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    根据平台构建认证头。
    小米 MiMo 使用 api-key 头，其他平台使用标准 Bearer token。
    """
    headers = {"Content-Type": "application/json"}
    if _is_mimo_endpoint(base_url):
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(extra_headers or {})
    return headers


def _request_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    """对 API 超时和临时网络错误进行有限重试（超时与次数由全局请求策略控制）。

    ``timeout`` 未显式指定时使用当前策略中的超时值；重试次数同样实时读取策略，
    因此界面上的「请求设置」改完立即生效。
    """
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = int(_REQUEST_POLICY["timeout_seconds"])
    max_attempts = int(_REQUEST_POLICY["max_retries"]) + 1
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return requests.request(method, url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt >= max_attempts:
                break
            wait_seconds = _API_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"[API重试] 第 {attempt} 次请求失败：{e}，{wait_seconds:.1f} 秒后重试（最多重试 {max_attempts - 1} 次）")
            time.sleep(wait_seconds)
    raise last_error


def fetch_openai_compatible_models(
    base_url: str,
    api_key: str,
    extra_headers: Mapping[str, str] | None = None,
    timeout: int | None = None,
) -> list[str]:
    """从 OpenAI 兼容接口读取 /models，返回模型 id 列表。

    ``timeout`` 为 None 时使用全局请求策略中的超时值。
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url 不能为空")
    if not api_key:
        raise ValueError("api_key 不能为空")

    headers = _build_auth_headers(api_key, base_url, extra_headers)
    if re.search(r"/v\d+$", base_url):
        url = f"{base_url}/models"
    else:
        url = f"{base_url}/v1/models"

    resp = _request_with_retries("GET", url, headers=headers, timeout=timeout)
    if not resp.ok:
        try:
            detail = resp.json()
            print(f"[模型列表错误 {resp.status_code}] {json.dumps(detail, ensure_ascii=False)}")
        except Exception:
            if resp.text:
                print(f"[模型列表错误 {resp.status_code}] {resp.text[:500]}")
    resp.raise_for_status()
    data = resp.json()

    raw_models = data.get("data", []) if isinstance(data, dict) else []
    model_ids = []
    for item in raw_models:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or item.get("name")
        else:
            model_id = str(item)
        if model_id:
            model_ids.append(str(model_id))
    return sorted(set(model_ids), key=str.lower)


def call_llm_text(
  base_url: str, api_key: str, model: str, prompt: str,
  extra_headers: dict | None = None, timeout: int | None = None,
  api_type: str | None = None,
) -> str:
    """
    通用文本 LLM 调用，自动适配标准 OpenAI Chat Completions 和 Responses API（火山引擎）。
    ``api_type`` 可显式指定接口类型（auto/chat/responses），为空时按域名自动判断。
    ``timeout`` 为 None 时使用全局请求策略中的超时值。
    返回 AI 回复文本。
    """
    base_url = (base_url or "").strip().rstrip("/")
    headers = _build_auth_headers(api_key, base_url, extra_headers)

    if _is_responses_api_endpoint(base_url, api_type):
        url = f"{base_url}/responses"
        payload = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
        }
        resp = _request_with_retries("POST", url, headers=headers, json=payload, timeout=timeout)
        if not resp.ok:
            try:
                detail = resp.json()
                print(f"[API错误 {resp.status_code}] {json.dumps(detail, ensure_ascii=False)}")
            except Exception:
                if resp.text:
                    print(f"[API错误 {resp.status_code}] {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        # Responses API 返回格式：output[].content[].text
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content_item in item.get("content", []):
                    if content_item.get("type") == "output_text":
                        return content_item.get("text", "")
        return ""
    else:
        if re.search(r"/v\d+$", base_url):
            url = f"{base_url}/chat/completions"
        else:
            url = f"{base_url}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        resp = _request_with_retries("POST", url, headers=headers, json=payload, timeout=timeout)
        if not resp.ok:
            try:
                detail = resp.json()
                print(f"[API错误 {resp.status_code}] {json.dumps(detail, ensure_ascii=False)}")
            except Exception:
                if resp.text:
                    print(f"[API错误 {resp.status_code}] {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content", "")


class BaseScorer:
    """评分器基类：统一分数提取逻辑"""

    def __init__(self, model: str):
        self.model = model
        self.last_ai_response = None

    def extract_score(self, text):
        """提取分数，增强容错性"""
        text = text.strip()

        # 预处理：去除 Markdown 粗体/斜体标记，防止 `得 **3分**` 中数字被 `*` 隔断
        text = re.sub(r'\*+', '', text)

        # 判断 AI 是否遵循了结构化格式（含分隔符标记）
        has_structured_format = "===反馈开始===" in text

        # 从第一行提取最终得分
        first_line = text.split('\n')[0].strip()
        first_line_patterns = [
            r"最终得分[：:=\s]*(\d+)\.?\d*\s*分",
            r"总分\s*[：:]\s*(\d+)\.?\d*\s*分",
            r"[=\u2248]\s*.*?(\d+)\.?\d*\s*分",
        ]
        for pattern in first_line_patterns:
            match = re.search(pattern, first_line, re.IGNORECASE)
            if match:
                try:
                    score = float(match.group(1))
                    if 0 <= score <= 150:
                        return int(score)
                except (ValueError, IndexError):
                    continue

        # 如果有分隔符但第一行没提取到，不再回退全文（防止从反馈区误提分数）
        if has_structured_format:
            print("[分数提取] 检测到结构化格式但第一行未匹配到得分，跳过全文兜底")
            return 0

        # 以下为兜底逻辑：仅在 AI 未遵循结构化格式时生效（兼容旧格式）
        summary_patterns = [
            r"最终得分[：:=\s]*(\d+)\.?\d*\s*分",
            r"总分\s*[：:]\s*(\d+)\.?\d*\s*分",
            r"最终.*?得分?[：:=\s]*(\d+)\.?\d*\s*分",
            r"[预估预计][得评]分\s*[：:]\s*(\d+)\.?\d*\s*分",
            r"理论得分\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"合计\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"阅卷[结果分数]*\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"[=\u2248]\s*.*?(\d+)\.?\d*\s*分",
        ]
        for pattern in summary_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    score = float(match.group(1))
                    if 0 <= score <= 150:
                        return int(score)
                except (ValueError, IndexError):
                    continue
        
        # 优先级 1：明确的得分表述（带上下文约束）
        priority_patterns = [
            r"得\s*([\d]+\.?\d*)\s*分",
            r"给\s*([\d]+\.?\d*)\s*分",
            r"得分 [：:]\s*([\d]+\.?\d*)",
            r"评分 [：:]\s*([\d]+\.?\d*)",
            r"该题得分 [：:]\s*([\d]+\.?\d*)",
            r"本题得分 [：:]\s*([\d]+\.?\d*)",
        ]
        
        for pattern in priority_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    score = float(match.group(1))
                    if 0 <= score <= 150:  # 放宽上限到 150，适配不同总分题目
                        return int(score)
                except (ValueError, IndexError):
                    continue
        
        # 优先级 2：单独的数字 + 分（在句子末尾或关键位置）
        end_patterns = [
            r"[\s，,。\.]\s*([\d]+\.?\d*)\s*分 [。\.]?$",
            r"[\s，,。\.]\s*([\d]+\.?\d*)\s*分$",
            r"得分为\s*([\d]+\.?\d*)[。\.]?$",
        ]
        
        for pattern in end_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    score = float(match.group(1))
                    if 0 <= score <= 150:
                        return int(score)
                except (ValueError, IndexError):
                    continue
        
        # 优先级 3：纯数字行（AI 只返回分数的情况）
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if re.match(r'^[\d]+\.?\d*$', line):
                try:
                    score = float(line)
                    if 0 <= score <= 150:
                        return int(score)
                except ValueError:
                    continue
        
        # 优先级 4：提取所有数字，找最可能的分数
        # 排除常见非分数数字（题号、百分比等）
        numbers_with_context = []
        for match in re.finditer(r'([\d]+\.?\d*)', text):
            num = float(match.group(1))
            start = match.start()
            context_before = text[max(0, start-10):start].lower()
            context_after = text[match.end():min(len(text), match.end()+10)].lower()
            
            # 排除题号、百分比等
            if re.search(r'题 [一二三四五六七八九十\d]+|第 [一二三四五六七八九十\d]+ 题', context_before):
                continue
            if '%' in context_after or '%' in context_before:
                continue
            
            if 0 <= num <= 150:
                numbers_with_context.append((num, match.start()))
        
        # 按位置排序，优先取靠后的（通常 AI 会把分数放在最后）
        if numbers_with_context:
            numbers_with_context.sort(key=lambda x: x[1])
            # 取最后一个合理的数字
            return int(numbers_with_context[-1][0])
        
        return 0

    def get_last_response(self):
        return self.last_ai_response

    @staticmethod
    def _prepare_criteria(criteria: str) -> str:
        """自动将系统级格式要求追加到用户评分标准之后。"""
        if "最终得分" in criteria:
            return criteria
        return criteria + FINAL_SCORE_INSTRUCTION


class ZhipuAIScorer(BaseScorer):
    """智谱 AI 评分器"""

    def __init__(self, api_key, model="glm-4v"):
        try:
            import zhipuai  # 按需导入：不用智谱时不要求安装  # pyright: ignore[reportMissingImports]
        except Exception as e:
            raise ImportError("未安装 zhipuai：请先运行 pip install zhipuai") from e

        super().__init__(model=model)
        self.client = zhipuai.ZhipuAI(api_key=api_key)

    def grade_answer(self, image_path, criteria):
        criteria = self._prepare_criteria(criteria)
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": criteria},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                }
            ],
        )

        result = response.choices[0].message.content
        score = self.extract_score(result)

        self.last_ai_response = {
            "full_response": result,
            "score": score,
            "model": self.model,
            "timestamp": time.strftime("%H:%M:%S"),
            "provider": "zhipuai",
        }
        return score


class OpenAICompatibleScorer(BaseScorer):
    """
    通用 OpenAI 兼容接口评分器（自定义 base_url）
    兼容常见的 /v1/chat/completions 结构（含图文 messages）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers=None,
        timeout: int | None = None,
        enable_thinking: bool | None = None,
        api_type: str | None = None,
    ):
        """
        :param timeout:
            None 表示使用全局请求策略中的超时值（界面「API 配置 → 请求设置」可改）。
        :param enable_thinking:
            - ``True``  强制发送 thinking 参数
            - ``False`` 永不发送（部分 OpenAI 兼容网关会因未知参数返回 400）
            - ``None``  按模型名自动判断；若服务端拒绝则自动降级重试（默认）
        :param api_type:
            接口类型：``"auto"`` 按域名自动判断 / ``"chat"`` 强制 Chat Completions /
            ``"responses"`` 强制 Responses API。
        """
        super().__init__(model=model)
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        self.api_type = normalize_api_type(api_type)
        # None=自动；记录思考参数当前尝试到第几种写法，全部失败则放弃
        self.enable_thinking = enable_thinking
        self._thinking_style_index = 0
        self._thinking_given_up = False

        if not self.base_url:
            raise ValueError("base_url 不能为空（例如 https://api.openai.com）")
        if not self.api_key:
            raise ValueError("api_key 不能为空")

    def _should_send_thinking(self) -> bool:
        """最终决定本次请求是否携带 thinking 参数。"""
        if self._thinking_given_up:
            return False
        if self.enable_thinking is True:
            return True
        if self.enable_thinking is False:
            return False
        return _supports_thinking(self.model)

    def _use_responses_api(self) -> bool:
        """本评分器是否使用 Responses API。"""
        return _is_responses_api_endpoint(self.base_url, self.api_type)

    def grade_answer(self, image_path, criteria):
        criteria = self._prepare_criteria(criteria)
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")

        headers = _build_auth_headers(self.api_key, self.base_url, self.extra_headers)

        use_responses_api = self._use_responses_api()

        if use_responses_api:
            # Responses API（火山引擎方舟 / 支持 responses 的网关）
            # 注意：此处 image_url 必须是纯字符串，且必须带 detail 字段（可放在同一 content_item 上），
            # 否则后端 pydantic 校验会报 "Input should be a valid string" / "Field required"。
            url = f"{self.base_url}/responses"
            payload = {
                "model": self.model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": criteria},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "auto",
                            },
                        ],
                    }
                ],
                "temperature": 0.3,
            }
        else:
            # 标准 OpenAI Chat Completions
            if re.search(r"/v\d+$", self.base_url):
                url = f"{self.base_url}/chat/completions"
            else:
                url = f"{self.base_url}/v1/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": criteria},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        ],
                    }
                ],
            }
            if self._should_send_thinking():
                style_name = _apply_thinking_param(
                    payload, self._thinking_style_index, enabled=(self.enable_thinking is not False)
                )
                print(f"[思考模式] 已为模型 {self.model} 启用思考模式（参数写法：{style_name}）")

        try:
            resp = _request_with_retries("POST", url, headers=headers, json=payload, timeout=self.timeout)

            # 自动降级：服务端不认识当前思考参数写法时，依次换下一种写法重试；
            # 所有写法都失败才彻底放弃思考模式，避免整个阅卷流程因 400 中断。
            styles = _THINKING_PARAM_STYLES if self.enable_thinking is not False else _THINKING_OFF_STYLES
            guard = 0
            while (
                not resp.ok
                and self._should_send_thinking()
                and _is_unsupported_thinking_error(resp.status_code, resp.text)
                and guard < len(styles)
            ):
                guard += 1
                self._thinking_style_index += 1
                if self._thinking_style_index >= len(styles):
                    print(f"[思考模式] 所有参数写法均不被服务端接受，本次改用不带思考参数的模式")
                    self._thinking_given_up = True
                    break
                # 清掉上一种写法的残留键
                for stale in ("thinking", "enable_thinking", "reasoning_effort", "extra_body"):
                    payload.pop(stale, None)
                style_name = _apply_thinking_param(
                    payload, self._thinking_style_index, enabled=(self.enable_thinking is not False)
                )
                print(f"[思考模式] 服务端 {resp.status_code} 不接受该写法，改用「{style_name}」重试")
                resp = _request_with_retries("POST", url, headers=headers, json=payload, timeout=self.timeout)

            # 最后兜底：若仍因思考参数报错，则彻底去掉参数再试一次
            if (
                not resp.ok
                and _should_send_thinking_error_only(resp)
            ):
                for stale in ("thinking", "enable_thinking", "reasoning_effort", "extra_body"):
                    payload.pop(stale, None)
                self._thinking_given_up = True
                print("[思考模式] 已关闭思考参数重试")
                resp = _request_with_retries("POST", url, headers=headers, json=payload, timeout=self.timeout)

            if not resp.ok:
                try:
                    detail = resp.json()
                    print(f"[API错误 {resp.status_code}] {json.dumps(detail, ensure_ascii=False)}")
                except Exception:
                    if resp.text:
                        print(f"[API错误 {resp.status_code}] {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout as e:
            print(f"[API超时] {self.base_url} / {self.model}：{e}")
            self.last_ai_response = {
                "full_response": "",
                "score": None,
                "model": self.model,
                "timestamp": time.strftime("%H:%M:%S"),
                "provider": "openai_compatible",
                "error": "timeout",
            }
            raise TimeoutError(f"API超时: {self.base_url} / {self.model}") from e
        except requests.RequestException as e:
            print(f"[API请求失败] {self.base_url} / {self.model}：{e}")
            self.last_ai_response = {
                "full_response": "",
                "score": None,
                "model": self.model,
                "timestamp": time.strftime("%H:%M:%S"),
                "provider": "openai_compatible",
                "error": "request_exception",
            }
            raise ConnectionError(f"API请求失败: {self.base_url} / {self.model}") from e

        if use_responses_api:
            # 解析 Responses API 返回格式
            result = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content_item in item.get("content", []):
                        if content_item.get("type") == "output_text":
                            result = content_item.get("text", "")
                            break
                    if result:
                        break
        else:
            message = data["choices"][0]["message"]
            result = message.get("content", "") or ""
            # 思考模型：如果 content 为空但有 reasoning_content，记录日志
            if not result and message.get("reasoning_content"):
                print("[思考模式] 模型返回了思考内容但无最终答案，尝试从思考内容提取")
                result = message["reasoning_content"]
            # 记录思考内容用于调试（不参与分数提取）
            if message.get("reasoning_content"):
                thinking_preview = message["reasoning_content"][:200]
                print(f"[思考过程] {thinking_preview}...")
        score = self.extract_score(result)

        self.last_ai_response = {
            "full_response": result,
            "score": score,
            "model": self.model,
            "timestamp": time.strftime("%H:%M:%S"),
            "provider": "openai_compatible",
        }
        return score


class BaiduScorer(BaseScorer):
    """百度千帆 ERNIE 评分器（使用 API_Key:Secret_Key 换取 access_token）"""

    def __init__(self, api_key: str, model: str = "ernie-4.0-8k"):
        super().__init__(model=model)
        parts = api_key.split(":", 1)
        self.client_id = parts[0].strip()
        self.client_secret = parts[1].strip() if len(parts) > 1 else ""

    def _get_access_token(self) -> str:
        resp = _request_with_retries(
            "POST",
            "https://aip.baidubce.com/oauth/2.0/token",
            params={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            # timeout 交给全局请求策略（GUI「API 配置 → 请求设置」可调）
            timeout=None,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def grade_answer(self, image_path, criteria):
        criteria = self._prepare_criteria(criteria)
        access_token = self._get_access_token()
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        url = f"https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/completions?access_token={access_token}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": criteria},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                }
            ],
        }

        resp = _request_with_retries("POST", url, json=payload, timeout=None)
        resp.raise_for_status()
        data = resp.json()
        # 百度千帆的响应 key 是 "result"
        result = data.get("result", "")
        score = self.extract_score(result)

        self.last_ai_response = {
            "full_response": result,
            "score": score,
            "model": self.model,
            "timestamp": time.strftime("%H:%M:%S"),
            "provider": "baidu",
        }
        return score


class XunfeiScorer(BaseScorer):
    """科大讯飞 Spark 评分器（使用 appId:apiKey:apiSecret 签名认证）"""

    def __init__(self, api_key: str, model: str = "spark-v4.0"):
        super().__init__(model=model)
        parts = api_key.split(":", 2)
        self.app_id = parts[0].strip() if len(parts) > 0 else ""
        self.api_key = parts[1].strip() if len(parts) > 1 else ""
        self.api_secret = parts[2].strip() if len(parts) > 2 else ""
        # Spark 4.0 视觉版端点
        self._base_url = "https://spark-api.xf-yun.com/v4.0/chat"

    def grade_answer(self, image_path, criteria):
        criteria = self._prepare_criteria(criteria)
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        now = time.gmtime()
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", now)

        # 构建请求体
        payload = {
            "header": {"app_id": self.app_id},
            "parameter": {"chat": {"domain": "4.0Ultra", "temperature": 0.5, "max_tokens": 2048}},
            "payload": {
                "message": {
                    "text": [
                        {"role": "user", "content": criteria},
                    ]
                }
            },
        }

        import hashlib
        import hmac
        from urllib.parse import urlparse, quote

        # 构建签名
        url_obj = urlparse(self._base_url)
        host = url_obj.hostname
        path = url_obj.path

        digest_data = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            digest_data.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature_b64 = base64.b64encode(signature).decode("utf-8")

        authorization = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature_b64}"'
        )

        headers = {
            "Content-Type": "application/json",
            "Host": host,
            "Date": date,
            "Authorization": authorization,
        }

        resp = _request_with_retries("POST", self._base_url, json=payload, headers=headers, timeout=None)
        resp.raise_for_status()
        data = resp.json()

        # 提取 AI 回复文本
        result = ""
        if data.get("payload", {}).get("choices", {}).get("text"):
            result = data["payload"]["choices"]["text"][0].get("content", "")
        score = self.extract_score(result)

        self.last_ai_response = {
            "full_response": result,
            "score": score,
            "model": self.model,
            "timestamp": time.strftime("%H:%M:%S"),
            "provider": "xunfei",
        }
        return score
