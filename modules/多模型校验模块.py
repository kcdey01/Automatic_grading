#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多模型交叉校验模块

对同一题由主模型与 1~2 个附加模型**并行批改**，比对各自返回的分数：
- 分数一致（最大差值不超过容差）：直接放行，采用主模型分数
- 分数不一致：进入第三轮校验
    * 独立仲裁：由仲裁模型独立重评（不参考前几轮结果），以其分数为准
    * 参考重评：由主模型带着前几轮的分数与评语重新评一次，以其分数为准
  第三轮模式为「自动选择」时：配置了仲裁模型则独立仲裁，否则主模型参考重评。

失败降级策略：
- 主模型评分失败：保持系统原有行为，把原异常抛回上层（超时/连接异常会触发停止阅卷）
- 附加/仲裁模型失败：记录警告后降级继续；只剩主模型一个有效分数时直接采用主模型分数
- 第三轮（重评/仲裁）失败：回退采用主模型第一轮分数，并在日志中提示人工复核
"""

import time
from concurrent.futures import ThreadPoolExecutor

from modules.自动评分模块 import FINAL_SCORE_INSTRUCTION


class MultiModelCrossChecker:
    """多模型交叉校验器（主模型 + 1~2 个附加模型 + 可选仲裁模型）。"""

    ROUND3_AUTO = "auto"
    ROUND3_REREVIEW = "rereview"
    ROUND3_ARBITER = "arbiter"

    def __init__(self, primary_scorer, extra_scorers=None, arbiter_scorer=None,
                 tolerance=0, round3_mode=ROUND3_AUTO):
        """
        :param primary_scorer: 主模型评分器（始终参与，需实现 grade_answer / get_last_response / model）
        :param extra_scorers:  附加模型评分器列表（1~2 个）
        :param arbiter_scorer: 仲裁模型评分器（可选，第三轮独立仲裁用）
        :param tolerance:      分数容差，0 表示必须完全一致
        :param round3_mode:    "auto"（自动选择）/ "rereview"（主模型参考重评）/ "arbiter"（独立仲裁）
        """
        self.primary_scorer = primary_scorer
        self.extra_scorers = list(extra_scorers or [])
        self.arbiter_scorer = arbiter_scorer
        try:
            self.tolerance = max(0, int(tolerance))
        except (TypeError, ValueError):
            self.tolerance = 0
        self.round3_mode = round3_mode or self.ROUND3_AUTO
        self.last_details = None
        self._last_adopted_response = None

    # ───────────────────────── 对外接口 ─────────────────────────

    def grade(self, image_path, criteria) -> int:
        """并行批改同一题 → 比对分数 →（不一致时）第三轮校验，返回最终分数。"""
        started = time.time()
        participants = [("主模型", "primary", self.primary_scorer)]
        for i, scorer in enumerate(self.extra_scorers, 1):
            participants.append((f"附加模型{i}", "extra", scorer))

        print(
            "[交叉校验] 开始并行批改，共 "
            + str(len(participants))
            + " 个模型："
            + "、".join(f"{name}（{getattr(p, 'model', '?')}）" for name, _, p in participants)
            + f"；容差 {self.tolerance} 分"
        )

        results = self._grade_parallel(participants, image_path, criteria)
        for r in results:
            if r["score"] is None:
                print(f"[交叉校验] {r['name']}（{r['model']}）评分失败：{r['error']}")
            else:
                print(f"[交叉校验] {r['name']}（{r['model']}）给出 {r['score']} 分（用时 {r['elapsed']:.1f}s）")

        primary_result = results[0]
        if primary_result["score"] is None:
            # 主模型失败：保持系统原有行为，把原异常抛回上层
            error = primary_result.get("error_object") or RuntimeError(primary_result["error"] or "主模型评分失败")
            raise error

        ok_results = [r for r in results if r["score"] is not None]
        if len(ok_results) < len(results):
            print(f"[交叉校验] 注意：{len(results) - len(ok_results)} 个模型评分失败，按剩余 {len(ok_results)} 个模型的分数比对")

        scores = [r["score"] for r in ok_results]
        consistent = (max(scores) - min(scores)) <= self.tolerance

        details = {
            "tolerance": self.tolerance,
            "consistent": consistent,
            "scores": self._serializable(results),
            "round3": None,
        }

        if consistent:
            final_score = primary_result["score"]
            if len(ok_results) >= 2:
                source = "多模型一致"
                flow = "首轮并行批改·分数一致放行"
                print(f"[交叉校验] 分数一致 {scores}，放行，最终采用主模型分数：{final_score} 分")
            else:
                source = "仅主模型可用"
                flow = "首轮并行批改·仅主模型可用"
                print(f"[交叉校验] 无其他有效模型可比对，采用主模型分数：{final_score} 分")
            adopted_response = primary_result.get("response")
        else:
            print(f"[交叉校验] 分数不一致 {scores}，进入第三轮校验")
            final_score, round3_info, adopted_response = self._third_round(image_path, criteria, results)
            details["round3"] = round3_info
            source = round3_info["source"]
            round3_label = {
                "独立仲裁": "独立仲裁",
                "主模型参考重评": "参考重评",
            }.get(source, source)
            flow = f"首轮并行批改·分数不一致 → 第三轮{round3_label}"

        details["final_score"] = final_score
        details["final_source"] = source
        details["flow"] = flow
        details["elapsed_total"] = round(time.time() - started, 1)
        self.last_details = details
        self._last_adopted_response = self._make_response(adopted_response, details)
        print(f"[交叉校验] 结束：{flow}，最终 {final_score} 分（总用时 {details['elapsed_total']}s）")
        return final_score

    def get_last_adopted_response(self):
        """返回最终采纳的响应信息（附带 cross_check 明细），供上层展示/记录。"""
        return self._last_adopted_response

    # ───────────────────────── 并行批改 ─────────────────────────

    def _grade_parallel(self, participants, image_path, criteria):
        """并行调用所有评分器；单个模型失败不影响其他模型。"""

        def _worker(participant):
            name, role, scorer = participant
            entry = {
                "name": name,
                "role": role,
                "model": str(getattr(scorer, "model", "") or ""),
                "score": None,
                "error": None,
                "error_object": None,
                "response": None,
                "elapsed": 0.0,
            }
            t0 = time.time()
            try:
                score = scorer.grade_answer(image_path, criteria)
                if score is None:
                    raise ValueError("模型未返回有效分数")
                entry["score"] = int(score)
                # 立刻取回响应快照，避免后续重评调用覆盖首轮响应
                entry["response"] = scorer.get_last_response()
            except Exception as e:  # noqa: BLE001 - 单模型失败降级继续
                entry["error"] = f"{type(e).__name__}: {e}"
                entry["error_object"] = e
            entry["elapsed"] = time.time() - t0
            return entry

        if len(participants) <= 1:
            return [_worker(participants[0])]
        with ThreadPoolExecutor(max_workers=len(participants)) as pool:
            futures = [pool.submit(_worker, p) for p in participants]
            return [f.result() for f in futures]

    # ───────────────────────── 第三轮校验 ─────────────────────────

    def _resolve_round3_plan(self):
        """根据模式与仲裁模型配置，决定第三轮用哪种方式。"""
        if self.round3_mode == self.ROUND3_ARBITER:
            return self.ROUND3_ARBITER
        if self.round3_mode == self.ROUND3_REREVIEW:
            return self.ROUND3_REREVIEW
        # auto：配了仲裁模型就用独立仲裁，否则退回参考重评
        return self.ROUND3_ARBITER if self.arbiter_scorer is not None else self.ROUND3_REREVIEW

    def _third_round(self, image_path, criteria, results):
        if self._resolve_round3_plan() == self.ROUND3_ARBITER:
            return self._arbiter_round(image_path, criteria, results)
        return self._rereview_round(image_path, criteria, results)

    def _arbiter_round(self, image_path, criteria, results):
        arbiter = self.arbiter_scorer
        print(f"[交叉校验] 第三轮：仲裁模型（{getattr(arbiter, 'model', '?')}）独立重评（不参考前几轮结果）")
        try:
            score = arbiter.grade_answer(image_path, criteria)
            if score is None:
                raise ValueError("仲裁模型未返回有效分数")
            score = int(score)
        except Exception as e:  # noqa: BLE001 - 仲裁失败时回退参考重评
            print(f"[交叉校验] 仲裁模型失败（{type(e).__name__}: {e}），回退为主模型参考重评")
            return self._rereview_round(image_path, criteria, results)
        print(f"[交叉校验] 仲裁模型最终分数：{score} 分")
        info = {
            "mode": "arbiter",
            "model": str(getattr(arbiter, "model", "") or ""),
            "score": score,
            "source": "独立仲裁",
        }
        return score, info, arbiter.get_last_response()

    def _rereview_round(self, image_path, criteria, results):
        primary = self.primary_scorer
        print(f"[交叉校验] 第三轮：主模型（{getattr(primary, 'model', '?')}）参考前几轮分数与评语重评")
        prompt = self._build_rereview_prompt(criteria, results)
        try:
            score = primary.grade_answer(image_path, prompt)
            if score is None:
                raise ValueError("主模型重评未返回有效分数")
            score = int(score)
        except Exception as e:  # noqa: BLE001 - 重评失败时回退首轮分数
            fallback = results[0]["score"]
            print(
                f"[交叉校验] 主模型重评失败（{type(e).__name__}: {e}），"
                f"回退采用主模型第一轮分数 {fallback} 分，请人工留意复核"
            )
            info = {
                "mode": "rereview",
                "model": str(getattr(primary, "model", "") or ""),
                "score": fallback,
                "error": f"{type(e).__name__}: {e}",
                "source": "重评失败·回退首轮分数",
            }
            return fallback, info, results[0].get("response")
        print(f"[交叉校验] 主模型重评最终分数：{score} 分")
        info = {
            "mode": "rereview",
            "model": str(getattr(primary, "model", "") or ""),
            "score": score,
            "source": "主模型参考重评",
        }
        return score, info, primary.get_last_response()

    # ───────────────────────── 辅助方法 ─────────────────────────

    @staticmethod
    def _feedback_excerpt(response, limit=400):
        text = ""
        if isinstance(response, dict):
            text = str(response.get("full_response") or "")
        if not text:
            return "（无反馈文本）"
        text = text.replace("\r", "").strip()
        if len(text) > limit:
            text = text[:limit] + "…（略）"
        return text

    def _build_rereview_prompt(self, criteria, results):
        """构造「主模型参考重评」的提示词：原始评分标准 + 前几轮分数与评语摘要 + 输出格式要求。"""
        lines = []
        for r in results:
            if r["score"] is None:
                lines.append(f"- {r['name']}（{r['model']}）：评分失败，无意见")
            else:
                excerpt = self._feedback_excerpt(r.get("response"))
                lines.append(f"- {r['name']}（{r['model']}）：给出 {r['score']} 分。其反馈：{excerpt}")

        reference_block = (
            "\n\n---\n"
            "【第三轮校验】\n"
            "此前已有多个评阅者对本卷给出意见，但分数不一致：\n"
            + "\n".join(lines)
            + "\n\n请你重新独立审视答卷原文，参考上述分歧，给出你自己的最终裁决。\n"
            "严格按以下格式输出，不要有任何例外：\n"
            "第一行：最终得分：X分（X为整数）\n"
            "第二行：===反馈开始===\n"
            "接着写你的分析与裁决理由\n"
            "最后一行：===反馈结束===\n"
            "- 禁止在第一行之前输出任何内容\n"
            "- 你是最终的裁决者，该分数将直接采用，请务必严谨\n"
        )
        prompt = criteria
        if "最终得分" not in prompt:
            prompt = prompt + FINAL_SCORE_INSTRUCTION
        return prompt + reference_block

    @staticmethod
    def _serializable(results):
        return [
            {
                "name": r["name"],
                "role": r["role"],
                "model": r["model"],
                "score": r["score"],
                "error": r["error"],
                "elapsed_seconds": round(r["elapsed"], 1),
            }
            for r in results
        ]

    @staticmethod
    def _make_response(base_response, details):
        if isinstance(base_response, dict):
            response = dict(base_response)
        else:
            response = {"full_response": "", "score": None, "model": "", "provider": ""}
        response["cross_check"] = details
        return response
