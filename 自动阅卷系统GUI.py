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

from modules.自动填分模块 import AutoFiller
from modules.自动截图模块 import ScreenshotTool
from modules.自动评分模块 import OpenAICompatibleScorer, ZhipuAIScorer


def check_dependencies(required_modules=None):
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
    ):
        self.screenshot_tool = ScreenshotTool()
        self.scorer = scorer if scorer is not None else ZhipuAIScorer(api_key, model)
        self.filler = AutoFiller(root, mode=filler_mode, config=filler_config or {})
        self.criteria = criteria
        self.running = False
        self.thread = None
        self.batch_mode = batch_mode
        self.question_count = 0
        self.total_questions = 0
        self.capture_dir = Path(capture_dir) if capture_dir else Path(__file__).with_name("captures")
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.on_score_callback = on_score_callback

    def _process_one_question(self, question_index=None):
        image = self.screenshot_tool.capture_current_question()
        qid = question_index if question_index is not None else "single"
        filename = str(self.capture_dir / f"question_{qid}_{int(time.time())}.jpg")
        self.screenshot_tool.save_image(image, filename)

        score = self.scorer.grade_answer(filename, self.criteria)
        print(f"评分结果：{score}分")
        response_info = self.scorer.get_last_response()
        if response_info:
            if question_index is None:
                print(f"当前题目评分：{score}分")
            else:
                print(f"题目 {question_index} 评分：{score}分")
            print(f"AI返回信息：{response_info['full_response']}")
        if self.on_score_callback and response_info:
            try:
                self.on_score_callback(question_index, score, response_info)
            except Exception as e:
                print(f"[回调错误] {e}")
        self.filler.fill_score(score)

    def _handle_run_exception(self, error):
        print(f"错误：{error}")
        import traceback

        traceback.print_exc()
        time.sleep(5)

    def run(self):
        if not self.batch_mode:
            try:
                self._process_one_question()
                self.running = False
                print("阅卷完成，非批量模式只执行一次")
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
