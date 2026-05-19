#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上层 GUI：调用 `自动阅卷系统GUI.py` 核心模块完成阅卷。

运行：
python 上层GUI.py
"""

__version__ = "1.0.0"

import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import json
import os
import queue
import requests
import sys
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageTk

from 自动阅卷系统GUI import AutoScoringSystem, check_dependencies
from modules.自动评分模块 import OpenAICompatibleScorer, ZhipuAIScorer, BaiduScorer, XunfeiScorer, fetch_openai_compatible_models
from modules.自动填分模块 import AutoFiller
from modules.规则调优模块 import RuleTuner, ScoringRecord
from modules.评分数据库模块 import ScoringDatabase


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
        self.score_db = ScoringDatabase(Path(__file__).with_name("scores.db"))
        self._score_session_id = uuid.uuid4().hex
        self._record_db_ids: dict[int, int] = {}

        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _QueueStdout(self._log_q)
        sys.stderr = _QueueStdout(self._log_q)

        self.system: AutoScoringSystem | None = None
        self._system_cfg_key: str | None = None
        self._runtime_config = {
            "screenshot_region_norm": None,
            "score_input_pos": None,
            "submit_btn_pos": None,
            "next_btn_pos": None,
        }
        self._provider_notice_provider: str | None = None
        self._ui_thread_guard: threading.Lock = threading.Lock()
        self._region_overlay: tk.Toplevel | None = None
        self._region_overlay_canvas: tk.Canvas | None = None
        self._region_overlay_visible = False
        self.tuner = RuleTuner()
        self._next_record_index = 0
        self._tuning_running = False
        self._tune_preview_window: tk.Toplevel | None = None
        self._tune_preview_image_ref = None
        self._tune_preview_item = None
        self._tune_preview_last_xy = (0, 0)

        self._build_menu()
        self._build_ui()
        self._load_config(silent=True)
        self._sync_batch_state()
        self._update_ready_status()
        self.after(60, self._drain_log_queue)

    def _build_menu(self):
        menubar = tk.Menu(self, tearoff=0)
        self.configure(menu=menubar)

        # ── 文件 ──
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="保存配置", command=self._save_config)
        file_menu.add_command(label="加载配置", command=lambda: self._load_config(silent=False))
        file_menu.add_separator()
        file_menu.add_command(label="导出评分记录为 CSV", command=self._export_scores_csv)
        file_menu.add_separator()
        file_menu.add_command(label="清空截图目录", command=self._clear_captures)
        menubar.add_cascade(label="文件", menu=file_menu)

        # ── 配置 ──
        cfg_menu = tk.Menu(menubar, tearoff=0)
        cfg_menu.add_command(label="选择截图区域", command=self._select_region)
        cfg_menu.add_command(label="多区域框选（填空题）", command=self._select_multi_regions)
        cfg_menu.add_command(label="测试截图", command=self._test_screenshot)
        cfg_menu.add_command(label="显示/刷新截图区域提示", command=self._show_region_overlay)
        cfg_menu.add_command(label="隐藏截图区域提示", command=self._hide_region_overlay)
        cfg_menu.add_separator()
        cfg_menu.add_command(label="选择分数输入框", command=self._select_score_input)
        cfg_menu.add_command(label="选择提交按钮", command=self._select_submit_btn)
        cfg_menu.add_command(label="选择下一题按钮", command=self._select_next_btn)
        menubar.add_cascade(label="截图配置", menu=cfg_menu)

        # ── 生成 ──
        gen_menu = tk.Menu(menubar, tearoff=0)
        gen_menu.add_command(label="从截图生成评分标准", command=self._generate_criteria_from_screenshot)
        gen_menu.add_command(label="从图片文件生成评分标准", command=self._generate_criteria_from_file)
        menubar.add_cascade(label="生成", menu=gen_menu)

        # ── 关于 ──
        about_menu = tk.Menu(menubar, tearoff=0)
        about_menu.add_command(label="关于本项目", command=self._show_about)
        menubar.add_cascade(label="关于", menu=about_menu)

    def _show_about(self):
        win = tk.Toplevel(self)
        win.title("关于")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        ttk.Label(win, text="自动阅卷系统", font=("Microsoft YaHei UI", 14, "bold")).pack(padx=24, pady=(20, 8))
        ttk.Label(win, text=f"版本：{__version__}", font=("Microsoft YaHei UI", 10)).pack(padx=24, anchor="w")
        ttk.Label(win, text="项目地址：").pack(padx=24, anchor="w")
        link = tk.Label(win, text="https://github.com/kcdey01/Automatic_grading",
                        fg="#1a0dab", cursor="hand2", font=("Microsoft YaHei UI", 10, "underline"))
        link.pack(padx=24, anchor="w")
        link.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://github.com/kcdey01/Automatic_grading"))
        ttk.Label(win, text="欢迎 Star & Issue & PR").pack(padx=24, pady=(8, 20))
        ttk.Button(win, text="确定", command=win.destroy).pack(pady=(0, 16))
        win.transient(self)
        win.grab_set()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # ── 可滚动容器 ──
        self._canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        inner = ttk.Frame(self._canvas)
        self._canvas.create_window((0, 0), window=inner, anchor="nw")

        def _configure_inner(event):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._canvas.itemconfig(1, width=event.width)
        inner.bind("<Configure>", _configure_inner)
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(1, width=e.width))

        def _on_mousewheel(event):
            # 当鼠标悬停在带滚动条的 Text 控件上时，不触发 Canvas 滚动
            w = event.widget
            while w is not None:
                if isinstance(w, tk.Text):
                    try:
                        if w.cget("yscrollcommand") != "":
                            return  # 该 Text 有自己的滚动条，跳过 Canvas 滚动
                    except tk.TclError:
                        pass
                    break
                w = w.master if hasattr(w, "master") else None
            self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        self._canvas.bind_all("<MouseWheel>", _on_mousewheel)

        top = ttk.Frame(inner)
        top.pack(fill=tk.X, **pad)

        ttk.Label(top, text="服务商").grid(row=0, column=0, sticky="w")
        self.provider_var = tk.StringVar(value="OpenAI")
        ttk.Combobox(
            top,
            textvariable=self.provider_var,
            width=14,
            values=[
                "OpenAI", "智谱AI", "阿里通义千问", "字节豆包",
                "零一万物", "硅基流动", "百度千帆", "科大讯飞", "小米MiMo", "自定义"
            ],
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(top, text="API Key").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.api_key_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.api_key_var, width=45, show="*").grid(row=0, column=3, sticky="we", padx=(6, 0))

        ttk.Label(top, text="模型").grid(row=1, column=0, sticky="w")
        self.model_var = tk.StringVar(value="gpt-4o-mini")
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, width=25, values=[])
        self.model_combo.grid(row=1, column=1, sticky="w", padx=(6, 0))
        self.fetch_models_btn = ttk.Button(top, text="获取模型列表", command=self._fetch_models)
        self.fetch_models_btn.grid(row=1, column=2, sticky="w", padx=(12, 0))
        self.open_platform_btn = ttk.Button(top, text="打开平台", command=self._open_provider_platform)
        self.open_platform_btn.grid(row=1, column=3, sticky="w", padx=(6, 0))

        ttk.Label(top, text="base_url(OpenAI兼容)").grid(row=2, column=0, sticky="w")
        self.base_url_var = tk.StringVar(value="https://api.openai.com")
        self.base_url_entry = ttk.Entry(top, textvariable=self.base_url_var, width=35)
        self.base_url_entry.grid(row=2, column=1, columnspan=3, sticky="we", padx=(6, 0))

        ttk.Label(top, text="额外请求头JSON(可选)").grid(row=3, column=0, sticky="w")
        self.extra_headers_var = tk.StringVar(value="")
        self.extra_headers_entry = ttk.Entry(top, textvariable=self.extra_headers_var, width=90)
        self.extra_headers_entry.grid(row=3, column=1, columnspan=3, sticky="we", padx=(6, 0))

        top.columnconfigure(3, weight=1)

        self.provider_var.trace_add("write", lambda *_: self._sync_provider_state())
        self._sync_provider_state()

        criteria_frame = ttk.LabelFrame(inner, text="评分标准（直接粘贴你的阅卷要求/评分细则）")
        criteria_frame.pack(fill=tk.BOTH, expand=False, **pad)
        _criteria_inner = ttk.Frame(criteria_frame)
        _criteria_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.criteria_text = tk.Text(_criteria_inner, height=8, wrap="word")
        _criteria_sb = ttk.Scrollbar(_criteria_inner, command=self.criteria_text.yview)
        self.criteria_text.configure(yscrollcommand=_criteria_sb.set)
        _criteria_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.criteria_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        mid = ttk.Frame(inner)
        mid.pack(fill=tk.X, **pad)

        self.batch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mid, text="批量模式", variable=self.batch_var, command=self._sync_batch_state).grid(row=0, column=0, sticky="w")

        ttk.Label(mid, text="总份数(可选)").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.total_var = tk.StringVar(value="0")
        self.total_entry = ttk.Entry(mid, textvariable=self.total_var, width=10, state="disabled")
        self.total_entry.grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Label(mid, text="空白阈值").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.blank_threshold_var = tk.DoubleVar(value=15.0)
        blank_scale = ttk.Scale(
            mid,
            from_=0,
            to=40,
            length=180,
            variable=self.blank_threshold_var,
            command=self._on_blank_threshold_change,
        )
        blank_scale.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        self.blank_threshold_label_var = tk.StringVar(value="阈值 15")
        ttk.Label(mid, textvariable=self.blank_threshold_label_var).grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(4, 0))
        ttk.Label(mid, text="高=更容易判空白").grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(4, 0))
        ttk.Button(mid, text="测试空白检测", command=self._test_blank_detection).grid(row=1, column=4, sticky="w", padx=(8, 0), pady=(4, 0))
        ttk.Button(mid, text="标记空白卷", command=self._mark_blank_paper).grid(row=1, column=5, sticky="w", padx=(4, 0), pady=(4, 0))

        self.ready_status_var = tk.StringVar(value="准备状态：未检查")
        ttk.Label(mid, textvariable=self.ready_status_var).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))


        runbox = ttk.LabelFrame(inner, text="运行")
        runbox.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(runbox, text="开始单题阅卷", command=self._start)
        self.start_btn.grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="停止", command=self._stop).grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="清空日志", command=self._clear_log).grid(row=0, column=2, padx=8, pady=8, sticky="w")

        self.progress_var = tk.StringVar(value="未开始")
        ttk.Label(runbox, textvariable=self.progress_var).grid(row=0, column=3, padx=12, pady=8, sticky="w")

        # ── 规则调优 ──
        tune_frame = ttk.LabelFrame(inner, text="规则调优（收集评分记录→标记正确分数→自动优化评分标准）")
        tune_frame.pack(fill=tk.BOTH, expand=False, **pad)

        tune_top = ttk.Frame(tune_frame)
        tune_top.pack(fill=tk.X, padx=8, pady=(4, 0))

        tree_frame = ttk.Frame(tune_top)
        tree_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        columns = ("序号", "AI分数", "正确分数", "状态")
        self.tune_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=4)
        for col in columns:
            self.tune_tree.heading(col, text=col)
        self.tune_tree.column("序号", width=50, anchor="center")
        self.tune_tree.column("AI分数", width=70, anchor="center")
        self.tune_tree.column("正确分数", width=70, anchor="center")
        self.tune_tree.column("状态", width=80, anchor="center")
        self.tune_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tune_tree.bind("<<TreeviewSelect>>", self._on_tune_tree_select)
        self.tune_tree.bind("<Motion>", self._on_tune_tree_motion)
        self.tune_tree.bind("<Leave>", self._hide_tune_preview)

        tune_btns = ttk.Frame(tune_top)
        tune_btns.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Label(tune_btns, text="正确分数:").pack(anchor="w")
        self.tune_manual_var = tk.StringVar()
        ttk.Entry(tune_btns, textvariable=self.tune_manual_var, width=8).pack(anchor="w", pady=2)
        ttk.Button(tune_btns, text="标记", width=10, command=self._tune_mark_score).pack(anchor="w", pady=1)

        tune_bar = ttk.Frame(tune_frame)
        tune_bar.pack(fill=tk.X, padx=8, pady=(2, 4))
        ttk.Button(tune_bar, text="规则调优（分析偏差→生成新规则）", command=self._run_tuning).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(tune_bar, text="应用新规则", command=self._apply_tuning).pack(side=tk.LEFT, padx=4)
        ttk.Button(tune_bar, text="清空记录", command=self._clear_tuning).pack(side=tk.LEFT, padx=4)

        self.tune_status_var = tk.StringVar(value="未收集评分记录")
        ttk.Label(tune_bar, textvariable=self.tune_status_var).pack(side=tk.LEFT, padx=12)

        _tune_result_inner = ttk.Frame(tune_frame)
        _tune_result_inner.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.tune_result_text = tk.Text(_tune_result_inner, height=4, wrap="word", state="disabled")
        _tune_result_sb = ttk.Scrollbar(_tune_result_inner, command=self.tune_result_text.yview)
        self.tune_result_text.configure(yscrollcommand=_tune_result_sb.set)
        _tune_result_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tune_result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── 快捷优化建议 ──
        quick_frame = ttk.LabelFrame(inner, text="快捷优化建议（输入优化想法→AI分析→生成新评分标准）")
        quick_frame.pack(fill=tk.X, **pad)

        qf_top = ttk.Frame(quick_frame)
        qf_top.pack(fill=tk.X, padx=8, pady=(4, 0))

        ttk.Label(qf_top, text="优化建议：").pack(side=tk.LEFT)
        self.optimize_suggestion_text = tk.Text(qf_top, height=3, wrap="word")
        self.optimize_suggestion_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        qf_btn_bar = ttk.Frame(quick_frame)
        qf_btn_bar.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.optimize_btn = ttk.Button(qf_btn_bar, text="执行优化", command=self._optimize_criteria)
        self.optimize_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.apply_opt_btn = ttk.Button(qf_btn_bar, text="应用新规则", command=self._apply_optimized_criteria, state="disabled")
        self.apply_opt_btn.pack(side=tk.LEFT, padx=4)
        self.optimize_status_var = tk.StringVar(value="")
        ttk.Label(qf_btn_bar, textvariable=self.optimize_status_var).pack(side=tk.LEFT, padx=12)

        _opt_result_inner = ttk.Frame(quick_frame)
        _opt_result_inner.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.optimize_result_text = tk.Text(_opt_result_inner, height=4, wrap="word", state="disabled")
        _opt_result_sb = ttk.Scrollbar(_opt_result_inner, command=self.optimize_result_text.yview)
        self.optimize_result_text.configure(yscrollcommand=_opt_result_sb.set)
        _opt_result_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.optimize_result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── 日志 ──
        log_frame = ttk.LabelFrame(inner, text="运行日志 / AI返回（自动滚动）")
        log_frame.pack(fill=tk.X, **pad)

        self.log_text = tk.Text(log_frame, wrap="word", height=12)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        self.log_text.configure(yscrollcommand=sb.set)

    def _sync_batch_state(self):
        self.total_entry.configure(state=("normal" if self.batch_var.get() else "disabled"))
        if hasattr(self, "start_btn"):
            text = "开始批量阅卷" if self.batch_var.get() else "开始单题阅卷"
            self.start_btn.configure(text=text)

    def _get_blank_threshold(self) -> float:
        try:
            return round(max(0.0, min(40.0, float(self.blank_threshold_var.get()))), 1)
        except (TypeError, ValueError, tk.TclError):
            return 15.0

    def _set_blank_threshold(self, threshold: float):
        self.blank_threshold_var.set(max(0.0, min(40.0, float(threshold))))

    def _sync_blank_threshold_label(self):
        try:
            val = round(max(0.0, min(40.0, float(self.blank_threshold_var.get()))), 1)
        except (TypeError, ValueError, tk.TclError):
            val = 15.0
        self.blank_threshold_label_var.set(f"阈值 {val}")

    def _on_blank_threshold_change(self, _value=None):
        self._sync_blank_threshold_label()
        if self.system is not None:
            self.system.blank_threshold = self._get_blank_threshold()

    def _format_ready_item(self, label: str, ok: bool) -> str:
        return f"{label}{'已设置' if ok else '未设置'}"

    def _get_ready_status_text(self) -> str:
        cfg = self._collect_runtime_config()
        parts = [
            self._format_ready_item("截图区域", bool(cfg.get("screenshot_region_norm"))),
            self._format_ready_item("分数框", bool(cfg.get("score_input_pos"))),
            self._format_ready_item("提交按钮", bool(cfg.get("submit_btn_pos"))),
            self._format_ready_item("下一题按钮", bool(cfg.get("next_btn_pos"))),
        ]
        return "准备状态：" + " | ".join(parts)

    def _update_ready_status(self):
        if hasattr(self, "ready_status_var"):
            self.ready_status_var.set(self._get_ready_status_text())

    def _format_region_status(self, region):
        vals = self._normalize_number_list(region, 4)
        if not vals:
            return "未选择"
        left, top, right, bottom = vals
        return f"已选择：左{left:.4f} 上{top:.4f} 右{right:.4f} 下{bottom:.4f}"

    def _update_region_status(self):
        region = self._runtime_config.get("screenshot_region_norm")
        if self.system is not None and self.system.screenshot_tool.selected_region_norm:
            region = self.system.screenshot_tool.selected_region_norm
        if hasattr(self, "region_status_var"):
            self.region_status_var.set(self._format_region_status(region))
        self._show_region_overlay(show_warning=False)

    def _get_region_overlay_geometry(self):
        region = self._runtime_config.get("screenshot_region_norm")
        if self.system is not None and self.system.screenshot_tool.selected_region_norm:
            region = self.system.screenshot_tool.selected_region_norm
        vals = self._normalize_number_list(region, 4)
        if not vals:
            return None

        try:
            from PIL import ImageGrab
            full = ImageGrab.grab(all_screens=True)
            screen_w, screen_h = full.size
        except Exception:
            import pyautogui
            screen_w, screen_h = pyautogui.size()

        left, top, right, bottom = vals
        x = int(max(0, min(screen_w - 1, left * screen_w)))
        y = int(max(0, min(screen_h - 1, top * screen_h)))
        width = int(max(1, min(screen_w, right * screen_w) - x))
        height = int(max(1, min(screen_h, bottom * screen_h) - y))
        return x, y, width, height

    def _make_overlay_click_through(self, window):
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = window.winfo_id()
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, ex_style | 0x20 | 0x80000)
        except Exception:
            pass

    def _show_region_overlay(self, show_warning: bool = True):
        geometry = self._get_region_overlay_geometry()
        if geometry is None:
            if show_warning:
                messagebox.showinfo("提示", "请先选择截图区域。")
            self._hide_region_overlay()
            return

        x, y, width, height = geometry
        if self._region_overlay is None or not self._region_overlay.winfo_exists():
            self._region_overlay = tk.Toplevel(self)
            self._region_overlay.overrideredirect(True)
            self._region_overlay.attributes("-topmost", True)
            try:
                self._region_overlay.attributes("-alpha", 0.35)
            except tk.TclError:
                pass
            self._region_overlay_canvas = tk.Canvas(
                self._region_overlay,
                bg="#2f80ed",
                highlightthickness=3,
                highlightbackground="#ff3b30",
            )
            self._region_overlay_canvas.pack(fill=tk.BOTH, expand=True)
            self._make_overlay_click_through(self._region_overlay)

        self._region_overlay.geometry(f"{width}x{height}+{x}+{y}")
        self._region_overlay.deiconify()
        self._region_overlay.lift()
        self._region_overlay_visible = True

    def _hide_region_overlay(self):
        if self._region_overlay is not None and self._region_overlay.winfo_exists():
            self._region_overlay.withdraw()
        self._region_overlay_visible = False

    def _before_capture(self):
        if self._region_overlay is not None and self._region_overlay.winfo_exists():
            self._region_overlay_visible = str(self._region_overlay.state()) != "withdrawn"
            self._region_overlay.withdraw()
            self.update_idletasks()
            time.sleep(0.08)

    def _after_capture(self):
        if self._region_overlay_visible:
            self.after(0, self._show_region_overlay)

    def _sync_filler_state(self):
        return

    def _fetch_models(self):
        provider = self.provider_var.get()
        if provider in {"智谱AI", "百度千帆", "科大讯飞"}:
            messagebox.showinfo("提示", f"{provider} 当前使用专用 SDK/接口，暂不支持自动获取模型列表。")
            return

        api_key = (self.api_key_var.get() or "").strip()
        if not api_key:
            messagebox.showerror("配置不完整", "请先填写 API Key")
            return

        base_url = (self.base_url_var.get() or "").strip()
        if not base_url and provider != "自定义":
            preset = self.PROVIDER_PRESETS.get(provider)
            if preset:
                base_url = preset[0]
        if not base_url:
            messagebox.showerror("配置不完整", "请先填写 base_url")
            return

        extra_headers_raw = (self.extra_headers_var.get() or "").strip()
        extra_headers = {}
        if extra_headers_raw:
            try:
                extra_headers = json.loads(extra_headers_raw)
                if not isinstance(extra_headers, dict):
                    raise ValueError("额外请求头必须是 JSON 对象")
            except Exception as e:
                messagebox.showerror("配置错误", f"额外请求头JSON解析失败：{e}")
                return

        self.fetch_models_btn.configure(state="disabled", text="获取中…")
        print(f"[模型列表] 正在获取 {provider} 模型列表：{base_url}")

        def _do_fetch():
            try:
                models = fetch_openai_compatible_models(
                    base_url=base_url,
                    api_key=api_key,
                    extra_headers=extra_headers,
                    timeout=30,
                )
                self.after(0, self._fetch_models_done, models)
            except Exception as e:
                self.after(0, self._fetch_models_error, str(e))

        threading.Thread(target=_do_fetch, daemon=True).start()

    def _fetch_models_done(self, models: list[str]):
        self.fetch_models_btn.configure(state="normal", text="获取模型列表")
        if not models:
            messagebox.showwarning("模型列表", "接口返回成功，但没有解析到模型 id。")
            return
        current = (self.model_var.get() or "").strip()
        self.model_combo.configure(values=models)
        if current not in models:
            self.model_var.set(models[0])
        print(f"[模型列表] 已获取 {len(models)} 个模型")
        messagebox.showinfo("模型列表", f"已获取 {len(models)} 个模型，已填入模型下拉框。")

    def _fetch_models_error(self, err: str):
        self.fetch_models_btn.configure(state="normal", text="获取模型列表")
        print(f"[模型列表] 获取失败：{err}")
        messagebox.showerror("获取模型列表失败", err)

    PROVIDER_PRESETS = {
        "OpenAI":       ("https://api.openai.com",                          "gpt-4o"),
        "智谱AI":       ("",                                                "glm-4v"),
        "阿里通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1","qwen-vl-max"),
        "字节豆包":     ("https://ark.cn-beijing.volces.com/api/v3",        "doubao-seed-1-8-251228"),
        "零一万物":     ("https://api.lingyiwanwu.com/v1",                  "yi-vision"),
        "硅基流动":     ("https://api.siliconflow.cn/v1",                   "Qwen/Qwen2-VL-72B-Instruct"),
        "百度千帆":     ("https://qianfan.baidubce.com",                    "ernie-4.0-8k"),
        "科大讯飞":     ("",                                                "spark-v4.0"),
        "小米MiMo":     ("https://token-plan-cn.xiaomimimo.com/v1",          "mimo-v2.5-pro"),
        "自定义":       ("",                                                ""),
    }

    PROVIDER_PLATFORMS = {
        "OpenAI":       "https://platform.openai.com",
        "智谱AI":       "https://www.bigmodel.cn/glm-coding?ic=JCASAUKSRL",
        "阿里通义千问": "https://www.aliyun.com/minisite/goods?userCode=f9ablkb2",
        "字节豆包":     "https://console.volcengine.com/ark",
        "零一万物":     "https://platform.lingyiwanwu.com",
        "硅基流动":     "https://cloud.siliconflow.cn/i/3w6SanhF",
        "百度千帆":     "https://qianfan.cloud.baidu.com",
        "科大讯飞":     "https://console.xfyun.cn",
        "小米MiMo":     "https://platform.xiaomimimo.com?ref=6LYNWJ",
    }

    def _open_provider_platform(self):
        provider = self.provider_var.get()
        url = self.PROVIDER_PLATFORMS.get(provider)
        if not url:
            messagebox.showinfo("提示", f"「{provider}」没有对应的平台地址，请手动打开。")
            return
        import webbrowser
        webbrowser.open(url)

    def _sync_provider_state(self):
        provider = self.provider_var.get()
        preset = self.PROVIDER_PRESETS.get(provider)
        if preset:
            preset_url, preset_model = preset
            if provider != "自定义":
                self.base_url_var.set(preset_url)
            if self.model_var.get().strip() in {"gpt-4o", "gpt-4o-mini", "glm-4v", "qwen-vl-max", "yi-vision", "spark-v4.0", "ernie-4.0-8k", "doubao-vision-pro-32k", "mimo-v2.5-pro", "doubao-seed-1-8-251228", ""}:
                self.model_var.set(preset_model)

        if provider == "智谱AI":
            self.base_url_entry.configure(state="disabled")
            self.extra_headers_entry.configure(state="disabled")
        elif provider == "百度千帆":
            self.base_url_entry.configure(state="disabled")
            self.extra_headers_entry.configure(state="disabled")
            print("百度千帆：API Key 请填写 API_Key:Secret_Key 格式")
        elif provider == "科大讯飞":
            self.base_url_entry.configure(state="disabled")
            self.extra_headers_entry.configure(state="disabled")
            print("科大讯飞：API Key 请填写 appId:apiKey:apiSecret 格式")
        elif provider == "小米MiMo":
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="disabled")
            if self._provider_notice_provider != provider:
                print("小米MiMo：API Key 格式为 tp-xxxxx（Token Plan），请在订阅管理页面获取")
                self._provider_notice_provider = provider
        elif provider == "自定义":
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="normal")
        else:
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="normal")

    def _collect_config(self) -> dict:
        cfg = {
            "provider": self.provider_var.get(),
            "api_key": self.api_key_var.get(),
            "model": self.model_var.get(),
            "base_url": self.base_url_var.get(),
            "extra_headers_json": self.extra_headers_var.get(),
            "criteria": self.criteria_text.get("1.0", "end").strip(),
            "batch_mode": bool(self.batch_var.get()),
            "total_questions": self.total_var.get(),
            "blank_threshold": self._get_blank_threshold(),
            "filler_mode": "pyautogui",
        }
        cfg.update(self._collect_runtime_config())
        return cfg

    def _collect_runtime_config(self) -> dict:
        cfg = dict(self._runtime_config)
        if self.system is not None:
            region = self.system.screenshot_tool.selected_region_norm
            if region:
                cfg["screenshot_region_norm"] = list(region)
            if self.system.filler.score_input_pos:
                cfg["score_input_pos"] = list(self.system.filler.score_input_pos)
            if self.system.filler.submit_btn_pos:
                cfg["submit_btn_pos"] = list(self.system.filler.submit_btn_pos)
            if self.system.filler.next_btn_pos:
                cfg["next_btn_pos"] = list(self.system.filler.next_btn_pos)
        return cfg

    def _normalize_number_list(self, value, length: int, as_int: bool = False):
        if not isinstance(value, (list, tuple)) or len(value) != length:
            return None
        try:
            vals = [int(v) if as_int else float(v) for v in value]
        except (TypeError, ValueError):
            return None
        return vals

    def _sync_runtime_config_to_system(self):
        if self.system is None:
            return
        region = self._normalize_number_list(self._runtime_config.get("screenshot_region_norm"), 4)
        if region:
            self.system.screenshot_tool.selected_region_norm = tuple(region)
        for key in ("score_input_pos", "submit_btn_pos", "next_btn_pos"):
            pos = self._normalize_number_list(self._runtime_config.get(key), 2, as_int=True)
            if pos:
                setattr(self.system.filler, key, tuple(pos))
        self._update_region_status()
        self._update_ready_status()

    def _on_region_selected(self, region):
        self._runtime_config["screenshot_region_norm"] = list(region)
        self._update_region_status()
        self._update_ready_status()
        print("[配置] 截图区域已更新（尚未保存，点击 文件 → 保存配置 后写入 config.json）")
        messagebox.showinfo("完成", "截图区域已选择。")

    def _on_position_selected(self, attr_name, pos):
        self._runtime_config[attr_name] = list(pos)
        self._update_ready_status()
        print(f"[配置] {attr_name} 已更新（尚未保存，点击 文件 → 保存配置 后写入 config.json）")

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

        if "blank_threshold" in cfg:
            try:
                self._set_blank_threshold(float(cfg["blank_threshold"]))
            except (TypeError, ValueError, tk.TclError):
                self._set_blank_threshold(15.0)
            self._sync_blank_threshold_label()

        if "filler_mode" in cfg:
            pass

        for key in ("screenshot_region_norm", "score_input_pos", "submit_btn_pos", "next_btn_pos"):
            if key in cfg:
                self._runtime_config[key] = cfg.get(key)

        self._sync_provider_state()
        self._sync_filler_state()
        self._sync_runtime_config_to_system()
        self._update_region_status()
        self._update_ready_status()

    def _save_config(self, silent: bool = False):
        cfg = self._collect_config()
        try:
            self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            if not silent:
                messagebox.showinfo("成功", f"配置已保存到：{self.config_path}")
        except Exception as e:
            if silent:
                print(f"保存配置失败：{e}")
            else:
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
            self.system.blank_threshold = self._get_blank_threshold()
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
        if provider == "智谱AI":
            scorer = ZhipuAIScorer(api_key=api_key, model=model)
        elif provider == "百度千帆":
            scorer = BaiduScorer(api_key=api_key, model=model)
        elif provider == "科大讯飞":
            scorer = XunfeiScorer(api_key=api_key, model=model)
        else:
            base_url = (self.base_url_var.get() or "").strip()
            if not base_url and provider != "自定义":
                preset = self.PROVIDER_PRESETS.get(provider)
                if preset:
                    base_url = preset[0]
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
            on_score_callback=self._tune_add_record,
            on_region_selected=self._on_region_selected,
            on_position_selected=self._on_position_selected,
            before_capture=self._before_capture,
            after_capture=self._after_capture,
            blank_threshold=self._get_blank_threshold(),
        )
        self._system_cfg_key = new_key
        self._sync_runtime_config_to_system()

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

    def _select_multi_regions(self):
        try:
            sys = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        sys.screenshot_tool.select_regions_interactive(self)

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

    def _test_blank_detection(self):
        try:
            sys_ = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return

        try:
            img = sys_.screenshot_tool.capture_current_question()
            if img is None:
                raise ValueError("没有拿到截图，请先选择截图区域或检查截图权限。")
            path = self.capture_dir / f"__blank_test_{int(time.time())}.png"
            img.save(path)

            from PIL import ImageStat
            stat = ImageStat.Stat(img.convert("L"))
            stddev = float(stat.stddev[0])
            threshold = self._get_blank_threshold()
            is_blank = stddev < threshold
            result = "空白" if is_blank else "非空白"
            msg = f"灰度波动：{stddev:.1f}\n当前阈值：{threshold:.1f}\n判定结果：{result}\n截图已保存：{path}"
            print(f"[空白检测测试] 灰度波动={stddev:.1f} 阈值={threshold:.1f} 判定={result} 文件={path}")
            messagebox.showinfo("空白检测测试", msg)
        except Exception as e:
            messagebox.showerror("测试失败", str(e))

    def _mark_blank_paper(self):
        try:
            sys_ = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        try:
            img = sys_.screenshot_tool.capture_current_question()
            if img is None:
                raise ValueError("没有拿到截图，请先选择截图区域或检查截图权限。")
            path = self.capture_dir / f"__blank_mark_{int(time.time())}.png"
            img.save(path)
            from PIL import ImageStat
            stat = ImageStat.Stat(img.convert("L"))
            stddev = float(stat.stddev[0])
            new_threshold = round(stddev + 2.0, 1)
            new_threshold = max(0.0, min(40.0, new_threshold))
            self._set_blank_threshold(new_threshold)
            if self.system is not None:
                self.system.blank_threshold = new_threshold
            print(f"[标记空白卷] 灰度波动={stddev:.1f} 已设置阈值={new_threshold:.1f} 文件={path}")
            messagebox.showinfo("标记空白卷", f"已识别空白卷灰度波动：{stddev:.1f}\n已自动设置空白阈值为：{new_threshold:.1f}\n\n后续灰度波动低于此值的截图将被判定为空白卷。")
        except Exception as e:
            messagebox.showerror("标记失败", str(e))

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
                    self.progress_var.set(f"批量中：已处理 {sys_.question_count} 份")
            else:
                self.progress_var.set("单题处理中…")
            self.after(350, self._poll_progress)
        else:
            if sys_.batch_mode:
                self.progress_var.set(f"已停止（已处理 {sys_.question_count} 份）")
            else:
                self.progress_var.set("已完成（单题）")

    def _stop(self):
        if self.system:
            self.system.stop()
        self.progress_var.set("已停止")

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _export_scores_csv(self):
        default_name = f"评分记录_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(
            title="导出评分记录",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            count = self.score_db.export_csv(path)
            messagebox.showinfo("导出完成", f"已导出 {count} 条评分记录：\n{path}")
            print(f"[评分数据库] 已导出 {count} 条记录：{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _clear_captures(self):
        if not messagebox.askyesno("确认", "确定要清空截图目录中的所有文件吗？"):
            return
        try:
            files = list(self.capture_dir.iterdir())
            if not files:
                messagebox.showinfo("提示", "截图目录已经是空的。")
                return
            count = 0
            for f in files:
                if f.is_file():
                    f.unlink()
                    count += 1
            print(f"[清空截图] 已删除 {count} 个文件")
        except Exception as e:
            messagebox.showerror("清空失败", str(e))

    # ── 生成评分标准 ──

    def _generate_criteria_from_screenshot(self):
        """从当前屏幕截图生成评分标准"""
        try:
            sys_ = self._ensure_system()
        except Exception as e:
            messagebox.showerror("配置不完整", str(e))
            return
        try:
            img = sys_.screenshot_tool.capture_current_question()
            if img is None:
                raise ValueError("请先选择截图区域（菜单 截图配置 → 选择截图区域）")
            path = self.capture_dir / f"__criteria_gen_{int(time.time())}.png"
            img.save(path)
            print(f"[生成评分标准] 已截图：{path}")
            self._generate_criteria(path)
        except Exception as e:
            messagebox.showerror("截图失败", str(e))

    def _generate_criteria_from_file(self):
        """从图片文件生成评分标准"""
        path = filedialog.askopenfilename(
            title="选择题目图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        if not path:
            return
        print(f"[生成评分标准] 已选择文件：{path}")
        self._generate_criteria(path)

    def _generate_criteria(self, image_path):
        """核心方法：将图片发送给 AI，生成评分标准并填入文本框"""
        api_key = (self.api_key_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()

        if not api_key:
            messagebox.showerror("配置不完整", "请先填写 API Key")
            return
        if not model:
            messagebox.showerror("配置不完整", "请先填写模型")
            return

        provider = self.provider_var.get()

        existing_criteria = self.criteria_text.get("1.0", "end").strip()

        if existing_criteria:
            prompt = (
                "你是一个考试命题和评分标准制定专家。\n\n"
                "请根据这张题目图片，制定详细的阅卷评分标准。\n\n"
                f"## 参考：现有评分标准（请在此基础上改进、补充，保留合理的部分）\n{existing_criteria}\n\n"
                "要求：\n"
                "1. 明确指出各题/各小问的分值分布\n"
                "2. 列出每个得分点和扣分标准\n"
                "3. 评分标准要具体、可操作，方便AI对照评分\n"
                "4. 严格按照以下格式输出（包括冒号和标题）：\n"
                "\n"
                "总分：<总分数>分\n"
                "\n"
                "评分细则：\n"
                "<题号1> <分值>分。得分标准：<评分要点>\n"
                "<题号2> <分值>分。扣分说明：<扣分标准>\n"
                "\n"
                "请直接输出优化后的完整评分标准，不要输出多余内容。"
            )
        else:
            prompt = (
                "你是一个考试命题和评分标准制定专家。\n\n"
                "请根据这张题目图片，制定详细的阅卷评分标准。要求：\n"
                "1. 明确指出各题/各小问的分值分布\n"
                "2. 列出每个得分点和扣分标准\n"
                "3. 评分标准要具体、可操作，方便AI对照评分\n"
                "4. 严格按照以下格式输出（包括冒号和标题）：\n"
                "\n"
                "总分：<总分数>分\n"
                "\n"
                "评分细则：\n"
                "<题号1> <分值>分。得分标准：<评分要点>\n"
                "<题号2> <分值>分。扣分说明：<扣分标准>\n"
                "\n"
                "请直接输出评分标准，不要输出多余内容。"
            )

        print(f"[生成评分标准] 正在调用 AI（{provider}/{model}）…")

        try:
            # 先压缩图片，避免原始文件过大导致连接被重置
            from PIL import Image
            img = Image.open(image_path)
            # RGBA/PA 转 RGB（JPEG 不支持 alpha）
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            max_dim = 2048
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            temp_path = str(self.capture_dir / f"__criteria_compressed_{int(time.time())}.jpg")
            with open(temp_path, "wb") as f:
                f.write(buf.getvalue())
            print(f"[生成评分标准] 图片已压缩：{os.path.getsize(temp_path) / 1024:.0f} KB")

            # 复用现有评分器发送请求（已验证的工作路径，兼容所有服务商）
            sys_ = self._ensure_system()
            # 强制延长超时，避免长 prompt 推理中断
            if hasattr(sys_.scorer, "timeout"):
                sys_.scorer.timeout = 180
            # 打印诊断信息
            if hasattr(sys_.scorer, "base_url"):
                print(f"[生成评分标准] 请求 URL 基础路径: {sys_.scorer.base_url}")

            # 带重试的评分调用
            last_err = None
            for attempt in range(3):
                try:
                    sys_.scorer.grade_answer(temp_path, prompt)
                    break
                except Exception as retry_err:
                    last_err = retry_err
                    if attempt < 2:
                        print(f"[生成评分标准] 第 {attempt+1} 次失败，2 秒后重试: {retry_err}")
                        time.sleep(2)
                    else:
                        raise last_err

            info = sys_.scorer.get_last_response()
            if not info or not info.get("full_response", "").strip():
                raise ValueError("AI 返回内容为空")
            result = info["full_response"].strip()

            self.criteria_text.delete("1.0", "end")
            self.criteria_text.insert("1.0", result)
            print(f"[生成评分标准] 已填入评分标准框 ({len(result)} 字)")
            messagebox.showinfo("完成", "评分标准已生成并填入上方文本框。")

        except Exception as e:
            messagebox.showerror("生成失败", f"生成评分标准时出错：{e}")

    def _drain_log_queue(self):
        try:
            while True:
                s = self._log_q.get_nowait()
                self.log_text.insert("end", s)
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(60, self._drain_log_queue)

    # ── 规则调优方法 ──

    def _get_tune_record_by_item(self, item_id):
        try:
            values = self.tune_tree.item(item_id).get("values", [])
        except tk.TclError:
            return None
        if not values:
            return None
        try:
            idx = int(values[0])
        except (TypeError, ValueError):
            return None
        return next((r for r in self.tuner.records if r.index == idx), None)

    def _on_tune_tree_motion(self, event):
        row_id = self.tune_tree.identify_row(event.y)
        if not row_id:
            self._hide_tune_preview()
            return
        record = self._get_tune_record_by_item(row_id)
        if record is None:
            self._hide_tune_preview()
            return
        self._tune_preview_last_xy = (event.x_root, event.y_root)
        if row_id == self._tune_preview_item and self._tune_preview_window is not None:
            self._position_tune_preview(event.x_root, event.y_root)
            return
        self._show_tune_preview(row_id, record, event.x_root, event.y_root)

    def _position_tune_preview(self, x_root, y_root):
        if self._tune_preview_window is None:
            return
        try:
            self._tune_preview_window.geometry(f"+{x_root + 16}+{y_root + 16}")
        except tk.TclError:
            self._tune_preview_window = None

    def _show_tune_preview(self, item_id, record, x_root, y_root):
        self._hide_tune_preview()
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        frame = ttk.Frame(win, padding=8, relief="solid", borderwidth=1)
        frame.pack(fill=tk.BOTH, expand=True)

        manual_score = "未标记" if record.manual_score is None else f"{record.manual_score}分"
        info_lines = [
            f"记录序号：{record.index}",
            f"AI 分数：{record.ai_score}分",
            f"正确分数：{manual_score}",
            f"状态：{record.status}",
        ]
        for line in info_lines:
            ttk.Label(frame, text=line).pack(anchor="w")

        # 显示 AI 反馈信息
        ai_resp = (record.ai_response or "").strip()
        if ai_resp:
            if "===反馈开始===" in ai_resp and "===反馈结束===" in ai_resp:
                feedback = ai_resp.split("===反馈开始===")[1].split("===反馈结束===")[0].strip()
            else:
                feedback = ai_resp
            if feedback:
                ttk.Separator(frame, orient="horizontal").pack(fill=tk.X, pady=4)
                ttk.Label(frame, text="AI 反馈：", font=("", 9, "bold")).pack(anchor="w")
                fb_text = tk.Text(frame, height=4, wrap="word", font=("微软雅黑", 9))
                fb_text.insert("1.0", feedback)
                fb_text.configure(state="disabled")
                fb_text.pack(fill=tk.X, pady=(2, 4))

        image_path = (record.image_path or "").strip()
        if not image_path:
            ttk.Label(frame, text="截图路径缺失").pack(anchor="w", pady=(6, 0))
            self._tune_preview_image_ref = None
        else:
            path = Path(image_path)
            ttk.Label(frame, text=f"截图：{path.name}").pack(anchor="w", pady=(6, 0))
            if not path.exists():
                ttk.Label(frame, text="截图文件不存在").pack(anchor="w")
                self._tune_preview_image_ref = None
            else:
                try:
                    with Image.open(path) as img:
                        img.thumbnail((420, 320))
                        photo = ImageTk.PhotoImage(img.copy())
                    self._tune_preview_image_ref = photo
                    ttk.Label(frame, image=photo).pack(anchor="w", pady=(4, 0))
                except Exception as e:
                    ttk.Label(frame, text=f"截图预览失败：{e}").pack(anchor="w")
                    self._tune_preview_image_ref = None

        self._tune_preview_window = win
        self._tune_preview_item = item_id
        self._position_tune_preview(x_root, y_root)

    def _hide_tune_preview(self, event=None):
        if self._tune_preview_window is not None:
            try:
                self._tune_preview_window.destroy()
            except tk.TclError:
                pass
        self._tune_preview_window = None
        self._tune_preview_image_ref = None
        self._tune_preview_item = None

    def _tune_add_record(self, question_index, score, response_info, image_path=""):
        """从评分回调线程接收记录（线程安全）"""
        self.after(0, self._tune_add_record_ui, question_index, score, response_info, image_path)

    def _save_score_record(self, record, question_index):
        mode = "batch" if self.batch_var.get() else "single"
        question_value = "single" if question_index is None else str(question_index)
        db_id = self.score_db.insert_record(
            session_id=self._score_session_id,
            record_index=record.index,
            question_index=question_value,
            mode=mode,
            provider=self.provider_var.get(),
            model=(self.model_var.get() or "").strip(),
            base_url=(self.base_url_var.get() or "").strip(),
            ai_score=record.ai_score,
            manual_score=record.manual_score,
            status=record.status,
            criteria=record.criteria,
            ai_response=record.ai_response,
            image_path=record.image_path,
        )
        self._record_db_ids[record.index] = db_id
        return db_id

    def _tune_add_record_ui(self, question_index, score, response_info, image_path=""):
        idx = self._next_record_index
        self._next_record_index += 1
        criteria = self.criteria_text.get("1.0", "end").strip()
        record = ScoringRecord(
            index=idx,
            ai_score=score,
            ai_response=response_info.get("full_response", ""),
            criteria=criteria,
            image_path=image_path or "",
        )
        self.tuner.add_record(record)
        try:
            self._save_score_record(record, question_index)
        except Exception as e:
            print(f"[评分数据库] 写入失败：{e}")
        self.tune_tree.insert("", "end", values=(idx, score, "—", "待标记"))
        q_label = f"题目 {question_index}" if question_index is not None else "当前题目"
        print(f"[规则调优] 记录 #{idx} 已添加 | {q_label} | AI分数：{score}分")
        self._tune_update_status()

    def _on_tune_tree_select(self, event):
        sel = self.tune_tree.selection()
        if sel:
            item = self.tune_tree.item(sel[0])
            vals = item["values"]
            if vals:
                self.tune_manual_var.set(str(vals[1]))  # 预设为 AI 分数

    def _tune_mark_score(self):
        sel = self.tune_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选择一条记录")
            return
        raw = self.tune_manual_var.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请输入正确分数")
            return
        try:
            manual = int(raw)
        except ValueError:
            messagebox.showerror("错误", "分数必须是整数")
            return

        item = self.tune_tree.item(sel[0])
        try:
            idx = int(item["values"][0])
        except (TypeError, ValueError):
            messagebox.showerror("错误", "记录序号无效")
            return
        ok = self.tuner.set_manual_score(idx, manual)
        if not ok:
            return
        # 更新 treeview
        record = next(r for r in self.tuner.records if r.index == idx)
        self.tune_tree.item(sel[0], values=(idx, record.ai_score, manual, record.status))
        db_id = self._record_db_ids.get(idx)
        if db_id is not None:
            try:
                self.score_db.update_manual_score(db_id, manual, record.status)
            except Exception as e:
                print(f"[评分数据库] 更新人工分失败：{e}")
        if self._tune_preview_item == sel[0]:
            x_root, y_root = self._tune_preview_last_xy
            self._show_tune_preview(sel[0], record, x_root, y_root)
        self._tune_update_status()

    def _run_tuning(self):
        if self._tuning_running:
            return
        criteria = self.criteria_text.get("1.0", "end").strip()
        if not criteria:
            messagebox.showwarning("提示", "请先填写评分标准再调优")
            return
        stats = self.tuner.get_stats()
        if stats["mismatches"] == 0 and stats["marked"] < 2:
            messagebox.showwarning("提示", f"已标记 {stats['marked']} 条，需要至少 1 条偏差记录（或 ≥2 条已标记记录）才能调优")
            return

        # 同步 tuner 的 API 配置
        api_key = (self.api_key_var.get() or "").strip()
        base_url = (self.base_url_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()
        extra_headers_raw = (self.extra_headers_var.get() or "").strip()
        extra_headers = {}
        if extra_headers_raw:
            try:
                extra_headers = json.loads(extra_headers_raw)
            except Exception:
                pass
        self.tuner.update_config(api_key=api_key, base_url=base_url, model=model, extra_headers=extra_headers)

        self._tuning_running = True
        self.tune_status_var.set("正在分析调优…")
        self.tune_result_text.configure(state="normal")
        self.tune_result_text.delete("1.0", "end")
        self.tune_result_text.insert("1.0", "正在调用大模型分析评分偏差，请稍候…\n")
        self.tune_result_text.configure(state="disabled")
        self.update_idletasks()

        def _do_tune():
            try:
                result = self.tuner.tune(criteria)
                self.after(0, self._tune_done, result)
            except Exception as e:
                self.after(0, self._tune_error, str(e))

        t = threading.Thread(target=_do_tune, daemon=True)
        t.start()

    def _tune_done(self, result):
        self._tuning_running = False
        self.tune_result_text.configure(state="normal")
        self.tune_result_text.delete("1.0", "end")
        if result is None:
            self.tune_result_text.insert("1.0", "数据不足：需要至少 1 条偏差记录或 ≥2 条已标记记录。")
        else:
            self.tune_result_text.insert("1.0", result)
        self.tune_result_text.configure(state="disabled")
        self._tune_update_status()
        if self.tuner.suggested_criteria:
            messagebox.showinfo("规则调优", "调优完成！点击「应用新规则」可将优化后的规则写入评分标准。")

    def _tune_error(self, err):
        self._tuning_running = False
        self.tune_result_text.configure(state="normal")
        self.tune_result_text.delete("1.0", "end")
        self.tune_result_text.insert("1.0", f"调优失败：{err}")
        self.tune_result_text.configure(state="disabled")
        self._tune_update_status()

    def _apply_tuning(self):
        if not self.tuner.suggested_criteria:
            messagebox.showwarning("提示", "没有可用的优化规则，请先执行「规则调优」")
            return
        self.criteria_text.delete("1.0", "end")
        self.criteria_text.insert("1.0", self.tuner.suggested_criteria)
        messagebox.showinfo("成功", "优化后的评分标准已应用到评分标准输入框")
        print("[规则调优] 已应用优化后的评分标准")

    def _clear_tuning(self):
        self._hide_tune_preview()
        self.tuner.records.clear()
        self.tuner.suggested_criteria = ""
        self._next_record_index = 0
        for item in self.tune_tree.get_children():
            self.tune_tree.delete(item)
        self.tune_result_text.configure(state="normal")
        self.tune_result_text.delete("1.0", "end")
        self.tune_result_text.configure(state="disabled")
        self._tune_update_status()

    def _tune_update_status(self):
        stats = self.tuner.get_stats()
        try:
            db_stats = self.score_db.get_stats()
            avg_score = db_stats["avg_ai_score"]
            history = f" | 历史 {db_stats['total']} 条 | 平均AI {avg_score:.1f}分"
        except Exception:
            history = ""
        self.tune_status_var.set(
            f"本次 {stats['total']} 条 | 已标记 {stats['marked']} 条 | 偏差 {stats['mismatches']} 条{history}"
        )

    # ── 快捷优化评分标准 ──

    def _optimize_criteria(self):
        suggestion = self.optimize_suggestion_text.get("1.0", "end").strip()
        if not suggestion:
            messagebox.showwarning("提示", "请先输入优化建议")
            return

        criteria = self.criteria_text.get("1.0", "end").strip()
        if not criteria:
            messagebox.showwarning("提示", "请先填写评分标准")
            return

        api_key = (self.api_key_var.get() or "").strip()
        if not api_key:
            messagebox.showerror("配置不完整", "请先填写 API Key")
            return

        base_url = (self.base_url_var.get() or "").strip()
        model = (self.model_var.get() or "").strip()
        extra_headers_raw = (self.extra_headers_var.get() or "").strip()
        extra_headers = {}
        if extra_headers_raw:
            try:
                extra_headers = json.loads(extra_headers_raw)
            except Exception:
                pass

        prompt = (
            "你是一个专业的考试评分规则优化专家。\n\n"
            f"## 当前评分规则\n{criteria}\n\n"
            f"## 用户优化建议\n{suggestion}\n\n"
            "## 任务\n"
            "请根据用户的优化建议，改进上述评分规则。要求：\n"
            "- 保留原有规则中合理的部分\n"
            "- 只根据用户建议做针对性的修改\n"
            "- 输出格式保持清晰、可读、可直接用于评分\n"
            "- 不要添加无关的说明文字\n\n"
            "直接输出优化后的完整评分规则。"
        )

        self.optimize_btn.configure(state="disabled")
        self.optimize_status_var.set("正在调用 AI 优化，请稍候…")
        self.optimize_result_text.configure(state="normal")
        self.optimize_result_text.delete("1.0", "end")
        self.optimize_result_text.insert("1.0", "正在分析并优化评分标准…\n")
        self.optimize_result_text.configure(state="disabled")
        self.apply_opt_btn.configure(state="disabled")
        self.update_idletasks()

        def _do_optimize():
            try:
                from modules.自动评分模块 import call_llm_text
                result = call_llm_text(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    prompt=prompt,
                    extra_headers=extra_headers,
                    timeout=120,
                )
                self.after(0, self._optimize_done, result)
            except Exception as e:
                self.after(0, self._optimize_error, str(e))

        t = threading.Thread(target=_do_optimize, daemon=True)
        t.start()

    def _optimize_done(self, result):
        self.optimize_btn.configure(state="normal")
        self.optimize_status_var.set("优化完成")
        self.optimize_result_text.configure(state="normal")
        self.optimize_result_text.delete("1.0", "end")
        self.optimize_result_text.insert("1.0", result)
        self.optimize_result_text.configure(state="disabled")
        self.apply_opt_btn.configure(state="normal")
        self._optimized_result = result
        messagebox.showinfo("优化完成", "优化后的评分标准已生成，点击「应用新规则」可将其写入评分标准输入框。")

    def _optimize_error(self, err):
        self.optimize_btn.configure(state="normal")
        self.optimize_status_var.set("优化失败")
        self.optimize_result_text.configure(state="normal")
        self.optimize_result_text.delete("1.0", "end")
        self.optimize_result_text.insert("1.0", f"优化失败：{err}")
        self.optimize_result_text.configure(state="disabled")
        self.apply_opt_btn.configure(state="disabled")

    def _apply_optimized_criteria(self):
        result = getattr(self, "_optimized_result", "")
        if not result:
            messagebox.showwarning("提示", "没有可用的优化结果，请先执行「优化」")
            return
        self.criteria_text.delete("1.0", "end")
        self.criteria_text.insert("1.0", result)
        messagebox.showinfo("成功", "优化后的评分标准已应用到评分标准输入框")

    def destroy(self):
        try:
            if self.system:
                self.system.stop()
        finally:
            sys.stdout = self._orig_stdout
            sys.stderr = self._orig_stderr
            super().destroy()


def main():
    app = App()
    try:
        app.mainloop()
    finally:
        time.sleep(0.05)


if __name__ == "__main__":
    main()

