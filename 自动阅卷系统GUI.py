#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动网上阅卷系统核心模块
支持截图、AI评分、自动填分和批量处理流程
"""

import threading
import time
from importlib.util import find_spec
from pathlib import Path
from typing import Mapping

from modules.自动填分模块 import AutoFiller, ScoreMismatchError
from modules.自动截图模块 import ScreenshotTool
from modules.自动评分模块 import OpenAICompatibleScorer, ZhipuAIScorer


def check_dependencies(required_modules: Mapping[str, str] | None = None) -> tuple[bool, list[str]]:
    if required_modules is None:
        required_modules = {
            "pyautogui": "pyautogui",
            "PIL": "Pillow",
            "requests": "requests",
        }
    missing = [pkg_name for module_name, pkg_name in required_modules.items() if find_spec(module_name) is None]
    if missing:
        return False, missing
    return True, []


class AutoScoringSystem:
    """自动阅卷系统主类（流程编排层）"""

    def __init__(
        self,
        root,
        api_key,
        criteria,
        model="glm-4v",
        batch_mode=False,
        scorer=None,
        capture_dir=None,
        filler_mode="pyautogui",
        filler_config=None,
        on_score_callback=None,
        on_region_selected=None,
        on_position_selected=None,
        before_capture=None,
        after_capture=None,
        blank_threshold=15.0,
        cross_checker=None,
        on_notify=None,
    ):
        self.screenshot_tool = ScreenshotTool(
            on_region_selected=on_region_selected,
            before_capture=before_capture,
            after_capture=after_capture,
        )
        self.scorer = scorer if scorer is not None else ZhipuAIScorer(api_key, model)
        # 多模型交叉校验器（None 表示未启用）；启用后由它完成评分并返回最终分数
        self.cross_checker = cross_checker
        # 通知回调（弹窗+系统通知）：由上层注入的线程安全实现，None 表示不通知
        self.on_notify = on_notify
        self.filler = AutoFiller(root, mode=filler_mode, config=filler_config or {}, on_position_selected=on_position_selected)
        self.criteria = criteria
        self.running = False
        self.thread = None
        self.batch_mode = batch_mode
        self.question_count = 0
        self.total_questions = 0
        self.capture_dir = Path(capture_dir) if capture_dir else Path(__file__).with_name("captures")
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.on_score_callback = on_score_callback
        self.blank_threshold = float(blank_threshold)

    @staticmethod
    def _is_blank_image(image_path: str, threshold: float = 15.0) -> bool:
        """检测图片是否接近空白（基于像素亮度标准差）。阈值 15 以下判定为空白。"""
        from PIL import Image, ImageStat

        with Image.open(image_path) as img:
            stat = ImageStat.Stat(img.convert("L"))
        return float(stat.stddev[0]) < threshold

    def _notify(self, title: str, message: str, level: str = "info"):
        """发送弹窗+系统通知（回调由上层注入，负责线程安全与降级）。"""
        print(f"[通知] {title}：{message.replace(chr(10), ' ')}")
        callback = self.on_notify
        if callback is None:
            return
        try:
            callback(title, message, level)
        except Exception as e:
            print(f"[通知] 发送失败：{e}")

    def _process_one_question(self, question_index=None):
        started = time.perf_counter()
        image = self.screenshot_tool.capture_current_question()
        qid = question_index if question_index is not None else "single"
        filename = str(self.capture_dir / f"question_{qid}_{int(time.time())}.jpg")
        self.screenshot_tool.save_image(image, filename)

        # 空白卷检测：跳过 AI 节省开销，直接给 0 分
        if self._is_blank_image(filename, threshold=self.blank_threshold):
            print(f"[空白检测] 题目 {qid} 截图接近空白（阈值 {self.blank_threshold:.1f}），直接判定 0 分，跳过 AI 评分")
            self.filler.config["batch_mode"] = self.batch_mode
            self.filler.fill_score(0)
            return 0

        # 评分：启用多模型交叉校验时，由校验器并行批改并裁决最终分数
        if self.cross_checker is not None:
            score = self.cross_checker.grade(filename, self.criteria)
            response_info = self.cross_checker.get_last_adopted_response() or self.scorer.get_last_response()
        else:
            score = self.scorer.grade_answer(filename, self.criteria)
            response_info = self.scorer.get_last_response()

        print(f"评分结果：{score}分")
        if response_info:
            # 记录本题（截图→AI 评分）实际耗时，供评分记录展示「用时」
            elapsed = round(time.perf_counter() - started, 1)
            if isinstance(response_info, dict):
                response_info.setdefault("elapsed_seconds", elapsed)
            if question_index is None:
                print(f"当前题目评分：{score}分")
            else:
                print(f"题目 {question_index} 评分：{score}分")
            full_text = response_info['full_response']
            if "===反馈开始===" in full_text and "===反馈结束===" in full_text:
                feedback = full_text.split("===反馈开始===")[1].split("===反馈结束===")[0].strip()
                print(f"AI反馈信息：{feedback}")
            else:
                print(f"AI返回信息：{full_text}")
        if self.on_score_callback and response_info:
            try:
                try:
                    self.on_score_callback(question_index, score, response_info, filename)
                except TypeError:
                    self.on_score_callback(question_index, score, response_info)
            except Exception as e:
                print(f"[回调错误] {e}")
        self.filler.config["batch_mode"] = self.batch_mode
        self.filler.fill_score(score)
        return score

    def _handle_run_exception(self, error):
        print(f"错误：{error}")
        import traceback

        traceback.print_exc()
        if isinstance(error, ScoreMismatchError):
            print("[停止] 回评/二评分数不一致，阅卷已终止")
            self.running = False
        elif isinstance(error, (TimeoutError, ConnectionError)):
            print("[停止] API 连接异常，阅卷已终止")
            self.running = False
            self._notify(
                "API 无响应",
                f"API 无响应，本轮阅卷已终止（不会误填 0 分）。\n\n错误详情：{error}",
                level="warning",
            )

    def run(self):
        if not self.batch_mode:
            try:
                score = self._process_one_question()
                self.running = False
                print("阅卷完成，非批量模式只执行一次")
                self._notify("任务完成", f"单题阅卷完成，评分 {score} 分。")
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                self._handle_run_exception(e)
            return

        while self.running:
            try:
                # 检查是否已达到设定份数
                if self.total_questions > 0 and self.question_count >= self.total_questions:
                    print(f"已完成 {self.question_count} 份，达到设定数量，停止运行")
                    self._notify("任务完成", f"批量阅卷完成，共处理 {self.question_count} 份。")
                    break
                self._process_one_question(self.question_count + 1)
                self.question_count += 1
                if self.total_questions > 0:
                    progress = (self.question_count / self.total_questions) * 100
                    print(f"进度：{progress:.1f}% ({self.question_count}/{self.total_questions})")
                time.sleep(2)
            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                self._handle_run_exception(e)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and isinstance(self.thread, threading.Thread):
            self.thread.join(timeout=2)
        self.filler.close()


if __name__ == "__main__":
    print("当前脚本已精简为核心模块，请由上层 GUI 程序调用。")
