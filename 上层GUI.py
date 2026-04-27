#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上层 GUI：调用 `自动阅卷系统GUI.py` 核心模块完成阅卷。

运行：
python 上层GUI.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import json
from pathlib import Path
import queue
import sys
import threading
import time

from 自动阅卷系统GUI import AutoScoringSystem, OpenAICompatibleScorer, check_dependencies
from modules.自动填分模块 import AutoFiller


class _QueueStdout:
    def __init__(self, q: "queue.Queue[str]"):
        self._q = q

    def write(self, s: str):
        if s:
            self._q.put(s)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("自动阅卷 - 上层GUI")
        self.geometry("800x600")
        self.attributes("-topmost", True)
        self.after(200, self.lift)

        self.config_path = Path(__file__).with_name("config.json")
        self.capture_dir = Path(__file__).with_name("captures")
        self.capture_dir.mkdir(parents=True, exist_ok=True)

        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _QueueStdout(self._log_q)
        sys.stderr = _QueueStdout(self._log_q)

        self.system: AutoScoringSystem | None = None
        self._system_cfg_key: str | None = None
        self._ui_thread_guard: threading.Lock = threading.Lock()

        self._build_ui()
        self._load_config(silent=True)
        self.after(60, self._drain_log_queue)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        ttk.Label(top, text="服务商").grid(row=0, column=0, sticky="w")
        self.provider_var = tk.StringVar(value="OpenAI兼容")
        ttk.Combobox(
            top,
            textvariable=self.provider_var,
            width=12,
            values=["OpenAI兼容", "智谱AI"],
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(top, text="API Key").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.api_key_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.api_key_var, width=45, show="*").grid(row=0, column=3, sticky="we", padx=(6, 0))

        ttk.Label(top, text="模型").grid(row=1, column=0, sticky="w")
        self.model_var = tk.StringVar(value="gpt-4o-mini")
        ttk.Entry(top, textvariable=self.model_var, width=25).grid(row=1, column=1, sticky="w", padx=(6, 0))

        ttk.Label(top, text="base_url(OpenAI兼容)").grid(row=1, column=2, sticky="w", padx=(12, 0))
        self.base_url_var = tk.StringVar(value="https://api.openai.com")
        self.base_url_entry = ttk.Entry(top, textvariable=self.base_url_var, width=35)
        self.base_url_entry.grid(row=1, column=3, sticky="we", padx=(6, 0))

        ttk.Label(top, text="额外请求头JSON(可选)").grid(row=2, column=0, sticky="w")
        self.extra_headers_var = tk.StringVar(value="")
        self.extra_headers_entry = ttk.Entry(top, textvariable=self.extra_headers_var, width=90)
        self.extra_headers_entry.grid(row=2, column=1, columnspan=3, sticky="we", padx=(6, 0))

        top.columnconfigure(3, weight=1)

        self.provider_var.trace_add("write", lambda *_: self._sync_provider_state())
        self._sync_provider_state()

        criteria_frame = ttk.LabelFrame(self, text="评分标准（直接粘贴你的阅卷要求/评分细则）")
        criteria_frame.pack(fill=tk.BOTH, expand=False, **pad)
        self.criteria_text = tk.Text(criteria_frame, height=8, wrap="word")
        self.criteria_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.X, **pad)

        self.batch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mid, text="批量模式", variable=self.batch_var, command=self._sync_batch_state).grid(row=0, column=0, sticky="w")

        ttk.Label(mid, text="总题数(可选)").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.total_var = tk.StringVar(value="0")
        self.total_entry = ttk.Entry(mid, textvariable=self.total_var, width=10, state="disabled")
        self.total_entry.grid(row=0, column=2, sticky="w", padx=(6, 0))



        btns = ttk.LabelFrame(self, text="一次性配置（第一次用需要点）")
        btns.pack(fill=tk.X, **pad)

        ttk.Button(btns, text="选择截图区域", command=self._select_region).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Button(btns, text="测试截图(显示到日志)", command=self._test_screenshot).grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ttk.Button(btns, text="选择分数输入框", command=self._select_score_input).grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Button(btns, text="选择提交按钮", command=self._select_submit_btn).grid(row=0, column=3, padx=8, pady=8, sticky="w")
        ttk.Button(btns, text="选择下一题按钮", command=self._select_next_btn).grid(row=0, column=4, padx=8, pady=8, sticky="w")

        runbox = ttk.LabelFrame(self, text="运行")
        runbox.pack(fill=tk.X, **pad)
        ttk.Button(runbox, text="开始（单题/批量）", command=self._start).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="停止", command=self._stop).grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="清空日志", command=self._clear_log).grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="保存配置", command=self._save_config).grid(row=0, column=3, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="加载配置", command=lambda: self._load_config(silent=False)).grid(row=0, column=4, padx=8, pady=8, sticky="w")

        self.progress_var = tk.StringVar(value="未开始")
        ttk.Label(runbox, textvariable=self.progress_var).grid(row=0, column=5, padx=12, pady=8, sticky="w")

        log_frame = ttk.LabelFrame(self, text="运行日志 / AI返回（自动滚动）")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self.log_text = tk.Text(log_frame, wrap="word")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        self.log_text.configure(yscrollcommand=sb.set)

    def _sync_batch_state(self):
        self.total_entry.configure(state=("normal" if self.batch_var.get() else "disabled"))

    def _sync_filler_state(self):
        return

    def _sync_provider_state(self):
        provider = self.provider_var.get()
        if provider == "智谱AI":
            self.base_url_entry.configure(state="disabled")
            self.extra_headers_entry.configure(state="disabled")
            if self.model_var.get().strip() in {"gpt-4o-mini", ""}:
                self.model_var.set("glm-4v")
        else:
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="normal")
            if self.model_var.get().strip() in {"glm-4v", ""}:
                self.model_var.set("gpt-4o-mini")

    def _collect_config(self) -> dict:
        return {
            "provider": self.provider_var.get(),
            "api_key": self.api_key_var.get(),
            "model": self.model_var.get(),
            "base_url": self.base_url_var.get(),
            "extra_headers_json": self.extra_headers_var.get(),
            "criteria": self.criteria_text.get("1.0", "end").strip(),
            "batch_mode": bool(self.batch_var.get()),
            "total_questions": self.total_var.get(),
            "filler_mode": "pyautogui",
        }

    def _config_key_for_system(self) -> str:
        """
        影响“评分器/请求方式”的关键配置。
        这些变了就需要重建 system；否则应复用（保留截图区域、按钮坐标等一次性设置）。
        """
        cfg = self._collect_config()
        key_obj = {
            "provider": cfg.get("provider", ""),
            "api_key": cfg.get("api_key", ""),
            "model": cfg.get("model", ""),
            "base_url": cfg.get("base_url", ""),
            "extra_headers_json": cfg.get("extra_headers_json", ""),
            "filler_mode": cfg.get("filler_mode", ""),
            "dom_target_url": cfg.get("dom_target_url", ""),
            "dom_browser_mode": cfg.get("dom_browser_mode", ""),
            "dom_cdp_url": cfg.get("dom_cdp_url", ""),
            "dom_score_selector": cfg.get("dom_score_selector", ""),
            "dom_submit_selector": cfg.get("dom_submit_selector", ""),
            "dom_next_selector": cfg.get("dom_next_selector", ""),
        }
        return json.dumps(key_obj, ensure_ascii=False, sort_keys=True)

    def _apply_config(self, cfg: dict):
        if not isinstance(cfg, dict):
            raise ValueError("配置文件格式错误：根必须是 JSON 对象")

        if "provider" in cfg:
            self.provider_var.set(str(cfg["provider"]))
        if "api_key" in cfg:
            self.api_key_var.set(str(cfg["api_key"]))
        if "model" in cfg:
            self.model_var.set(str(cfg["model"]))
        if "base_url" in cfg:
            self.base_url_var.set(str(cfg["base_url"]))
        if "extra_headers_json" in cfg:
            self.extra_headers_var.set(str(cfg["extra_headers_json"]))

        if "criteria" in cfg:
            self.criteria_text.delete("1.0", "end")
            self.criteria_text.insert("1.0", str(cfg["criteria"]))

        if "batch_mode" in cfg:
            self.batch_var.set(bool(cfg["batch_mode"]))
            self._sync_batch_state()

        if "total_questions" in cfg:
            self.total_var.set(str(cfg["total_questions"]))

        if "filler_mode" in cfg:
            pass
        if "dom_target_url" in cfg:
            self.dom_url_var.set(str(cfg["dom_target_url"]))
        if "dom_browser_mode" in cfg:
            self.dom_browser_mode_var.set(str(cfg["dom_browser_mode"]))
        if "dom_cdp_url" in cfg:
            self.dom_cdp_url_var.set(str(cfg["dom_cdp_url"]))
        if "dom_score_selector" in cfg:
            self.dom_score_selector_var.set(str(cfg["dom_score_selector"]))
        if "dom_submit_selector" in cfg:
            self.dom_submit_selector_var.set(str(cfg["dom_submit_selector"]))
        if "dom_next_selector" in cfg:
            self.dom_next_selector_var.set(str(cfg["dom_next_selector"]))

        self._sync_provider_state()
        self._sync_filler_state()

    def _save_config(self):
        cfg = self._collect_config()
        try:
            self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("成功", f"配置已保存到：{self.config_path}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _load_config(self, silent: bool):
        if not self.config_path.exists():
            if not silent:
                messagebox.showinfo("提示", f"未找到配置文件：{self.config_path}")
            return
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._apply_config(cfg)
            if not silent:
                messagebox.showinfo("成功", "配置已加载。")
        except Exception as e:
            if silent:
                print(f"加载配置失败：{e}")
            else:
                messagebox.showerror("加载失败", str(e))

    def _ensure_system(self) -> AutoScoringSystem:
        ok, missing = check_dependencies()
        if not ok:
            raise ValueError(f"缺少依赖包：{', '.join(missing)}。请先 pip install pyautogui Pillow requests")

        # 若配置没变，复用已有 system，避免丢失截图选区/按钮位置
        new_key = self._config_key_for_system()
        if self.system is not None and self._system_cfg_key == new_key:
            # 同步可变配置
            criteria = self.criteria_text.get("1.0", "end").strip()
            if criteria:
                self.system.criteria = criteria
            self.system.batch_mode = bool(self.batch_var.get())
            if self.system.batch_mode:
                try:
                    self.system.total_questions = int(self.total_var.get() or "0")
                except ValueError:
                    self.system.total_questions = 0
            return self.system

        api_key = (self.api_key_var.get() or "").strip()
        if not api_key:
            raise ValueError("请先填写 API Key")

        criteria = self.criteria_text.get("1.0", "end").strip()
        if not criteria:
            raise ValueError("请先填写评分标准")

        model = (self.model_var.get() or "").strip()
        if not model:
            raise ValueError("请先填写模型")
        batch_mode = bool(self.batch_var.get())

        provider = self.provider_var.get()
        scorer = None
        if provider == "OpenAI兼容":
            base_url = (self.base_url_var.get() or "").strip()
            extra_headers_raw = (self.extra_headers_var.get() or "").strip()
            extra_headers = {}
            if extra_headers_raw:
                try:
                    extra_headers = json.loads(extra_headers_raw)
                    if not isinstance(extra_headers, dict):
                        raise ValueError("额外请求头必须是 JSON 对象，例如 {\"X-My-Header\":\"1\"}")
                except Exception as e:
                    raise ValueError(f"额外请求头JSON解析失败：{e}") from e

            scorer = OpenAICompatibleScorer(
                base_url=base_url,
                api_key=api_key,
                model=model,
                extra_headers=extra_headers,
            )

        self.system = AutoScoringSystem(
            root=self,
            api_key=api_key,
            criteria=criteria,
            model=model,
            batch_mode=batch_mode,
            scorer=scorer,
            capture_dir=str(self.capture_dir),
            filler_mode="pyautogui",
            filler_config={},
        )
        self._system_cfg_key = new_key

        if batch_mode:
            try:
                self.system.total_questions = int(self.total_var.get() or "0")
            except ValueError:
                self.system.total_questions = 0

        return self.system

    def _select_region(self):
        try:
            sys = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        sys.screenshot_tool.select_region_interactive(self)
        messagebox.showinfo("完成", "截图区域已选择。")

    def _test_screenshot(self):
        try:
            sys_ = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return

        try:
            img = sys_.screenshot_tool.capture_current_question()
            if img is None:
                raise ValueError("没有拿到截图，请先选择截图区域或检查截图权限。")
            path = self.capture_dir / f"__test_capture_{int(time.time())}.png"
            img.save(path)
            print(f"[测试截图] 已保存：{path}  size={img.size}")
            messagebox.showinfo("完成", f"测试截图已保存：{path}")
        except Exception as e:
            messagebox.showerror("测试失败", str(e))

    def _select_score_input(self):
        try:
            sys = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        sys.filler.select_score_input()

    def _select_submit_btn(self):
        try:
            sys = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        sys.filler.select_submit_button()

    def _select_next_btn(self):
        try:
            sys = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        sys.filler.select_next_button()

    def _ensure_dom_picker(self) -> AutoFiller:
        target_url = (self.dom_url_var.get() or "").strip()
        browser_mode = "connect" if self.dom_browser_mode_var.get() == "连接已有浏览器(CDP)" else "launch"
        cdp_url = (self.dom_cdp_url_var.get() or "").strip()
        if browser_mode == "launch" and not target_url:
            raise ValueError("请先填写页面URL(DOM)")
        if browser_mode == "connect" and not cdp_url:
            raise ValueError("连接已有浏览器时，请先填写 CDP 地址")
        try:
            import playwright  # noqa: F401
        except Exception as e:
            raise ValueError("缺少 playwright，请先执行 pip install playwright") from e

        picker_key = json.dumps({"target_url": target_url, "browser_mode": browser_mode, "cdp_url": cdp_url}, ensure_ascii=False, sort_keys=True)
        if self._dom_picker is not None and self._dom_picker_key == picker_key:
            return self._dom_picker

        if self._dom_picker is not None:
            self._dom_picker.close()
        self._dom_picker = AutoFiller(
            self,
            mode="dom",
            config={"target_url": target_url, "browser_mode": browser_mode, "cdp_url": cdp_url},
        )
        self._dom_picker_key = picker_key
        return self._dom_picker

    def _pick_dom_selector_into(self, field_name: str, var: tk.StringVar):
        try:
            picker = self._ensure_dom_picker()
            messagebox.showinfo("操作提示", f"浏览器会打开目标页面，请点击【{field_name}】对应元素。")
            selector = picker.pick_selector(field_name)
            var.set(selector)
        except Exception as e:
            messagebox.showerror("识别失败", str(e))

    def _pick_dom_score_selector(self):
        self._pick_dom_selector_into("分数输入框", self.dom_score_selector_var)

    def _pick_dom_submit_selector(self):
        self._pick_dom_selector_into("提交按钮", self.dom_submit_selector_var)

    def _pick_dom_next_selector(self):
        self._pick_dom_selector_into("下一题按钮", self.dom_next_selector_var)

    def _start(self):
        try:
            sys_ = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return

        if sys_.thread and sys_.thread.is_alive():
            messagebox.showinfo("提示", "已在运行中。")
            return

        sys_.question_count = 0
        sys_.start()
        self.progress_var.set("运行中…")
        self.after(200, self._poll_progress)

    def _poll_progress(self):
        sys_ = self.system
        if not sys_:
            return

        if sys_.thread and sys_.thread.is_alive():
            if sys_.batch_mode:
                if sys_.total_questions > 0:
                    self.progress_var.set(f"批量中：{sys_.question_count}/{sys_.total_questions}")
                else:
                    self.progress_var.set(f"批量中：已处理 {sys_.question_count} 题")
            else:
                self.progress_var.set("单题处理中…")
            self.after(350, self._poll_progress)
        else:
            if sys_.batch_mode:
                self.progress_var.set(f"已停止（已处理 {sys_.question_count} 题）")
            else:
                self.progress_var.set("已完成（单题）")

    def _stop(self):
        if self.system:
            self.system.stop()
        self.progress_var.set("已停止")

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _drain_log_queue(self):
        try:
            while True:
                s = self._log_q.get_nowait()
                self.log_text.insert("end", s)
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(60, self._drain_log_queue)

    def destroy(self):
        try:
            if self.system:
                self.system.stop()
        finally:
            sys.stdout = self._orig_stdout
            sys.stderr = self._orig_stderr
            super().destroy()


if __name__ == "__main__":
    app = App()
    try:
        app.mainloop()
    finally:
        time.sleep(0.05)

