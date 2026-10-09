#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上层 GUI：调用 `自动阅卷系统GUI.py` 核心模块完成阅卷。

界面分四个选项卡（每个选项卡内的模块均可折叠）：
- 阅卷主界面：阅卷参数、运行、AI 评分记录、运行日志
- 接口与模型：接口配置/模型选择、多模型交叉校验
- 评分标准：评分标准编辑、快捷优化建议
- API 配置：维护自定义 API（名称/服务商/base_url/API Key/模型列表），保存到 config.json

运行：
python 上层GUI.py
"""

__version__ = "1.12.0"

import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import json
import os
import queue
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageTk

from 自动阅卷系统GUI import AutoScoringSystem, check_dependencies
from modules.自动截图模块 import format_tk_geometry, get_virtual_screen_geometry
from modules.自动评分模块 import (
    API_MAX_RETRIES,
    API_RETRIES_MAX,
    API_RETRIES_MIN,
    API_TIMEOUT_MAX,
    API_TIMEOUT_MIN,
    API_TIMEOUT_SECONDS,
    OpenAICompatibleScorer,
    ZhipuAIScorer,
    BaiduScorer,
    XunfeiScorer,
    configure_request_policy,
    fetch_openai_compatible_models,
    get_request_policy,
)
from modules.多模型校验模块 import MultiModelCrossChecker
from modules.系统通知模块 import send_windows_notification
# AutoFiller was previously imported but not used in this file; remove to avoid unused-import errors
from modules.规则调优模块 import RuleTuner, ScoringRecord
from modules.评分数据库模块 import ScoringDatabase

# 「接口类型」下拉框显示文本 -> 内部常量
_API_TYPE_BY_LABEL = {
    "自动判断": "auto",
    "Chat Completions": "chat",
    "Responses API": "responses",
}


class _QueueStdout:
    def __init__(self, q: "queue.Queue[str]"):
        self._q = q

    def write(self, s: str):
        if s:
            self._q.put(s)

    def flush(self):
        pass


def _theme_bg(widget) -> str:
    """推断 ttk 主题的默认背景色，用于让自绘卡片与整体界面协调。"""
    try:
        bg = ttk.Style(widget).lookup("TFrame", "background")
        if bg:
            return str(bg)
    except tk.TclError:
        pass
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return "#f0f0f0"


class CollapsibleFrame(tk.Frame):
    """可折叠的模块卡片：点击标题栏展开/收起内容，箭头指示当前状态。

    约定：
    - 内容请放进 `frame.body`（一个普通的 tk.Frame 容器）。
    - `name` 为持久化标识，配合上层窗口保存/恢复折叠状态。
    - `on_toggle(name, collapsed)` 在用户点击标题栏时回调。
    """

    _BG_HEADER = "#e8eef7"
    _BG_HEADER_HOVER = "#d8e3f3"
    _FG_TITLE = "#1f2d3d"
    _FG_ARROW = "#3a4a5f"
    _BORDER = "#c2cee0"

    def __init__(
        self,
        parent,
        title: str = "",
        *,
        name: str | None = None,
        collapsed: bool = False,
        on_toggle: Callable[[str, bool], None] | None = None,
        bg: str | None = None,
        **kwargs,
    ):
        base_bg = bg or _theme_bg(parent)
        super().__init__(
            parent,
            bg=base_bg,
            bd=0,
            highlightthickness=1,
            highlightbackground=self._BORDER,
            highlightcolor=self._BORDER,
            **kwargs,
        )
        self.section_name = name
        self._collapsed = bool(collapsed)
        self._on_toggle_cb = on_toggle

        self._header = tk.Frame(self, bg=self._BG_HEADER, cursor="hand2")
        self._header.pack(side=tk.TOP, fill=tk.X)

        self._arrow = tk.Label(
            self._header,
            text="",
            bg=self._BG_HEADER,
            fg=self._FG_ARROW,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self._arrow.pack(side=tk.LEFT, padx=(8, 2), pady=5)

        self._title = tk.Label(
            self._header,
            text=title,
            bg=self._BG_HEADER,
            fg=self._FG_TITLE,
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
            justify="left",
            cursor="hand2",
        )
        self._title.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5, padx=(0, 8))

        self.body = tk.Frame(self, bg=base_bg)

        for w in (self._header, self._arrow, self._title):
            w.bind("<Button-1>", self._toggle_event)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

        self.set_collapsed(self._collapsed, notify=False)

    # ── 内部样式 ──
    def _paint_header(self, bg: str):
        for w in (self._header, self._arrow, self._title):
            w.configure(bg=bg)

    def _on_enter(self, _event=None):
        self._paint_header(self._BG_HEADER_HOVER)

    def _on_leave(self, _event=None):
        self._paint_header(self._BG_HEADER)

    def _toggle_event(self, _event=None):
        self.set_collapsed(not self._collapsed)

    # ── 公共接口 ──
    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool, notify: bool = True):
        self._collapsed = bool(collapsed)
        if self._collapsed:
            self.body.pack_forget()
            self._arrow.configure(text="\u25b6")  # ▶
        else:
            self.body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self._arrow.configure(text="\u25bc")  # ▼
        if notify and self._on_toggle_cb is not None:
            self._on_toggle_cb(self.section_name or "", self._collapsed)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("自动阅卷 - 上层GUI")
        self.geometry("800x600")
        self.attributes("-topmost", True)  # type: ignore
        # call lift after a short delay; use a lambda to avoid type-checker
        # complaints about the bound method signature
        # mypy/pyright may report the type of `lift` as partially unknown; silence
        # that complaint here.
        self.after(200, lambda: self.lift())  # type: ignore

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
        # 已保存的接口配置（API 配置选项卡维护，随 config.json 持久化）
        self._api_profiles: list[dict] = []
        self._active_profile_var = tk.StringVar(value="")
        self._ap_loaded_name = ""
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
        # 本轮评分计时（用于「本次评分总用时」展示）
        self._run_started_at: float | None = None

        # 可折叠模块（界面优化）：name -> CollapsibleFrame，配合折叠状态持久化
        self._sections: dict[str, CollapsibleFrame] = {}
        self._ui_section_state: dict[str, bool] = {}
        # 选项卡分组：tab_key -> [section name...]（供「全部展开/折叠」按页生效）
        self._tab_section_names: dict[str, list[str]] = {}
        # 可滚动页面：选项卡索引 -> Canvas
        self._scroll_canvases: dict[int, tk.Canvas] = {}

        self._build_menu()
        self._build_ui()
        self._load_config(silent=True)
        self._ensure_default_profile()
        self._apply_request_policy()  # 超时/重试：配置文件缺失时也保证策略生效
        self._sync_batch_state()
        self._update_ready_status()
        self._load_score_history()
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
        win.tk.call("wm", "attributes", str(win), "-topmost", True)
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

        # ── 选项卡容器：阅卷主界面 / 接口与模型 / 评分标准 / API 配置 ──
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._MAIN_TAB_INDEX = 0
        self._MODEL_TAB_INDEX = 1
        self._CRITERIA_TAB_INDEX = 2
        self._API_TAB_INDEX = 3

        main_tab = ttk.Frame(self._notebook)
        self._notebook.add(main_tab, text="  阅卷主界面  ")
        model_tab = ttk.Frame(self._notebook)
        self._notebook.add(model_tab, text="  接口与模型  ")
        criteria_tab = ttk.Frame(self._notebook)
        self._notebook.add(criteria_tab, text="  评分标准  ")
        api_tab = ttk.Frame(self._notebook)
        self._notebook.add(api_tab, text="  API 配置  ")

        def _make_text_yview_command(text: tk.Text) -> Callable[..., None]:
            def _scroll_text(*args: str) -> None:
                text.tk.call(str(text), "yview", *args)

            return _scroll_text

        # ── 每个选项卡：顶部固定工具栏 + 独立可滚动内容区 ──
        self._build_section_toolbar(main_tab, "main")
        self._build_section_toolbar(model_tab, "model")
        self._build_section_toolbar(criteria_tab, "criteria")
        self._build_section_toolbar(api_tab, "api")

        main_inner = self._create_scroll_page(main_tab, self._MAIN_TAB_INDEX)
        model_inner = self._create_scroll_page(model_tab, self._MODEL_TAB_INDEX)
        criteria_inner = self._create_scroll_page(criteria_tab, self._CRITERIA_TAB_INDEX)

        # 鼠标滚轮：滚动当前选项卡对应的内容区
        self._notebook.bind_all("<MouseWheel>", self._on_mousewheel_global)

        # ── 可折叠模块工厂（界面优化） ──
        base_bg = _theme_bg(self)

        def _section_factory(target_inner: tk.Widget, tab_key: str):
            def _make_section(name: str, title: str, *, collapsed: bool = False,
                              fill: str = tk.X, expand: bool = False) -> CollapsibleFrame:
                sec = CollapsibleFrame(
                    target_inner,
                    title=title,
                    name=name,
                    collapsed=collapsed,
                    bg=base_bg,
                    on_toggle=self._on_section_toggle,
                )
                sec.pack(side=tk.TOP, fill=fill, expand=expand, padx=pad["padx"], pady=pad["pady"])
                self._register_section(name, sec, tab_key)
                return sec

            return _make_section

        make_main = _section_factory(main_inner, "main")
        make_model = _section_factory(model_inner, "model")
        make_criteria = _section_factory(criteria_inner, "criteria")

        sec_conn = make_model("connection", "接口与模型（选择接口配置与模型后即可开始阅卷）")
        top = ttk.Frame(sec_conn.body)
        # Avoid passing geometry options as a positional argument to pack
        # (some type checkers treat them as the first positional `cnf` arg).
        # Expand padding explicitly to satisfy strict type checkers.
        top.pack(side="top", fill=tk.X, padx=8, pady=8)

        # 当前生效的连接参数（由「接口配置」自动填充，在「API 配置」选项卡维护）
        self.provider_var = tk.StringVar(value="OpenAI")
        self.api_key_var = tk.StringVar()
        self.model_var = tk.StringVar(value="gpt-4o-mini")
        self.base_url_var = tk.StringVar(value="https://api.openai.com")
        self.extra_headers_var = tk.StringVar(value="")
        self.thinking_mode_var = tk.StringVar(value="自动")
        self.api_type_var = tk.StringVar(value="自动判断")

        # 第 0 行：接口配置（已保存的供应商/API）
        ttk.Label(top, text="接口配置").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(
            top,
            textvariable=self._active_profile_var,
            width=30,
            values=[],
            state="readonly",
        )
        self.profile_combo.grid(row=0, column=1, columnspan=2, sticky="we", padx=(6, 0))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_main_profile_combo)
        ttk.Button(top, text="管理配置…", command=self._goto_api_tab).grid(row=0, column=3, sticky="w", padx=(6, 0))

        # 第 1 行：模型选择
        ttk.Label(top, text="模型").grid(row=1, column=0, sticky="w")
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, width=30, values=[])
        self.model_combo.grid(row=1, column=1, columnspan=2, sticky="we", padx=(6, 0))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_main_model_combo)
        self.fetch_models_btn = ttk.Button(top, text="获取模型列表", command=self._fetch_models)
        self.fetch_models_btn.grid(row=1, column=3, sticky="w", padx=(6, 0))

        # 第 2 行：当前连接摘要
        ttk.Label(top, text="当前连接").grid(row=2, column=0, sticky="w")
        self.conn_summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.conn_summary_var, foreground="#444444", wraplength=760, justify="left").grid(
            row=2, column=1, columnspan=3, sticky="w", padx=(6, 0)
        )

        ttk.Label(
            top,
            text="提示：接口、密钥、模型列表与「请求设置（超时/重试）」都在「API 配置」选项卡中维护；主界面直接选择配置好的供应商与模型即可。",
            foreground="#777777",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))

        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=1)

        # trace_add callback receives (name, index, mode) -> provide explicit params for static type checkers
        self.provider_var.trace_add("write", lambda name, index, mode: self._sync_provider_state())
        self._sync_provider_state()

        sec_criteria = make_criteria("criteria", "评分标准（直接粘贴你的阅卷要求 / 评分细则）")
        _criteria_inner = ttk.Frame(sec_criteria.body)
        _criteria_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.criteria_text = tk.Text(_criteria_inner, height=8, wrap="word")
        _criteria_sb = ttk.Scrollbar(_criteria_inner, command=_make_text_yview_command(self.criteria_text))
        self.criteria_text.configure(yscrollcommand=_criteria_sb.set)
        _criteria_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.criteria_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sec_params = make_main("params", "阅卷参数（批量模式 / 空白检测 / 准备状态）")
        mid = ttk.Frame(sec_params.body)
        mid.pack(fill=tk.X, padx=8, pady=8)

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


        sec_run = make_main("run", "运行（开始阅卷 / 停止 / 运行选项）")
        runbox = sec_run.body
        self.start_btn = ttk.Button(runbox, text="开始单题阅卷", command=self._start)
        self.start_btn.grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="停止", command=self._stop).grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ttk.Button(runbox, text="清空日志", command=self._clear_log).grid(row=0, column=2, padx=8, pady=8, sticky="w")

        self.progress_var = tk.StringVar(value="未开始")
        ttk.Label(runbox, textvariable=self.progress_var).grid(row=0, column=3, padx=12, pady=8, sticky="w")

        ttk.Label(runbox, text="输入框全选方式").grid(row=1, column=0, padx=8, sticky="w")
        self.select_all_method_var = tk.StringVar(value="三击选中（推荐）")
        _select_all_combo = ttk.Combobox(
            runbox,
            textvariable=self.select_all_method_var,
            width=18,
            values=["三击选中（推荐）", "Home+Shift+End", "Ctrl+A（兼容模式）"],
            state="readonly",
        )
        _select_all_combo.grid(row=1, column=1, columnspan=2, padx=8, sticky="w")
        ttk.Label(runbox, text="避免评分网站快捷键冲突").grid(row=1, column=3, padx=4, sticky="w")

        self.review_score_check_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            runbox,
            text="回评/二评校验",
            variable=self.review_score_check_var,
            command=self._sync_filler_state,
        ).grid(row=2, column=0, padx=8, pady=(4, 8), sticky="w")
        ttk.Label(runbox, text="单题不提交/下一题；批量只点下一题不提交").grid(row=2, column=1, columnspan=3, padx=8, pady=(4, 8), sticky="w")

        # ── 多模型交叉校验 ──
        sec_cross = make_model(
            "cross_check",
            "多模型交叉校验（主模型＋附加模型同题并行批改 → 分数比对 → 不一致时第三轮校验）",
        )
        crossbox = sec_cross.body

        cross_head = ttk.Frame(crossbox)
        cross_head.pack(fill=tk.X, padx=8, pady=(8, 0))

        self.cross_check_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cross_head,
            text="启用多模型交叉校验",
            variable=self.cross_check_var,
            command=self._sync_crosscheck_state,
        ).pack(side=tk.LEFT)

        ttk.Label(cross_head, text="分数容差").pack(side=tk.LEFT, padx=(16, 0))
        self.cross_tolerance_var = tk.StringVar(value="0")
        self.cross_tolerance_spin = ttk.Spinbox(
            cross_head, from_=0, to=10, width=4, textvariable=self.cross_tolerance_var
        )
        self.cross_tolerance_spin.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(cross_head, text="分（0=必须完全一致）").pack(side=tk.LEFT)

        ttk.Label(cross_head, text="第三轮").pack(side=tk.LEFT, padx=(16, 0))
        self.round3_mode_var = tk.StringVar(value="自动选择")
        self.round3_mode_combo = ttk.Combobox(
            cross_head,
            textvariable=self.round3_mode_var,
            width=14,
            values=["自动选择", "主模型参考重评", "独立仲裁模型"],
            state="readonly",
        )
        self.round3_mode_combo.pack(side=tk.LEFT, padx=(4, 0))

        cross_grid = ttk.Frame(crossbox)
        cross_grid.pack(fill=tk.X, padx=8, pady=(2, 6))
        self._cross_slots = {}
        next_row = 0
        next_row = self._build_cross_slot(
            cross_grid, next_row, "b", "模型B", "「复用主模型」= 用主模型网关；选接口配置则自动填充连接"
        )
        next_row = self._build_cross_slot(
            cross_grid, next_row, "c", "模型C", "「复用主模型」= 用主模型网关；选接口配置则自动填充连接"
        )
        self._build_cross_slot(
            cross_grid, next_row, "arbiter", "仲裁模型", "第三轮独立仲裁；未配置且第三轮为「自动选择」时改为主模型重评"
        )
        self._sync_crosscheck_state()

        # ── AI 评分记录 ──
        sec_records = make_main(
            "records", "AI 评分记录（双击记录查看截图与评分详情）", fill=tk.BOTH
        )
        record_frame = sec_records.body

        record_top = ttk.Frame(record_frame)
        record_top.pack(fill=tk.X, padx=8, pady=(8, 0))

        tree_frame = ttk.Frame(record_top)
        tree_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        columns = ("序号", "题号", "AI分数", "用时", "模型", "交叉校验")
        self.tune_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=6)
        for col in columns:
            self.tune_tree.heading(col, text=col)
        self.tune_tree.column("序号", width=50, anchor="center")
        self.tune_tree.column("题号", width=60, anchor="center")
        self.tune_tree.column("AI分数", width=70, anchor="center")
        self.tune_tree.column("用时", width=70, anchor="center")
        self.tune_tree.column("模型", width=170, anchor="w")
        self.tune_tree.column("交叉校验", width=110, anchor="center")
        _record_sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tune_tree.yview)
        self.tune_tree.configure(yscrollcommand=_record_sb.set)
        _record_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tune_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tune_tree.bind("<Double-1>", self._on_record_double_click)
        self.tune_tree.bind("<Motion>", self._on_tune_tree_motion)
        self.tune_tree.bind("<Leave>", self._hide_tune_preview)

        record_bar = ttk.Frame(record_frame)
        record_bar.pack(fill=tk.X, padx=10, pady=(2, 4))
        self.tune_status_var = tk.StringVar(value="未收集评分记录")
        ttk.Label(record_bar, textvariable=self.tune_status_var).pack(side=tk.LEFT)
        self.run_duration_var = tk.StringVar(value="本次评分总用时：—")
        ttk.Label(record_bar, textvariable=self.run_duration_var).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(record_bar, text="清空记录", command=self._clear_score_records).pack(side=tk.RIGHT, padx=(8, 0))

        # ── 快捷优化建议 ──
        sec_quick = make_criteria(
            "quick_optimize", "快捷优化建议（输入优化想法 → AI 分析 → 生成新评分标准）"
        )
        quick_frame = sec_quick.body

        qf_top = ttk.Frame(quick_frame)
        qf_top.pack(fill=tk.X, padx=8, pady=(8, 0))

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
        _opt_result_sb = ttk.Scrollbar(_opt_result_inner, command=_make_text_yview_command(self.optimize_result_text))
        self.optimize_result_text.configure(yscrollcommand=_opt_result_sb.set)
        _opt_result_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.optimize_result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── 日志 ──
        sec_log = make_main("log", "运行日志 / AI 返回（自动滚动）")
        log_frame = sec_log.body

        self.log_text = tk.Text(log_frame, wrap="word", height=12)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(log_frame, command=_make_text_yview_command(self.log_text))
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        self.log_text.configure(yscrollcommand=sb.set)

        # ── API 配置选项卡 ──
        self._build_api_tab(api_tab)

    # ══════════════════════════════════════════════════════════
    #  API 配置选项卡：配置自定义 API（供应商 / 密钥 / 模型列表）
    # ══════════════════════════════════════════════════════════

    # 使用专用 SDK / 签名接口、不走 OpenAI 兼容协议的服务商
    _SPECIAL_PROVIDERS = ("智谱AI", "百度千帆", "科大讯飞")

    PROVIDER_KEY_HINTS = {
        "OpenAI": "标准 OpenAI 兼容接口",
        "智谱AI": "使用官方 SDK，无需 base_url；API Key 直接填写",
        "阿里通义千问": "DashScope 兼容模式，base_url 一般无需修改",
        "字节豆包": "火山方舟 Ark 接口",
        "零一万物": "Yi 开放平台",
        "硅基流动": "SiliconFlow 平台，一个 Key 可调用多个模型",
        "百度千帆": "API Key 填写 API_Key:Secret_Key 格式；无需 base_url",
        "科大讯飞": "API Key 填写 appId:apiKey:apiSecret 格式；无需 base_url",
        "小米MiMo": "API Key 为 tp-xxxxx 格式；base_url 形如 https://token-plan-cn.xiaomimimo.com/v1",
        "自定义": "任意 OpenAI 兼容网关：填写完整 base_url（通常以 /v1 结尾）",
    }

    def _build_api_tab(self, parent):
        """构建「API 配置」选项卡：左侧配置列表 + 请求设置，右侧配置详情表单。"""
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left_col = ttk.Frame(wrap)
        left_col.pack(side=tk.LEFT, fill=tk.Y)

        # ── 左侧上：配置列表 ──
        sec_profiles = CollapsibleFrame(
            left_col,
            title="已保存的接口配置",
            name="api_profiles",
            bg=_theme_bg(self),
            on_toggle=self._on_section_toggle,
        )
        sec_profiles.pack(fill=tk.BOTH, expand=True)
        self._register_section("api_profiles", sec_profiles, "api")
        left = sec_profiles.body

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        self.api_profile_tree = ttk.Treeview(tree_frame, columns=("名称", "服务商"), show="headings", height=12)
        self.api_profile_tree.heading("名称", text="名称")
        self.api_profile_tree.heading("服务商", text="服务商")
        self.api_profile_tree.column("名称", width=180, anchor="w")
        self.api_profile_tree.column("服务商", width=90, anchor="center")
        _tree_sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.api_profile_tree.yview)
        self.api_profile_tree.configure(yscrollcommand=_tree_sb.set)
        _tree_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.api_profile_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.api_profile_tree.bind("<<TreeviewSelect>>", self._on_ap_tree_select)
        self.api_profile_tree.tag_configure("active", background="#cfe4ff")

        list_btns = ttk.Frame(left)
        list_btns.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(list_btns, text="新建", width=6, command=self._ap_new_profile).pack(side=tk.LEFT)
        ttk.Button(list_btns, text="复制", width=6, command=self._ap_duplicate_profile).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(list_btns, text="删除", width=6, command=self._ap_delete_profile).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(left, text="设为当前使用", command=self._ap_apply_active).pack(fill=tk.X, padx=8, pady=(0, 8))

        # ── 左侧下：请求设置（全局超时 / 失败重试次数） ──
        sec_policy = CollapsibleFrame(
            left_col,
            title="请求设置（全局）",
            name="api_request_policy",
            bg=_theme_bg(self),
            on_toggle=self._on_section_toggle,
        )
        sec_policy.pack(fill=tk.X, pady=(8, 0))
        self._register_section("api_request_policy", sec_policy, "api")
        policy_frame = sec_policy.body

        policy_grid = ttk.Frame(policy_frame)
        policy_grid.pack(fill=tk.X, padx=8, pady=(8, 0))

        ttk.Label(policy_grid, text="响应超时(秒)").grid(row=0, column=0, sticky="w")
        self.request_timeout_var = tk.StringVar(value=str(API_TIMEOUT_SECONDS))
        self.request_timeout_spin = ttk.Spinbox(
            policy_grid,
            from_=API_TIMEOUT_MIN,
            to=API_TIMEOUT_MAX,
            increment=5,
            width=6,
            textvariable=self.request_timeout_var,
            command=self._on_request_policy_change,
        )
        self.request_timeout_spin.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.request_timeout_spin.bind("<Return>", self._on_request_policy_change)
        self.request_timeout_spin.bind("<FocusOut>", self._on_request_policy_change)
        ttk.Label(policy_grid, text=f"{API_TIMEOUT_MIN}~{API_TIMEOUT_MAX}").grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Label(policy_grid, text="失败重试次数").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.request_max_retries_var = tk.StringVar(value=str(API_MAX_RETRIES))
        self.request_max_retries_spin = ttk.Spinbox(
            policy_grid,
            from_=API_RETRIES_MIN,
            to=API_RETRIES_MAX,
            increment=1,
            width=6,
            textvariable=self.request_max_retries_var,
            command=self._on_request_policy_change,
        )
        self.request_max_retries_spin.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        self.request_max_retries_spin.bind("<Return>", self._on_request_policy_change)
        self.request_max_retries_spin.bind("<FocusOut>", self._on_request_policy_change)
        ttk.Label(policy_grid, text="0=不重试").grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0))

        self.request_policy_status_var = tk.StringVar(
            value=f"已生效：超时 {API_TIMEOUT_SECONDS}s · 重试 {API_MAX_RETRIES} 次"
        )
        ttk.Label(
            policy_frame, textvariable=self.request_policy_status_var, foreground="#666666", wraplength=250
        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Label(
            policy_frame,
            text="重试次数不含首次请求；对所有接口配置生效，修改后立即保存",
            foreground="#777777",
            wraplength=250,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(2, 8))

        # ── 右侧：配置详情 ──
        sec_detail = CollapsibleFrame(
            wrap,
            title="配置详情（修改后点「保存配置」写入 config.json）",
            name="api_detail",
            bg=_theme_bg(self),
            on_toggle=self._on_section_toggle,
        )
        sec_detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        self._register_section("api_detail", sec_detail, "api")

        form = ttk.Frame(sec_detail.body)
        form.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        r = 0
        ttk.Label(form, text="配置名称").grid(row=r, column=0, sticky="w")
        self.ap_name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.ap_name_var).grid(row=r, column=1, columnspan=3, sticky="we", padx=(6, 0))
        r += 1

        ttk.Label(form, text="服务商").grid(row=r, column=0, sticky="w")
        self.ap_provider_var = tk.StringVar(value="自定义")
        self.ap_provider_combo = ttk.Combobox(
            form,
            textvariable=self.ap_provider_var,
            width=16,
            values=list(self.PROVIDER_PRESETS.keys()),
            state="readonly",
        )
        self.ap_provider_combo.grid(row=r, column=1, sticky="w", padx=(6, 0))
        self.ap_provider_combo.bind("<<ComboboxSelected>>", self._on_ap_provider_change)
        self.ap_provider_hint_var = tk.StringVar(value="")
        ttk.Label(form, textvariable=self.ap_provider_hint_var, foreground="#777777", wraplength=330, justify="left").grid(
            row=r, column=2, columnspan=2, sticky="w", padx=(10, 0)
        )
        r += 1

        ttk.Label(form, text="base_url").grid(row=r, column=0, sticky="w")
        self.ap_base_url_var = tk.StringVar()
        self.ap_base_url_entry = ttk.Entry(form, textvariable=self.ap_base_url_var)
        self.ap_base_url_entry.grid(row=r, column=1, columnspan=3, sticky="we", padx=(6, 0))
        r += 1

        ttk.Label(form, text="API Key").grid(row=r, column=0, sticky="w")
        self.ap_api_key_var = tk.StringVar()
        self.ap_api_key_entry = ttk.Entry(form, textvariable=self.ap_api_key_var, show="*")
        self.ap_api_key_entry.grid(row=r, column=1, columnspan=2, sticky="we", padx=(6, 0))
        self.ap_show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示", variable=self.ap_show_key_var, command=self._ap_toggle_key_visible).grid(
            row=r, column=3, sticky="w", padx=(6, 0)
        )
        r += 1

        ttk.Label(form, text="额外请求头JSON(可选)").grid(row=r, column=0, sticky="w")
        self.ap_extra_headers_var = tk.StringVar()
        self.ap_extra_headers_entry = ttk.Entry(form, textvariable=self.ap_extra_headers_var)
        self.ap_extra_headers_entry.grid(row=r, column=1, columnspan=3, sticky="we", padx=(6, 0))
        r += 1

        ttk.Label(form, text="思考模式").grid(row=r, column=0, sticky="w")
        self.ap_thinking_var = tk.StringVar(value="自动")
        ttk.Combobox(form, textvariable=self.ap_thinking_var, width=10, values=["自动", "关闭", "开启"], state="readonly").grid(
            row=r, column=1, sticky="w", padx=(6, 0)
        )
        ttk.Label(form, text="接口类型").grid(row=r, column=2, sticky="w", padx=(10, 0))
        self.ap_api_type_var = tk.StringVar(value="自动判断")
        ttk.Combobox(form, textvariable=self.ap_api_type_var, width=16, values=list(_API_TYPE_BY_LABEL.keys()), state="readonly").grid(
            row=r, column=3, sticky="w", padx=(6, 0)
        )
        r += 1

        ttk.Label(form, text="默认模型").grid(row=r, column=0, sticky="w")
        self.ap_default_model_var = tk.StringVar()
        self.ap_default_model_combo = ttk.Combobox(form, textvariable=self.ap_default_model_var)
        self.ap_default_model_combo.grid(row=r, column=1, columnspan=3, sticky="we", padx=(6, 0))
        r += 1

        ttk.Label(form, text="模型列表").grid(row=r, column=0, columnspan=4, sticky="w", pady=(8, 2))
        r += 1

        model_btns = ttk.Frame(form)
        model_btns.grid(row=r, column=0, columnspan=4, sticky="we", pady=(0, 4))
        self.ap_fetch_btn = ttk.Button(model_btns, text="获取模型列表", command=self._ap_fetch_models)
        self.ap_fetch_btn.pack(side=tk.LEFT)
        ttk.Button(model_btns, text="手动添加", command=self._ap_add_model).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(model_btns, text="删除选中", command=self._ap_remove_model).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(model_btns, text="清空列表", command=self._ap_clear_models).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(model_btns, text="双击设为默认", foreground="#777777").pack(side=tk.LEFT, padx=(10, 0))
        r += 1

        models_box = ttk.Frame(form)
        models_box.grid(row=r, column=0, columnspan=4, sticky="nsew")
        self.ap_models_listbox = tk.Listbox(models_box, height=8, activestyle="dotbox")
        _mb_sb = ttk.Scrollbar(models_box, orient="vertical", command=self.ap_models_listbox.yview)
        self.ap_models_listbox.configure(yscrollcommand=_mb_sb.set)
        _mb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ap_models_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.ap_models_listbox.bind("<Double-1>", self._ap_set_default_model)
        form.rowconfigure(r, weight=1)
        r += 1

        bottom = ttk.Frame(form)
        bottom.grid(row=r, column=0, columnspan=4, sticky="we", pady=(8, 0))
        ttk.Button(bottom, text="保存配置", command=self._ap_save_form).pack(side=tk.LEFT)
        ttk.Button(bottom, text="保存并应用（返回主界面）", command=lambda: self._ap_apply_active(return_to_main=True)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.ap_status_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.ap_status_var, foreground="#666666").pack(side=tk.LEFT, padx=(12, 0))

    # ── 选项卡布局 / 可折叠模块控制 ──

    def _create_scroll_page(self, page: tk.Widget, index: int) -> tk.Widget:
        """在选项卡页面内创建纵向可滚动内容区，返回内容父容器（inner）。"""
        host = ttk.Frame(page)
        host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(host, highlightthickness=0)

        def _scroll_canvas(*args: str) -> None:
            canvas.tk.call(str(canvas), "yview", *args)

        scrollbar = ttk.Scrollbar(host, orient="vertical", command=_scroll_canvas)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _configure_inner(event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window_id, width=event.width)

        inner.bind("<Configure>", _configure_inner)

        def _configure_canvas(event: tk.Event) -> None:
            canvas.itemconfig(window_id, width=event.width)

        canvas.bind("<Configure>", _configure_canvas)

        self._scroll_canvases[index] = canvas
        return inner

    def _build_section_toolbar(self, page: tk.Widget, tab_key: str):
        """为选项卡添加固定的模块折叠工具栏（只作用于本页模块）。"""
        bar = ttk.Frame(page)
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 0))
        ttk.Label(bar, text="模块：").pack(side=tk.LEFT)
        ttk.Button(
            bar, text="全部展开", width=9,
            command=lambda k=tab_key: self._toggle_tab_sections(k, False),
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            bar, text="全部折叠", width=9,
            command=lambda k=tab_key: self._toggle_tab_sections(k, True),
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(bar, text="点击各模块标题栏可单独展开 / 收起", foreground="#777777").pack(
            side=tk.LEFT, padx=(10, 0)
        )

    def _register_section(self, name: str, section: CollapsibleFrame, tab_key: str) -> CollapsibleFrame:
        """登记可折叠模块，并归入所属选项卡分组。"""
        self._sections[name] = section
        self._tab_section_names.setdefault(tab_key, []).append(name)
        return section

    def _on_mousewheel_global(self, event: tk.Event) -> None:
        # 当鼠标悬停在带滚动条的 Text 控件上时，不触发页面滚动
        w = event.widget
        while w is not None:
            if isinstance(w, tk.Text):
                try:
                    if w.cget("yscrollcommand") != "":
                        return  # 该 Text 有自己的滚动条，跳过页面滚动
                except tk.TclError:
                    pass
                break
            w = w.master if hasattr(w, "master") else None
        try:
            index = self._notebook.index(self._notebook.select())
        except tk.TclError:
            return
        canvas = self._scroll_canvases.get(index)
        if canvas is None:
            return
        canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _refresh_scrollregion(self):
        """折叠/展开后刷新各页面画布的滚动区域（避免残留空白或截断）。"""
        def _apply():
            for canvas in self._scroll_canvases.values():
                try:
                    canvas.configure(scrollregion=canvas.bbox("all"))
                except tk.TclError:
                    pass

        try:
            self.update_idletasks()
        except tk.TclError:
            return
        _apply()
        try:
            self.after_idle(_apply)
        except tk.TclError:
            pass

    def _on_section_toggle(self, name: str, collapsed: bool):
        """用户点击模块标题栏：记录状态并即时持久化到 config.json 的 ui_sections。"""
        if not name:
            return
        self._ui_section_state[name] = bool(collapsed)
        self._persist_ui_sections()
        self._refresh_scrollregion()

    def _persist_ui_sections(self):
        """只把折叠状态合并写回 config.json，不影响其它字段（含未保存的编辑）。"""
        try:
            data: dict = {}
            if self.config_path.exists():
                try:
                    loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
            data["ui_sections"] = dict(self._ui_section_state)
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[界面] 保存模块折叠状态失败：{e}")

    def _toggle_tab_sections(self, tab_key: str, collapsed: bool):
        """展开 / 收起某个选项卡内的全部模块。"""
        for name in self._tab_section_names.get(tab_key, []):
            sec = self._sections.get(name)
            if sec is None:
                continue
            sec.set_collapsed(collapsed, notify=False)
            self._ui_section_state[name] = bool(collapsed)
        self._persist_ui_sections()
        self._refresh_scrollregion()

    def _goto_api_tab(self):
        """切换到「API 配置」选项卡，并加载当前使用的配置。"""
        try:
            self._notebook.select(self._API_TAB_INDEX)
        except tk.TclError:
            pass
        self._ap_load_form(self._ap_selected_name() or self._active_profile_var.get())

    # ── 接口配置公共方法 ──

    def _find_profile(self, name: str) -> dict | None:
        target = (name or "").strip()
        if not target:
            return None
        for profile in self._api_profiles:
            if profile.get("name") == target:
                return profile
        return None

    @staticmethod
    def _normalize_profile(raw: dict) -> dict:
        """把任意来源的配置字典标准化（补默认值、清洗非法取值）。"""
        thinking = str(raw.get("thinking_mode") or "自动")
        if thinking not in ("自动", "关闭", "开启"):
            thinking = "自动"
        api_type = str(raw.get("api_type") or "自动判断")
        if api_type not in _API_TYPE_BY_LABEL:
            api_type = "自动判断"
        models: list[str] = []
        raw_models = raw.get("models")
        if isinstance(raw_models, (list, tuple)):
            for item in raw_models:
                model = str(item).strip()
                if model and model not in models:
                    models.append(model)
        default_model = str(raw.get("default_model") or "").strip()
        if default_model and default_model not in models:
            models.insert(0, default_model)
        return {
            "name": str(raw.get("name") or "").strip() or "未命名配置",
            "provider": str(raw.get("provider") or "自定义").strip() or "自定义",
            "base_url": str(raw.get("base_url") or "").strip(),
            "api_key": str(raw.get("api_key") or "").strip(),
            "extra_headers_json": str(raw.get("extra_headers_json") or "").strip(),
            "thinking_mode": thinking,
            "api_type": api_type,
            "models": models,
            "default_model": default_model,
        }

    def _unique_profile_name(self, base: str) -> str:
        names = {p["name"] for p in self._api_profiles}
        if base not in names:
            return base
        index = 2
        while f"{base} {index}" in names:
            index += 1
        return f"{base} {index}"

    @staticmethod
    def _mask_secret(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return "未填写"
        if len(text) <= 8:
            return text[0] + "*" * (len(text) - 1)
        return f"{text[:4]}****{text[-4:]}"

    def _describe_connection(self) -> str:
        policy = get_request_policy()
        policy_text = f" | 超时 {policy['timeout_seconds']}s · 重试 {policy['max_retries']} 次"
        provider = self.provider_var.get()
        if provider in self._SPECIAL_PROVIDERS:
            key_format = {
                "智谱AI": "智谱官方 SDK",
                "百度千帆": "API_Key:Secret_Key",
                "科大讯飞": "appId:apiKey:apiSecret",
            }.get(provider, "专用接口")
            return f"{provider}（专用接口，API Key 格式：{key_format}）{policy_text}"
        base_url = (self.base_url_var.get() or "").strip() or "（默认地址）"
        return (
            f"{provider} | {base_url} | Key {self._mask_secret(self.api_key_var.get())}"
            f" | 接口类型 {self.api_type_var.get()} | 思考模式 {self.thinking_mode_var.get()}"
            f"{policy_text}"
        )

    # ── 请求设置（全局超时 / 失败重试次数） ──

    def _get_request_timeout(self) -> int:
        try:
            value = int(round(float(self.request_timeout_var.get())))
        except (TypeError, ValueError, tk.TclError):
            value = API_TIMEOUT_SECONDS
        return max(API_TIMEOUT_MIN, min(API_TIMEOUT_MAX, value))

    def _get_request_max_retries(self) -> int:
        try:
            value = int(round(float(self.request_max_retries_var.get())))
        except (TypeError, ValueError, tk.TclError):
            value = API_MAX_RETRIES
        return max(API_RETRIES_MIN, min(API_RETRIES_MAX, value))

    def _apply_request_policy(self, save: bool = False) -> dict:
        """把界面上的请求设置写入运行时策略（可选持久化）。"""
        policy = configure_request_policy(
            timeout_seconds=self._get_request_timeout(),
            max_retries=self._get_request_max_retries(),
        )
        # 回写规范化后的值（越界输入会被截断，避免界面与生效值不一致）
        self.request_timeout_var.set(str(policy["timeout_seconds"]))
        self.request_max_retries_var.set(str(policy["max_retries"]))
        if hasattr(self, "request_policy_status_var"):
            self.request_policy_status_var.set(
                f"已生效：超时 {policy['timeout_seconds']}s · 重试 {policy['max_retries']} 次"
            )
        self._sync_provider_state()
        if save:
            self._save_config(silent=True)
        return policy

    def _on_request_policy_change(self, event=None):
        """超时/重试次数变化：立即生效并持久化到 config.json。"""
        policy = self._apply_request_policy(save=True)
        print(
            f"[请求设置] 响应超时 {policy['timeout_seconds']} 秒 · 失败重试 {policy['max_retries']} 次"
            f"（重试不含首次请求）"
        )

    def _ensure_default_profile(self):
        """首次运行（无配置文件）时，用当前默认连接创建一个配置，保证主界面可用。"""
        if self._api_profiles:
            return
        model = (self.model_var.get() or "").strip()
        profile = self._normalize_profile({
            "name": f"{self.provider_var.get() or 'OpenAI'} 默认配置",
            "provider": self.provider_var.get() or "OpenAI",
            "base_url": self.base_url_var.get(),
            "api_key": self.api_key_var.get(),
            "extra_headers_json": self.extra_headers_var.get(),
            "thinking_mode": self.thinking_mode_var.get(),
            "api_type": self.api_type_var.get(),
            "models": [model] if model else [],
            "default_model": model,
        })
        self._api_profiles = [profile]
        self._active_profile_var.set(profile["name"])
        self._refresh_profile_choices()
        if hasattr(self, "ap_status_var"):
            self.ap_status_var.set("未找到配置文件，已创建默认接口配置")

    def _apply_profile_to_vars(self, name: str) -> bool:
        """把指定配置写入主界面当前生效的连接变量，并刷新模型下拉框。"""
        profile = self._find_profile(name)
        if profile is None:
            return False
        self._active_profile_var.set(profile["name"])
        self.provider_var.set(profile["provider"])
        self.api_key_var.set(profile["api_key"])
        self.base_url_var.set(profile["base_url"])
        self.extra_headers_var.set(profile["extra_headers_json"])
        self.thinking_mode_var.set(profile["thinking_mode"])
        self.api_type_var.set(profile["api_type"])
        models = list(profile["models"])
        if hasattr(self, "model_combo"):
            self.model_combo.configure(values=models)
        current = (self.model_var.get() or "").strip()
        default_model = (profile.get("default_model") or "").strip()
        if models and current not in models:
            self.model_var.set(default_model if default_model in models else models[0])
        elif not current and default_model:
            self.model_var.set(default_model)
        self._sync_provider_state()
        return True

    def _sync_active_profile_from_main(self):
        """收集配置前，把主界面当前模型回写到当前配置（保持默认模型最新）。"""
        profile = self._find_profile(self._active_profile_var.get())
        if profile is None:
            return
        model = (self.model_var.get() or "").strip()
        if model:
            profile["default_model"] = model
            if model not in profile["models"]:
                profile["models"].append(model)

    @staticmethod
    def _legacy_profile_from_cfg(cfg: dict) -> dict:
        """旧版 config.json（无 api_profiles）→ 合成一个接口配置，保证向后兼容。"""
        provider = str(cfg.get("provider") or "OpenAI")
        model = str(cfg.get("model") or "").strip()
        return {
            "name": f"{provider}（默认）",
            "provider": provider,
            "base_url": cfg.get("base_url"),
            "api_key": cfg.get("api_key"),
            "extra_headers_json": cfg.get("extra_headers_json"),
            "thinking_mode": cfg.get("thinking_mode"),
            "api_type": cfg.get("api_type"),
            "models": [model] if model else [],
            "default_model": model,
        }

    def _match_profile_by_connection(self, cfg: dict) -> str:
        """按 provider/base_url/api_key 匹配已有配置，用于恢复 active_profile。"""
        provider = str(cfg.get("provider") or "")
        base_url = str(cfg.get("base_url") or "").strip()
        api_key = str(cfg.get("api_key") or "").strip()
        for profile in self._api_profiles:
            if profile["provider"] != provider:
                continue
            if base_url and profile["base_url"] and profile["base_url"] != base_url:
                continue
            if api_key and profile["api_key"] and profile["api_key"] != api_key:
                continue
            return profile["name"]
        return ""

    def _refresh_profile_choices(self):
        """配置列表变化后，同步主界面下拉框与交叉校验槽位的候选项。"""
        names = [p["name"] for p in self._api_profiles]
        if hasattr(self, "profile_combo"):
            self.profile_combo.configure(values=names)
        if self._active_profile_var.get() not in names:
            self._active_profile_var.set(names[0] if names else "")
        self._refresh_cross_profile_choices()
        self._ap_refresh_tree()

    def _on_main_profile_combo(self, event=None):
        name = self._active_profile_var.get()
        if not self._apply_profile_to_vars(name):
            return
        self._refresh_cross_profile_choices()
        print(f"[配置] 已切换到接口配置「{name}」（{self.provider_var.get()} / {self.model_var.get() or '未选择模型'}）")

    def _on_main_model_combo(self, event=None):
        model = (self.model_var.get() or "").strip()
        if model:
            print(f"[配置] 当前模型：{model}")

    # ── API 配置选项卡：列表操作 ──

    def _ap_refresh_tree(self):
        tree = getattr(self, "api_profile_tree", None)
        if tree is None:
            return
        selected = self._ap_selected_name()
        children = tree.get_children()
        if children:
            tree.delete(*children)
        active = self._active_profile_var.get()
        for profile in self._api_profiles:
            tags = ("active",) if profile["name"] == active else ()
            tree.insert("", "end", iid=profile["name"], values=(profile["name"], profile["provider"]), tags=tags)
        target = selected if selected and tree.exists(selected) else active
        if target and tree.exists(target):
            tree.selection_set(target)
            tree.see(target)

    def _ap_selected_name(self) -> str:
        tree = getattr(self, "api_profile_tree", None)
        if tree is None:
            return ""
        selection = tree.selection()
        if not selection:
            return ""
        return str(selection[0])

    def _on_ap_tree_select(self, event=None):
        name = self._ap_selected_name()
        # 仅在选中项变化时重新载入，避免重复选中把用户未保存的修改冲掉
        if name and name != getattr(self, "_ap_loaded_name", ""):
            self._ap_load_form(name)

    def _ap_load_form(self, name: str):
        profile = self._find_profile(name)
        if profile is None:
            return
        self._ap_loaded_name = profile["name"]
        self.ap_name_var.set(profile["name"])
        self.ap_provider_var.set(profile["provider"])
        self.ap_base_url_var.set(profile["base_url"])
        self.ap_api_key_var.set(profile["api_key"])
        self.ap_extra_headers_var.set(profile["extra_headers_json"])
        self.ap_thinking_var.set(profile["thinking_mode"])
        self.ap_api_type_var.set(profile["api_type"])
        self.ap_default_model_var.set(profile["default_model"])
        self.ap_models_listbox.delete(0, "end")
        for model in profile["models"]:
            self.ap_models_listbox.insert("end", model)
        self.ap_default_model_combo.configure(values=list(profile["models"]))
        self._on_ap_provider_change(silent=True)
        suffix = "（当前使用中）" if profile["name"] == self._active_profile_var.get() else ""
        self.ap_status_var.set(f"已加载「{profile['name']}」{suffix}")

    def _ap_new_profile(self):
        name = self._unique_profile_name("新配置")
        profile = self._normalize_profile({"name": name, "provider": "自定义"})
        self._api_profiles.append(profile)
        self._refresh_profile_choices()
        tree = self.api_profile_tree
        if tree.exists(name):
            tree.selection_set(name)
            tree.see(name)
        self._ap_load_form(name)
        self.ap_status_var.set("已创建新配置：填写 base_url / API Key 后点「保存配置」")

    def _ap_duplicate_profile(self):
        profile = self._find_profile(self._ap_selected_name())
        if profile is None:
            messagebox.showinfo("提示", "请先在左侧列表中选择一个配置。")
            return
        clone = dict(profile)
        clone["models"] = list(profile["models"])
        clone["name"] = self._unique_profile_name(f"{profile['name']} 副本")
        self._api_profiles.append(clone)
        self._refresh_profile_choices()
        tree = self.api_profile_tree
        if tree.exists(clone["name"]):
            tree.selection_set(clone["name"])
            tree.see(clone["name"])
        self._ap_load_form(clone["name"])
        self.ap_status_var.set(f"已复制为「{clone['name']}」")

    def _ap_delete_profile(self):
        name = self._ap_selected_name() or (self.ap_name_var.get() or "").strip()
        profile = self._find_profile(name)
        if profile is None:
            messagebox.showinfo("提示", "请先在左侧列表中选择一个配置。")
            return
        if len(self._api_profiles) <= 1:
            messagebox.showinfo("提示", "至少需要保留一个接口配置。")
            return
        if not messagebox.askyesno("删除配置", f"确定删除接口配置「{name}」吗？\n（不影响评分记录与截图文件）"):
            return
        was_active = (self._active_profile_var.get() == name)
        self._api_profiles = [p for p in self._api_profiles if p["name"] != name]
        if was_active and self._api_profiles:
            self._apply_profile_to_vars(self._api_profiles[0]["name"])
        self._refresh_profile_choices()
        self._save_config(silent=True)
        self._ap_load_form(self._active_profile_var.get())
        self.ap_status_var.set(f"已删除「{name}」")
        print(f"[API 配置] 已删除配置「{name}」")

    # ── API 配置选项卡：表单操作 ──

    def _ap_toggle_key_visible(self):
        self.ap_api_key_entry.configure(show=("" if self.ap_show_key_var.get() else "*"))

    def _on_ap_provider_change(self, event=None, silent: bool = False):
        provider = self.ap_provider_var.get()
        self.ap_provider_hint_var.set(self.PROVIDER_KEY_HINTS.get(provider, ""))
        special = provider in self._SPECIAL_PROVIDERS
        for widget in (self.ap_base_url_entry, self.ap_extra_headers_entry):
            widget.configure(state=("disabled" if special else "normal"))
        self.ap_fetch_btn.configure(state=("disabled" if special else "normal"))
        if silent:
            return
        preset = self.PROVIDER_PRESETS.get(provider)
        if preset:
            preset_url, preset_model = preset
            if preset_url and not (self.ap_base_url_var.get() or "").strip():
                self.ap_base_url_var.set(preset_url)
            if not (self.ap_default_model_var.get() or "").strip() and preset_model:
                self.ap_default_model_var.set(preset_model)

    def _ap_collect_form(self) -> dict | None:
        """读取表单并校验；返回标准化配置 dict，校验失败返回 None。"""
        name = (self.ap_name_var.get() or "").strip()
        if not name:
            messagebox.showerror("配置不完整", "请填写配置名称（用于在主界面下拉框中选择）")
            return None
        provider = self.ap_provider_var.get()
        extra_headers = (self.ap_extra_headers_var.get() or "").strip()
        if extra_headers:
            try:
                parsed = json.loads(extra_headers)
                if not isinstance(parsed, dict):
                    raise ValueError("额外请求头必须是 JSON 对象")
            except Exception as e:
                messagebox.showerror("配置错误", f"额外请求头JSON解析失败：{e}")
                return None
        if provider not in self._SPECIAL_PROVIDERS and not (self.ap_base_url_var.get() or "").strip():
            messagebox.showerror("配置不完整", "请填写 base_url（OpenAI 兼容接口地址）")
            return None
        models = [str(m).strip() for m in self.ap_models_listbox.get(0, "end")]
        models = [m for m in models if m]
        default_model = (self.ap_default_model_var.get() or "").strip()
        if not default_model and models:
            default_model = models[0]
            self.ap_default_model_var.set(default_model)
        if default_model and default_model not in models:
            models.insert(0, default_model)
        return self._normalize_profile({
            "name": name,
            "provider": provider,
            "base_url": self.ap_base_url_var.get(),
            "api_key": self.ap_api_key_var.get(),
            "extra_headers_json": extra_headers,
            "thinking_mode": self.ap_thinking_var.get(),
            "api_type": self.ap_api_type_var.get(),
            "models": models,
            "default_model": default_model,
        })

    def _ap_save_form(self, silent: bool = False) -> bool:
        old_name = self._ap_selected_name()
        new_profile = self._ap_collect_form()
        if new_profile is None:
            return False
        new_name = new_profile["name"]
        for profile in self._api_profiles:
            if profile["name"] == new_name and profile["name"] != old_name:
                messagebox.showerror("名称重复", f"已存在名为「{new_name}」的配置，请换一个名称。")
                return False

        target = self._find_profile(old_name) or self._find_profile(new_name)
        if target is None:
            self._api_profiles.append(new_profile)
        else:
            target.clear()
            target.update(new_profile)

        if self._active_profile_var.get() in (old_name, new_name):
            self._apply_profile_to_vars(new_name)
        self._refresh_profile_choices()
        self._ap_loaded_name = new_name
        tree = self.api_profile_tree
        if tree.exists(new_name):
            tree.selection_set(new_name)
            tree.see(new_name)
        self._save_config(silent=True)
        self.ap_status_var.set(f"已保存「{new_name}」")
        print(f"[API 配置] 已保存配置「{new_name}」（{new_profile['provider']}，{len(new_profile['models'])} 个模型）")
        if not silent:
            messagebox.showinfo("保存成功", f"接口配置「{new_name}」已保存。\n可在主界面「接口配置」下拉框中选择使用。")
        return True

    def _ap_apply_active(self, return_to_main: bool = False):
        """保存表单并设为当前使用；可选切回主界面。"""
        if not self._ap_save_form(silent=True):
            return
        name = (self.ap_name_var.get() or "").strip()
        if not self._apply_profile_to_vars(name):
            messagebox.showinfo("提示", "配置尚未保存，请先点「保存配置」。")
            return
        self._refresh_profile_choices()
        self._save_config(silent=True)  # 立即记录「当前使用」的配置，重启后保持
        self.ap_status_var.set(f"当前使用：{name}")
        print(f"[API 配置] 当前使用配置已切换为「{name}」")
        if return_to_main:
            try:
                self._notebook.select(0)
            except tk.TclError:
                pass
            self._update_ready_status()

    def _ap_fetch_models(self):
        provider = self.ap_provider_var.get()
        if provider in self._SPECIAL_PROVIDERS:
            messagebox.showinfo("提示", f"{provider} 当前使用专用 SDK/接口，暂不支持自动获取模型列表，请用「手动添加」。")
            return
        base_url = (self.ap_base_url_var.get() or "").strip()
        if not base_url:
            messagebox.showerror("配置不完整", "请先填写 base_url")
            return
        api_key = (self.ap_api_key_var.get() or "").strip()
        if not api_key:
            messagebox.showerror("配置不完整", "请先填写 API Key")
            return
        extra_headers_raw = (self.ap_extra_headers_var.get() or "").strip()
        extra_headers: dict[str, str] = {}
        if extra_headers_raw:
            try:
                parsed_headers: Any = json.loads(extra_headers_raw)
                if not isinstance(parsed_headers, dict):
                    raise ValueError("额外请求头必须是 JSON 对象")
                if not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed_headers.items()):
                    raise ValueError("额外请求头的键和值都必须是字符串")
                extra_headers = parsed_headers
            except Exception as e:
                messagebox.showerror("配置错误", f"额外请求头JSON解析失败：{e}")
                return

        policy = get_request_policy()
        self.ap_fetch_btn.configure(state="disabled", text="获取中…")
        print(
            f"[API 配置] 正在获取模型列表：{base_url}"
            f"（超时 {policy['timeout_seconds']}s · 重试 {policy['max_retries']} 次）"
        )

        def _do_fetch():
            try:
                models = fetch_openai_compatible_models(
                    base_url=base_url,
                    api_key=api_key,
                    extra_headers=extra_headers,
                    timeout=None,  # 使用全局请求设置
                )
                self.after(0, self._ap_fetch_done, models)
            except Exception as e:
                self.after(0, self._ap_fetch_error, str(e))

        threading.Thread(target=_do_fetch, daemon=True).start()

    def _ap_fetch_done(self, models: list[str]):
        self.ap_fetch_btn.configure(state="normal", text="获取模型列表")
        if not models:
            messagebox.showwarning("模型列表", "接口返回成功，但没有解析到模型 id。")
            return
        self.ap_models_listbox.delete(0, "end")
        for model in models:
            self.ap_models_listbox.insert("end", model)
        self.ap_default_model_combo.configure(values=models)
        if not (self.ap_default_model_var.get() or "").strip():
            self.ap_default_model_var.set(models[0])
        self.ap_status_var.set(f"已获取 {len(models)} 个模型，点「保存配置」写入")
        print(f"[API 配置] 已获取 {len(models)} 个模型")
        messagebox.showinfo("模型列表", f"已获取 {len(models)} 个模型，已填入模型列表。\n点「保存配置」后即可在主界面选择。")

    def _ap_fetch_error(self, err: str):
        self.ap_fetch_btn.configure(state="normal", text="获取模型列表")
        print(f"[API 配置] 获取模型列表失败：{err}")
        messagebox.showerror("获取模型列表失败", err)

    def _ap_add_model(self):
        from tkinter import simpledialog
        value = simpledialog.askstring("添加模型", "输入模型名（如 grok-4.5）：", parent=self)
        if not value:
            return
        model = value.strip()
        if not model:
            return
        existing = [str(m) for m in self.ap_models_listbox.get(0, "end")]
        if model in existing:
            messagebox.showinfo("提示", f"模型「{model}」已在列表中。")
            return
        self.ap_models_listbox.insert("end", model)
        self.ap_default_model_combo.configure(values=list(self.ap_models_listbox.get(0, "end")))
        if not (self.ap_default_model_var.get() or "").strip():
            self.ap_default_model_var.set(model)

    def _ap_remove_model(self):
        selection = self.ap_models_listbox.curselection()
        if not selection:
            messagebox.showinfo("提示", "请先在模型列表中选择要删除的模型。")
            return
        for index in reversed(selection):
            self.ap_models_listbox.delete(index)
        models = [str(m) for m in self.ap_models_listbox.get(0, "end")]
        self.ap_default_model_combo.configure(values=models)
        if (self.ap_default_model_var.get() or "").strip() not in models:
            self.ap_default_model_var.set(models[0] if models else "")

    def _ap_clear_models(self):
        if not self.ap_models_listbox.size():
            return
        if not messagebox.askyesno("清空模型列表", "确定清空当前配置的模型列表吗？（点「保存配置」后生效）"):
            return
        self.ap_models_listbox.delete(0, "end")
        self.ap_default_model_combo.configure(values=[])
        self.ap_default_model_var.set("")

    def _ap_set_default_model(self, event=None):
        selection = self.ap_models_listbox.curselection()
        if not selection:
            return
        model = self.ap_models_listbox.get(selection[0])
        self.ap_default_model_var.set(model)
        self.ap_status_var.set(f"默认模型：{model}（点「保存配置」生效）")

    def _sync_batch_state(self):
        self.total_entry.configure(state=("normal" if self.batch_var.get() else "disabled"))
        if hasattr(self, "start_btn"):
            text = "开始批量阅卷" if self.batch_var.get() else "开始单题阅卷"
            self.start_btn.configure(text=text)

    _SELECT_ALL_LABEL_MAP = {
        "三击选中（推荐）": "triple_click",
        "Home+Shift+End": "home_shift_end",
        "Ctrl+A（兼容模式）": "ctrl_a",
    }
    _SELECT_ALL_LABEL_REVERSE = {v: k for k, v in _SELECT_ALL_LABEL_MAP.items()}

    def _get_select_all_method(self) -> str:
        label = self.select_all_method_var.get()
        return self._SELECT_ALL_LABEL_MAP.get(label, "triple_click")

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

        virtual_x, virtual_y, screen_w, screen_h = get_virtual_screen_geometry()

        left, top, right, bottom = vals
        rel_x = int(max(0, min(screen_w - 1, left * screen_w)))
        rel_y = int(max(0, min(screen_h - 1, top * screen_h)))
        rel_right = int(max(1, min(screen_w, right * screen_w)))
        rel_bottom = int(max(1, min(screen_h, bottom * screen_h)))
        x = virtual_x + rel_x
        y = virtual_y + rel_y
        width = int(max(1, rel_right - rel_x))
        height = int(max(1, rel_bottom - rel_y))
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

        self._region_overlay.geometry(format_tk_geometry(width, height, x, y))
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

    def _notify(self, title: str, message: str, level: str = "info"):
        """弹窗 + Windows 系统通知（线程安全，可从工作线程调用）。

        用于三类关键事件：API 无响应、任务完成、三轮校验分数均不一致。
        """
        def _show_popup():
            try:
                if level == "warning":
                    messagebox.showwarning(title, message, parent=self)
                else:
                    messagebox.showinfo(title, message, parent=self)
            except Exception as e:
                print(f"[通知] 弹窗显示失败：{e}")

        try:
            self.after(0, _show_popup)
        except Exception:
            _show_popup()
        send_windows_notification(title, message, level=level)

    def _sync_filler_state(self):
        if self.system is None:
            return
        self.system.filler.select_all_method = self._get_select_all_method()
        self.system.filler.config["review_score_check_enabled"] = bool(self.review_score_check_var.get())
        self.system.filler.config["batch_mode"] = bool(self.batch_var.get())

    # ── 多模型交叉校验 ──

    # 启动时最多加载的历史评分记录条数
    HISTORY_LOAD_LIMIT = 1000

    _ROUND3_MODE_MAP = {
        "自动选择": "auto",
        "主模型参考重评": "rereview",
        "独立仲裁模型": "arbiter",
    }

    def _build_cross_slot(self, parent, row, key, label, hint):
        """构建一个交叉校验模型配置槽位。

        可直接从已保存的接口配置中选择（自动填充 base_url / API Key / 模型列表），
        也可选「复用主模型」使用主模型的网关连接；下方输入框仍可手动微调。
        """
        slot = {
            "enabled": tk.BooleanVar(value=False),
            "profile": tk.StringVar(value=self._CROSS_REUSE_LABEL),
            "model": tk.StringVar(),
            "base_url": tk.StringVar(),
            "api_key": tk.StringVar(),
            "thinking": tk.StringVar(value="自动"),
            "api_type": tk.StringVar(value="自动判断"),
        }

        line1 = ttk.Frame(parent)
        line1.grid(row=row, column=0, sticky="we", pady=(2, 0))
        slot["check"] = ttk.Checkbutton(
            line1, text=label, variable=slot["enabled"], command=self._sync_crosscheck_state
        )
        slot["check"].pack(side=tk.LEFT)
        ttk.Label(line1, text="接口配置").pack(side=tk.LEFT, padx=(10, 0))
        combo_profile = ttk.Combobox(
            line1,
            textvariable=slot["profile"],
            width=20,
            values=self._cross_profile_choices(),
            state="readonly",
        )
        combo_profile.pack(side=tk.LEFT, padx=(4, 0))
        combo_profile.bind("<<ComboboxSelected>>", lambda event, k=key: self._on_cross_profile_change(k))
        ttk.Label(line1, text="模型").pack(side=tk.LEFT, padx=(10, 0))
        combo_model = ttk.Combobox(line1, textvariable=slot["model"], width=24, values=[])
        combo_model.pack(side=tk.LEFT, padx=(4, 0), fill=tk.X, expand=True)

        line2 = ttk.Frame(parent)
        line2.grid(row=row + 1, column=0, sticky="we")
        ttk.Label(line2, text="base_url").pack(side=tk.LEFT, padx=(28, 0))
        entry_url = ttk.Entry(line2, textvariable=slot["base_url"])
        entry_url.pack(side=tk.LEFT, padx=(4, 0), fill=tk.X, expand=True)
        ttk.Label(line2, text="API Key").pack(side=tk.LEFT, padx=(10, 0))
        entry_key = ttk.Entry(line2, textvariable=slot["api_key"], width=18, show="*")
        entry_key.pack(side=tk.LEFT, padx=(4, 0))

        line3 = ttk.Frame(parent)
        line3.grid(row=row + 2, column=0, sticky="we", pady=(0, 2))
        ttk.Label(line3, text="思考模式").pack(side=tk.LEFT, padx=(28, 0))
        combo_think = ttk.Combobox(
            line3, textvariable=slot["thinking"], width=8, values=["自动", "关闭", "开启"], state="readonly"
        )
        combo_think.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(line3, text="接口类型").pack(side=tk.LEFT, padx=(10, 0))
        combo_api = ttk.Combobox(
            line3, textvariable=slot["api_type"], width=15, values=list(_API_TYPE_BY_LABEL.keys()), state="readonly"
        )
        combo_api.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(line3, text=hint).pack(side=tk.LEFT, padx=(12, 0))

        slot["profile_combo"] = combo_profile
        slot["combo_model"] = combo_model
        slot["fields"] = [
            (combo_profile, "readonly"),
            (combo_model, "normal"),
            (entry_url, "normal"),
            (entry_key, "normal"),
            (combo_think, "readonly"),
            (combo_api, "readonly"),
        ]
        self._cross_slots[key] = slot
        return row + 3

    # 交叉校验槽位中「复用主模型连接」的显示文本
    _CROSS_REUSE_LABEL = "复用主模型"

    def _cross_profile_choices(self) -> list[str]:
        names = [p["name"] for p in getattr(self, "_api_profiles", [])]
        return [self._CROSS_REUSE_LABEL] + names

    def _on_cross_profile_change(self, key):
        """槽位选择接口配置后，自动填充 base_url / API Key，并刷新模型候选列表。"""
        slot = getattr(self, "_cross_slots", {}).get(key)
        if slot is None:
            return
        name = slot["profile"].get()
        if name == self._CROSS_REUSE_LABEL:
            slot["base_url"].set("")
            slot["api_key"].set("")
            profile = self._find_profile(self._active_profile_var.get())
            models = list(profile["models"]) if profile else []
            if not (slot["model"].get() or "").strip() and models:
                slot["model"].set((profile.get("default_model") or "").strip() or models[0])
        else:
            profile = self._find_profile(name)
            if profile is None:
                slot["profile"].set(self._CROSS_REUSE_LABEL)
                self._on_cross_profile_change(key)
                return
            slot["base_url"].set(profile["base_url"])
            slot["api_key"].set(profile["api_key"])
            models = list(profile["models"])
            default_model = (profile.get("default_model") or "").strip()
            if default_model:
                slot["model"].set(default_model)
            elif not (slot["model"].get() or "").strip() and models:
                slot["model"].set(models[0])
            if profile["provider"] in self._SPECIAL_PROVIDERS:
                print(
                    f"[交叉校验] 注意：配置「{name}」为 {profile['provider']} 专用接口，"
                    "交叉校验仅支持 OpenAI 兼容接口，请改选兼容网关配置或使用「复用主模型」"
                )
        slot["combo_model"].configure(values=models)

    def _refresh_cross_profile_choices(self):
        """接口配置列表变化后，同步各交叉校验槽位的候选项。"""
        choices = self._cross_profile_choices()
        for key, slot in getattr(self, "_cross_slots", {}).items():
            try:
                slot["profile_combo"].configure(values=choices)
            except tk.TclError:
                continue
            name = slot["profile"].get()
            if name != self._CROSS_REUSE_LABEL and name not in choices:
                slot["profile"].set(self._CROSS_REUSE_LABEL)
            if slot["profile"].get() == self._CROSS_REUSE_LABEL:
                self._on_cross_profile_change(key)

    def _sync_crosscheck_state(self):
        """根据总开关与各槽位开关，联动启用/禁用交叉校验的所有输入控件。"""
        if not hasattr(self, "_cross_slots"):
            return
        master_enabled = bool(self.cross_check_var.get())
        for slot in self._cross_slots.values():
            slot["check"].configure(state=("normal" if master_enabled else "disabled"))
            slot_enabled = master_enabled and bool(slot["enabled"].get())
            for widget, enabled_state in slot["fields"]:
                widget.configure(state=(enabled_state if slot_enabled else "disabled"))

    def _get_cross_tolerance(self) -> int:
        try:
            return int(max(0, min(10, int(float(self.cross_tolerance_var.get())))))
        except (TypeError, ValueError, tk.TclError):
            return 0

    def _collect_cross_config(self) -> dict:
        cfg = {
            "enabled": bool(self.cross_check_var.get()),
            "tolerance": self._get_cross_tolerance(),
            "round3_mode": self.round3_mode_var.get(),
        }
        for key, slot in getattr(self, "_cross_slots", {}).items():
            cfg[key] = {
                "enabled": bool(slot["enabled"].get()),
                "profile": slot["profile"].get(),
                "model": slot["model"].get().strip(),
                "base_url": slot["base_url"].get().strip(),
                "api_key": slot["api_key"].get().strip(),
                "thinking_mode": slot["thinking"].get(),
                "api_type": slot["api_type"].get(),
            }
        return cfg

    def _fetch_models(self):
        provider = self.provider_var.get()
        if provider in self._SPECIAL_PROVIDERS:
            messagebox.showinfo("提示", f"{provider} 当前使用专用 SDK/接口，暂不支持自动获取模型列表。")
            return

        api_key = (self.api_key_var.get() or "").strip()
        if not api_key:
            messagebox.showerror("配置不完整", "请先在「API 配置」选项卡中为当前接口配置填写 API Key")
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
        extra_headers: dict[str, str] = {}
        if extra_headers_raw:
            try:
                parsed_headers: Any = json.loads(extra_headers_raw)
                if not isinstance(parsed_headers, dict):
                    raise ValueError("额外请求头必须是 JSON 对象")
                if not all(isinstance(key, str) and isinstance(value, str) for key, value in parsed_headers.items()):
                    raise ValueError("额外请求头的键和值都必须是字符串")
                extra_headers = parsed_headers
            except Exception as e:
                messagebox.showerror("配置错误", f"额外请求头JSON解析失败：{e}")
                return

        policy = get_request_policy()
        self.fetch_models_btn.configure(state="disabled", text="获取中…")
        print(
            f"[模型列表] 正在获取 {provider} 模型列表：{base_url}"
            f"（超时 {policy['timeout_seconds']}s · 重试 {policy['max_retries']} 次）"
        )

        def _do_fetch():
            try:
                models = fetch_openai_compatible_models(
                    base_url=base_url,
                    api_key=api_key,
                    extra_headers=extra_headers,
                    timeout=None,  # 使用全局请求设置
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
        profile = self._find_profile(self._active_profile_var.get())
        if current not in models:
            self.model_var.set(models[0])
        if profile is not None:
            # 模型列表写回当前接口配置并持久化，下次启动可直接选择
            profile["models"] = list(models)
            profile["default_model"] = (self.model_var.get() or "").strip() or models[0]
            self._save_config(silent=True)
            self._refresh_profile_choices()
            self._ap_refresh_tree()
        print(f"[模型列表] 已获取 {len(models)} 个模型，已保存到当前接口配置")
        messagebox.showinfo(
            "模型列表",
            f"已获取 {len(models)} 个模型，已填入模型下拉框并保存到当前接口配置。",
        )

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
        "小米MiMo":     ("https://api.xiaomimimo.com/v1",             "mimo-v2.5-pro"),
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
        """连接参数变化后刷新主界面摘要（连接详情在「API 配置」选项卡维护）。"""
        provider = self.provider_var.get()
        if hasattr(self, "conn_summary_var"):
            self.conn_summary_var.set(self._describe_connection())

        # 仅在参数为空时用预设兜底（正常流程由接口配置提供完整参数）
        preset = self.PROVIDER_PRESETS.get(provider)
        if preset:
            preset_url, preset_model = preset
            if preset_url and not (self.base_url_var.get() or "").strip():
                self.base_url_var.set(preset_url)
                if hasattr(self, "conn_summary_var"):
                    self.conn_summary_var.set(self._describe_connection())
            if not (self.model_var.get() or "").strip() and preset_model:
                self.model_var.set(preset_model)

        if provider in self._SPECIAL_PROVIDERS and self._provider_notice_provider != provider:
            key_format = {
                "智谱AI": "API Key 直接填写（智谱官方 SDK）",
                "百度千帆": "API Key 请填写 API_Key:Secret_Key 格式",
                "科大讯飞": "API Key 请填写 appId:apiKey:apiSecret 格式",
            }.get(provider, "")
            if key_format:
                print(f"{provider}：{key_format}")
            self._provider_notice_provider = provider

    def _resolve_api_type(self) -> str:
        """把界面上的「接口类型」下拉框取值翻译成内部常量。

        自动判断 -> "auto"（按域名识别）
        Chat Completions -> "chat"
        Responses API -> "responses"
        """
        label = (self.api_type_var.get() or "").strip()
        return _API_TYPE_BY_LABEL.get(label, "auto")

    def _resolve_thinking_flag(self) -> bool | None:
        """把界面上的「思考模式」三态选项翻译成评分器的 enable_thinking 参数。

        自动 -> None（按模型名判断，服务端拒绝时自动降级重试）
        关闭 -> False（永不发thinking，绕开不支持该参数的网关）
        开启 -> True
        """
        mode = self.thinking_mode_var.get()
        if mode == "关闭":
            return False
        if mode == "开启":
            return True
        return None

    def _collect_config(self) -> dict:
        # 保存前把主界面当前模型回写到当前接口配置
        self._sync_active_profile_from_main()
        cfg = {
            "provider": self.provider_var.get(),
            "api_key": self.api_key_var.get(),
            "model": self.model_var.get(),
            "base_url": self.base_url_var.get(),
            "extra_headers_json": self.extra_headers_var.get(),
            "thinking_mode": self.thinking_mode_var.get(),
            "api_type": self.api_type_var.get(),
            "api_profiles": [dict(p) for p in self._api_profiles],
            "active_profile": self._active_profile_var.get(),
            "request_timeout_seconds": self._get_request_timeout(),
            "request_max_retries": self._get_request_max_retries(),
            "criteria": self.criteria_text.get("1.0", "end").strip(),
            "batch_mode": bool(self.batch_var.get()),
            "total_questions": self.total_var.get(),
            "blank_threshold": self._get_blank_threshold(),
            "filler_mode": "pyautogui",
            "select_all_method": self._get_select_all_method(),
            "review_score_check_enabled": bool(self.review_score_check_var.get()),
            "cross_check": self._collect_cross_config(),
            "ui_sections": dict(self._ui_section_state),
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
        self.system.filler.select_all_method = self._get_select_all_method()
        self.system.filler.config["review_score_check_enabled"] = bool(self.review_score_check_var.get())
        self.system.filler.config["batch_mode"] = bool(self.batch_var.get())
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
            "thinking_mode": cfg.get("thinking_mode", ""),
            "api_type": cfg.get("api_type", ""),
            "cross_check": json.dumps(cfg.get("cross_check") or {}, ensure_ascii=False, sort_keys=True),
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
        if "thinking_mode" in cfg:
            mode = str(cfg["thinking_mode"])
            if mode in ("自动", "关闭", "开启"):
                self.thinking_mode_var.set(mode)
        if "api_type" in cfg:
            label = str(cfg["api_type"])
            if label in _API_TYPE_BY_LABEL:
                self.api_type_var.set(label)

        # ── 接口配置（API 配置选项卡）：加载配置列表并恢复当前选择 ──
        profiles: list[dict] = []
        raw_profiles = cfg.get("api_profiles")
        if isinstance(raw_profiles, list):
            seen_names: set[str] = set()
            for item in raw_profiles:
                if not isinstance(item, dict):
                    continue
                profile = self._normalize_profile(item)
                if profile["name"] in seen_names:
                    continue
                seen_names.add(profile["name"])
                profiles.append(profile)
        if not profiles:
            # 旧版配置（无 api_profiles）：从顶层连接字段合成一个配置
            profiles = [self._normalize_profile(self._legacy_profile_from_cfg(cfg))]
        self._api_profiles = profiles
        active_name = str(cfg.get("active_profile") or "")
        if active_name not in [p["name"] for p in profiles]:
            active_name = self._match_profile_by_connection(cfg) or profiles[0]["name"]
        self._apply_profile_to_vars(active_name)
        if cfg.get("model"):
            # 顶层 model 优先（保持旧配置文件的“当前模型”语义）
            self.model_var.set(str(cfg["model"]))
        self._refresh_profile_choices()

        # ── 请求设置（全局超时 / 失败重试次数） ──
        self.request_timeout_var.set(str(cfg.get("request_timeout_seconds", API_TIMEOUT_SECONDS)))
        self.request_max_retries_var.set(str(cfg.get("request_max_retries", API_MAX_RETRIES)))
        self._apply_request_policy()

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

        if "select_all_method" in cfg:
            method = str(cfg["select_all_method"])
            label = self._SELECT_ALL_LABEL_REVERSE.get(method)
            if label:
                self.select_all_method_var.set(label)

        if "review_score_check_enabled" in cfg:
            self.review_score_check_var.set(bool(cfg["review_score_check_enabled"]))

        cross_cfg = cfg.get("cross_check")
        if isinstance(cross_cfg, dict):
            self.cross_check_var.set(bool(cross_cfg.get("enabled", False)))
            try:
                self.cross_tolerance_var.set(str(int(cross_cfg.get("tolerance", 0))))
            except (TypeError, ValueError):
                self.cross_tolerance_var.set("0")
            mode = str(cross_cfg.get("round3_mode", "自动选择"))
            if mode in self._ROUND3_MODE_MAP:
                self.round3_mode_var.set(mode)
            for key, slot in getattr(self, "_cross_slots", {}).items():
                slot_cfg = cross_cfg.get(key)
                if not isinstance(slot_cfg, dict):
                    continue
                slot["enabled"].set(bool(slot_cfg.get("enabled", False)))
                prof_name = str(slot_cfg.get("profile") or self._CROSS_REUSE_LABEL)
                if prof_name not in self._cross_profile_choices():
                    prof_name = self._CROSS_REUSE_LABEL
                slot["profile"].set(prof_name)
                slot["model"].set(str(slot_cfg.get("model", "")))
                slot["base_url"].set(str(slot_cfg.get("base_url", "")))
                slot["api_key"].set(str(slot_cfg.get("api_key", "")))
                thinking = str(slot_cfg.get("thinking_mode", "自动"))
                if thinking in ("自动", "关闭", "开启"):
                    slot["thinking"].set(thinking)
                api_type = str(slot_cfg.get("api_type", "自动判断"))
                if api_type in _API_TYPE_BY_LABEL:
                    slot["api_type"].set(api_type)
                ref_profile = self._find_profile(
                    self._active_profile_var.get() if prof_name == self._CROSS_REUSE_LABEL else prof_name
                )
                slot["combo_model"].configure(values=list(ref_profile["models"]) if ref_profile else [])
            self._sync_crosscheck_state()

        for key in ("screenshot_region_norm", "score_input_pos", "submit_btn_pos", "next_btn_pos"):
            if key in cfg:
                self._runtime_config[key] = cfg.get(key)

        # ── 可折叠模块：恢复上次的展开/收起状态 ──
        ui_sections = cfg.get("ui_sections")
        if isinstance(ui_sections, dict):
            for name, sec in getattr(self, "_sections", {}).items():
                if name in ui_sections:
                    state = bool(ui_sections[name])
                    sec.set_collapsed(state, notify=False)
                    self._ui_section_state[name] = state
            self._refresh_scrollregion()

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
            self._sync_filler_state()
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
        main_extra_headers: dict = {}
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
            main_extra_headers = extra_headers

            scorer = OpenAICompatibleScorer(
                base_url=base_url,
                api_key=api_key,
                model=model,
                extra_headers=extra_headers,
                enable_thinking=self._resolve_thinking_flag(),
                api_type=self._resolve_api_type(),
            )

        cross_checker = self._build_cross_checker(scorer, main_extra_headers)

        self.system = AutoScoringSystem(
            root=self,
            api_key=api_key,
            criteria=criteria,
            model=model,
            batch_mode=batch_mode,
            scorer=scorer,
            capture_dir=str(self.capture_dir),
            filler_mode="pyautogui",
            filler_config={
                "review_score_check_enabled": bool(self.review_score_check_var.get()),
                "batch_mode": batch_mode,
                "score_readback_delay_seconds": 0.25,
            },
            on_score_callback=self._tune_add_record,
            on_region_selected=self._on_region_selected,
            on_position_selected=self._on_position_selected,
            before_capture=self._before_capture,
            after_capture=self._after_capture,
            blank_threshold=self._get_blank_threshold(),
            cross_checker=cross_checker,
            on_notify=self._notify,
        )
        self._system_cfg_key = new_key
        self._sync_runtime_config_to_system()

        if batch_mode:
            try:
                self.system.total_questions = int(self.total_var.get() or "0")
            except ValueError:
                self.system.total_questions = 0

        return self.system

    def _build_cross_checker(self, primary_scorer, main_extra_headers):
        """根据界面配置构建多模型交叉校验器；未启用时返回 None。

        附加模型（模型B / 模型C）与仲裁模型统一使用 OpenAI 兼容接口：
        - base_url / API Key 留空时复用主模型的连接（仅主模型为 OpenAI 兼容服务商时可用）
        """
        cc = self._collect_cross_config()
        if not cc.get("enabled"):
            return None

        main_provider = self.provider_var.get()
        main_base_url = (self.base_url_var.get() or "").strip()
        main_api_key = (self.api_key_var.get() or "").strip()
        main_is_special = main_provider in self._SPECIAL_PROVIDERS
        if not main_base_url and not main_is_special and main_provider != "自定义":
            preset = self.PROVIDER_PRESETS.get(main_provider)
            if preset and preset[0]:
                main_base_url = preset[0]

        # 交叉校验仅支持 OpenAI 兼容接口：所选配置为专用接口时提前给出明确报错
        for key, label in (("b", "模型B"), ("c", "模型C"), ("arbiter", "仲裁模型")):
            slot_cfg = cc.get(key) or {}
            if not slot_cfg.get("enabled"):
                continue
            prof_name = (slot_cfg.get("profile") or "").strip()
            if prof_name and prof_name != self._CROSS_REUSE_LABEL:
                prof = self._find_profile(prof_name)
                if prof is not None and prof["provider"] in self._SPECIAL_PROVIDERS:
                    raise ValueError(
                        f"「{label}」所选接口配置「{prof_name}」是 {prof['provider']} 专用接口，"
                        "交叉校验仅支持 OpenAI 兼容接口；请改选兼容网关配置或使用「复用主模型」"
                    )

        def _build_slot(slot_cfg, slot_label):
            model_name = (slot_cfg.get("model") or "").strip()
            if not model_name:
                return None
            base_url = (slot_cfg.get("base_url") or "").strip()
            api_key = (slot_cfg.get("api_key") or "").strip()
            if not base_url or not api_key:
                if main_is_special:
                    raise ValueError(
                        f"「{slot_label}」需要填写自己的 base_url 和 API Key"
                        f"（主模型为 {main_provider}，无法复用其连接）"
                    )
                base_url = base_url or main_base_url
                api_key = api_key or main_api_key
            if not base_url:
                raise ValueError(f"「{slot_label}」未填写 base_url，且主模型的 base_url 为空，无法复用")
            if not api_key:
                raise ValueError(f"「{slot_label}」未填写 API Key，且主模型的 API Key 为空，无法复用")
            # 仅在确实复用主模型连接时才带上主模型的额外请求头
            reuses_main = base_url == main_base_url and api_key == main_api_key
            thinking = {"关闭": False, "开启": True}.get(slot_cfg.get("thinking_mode", "自动"), None)
            api_type = _API_TYPE_BY_LABEL.get(slot_cfg.get("api_type", "自动判断"), "auto")
            return OpenAICompatibleScorer(
                base_url=base_url,
                api_key=api_key,
                model=model_name,
                extra_headers=(main_extra_headers or {}) if reuses_main else {},
                enable_thinking=thinking,
                api_type=api_type,
            )

        extras = []
        for key, label in (("b", "模型B"), ("c", "模型C")):
            slot_cfg = cc.get(key) or {}
            if not slot_cfg.get("enabled"):
                continue
            slot_scorer = _build_slot(slot_cfg, label)
            if slot_scorer is None:
                raise ValueError(f"已启用「{label}」但未填写模型名")
            extras.append(slot_scorer)
        if not extras:
            raise ValueError("已启用多模型交叉校验，但未启用任何附加模型（模型B / 模型C 至少启用一个）")

        arbiter_scorer = None
        arbiter_cfg = cc.get("arbiter") or {}
        if arbiter_cfg.get("enabled"):
            arbiter_scorer = _build_slot(arbiter_cfg, "仲裁模型")
            if arbiter_scorer is None:
                raise ValueError("已启用「仲裁模型」但未填写模型名")

        round3_mode = self._ROUND3_MODE_MAP.get(cc.get("round3_mode", "自动选择"), "auto")
        if round3_mode == "arbiter" and arbiter_scorer is None:
            raise ValueError("第三轮模式为「独立仲裁模型」，请启用仲裁模型并填写模型名（或改为「自动选择」）")

        summary = "、".join(getattr(s, "model", "") for s in extras)
        arbiter_note = (
            f"；仲裁模型 {arbiter_scorer.model}" if arbiter_scorer is not None
            else "；未配置仲裁模型（第三轮将由主模型参考重评）"
        )
        print(
            f"[交叉校验] 已启用：主模型 {getattr(primary_scorer, 'model', '')} + {summary}"
            f"{arbiter_note}；容差 {cc.get('tolerance', 0)} 分"
        )
        return MultiModelCrossChecker(
            primary_scorer=primary_scorer,
            extra_scorers=extras,
            arbiter_scorer=arbiter_scorer,
            tolerance=cc.get("tolerance", 0),
            round3_mode=round3_mode,
            on_notify=self._notify,
        )

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

        self._sync_filler_state()
        sys_.question_count = 0
        self._run_started_at = time.time()
        self.run_duration_var.set("本次评分总用时：0.0s（进行中）")
        sys_.start()
        self.progress_var.set("运行中…")
        self.after(200, self._poll_progress)

    def _refresh_run_duration(self, running: bool):
        """刷新「本次评分总用时」：运行中实时显示，结束后定格最终值。"""
        started = self._run_started_at
        if started is None:
            return
        elapsed = time.time() - started
        text = self._format_duration(elapsed)
        var = getattr(self, "run_duration_var", None)
        if var is None:
            return
        if running:
            var.set(f"本次评分总用时：{text}（进行中）")
        else:
            var.set(f"本次评分总用时：{text}")
            self._run_started_at = None

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
            self._refresh_run_duration(running=True)
            self.after(350, self._poll_progress)
        else:
            if sys_.batch_mode:
                self.progress_var.set(f"已停止（已处理 {sys_.question_count} 份）")
            else:
                self.progress_var.set("已完成（单题）")
            self._refresh_run_duration(running=False)

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
            policy = get_request_policy()
            print(
                f"[生成评分标准] 请求设置：超时 {policy['timeout_seconds']} 秒 · 最多重试 {policy['max_retries']} 次"
                "（可在「API 配置 → 请求设置」中调整）"
            )
            # 打印诊断信息
            if hasattr(sys_.scorer, "base_url"):
                print(f"[生成评分标准] 请求 URL 基础路径: {sys_.scorer.base_url}")

            # 调用评分器生成（超时与重试次数统一由全局请求设置控制，这里不再叠加重试）
            sys_.scorer.grade_answer(temp_path, prompt)

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

        info_lines = [
            f"记录 #{record.index} | 题号 {record.question_index or '—'}",
            f"时间：{record.created_at or '—'}",
            f"AI 分数：{self._format_score(record.ai_score)}分 | 模型 {record.model or '—'}",
        ]
        if isinstance(record.cross_check, dict) and record.cross_check:
            info_lines.append(f"多模型校验：{self._summarize_cross_check(record.cross_check)}")
        if record.manual_score is not None:
            info_lines.append(f"人工标注：{self._format_score(record.manual_score)}分")
            if record.manual_score != record.ai_score:
                info_lines.append(f"错误原因：{record.error_reason or '未填写'}")
        info_lines.append("双击查看完整详情")
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

    # ── 评分记录详情窗口（双击打开） ──

    def _on_record_double_click(self, event):
        row_id = self.tune_tree.identify_row(event.y)
        if not row_id:
            return
        record = self._get_tune_record_by_item(row_id)
        if record is None:
            return
        self._hide_tune_preview()
        self._show_record_detail(record)

    def _show_record_detail(self, record):
        """打开独立窗口显示截图与评分详情（含多模型交叉校验各模型分数）。"""
        old = getattr(self, "_record_detail_window", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        self._record_detail_window = win
        win.title(f"评分记录详情 #{record.index}")
        win.geometry("1020x700")
        win.minsize(800, 560)
        win.tk.call("wm", "attributes", str(win), "-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_record_detail(win))

        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 左侧：截图
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        self._record_detail_image_ref = None
        image_path = (record.image_path or "").strip()
        if not image_path:
            ttk.Label(left, text="截图路径缺失").pack(anchor="w")
        else:
            path = Path(image_path)
            ttk.Label(left, text=f"截图：{path.name}").pack(anchor="w", pady=(0, 4))
            if not path.exists():
                ttk.Label(left, text="截图文件不存在（可能已被清理）").pack(anchor="w")
            else:
                try:
                    with Image.open(path) as img:
                        img.thumbnail((560, 600))
                        photo = ImageTk.PhotoImage(img.copy())
                    self._record_detail_image_ref = photo
                    ttk.Label(left, image=photo).pack(anchor="w")
                except Exception as e:
                    ttk.Label(left, text=f"截图加载失败：{e}").pack(anchor="w")

        # 右侧：详情标签页
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notebook = ttk.Notebook(right)
        notebook.pack(fill=tk.BOTH, expand=True)
        self._add_detail_text_tab(notebook, "评分详情", self._build_record_detail_text(record))
        self._add_detail_text_tab(notebook, "多模型校验", self._build_cross_check_text(record))
        self._add_detail_text_tab(notebook, "AI 反馈", self._extract_feedback_text(record.ai_response))
        self._add_detail_text_tab(notebook, "评分标准", record.criteria or "（未记录评分标准）")

        # 底部按钮
        bar = ttk.Frame(win)
        bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        if image_path and Path(image_path).exists():
            ttk.Button(bar, text="用系统程序打开原图", command=lambda: self._open_image_file(image_path)).pack(side=tk.LEFT)
        ttk.Button(bar, text="关闭", command=lambda: self._close_record_detail(win)).pack(side=tk.RIGHT)

    def _close_record_detail(self, win):
        try:
            win.destroy()
        except tk.TclError:
            pass
        if getattr(self, "_record_detail_window", None) is win:
            self._record_detail_window = None
        self._record_detail_image_ref = None

    def _add_detail_text_tab(self, notebook, title, content):
        frame = ttk.Frame(notebook)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10))
        def _scroll(*args: str) -> None:
            text.tk.call(str(text), "yview", *args)
        sb = ttk.Scrollbar(frame, command=_scroll)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
        notebook.add(frame, text=title)

    def _build_record_detail_text(self, record):
        lines = [
            f"序号：#{record.index}",
            f"题号：{record.question_index or '—'}",
            f"主模型：{record.model or '—'}",
            f"评分时间：{record.created_at or '—'}",
            f"评分用时：{self._format_duration(self._record_elapsed(record))}",
            f"AI 最终分数：{self._format_score(record.ai_score)} 分",
        ]
        status = getattr(record, "status", "")
        if record.manual_score is not None:
            lines.append(f"人工标注分数：{self._format_score(record.manual_score)} 分（{status}）")
            if record.error_reason:
                lines.append(f"错误原因：{record.error_reason}")
        cross_check = getattr(record, "cross_check", None)
        lines.append("")
        if isinstance(cross_check, dict) and cross_check:
            lines.append(f"多模型交叉校验：已启用（{self._summarize_cross_check(cross_check)}）")
            lines.append(
                f"最终采用：{self._format_score(cross_check.get('final_score'))} 分"
                f"（{cross_check.get('final_source') or '—'}）"
            )
            lines.append("各模型分数详见「多模型校验」标签页")
        else:
            lines.append("多模型交叉校验：未启用（本记录无多模型明细）")
        lines.append("")
        lines.append(f"截图文件：{record.image_path or '（无）'}")
        return "\n".join(lines)

    def _build_cross_check_text(self, record):
        cross_check = getattr(record, "cross_check", None)
        if not isinstance(cross_check, dict) or not cross_check:
            return "该记录未启用多模型交叉校验，或没有可显示的明细。"
        lines = [
            f"流程：{cross_check.get('flow') or '—'}",
            f"分数容差：{cross_check.get('tolerance', 0)} 分",
            "",
            "【首轮各模型评分】",
        ]
        for item in cross_check.get("scores") or []:
            if not isinstance(item, dict):
                continue
            score = item.get("score")
            score_txt = f"{self._format_score(score)} 分" if score is not None else "评分失败"
            err_txt = f"（{item.get('error')}）" if item.get("error") else ""
            elapsed = item.get("elapsed_seconds")
            elapsed_txt = f"，用时 {elapsed}s" if elapsed is not None else ""
            lines.append(
                f"  {item.get('name') or '模型'}（{item.get('model') or '—'}）：{score_txt}{err_txt}{elapsed_txt}"
            )
        round3 = cross_check.get("round3")
        if isinstance(round3, dict):
            mode_label = "独立仲裁" if round3.get("mode") == "arbiter" else "参考重评"
            lines.append("")
            lines.append("【第三轮校验】")
            lines.append(f"  方式：{mode_label}（{round3.get('model') or '—'}）")
            lines.append(f"  结果：{self._format_score(round3.get('score'))} 分")
            if round3.get("error"):
                lines.append(f"  异常：{round3['error']}")
            lines.append(f"  说明：{round3.get('source') or '—'}")
        lines.append("")
        lines.append(
            f"【最终结果】{self._format_score(cross_check.get('final_score'))} 分"
            f"（{cross_check.get('final_source') or '—'}）"
        )
        if cross_check.get("elapsed_total") is not None:
            lines.append(f"总用时：{cross_check.get('elapsed_total')}s")
        return "\n".join(lines)

    @staticmethod
    def _extract_feedback_text(full_text):
        text = full_text or ""
        if not text.strip():
            return "（无 AI 反馈文本）"
        if "===反馈开始===" in text and "===反馈结束===" in text:
            feedback = text.split("===反馈开始===")[1].split("===反馈结束===")[0].strip()
            return feedback or "（反馈为空）"
        return text

    def _open_image_file(self, image_path):
        try:
            os.startfile(image_path)  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开截图：{e}")

    def _tune_add_record(self, question_index, score, response_info, image_path=""):
        """从评分回调线程接收记录（线程安全）"""
        self.after(0, self._tune_add_record_ui, question_index, score, response_info, image_path)

    def _save_score_record(self, record, question_index):
        mode = "batch" if self.batch_var.get() else "single"
        question_value = "single" if question_index is None else str(question_index)
        cross_check_raw = getattr(record, "cross_check", "") or ""
        if not isinstance(cross_check_raw, str):
            cross_check_raw = json.dumps(cross_check_raw, ensure_ascii=False)
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
            error_reason=record.error_reason,
            cross_check=cross_check_raw,
            elapsed_seconds=getattr(record, "elapsed_seconds", None),
        )
        self._record_db_ids[record.index] = db_id
        return db_id

    def _tune_add_record_ui(self, question_index, score, response_info, image_path=""):
        idx = self._next_record_index
        self._next_record_index += 1
        criteria = self.criteria_text.get("1.0", "end").strip()
        response_info = response_info or {}
        cross_check_info = response_info.get("cross_check")
        record = ScoringRecord(
            index=idx,
            ai_score=score,
            ai_response=response_info.get("full_response", ""),
            criteria=criteria,
            image_path=image_path or "",
            cross_check=(cross_check_info if isinstance(cross_check_info, dict) else ""),
            question_index=("single" if question_index is None else str(question_index)),
            model=str(response_info.get("model") or (self.model_var.get() or "").strip()),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_seconds=response_info.get("elapsed_seconds"),
        )
        self.tuner.add_record(record)
        try:
            self._save_score_record(record, question_index)
        except Exception as e:
            print(f"[评分数据库] 写入失败：{e}")
        self._append_record_row(record)
        q_label = f"题目 {question_index}" if question_index is not None else "当前题目"
        cc_note = ""
        if isinstance(cross_check_info, dict) and cross_check_info.get("flow"):
            cc_note = f" | 交叉校验：{cross_check_info.get('flow')}"
        print(f"[评分记录] 记录 #{idx} 已添加 | {q_label} | AI分数：{score}分{cc_note}")
        self._tune_update_status()

    def _append_record_row(self, record):
        """把记录追加到列表并滚动到最新一行。"""
        self.tune_tree.insert("", "end", values=self._record_tree_values(record))
        children = self.tune_tree.get_children()
        if children:
            self.tune_tree.see(children[-1])

    def _record_tree_values(self, record):
        return (
            record.index,
            record.question_index or "—",
            self._format_score(record.ai_score),
            self._format_duration(self._record_elapsed(record)),
            record.model or "—",
            self._summarize_cross_check(getattr(record, "cross_check", None)),
        )

    @staticmethod
    def _record_elapsed(record):
        """取单条记录用时（秒）：优先取记录自身用时，缺失时回退交叉校验总用时。"""
        elapsed = getattr(record, "elapsed_seconds", None)
        if elapsed is None:
            cross = getattr(record, "cross_check", None)
            if isinstance(cross, dict):
                elapsed = cross.get("elapsed_total")
        return elapsed

    @staticmethod
    def _format_duration(seconds):
        """把秒数格式化为易读文本：<60s 显示「12.3s」，否则显示「1分23秒」。"""
        if seconds is None:
            return "—"
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return str(seconds)
        if value < 60:
            return f"{value:.1f}s"
        minutes = int(value // 60)
        remain = value - minutes * 60
        return f"{minutes}分{remain:.0f}秒"

    @staticmethod
    def _format_score(value):
        if value is None:
            return "—"
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        return str(int(num)) if num.is_integer() else f"{num:g}"

    @staticmethod
    def _summarize_cross_check(cross_check) -> str:
        """把交叉校验明细压缩成列表里的短标签。"""
        if not isinstance(cross_check, dict) or not cross_check:
            return "—"
        flow = str(cross_check.get("flow") or "")
        if "独立仲裁" in flow:
            return "三轮·仲裁"
        if "参考重评" in flow:
            return "三轮·重评"
        if "回退" in flow:
            return "降级·回退"
        if "仅主模型" in flow:
            return "降级·单模型"
        if "一致放行" in flow:
            return "一致"
        return "已启用"

    def _load_score_history(self):
        """启动时从数据库加载历史评分记录到列表（与本次运行的记录连续编号）。"""
        try:
            rows = self.score_db.fetch_records(limit=self.HISTORY_LOAD_LIMIT)
        except Exception as e:
            print(f"[评分记录] 历史记录加载失败：{e}")
            return
        if not rows:
            self._tune_update_status()
            return
        loaded = 0
        for row in rows:
            record = self._record_from_db_row(row)
            if record is None:
                continue
            self.tuner.add_record(record)
            self.tune_tree.insert("", "end", values=self._record_tree_values(record))
            try:
                self._record_db_ids[record.index] = int(row.get("id"))
            except (TypeError, ValueError):
                pass
            loaded += 1
        if self.tune_tree.get_children():
            self.tune_tree.see(self.tune_tree.get_children()[-1])
        capped = len(rows) >= self.HISTORY_LOAD_LIMIT
        note = f"（超过上限，仅显示最近 {self.HISTORY_LOAD_LIMIT} 条）" if capped else ""
        print(f"[评分记录] 已从数据库加载历史记录 {loaded} 条{note}")
        self._tune_update_status()

    def _record_from_db_row(self, row):
        """把数据库行转换为 ScoringRecord（用于历史记录展示）。"""
        try:
            cross_raw = row.get("cross_check") or ""
            cross_data = ""
            if isinstance(cross_raw, dict):
                cross_data = cross_raw
            elif isinstance(cross_raw, str) and cross_raw.strip():
                try:
                    parsed = json.loads(cross_raw)
                    if isinstance(parsed, dict):
                        cross_data = parsed
                except (ValueError, TypeError):
                    cross_data = ""
            record = ScoringRecord(
                index=self._next_record_index,
                ai_score=row.get("ai_score"),
                ai_response=row.get("ai_response") or "",
                criteria=row.get("criteria") or "",
                image_path=row.get("image_path") or "",
                manual_score=row.get("manual_score"),
                error_reason=row.get("error_reason") or "",
                cross_check=cross_data,
                question_index=row.get("question_index") or "",
                model=row.get("model") or "",
                created_at=row.get("created_at") or "",
                elapsed_seconds=row.get("elapsed_seconds"),
            )
        except Exception as e:
            print(f"[评分记录] 历史记录解析失败：{e}")
            return None
        self._next_record_index += 1
        return record

    def _clear_score_records(self):
        """清空评分记录：列表 + 数据库（清空前自动备份数据库，截图文件不受影响）。"""
        try:
            db_total = self.score_db.get_stats()["total"]
        except Exception:
            db_total = len(self.tuner.records)
        if db_total <= 0 and not self.tuner.records:
            messagebox.showinfo("清空记录", "当前没有可清空的评分记录。")
            return
        if not messagebox.askyesno(
            "清空评分记录",
            f"将清空列表，并删除数据库中的全部评分记录（数据库共 {db_total} 条）。\n\n"
            "· 清空前会自动备份数据库到 scores.db.bak\n"
            "· 截图文件（captures 目录）不受影响\n"
            "· 此操作不可恢复，请确认后再继续。\n\n"
            "确定要清空吗？",
        ):
            return

        backup_note = ""
        try:
            db_path = Path(self.score_db.db_path)
            if db_path.exists():
                backup_path = db_path.with_name(db_path.name + ".bak")
                shutil.copy2(db_path, backup_path)
                backup_note = backup_path.name
        except Exception as e:
            print(f"[评分记录] 数据库备份失败：{e}")
            if not messagebox.askyesno("备份失败", f"数据库备份失败：{e}\n\n仍要继续清空吗？"):
                return

        try:
            deleted = self.score_db.clear_records()
        except Exception as e:
            messagebox.showerror("清空失败", f"数据库清空失败：{e}")
            return

        # 关闭详情窗口与悬停预览，重置内存与列表
        detail_win = getattr(self, "_record_detail_window", None)
        if detail_win is not None:
            try:
                detail_win.destroy()
            except tk.TclError:
                pass
            self._record_detail_window = None
        self._hide_tune_preview()
        self.tuner.records.clear()
        self.tuner.suggested_criteria = ""
        self._next_record_index = 0
        self._record_db_ids.clear()
        for item in self.tune_tree.get_children():
            self.tune_tree.delete(item)
        self._tune_update_status()
        self._run_started_at = None
        if getattr(self, "run_duration_var", None) is not None:
            self.run_duration_var.set("本次评分总用时：—")

        tail = f"（已备份到 {backup_note}）" if backup_note else ""
        print(f"[评分记录] 已清空 {deleted} 条评分记录{tail}")
        messagebox.showinfo("已清空", f"已清空 {deleted} 条评分记录{tail}。")

    # ── 以下标记/调优方法为历史功能保留（当前 UI 已简化为纯记录浏览，未绑定任何控件；
    #    如需恢复「标记正确分数 + 规则调优」界面，重新添加对应控件并绑定这些方法即可） ──

    def _on_tune_tree_select(self, event):
        sel = self.tune_tree.selection()
        if sel:
            item = self.tune_tree.item(sel[0])
            vals = item["values"]
            if vals:
                self.tune_manual_var.set(str(vals[1]))  # 预设为 AI 分数
                self.tune_reason_var.set(str(vals[3]) if len(vals) > 3 else "")

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
        reason = self.tune_reason_var.get().strip()

        item = self.tune_tree.item(sel[0])
        try:
            idx = int(item["values"][0])
        except (TypeError, ValueError):
            messagebox.showerror("错误", "记录序号无效")
            return
        ai_score = next((r.ai_score for r in self.tuner.records if r.index == idx), None)
        if ai_score is not None and manual != ai_score and not reason:
            messagebox.showwarning("提示", "正确分数与 AI 分数不同时，请填写错误原因")
            return
        ok = self.tuner.set_manual_score(idx, manual, reason)
        if not ok:
            return
        # 更新 treeview
        record = next(r for r in self.tuner.records if r.index == idx)
        self.tune_tree.item(sel[0], values=(idx, record.ai_score, manual, record.error_reason, record.status))
        db_id = self._record_db_ids.get(idx)
        if db_id is not None:
            try:
                self.score_db.update_manual_score(db_id, manual, record.status, record.error_reason)
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
        self.tuner.update_config(
            api_key=api_key,
            base_url=base_url,
            model=model,
            extra_headers=extra_headers,
            api_type=self._resolve_api_type(),
        )

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
        total = len(self.tuner.records)
        try:
            db_stats = self.score_db.get_stats()
            self.tune_status_var.set(
                f"列表共 {total} 条记录（数据库累计 {db_stats['total']} 条）· 双击记录查看截图与评分详情"
            )
        except Exception:
            self.tune_status_var.set(f"列表共 {total} 条记录 · 双击记录查看截图与评分详情")

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
                    timeout=None,  # 使用全局请求设置（超时/重试）
                    api_type=self._resolve_api_type(),
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

