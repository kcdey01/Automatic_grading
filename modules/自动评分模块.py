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

import requests

# 系统级提示：强制 AI 在回复末尾输出标准格式的最终得分，提升自动提取准确率。
# 由系统自动追加到每次评分请求中，用户无需手动维护。
FINAL_SCORE_INSTRUCTION = (
    "\n\n---\n"
    "【重要】评分结束后，你必须在回复的最后一行，以如下精确格式输出最终得分（不要附加任何其他文字）：\n"
    "最终得分：X分\n"
    "其中 X 为整数，代表该份答卷的总得分。\n"
    "【重要】空白卷处理：如果学生答卷为空白、无任何作答内容、仅有印刷题目或无法识别任何学生笔迹，你必须直接给出0分，且不得编写参考答案、示例答案或补全内容后评分。你只能依据学生实际写下的内容评分。"
)


def _is_responses_api_endpoint(base_url: str) -> bool:
    """检测是否为 Responses API 端点（火山引擎方舟等使用）"""
    return "volces.com" in base_url


def _is_mimo_endpoint(base_url: str) -> bool:
    """检测是否为小米 MiMo 平台"""
    return "xiaomimimo.com" in base_url


def _build_auth_headers(api_key: str, base_url: str = "", extra_headers: dict | None = None) -> dict:
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


def call_llm_text(
    base_url: str, api_key: str, model: str, prompt: str,
    extra_headers: dict | None = None, timeout: int = 120,
) -> str:
    """
    通用文本 LLM 调用，自动适配标准 OpenAI Chat Completions 和 Responses API（火山引擎）。
    返回 AI 回复文本。
    """
    base_url = (base_url or "").strip().rstrip("/")
    headers = _build_auth_headers(api_key, base_url, extra_headers)

    if _is_responses_api_endpoint(base_url):
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
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
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
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
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

        # 空白卷兜底：检测 AI 是否自行编写了参考答案（正常评分回复不应出现这些词）
        reference_keywords = ["参考答案", "示例答案", "建议答案", "标准答案", "正确答案"]
        if any(kw in text for kw in reference_keywords):
            print("[输出拦截] AI 回复包含参考答案关键词，判定为空白卷，强制 0 分")
            return 0

        # 优先级 0：明确的最终/总分表述（最高优先级，避免被中间小分干扰）
        summary_patterns = [
            r"最终得分[：:=\s]*(\d+)\.?\d*\s*分",
            r"总分\s*[：:]\s*(\d+)\.?\d*\s*分",
            r"最终.*?得分?[：:=\s]*(\d+)\.?\d*\s*分",
            r"[预估预计][得评]分\s*[：:]\s*(\d+)\.?\d*\s*分",
            r"理论得分\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"合计\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"阅卷[结果分数]*\s*[：:\s]*(\d+)\.?\d*\s*分",
            r"[=\u2248]\s*.*?(\d+)\.?\d*\s*分",  # 匹配 = 7分 / ≈ 7分 等格式
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

    def __init__(self, base_url: str, api_key: str, model: str, extra_headers=None, timeout=60):
        super().__init__(model=model)
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

        if not self.base_url:
            raise ValueError("base_url 不能为空（例如 https://api.openai.com）")
        if not self.api_key:
            raise ValueError("api_key 不能为空")

    def grade_answer(self, image_path, criteria):
        criteria = self._prepare_criteria(criteria)
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")

        headers = _build_auth_headers(self.api_key, self.base_url, self.extra_headers)

        if _is_responses_api_endpoint(self.base_url):
            # 火山引擎方舟 Responses API（/api/v3/responses）
            url = f"{self.base_url}/responses"
            payload = {
                "model": self.model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": criteria},
                            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64_image}"},
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

        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        if not resp.ok:
            try:
                detail = resp.json()
                print(f"[API错误 {resp.status_code}] {json.dumps(detail, ensure_ascii=False)}")
            except Exception:
                if resp.text:
                    print(f"[API错误 {resp.status_code}] {resp.text[:500]}")
            resp.raise_for_status()
        data = resp.json()

        if _is_responses_api_endpoint(self.base_url):
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
            result = data["choices"][0]["message"].get("content", "")
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
        resp = requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
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

        resp = requests.post(url, json=payload, timeout=120)
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

        resp = requests.post(self._base_url, json=payload, headers=headers, timeout=120)
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