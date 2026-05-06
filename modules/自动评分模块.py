#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动评分模块
负责调用多种 AI 接口并提取分数。
"""

import base64
import re
import time

import requests


class BaseScorer:
    """评分器基类：统一分数提取逻辑"""

    def __init__(self, model: str):
        self.model = model
        self.last_ai_response = None

    def extract_score(self, text):
        """提取分数，增强容错性"""
        text = text.strip()
        
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
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")

        # base_url 可能已经包含版本号（例如 /v2），尽量自动适配
        if self.base_url.endswith("/v1") or self.base_url.endswith("/v2"):
            url = f"{self.base_url}/chat/completions"
        else:
            url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

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
        resp.raise_for_status()
        data = resp.json()

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