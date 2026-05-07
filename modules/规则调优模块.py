#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则调优模块
利用大模型分析评分记录，自动优化评分规则。
"""

from typing import Optional

import requests


class ScoringRecord:
    """单次评分记录"""

    def __init__(self, index, ai_score, ai_response, criteria="", image_path="", manual_score=None):
        self.index = index
        self.ai_score = ai_score
        self.ai_response = ai_response
        self.criteria = criteria
        self.image_path = image_path
        self.manual_score = manual_score

    @property
    def status(self):
        if self.manual_score is None:
            return "待标记"
        return "✓" if self.manual_score == self.ai_score else "✗ 偏差"


class RuleTuner:
    """规则调优器"""

    def __init__(self, api_key="", base_url="", model="", extra_headers=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_headers = extra_headers or {}
        self.records: list[ScoringRecord] = []
        self.suggested_criteria = ""

    def update_config(self, api_key="", base_url="", model="", extra_headers=None):
        """同步 API 配置"""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_headers = extra_headers or {}

    def add_record(self, record: ScoringRecord):
        self.records.append(record)

    def set_manual_score(self, index, manual_score) -> bool:
        for r in self.records:
            if r.index == index:
                r.manual_score = manual_score
                return True
        return False

    def _call_llm(self, prompt: str) -> str:
        # base_url 可能已经包含版本号（例如 /v2），自动适配
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
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def tune(self, original_criteria: str) -> Optional[str]:
        """执行规则调优，返回完整分析文本（含错误分析和优化后规则）"""
        mismatches = [
            r for r in self.records
            if r.manual_score is not None and r.manual_score != r.ai_score
        ]
        matches = [
            r for r in self.records
            if r.manual_score is not None and r.manual_score == r.ai_score
        ]

        if not mismatches and len(matches) < 2:
            return None  # 数据不够

        examples = []
        for i, r in enumerate(mismatches, 1):
            examples.append(
                f"示例 {i}（偏差）:\n"
                f"- AI 评分: {r.ai_score}分\n"
                f"- 正确分数: {r.manual_score}分\n"
                f"- AI 评分思考过程: {r.ai_response}\n"
                "---"
            )

        for i, r in enumerate(matches, len(mismatches) + 1):
            examples.append(
                f"示例 {i}（正确）:\n"
                f"- AI 评分: {r.ai_score}分（正确）\n"
                f"- AI 评分思考过程: {r.ai_response}\n"
                "---"
            )

        prompt = (
            "你是一个专业的考试评分规则优化专家。请分析以下AI评分与人工评分之间的差异，"
            "找出评分规则中的不足，并给出优化后的评分规则。\n\n"
            f"## 当前评分规则\n{original_criteria}\n\n"
            f"## 评分示例（共 {len(examples)} 条）\n\n"
            + "\n".join(examples)
            + "\n\n## 任务\n"
            "请分析：\n"
            "1. AI 评分出错的原因（评分规则不明确、缺少关键判断标准、歧义等）\n"
            "2. 评分规则需要如何优化才能避免这些错误\n\n"
            "然后给出**优化后的评分规则**。要求：\n"
            "- 保留原有规则的合理部分\n"
            "- 补充避免上述错误的关键判断标准\n"
            "- 更加明确、具体、可操作\n"
            "- 保持格式清晰易读\n\n"
            "## 返回格式\n"
            "【错误分析】\n"
            "（你的分析）\n\n"
            "【优化后的评分规则】\n"
            "（完整的新评分规则）"
        )

        result = self._call_llm(prompt)

        # 提取优化后的规则
        if "【优化后的评分规则】" in result:
            parts = result.split("【优化后的评分规则】")
            self.suggested_criteria = parts[-1].strip().rstrip("---").strip()
        else:
            self.suggested_criteria = result

        return result

    def get_stats(self) -> dict:
        total = len(self.records)
        marked = sum(1 for r in self.records if r.manual_score is not None)
        mismatches = sum(
            1 for r in self.records
            if r.manual_score is not None and r.manual_score != r.ai_score
        )
        return {"total": total, "marked": marked, "mismatches": mismatches}