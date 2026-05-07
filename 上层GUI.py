#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上层 GUI：调用 `自动阅卷系统GUI.py` 核心模块完成阅卷。

运行：
python 上层GUI.py
"""

import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

from 自动阅卷系统GUI import AutoScoringSystem, check_dependencies
from modules.自动评分模块 import OpenAICompatibleScorer, ZhipuAIScorer, BaiduScorer, XunfeiScorer
from modules.自动填分模块 import AutoFiller
from modules.规则调优模块 import RuleTuner, ScoringRecord


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
        self.tuner = RuleTuner()
        self._next_record_index = 0
        self._tuning_running = False

        self._build_menu()
        self._build_ui()
        self._load_config(silent=True)
        self.after(60, self._drain_log_queue)

    def _build_menu(self):
        menubar = tk.Menu(self, tearoff=0)
        self.configure(menu=menubar)

        # ── 文件 ──
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="保存配置", command=self._save_config)
        file_menu.add_command(label="加载配置", command=lambda: self._load_config(silent=False))
        file_menu.add_separator()
        file_menu.add_command(label="清空截图目录", command=self._clear_captures)
        menubar.add_cascade(label="文件", menu=file_menu)

        # ── 配置 ──
        cfg_menu = tk.Menu(menubar, tearoff=0)
        cfg_menu.add_command(label="选择截图区域", command=self._select_region)
        cfg_menu.add_command(label="测试截图", command=self._test_screenshot)
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
                "零一万物", "硅基流动", "百度千帆", "科大讯飞", "自定义"
            ],
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

        criteria_frame = ttk.LabelFrame(inner, text="评分标准（直接粘贴你的阅卷要求/评分细则）")
        criteria_frame.pack(fill=tk.BOTH, expand=False, **pad)
        self.criteria_text = tk.Text(criteria_frame, height=8, wrap="word")
        self.criteria_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        mid = ttk.Frame(inner)
        mid.pack(fill=tk.X, **pad)

        self.batch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mid, text="批量模式", variable=self.batch_var, command=self._sync_batch_state).grid(row=0, column=0, sticky="w")

        ttk.Label(mid, text="总份数(可选)").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.total_var = tk.StringVar(value="0")
        self.total_entry = ttk.Entry(mid, textvariable=self.total_var, width=10, state="disabled")
        self.total_entry.grid(row=0, column=2, sticky="w", padx=(6, 0))



        runbox = ttk.LabelFrame(inner, text="运行")
        runbox.pack(fill=tk.X, **pad)
        ttk.Button(runbox, text="开始（单题/批量）", command=self._start).grid(row=0, column=0, padx=8, pady=8, sticky="w")
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

        self.tune_result_text = tk.Text(tune_frame, height=4, wrap="word", state="disabled")
        self.tune_result_text.pack(fill=tk.X, padx=8, pady=(0, 4))

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

    def _sync_filler_state(self):
        return

    PROVIDER_PRESETS = {
        "OpenAI":       ("https://api.openai.com",                          "gpt-4o-mini"),
        "智谱AI":       ("",                                                "glm-4v"),
        "阿里通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1","qwen-vl-max"),
        "字节豆包":     ("https://ark.cn-beijing.volces.com/api/v3",        "doubao-vision-pro-32k"),
        "零一万物":     ("https://api.lingyiwanwu.com/v1",                  "yi-vision"),
        "硅基流动":     ("https://api.siliconflow.cn/v1",                   "Qwen/Qwen2-VL-72B-Instruct"),
        "百度千帆":     ("https://qianfan.baidubce.com",                    "ernie-4.0-8k"),
        "科大讯飞":     ("",                                                "spark-v4.0"),
        "自定义":       ("",                                                "gpt-4o-mini"),
    }

    def _sync_provider_state(self):
        provider = self.provider_var.get()
        preset = self.PROVIDER_PRESETS.get(provider)
        if preset:
            preset_url, preset_model = preset
            self.base_url_var.set(preset_url)
            if self.model_var.get().strip() in {"gpt-4o-mini", "glm-4v", "qwen-vl-max", "yi-vision", "spark-v4.0", "ernie-4.0-8k", "doubao-vision-pro-32k", ""}:
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
        elif provider == "自定义":
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="normal")
            self.base_url_var.set("")
        else:
            self.base_url_entry.configure(state="normal")
            self.extra_headers_entry.configure(state="normal")

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
            "请直接输出评分标准，不要输出多余内容。\n"
            "\n"
            "另外，在评分细则的最后，加上以下格式要求（逐字保留）：\n"
            "\n"
            "---\n"
            "评分时请以如下格式输出最终结果：\n"
            "最终得分：X分"
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

    def _tune_add_record(self, question_index, score, response_info):
        """从评分回调线程接收记录（线程安全）"""
        self.after(0, self._tune_add_record_ui, question_index, score, response_info)

    def _tune_add_record_ui(self, question_index, score, response_info):
        idx = self._next_record_index
        self._next_record_index += 1
        criteria = self.criteria_text.get("1.0", "end").strip()
        record = ScoringRecord(
            index=idx,
            ai_score=score,
            ai_response=response_info.get("full_response", ""),
            criteria=criteria,
        )
        self.tuner.add_record(record)
        self.tune_tree.insert("", "end", values=(idx, score, "—", "待标记"))
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
        idx = item["values"][0]
        ok = self.tuner.set_manual_score(idx, manual)
        if not ok:
            return
        # 更新 treeview
        record = next(r for r in self.tuner.records if r.index == idx)
        self.tune_tree.item(sel[0], values=(idx, record.ai_score, manual, record.status))
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
        self.tune_status_var.set(f"共 {stats['total']} 条 | 已标记 {stats['marked']} 条 | 偏差 {stats['mismatches']} 条")

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

