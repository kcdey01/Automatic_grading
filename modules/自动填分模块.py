#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动填分模块
负责记录网页控件坐标并执行自动填分操作。
"""

import time
import tkinter as tk
from tkinter import messagebox

import pyautogui


class AutoFiller:
    """自动填分工具类"""

    def __init__(self, root, mode="pyautogui", config=None, on_position_selected=None):
        self.root = root
        self.mode = mode or "pyautogui"
        self.config = config or {}
        self.on_position_selected = on_position_selected
        self.score_input_pos = None
        self.submit_btn_pos = None
        self.next_btn_pos = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    def _select_position(self, title, prompt, attr_name, success_name):
        # 使用全屏蒙层记录“屏幕绝对坐标”，避免窗口内相对坐标导致点偏
        selection_window = tk.Toplevel(self.root)
        selection_window.attributes("-fullscreen", True)
        selection_window.attributes("-topmost", True)
        selection_window.attributes("-alpha", 0.25)
        selection_window.configure(bg="black")
        selection_window.title(title)

        hint = tk.Label(
            selection_window,
            text=f"{prompt}\n(按 Esc 取消)",
            bg="#1f1f1f",
            fg="white",
            font=("Microsoft YaHei UI", 12),
            justify="left",
            padx=12,
            pady=8,
        )
        hint.place(x=20, y=20)

        def on_click(event):
            x, y = event.x_root, event.y_root
            setattr(self, attr_name, (x, y))
            if self.on_position_selected:
                self.on_position_selected(attr_name, (x, y))
            selection_window.destroy()
            messagebox.showinfo("成功", f"{success_name}位置已选择: ({x}, {y})")

        def on_esc(event):
            selection_window.destroy()

        selection_window.bind("<Button-1>", on_click)
        selection_window.bind("<Escape>", on_esc)
        selection_window.focus_force()

    def select_score_input(self):
        self._select_position(
            title="选择分数输入框",
            prompt="请点击网页上的分数输入框",
            attr_name="score_input_pos",
            success_name="分数输入框",
        )

    def select_submit_button(self):
        self._select_position(
            title="选择提交按钮",
            prompt="请点击网页上的提交按钮",
            attr_name="submit_btn_pos",
            success_name="提交按钮",
        )

    def select_next_button(self):
        self._select_position(
            title="选择下一题按钮",
            prompt="请点击网页上的下一题按钮",
            attr_name="next_btn_pos",
            success_name="下一题按钮",
        )

    def _ensure_dom_page(self):
        if self._page is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise RuntimeError("DOM 模式需要安装 playwright：pip install playwright") from e

        target_url = (self.config.get("target_url") or "").strip()
        browser_mode = (self.config.get("browser_mode") or "launch").strip().lower()
        cdp_url = (self.config.get("cdp_url") or "").strip()

        self._playwright = sync_playwright().start()
        if browser_mode == "connect":
            if not cdp_url:
                raise ValueError("连接已有浏览器时，请先填写 CDP 地址")
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = self._browser.new_context()
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            if target_url and (self._page.url in ("", "about:blank")):
                self._page.goto(target_url, wait_until="domcontentloaded")
        else:
            if not target_url:
                raise ValueError("DOM 模式请先填写目标页面 URL")
            self._browser = self._playwright.chromium.launch(headless=False)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
            self._page.goto(target_url, wait_until="domcontentloaded")
        return self._page

    def _fill_score_dom(self, score):
        page = self._ensure_dom_page()
        score_selector = (self.config.get("score_selector") or "").strip()
        submit_selector = (self.config.get("submit_selector") or "").strip()
        next_selector = (self.config.get("next_selector") or "").strip()
        action_delay_ms = int(self.config.get("action_delay_ms", 600))

        if not score_selector:
            raise ValueError("DOM 模式请先填写分数输入框 CSS 选择器")
        if not submit_selector:
            raise ValueError("DOM 模式请先填写提交按钮 CSS 选择器")

        page.wait_for_selector(score_selector, timeout=8000)
        page.fill(score_selector, str(score))
        page.wait_for_selector(submit_selector, timeout=8000)
        page.click(submit_selector)
        page.wait_for_timeout(action_delay_ms)
        if next_selector:
            page.wait_for_selector(next_selector, timeout=8000)
            page.click(next_selector)

    def pick_selector(self, field_name):
        page = self._ensure_dom_page()
        page.bring_to_front()
        print(f"[DOM] 请在浏览器中点击：{field_name}")
        selector = page.evaluate(
            """
            (fieldName) => {
              const buildSelector = (el) => {
                if (!el) return "";
                if (el.id) return `#${CSS.escape(el.id)}`;
                const testId = el.getAttribute("data-testid");
                if (testId) return `[data-testid="${testId.replaceAll('"', '\\"')}"]`;
                const name = el.getAttribute("name");
                if (name) return `${el.tagName.toLowerCase()}[name="${name.replaceAll('"', '\\"')}"]`;

                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== "html") {
                  let part = node.tagName.toLowerCase();
                  if (node.classList && node.classList.length > 0) {
                    const cls = [...node.classList].slice(0, 2).map((x) => `.${CSS.escape(x)}`).join("");
                    if (cls) part += cls;
                  }
                  const parent = node.parentElement;
                  if (parent) {
                    const siblings = [...parent.children].filter((c) => c.tagName === node.tagName);
                    if (siblings.length > 1) {
                      const index = siblings.indexOf(node) + 1;
                      part += `:nth-of-type(${index})`;
                    }
                  }
                  parts.unshift(part);
                  node = parent;
                }
                return parts.join(" > ");
              };

              return new Promise((resolve) => {
                const old = document.getElementById("__dom_picker_hint__");
                if (old) old.remove();
                const hint = document.createElement("div");
                hint.id = "__dom_picker_hint__";
                hint.textContent = `请点击：${fieldName}`;
                hint.style.cssText = "position:fixed;top:12px;left:12px;z-index:2147483647;background:#111;color:#fff;padding:8px 12px;border-radius:6px;font:14px/1.4 sans-serif;";
                document.body.appendChild(hint);

                const onClick = (e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  e.stopImmediatePropagation();
                  document.removeEventListener("click", onClick, true);
                  hint.remove();
                  resolve(buildSelector(e.target));
                };
                document.addEventListener("click", onClick, true);

                setTimeout(() => {
                  document.removeEventListener("click", onClick, true);
                  hint.remove();
                  resolve("");
                }, 120000);
              });
            }
            """,
            field_name,
        )
        if not selector:
            raise TimeoutError(f"等待点击超时：{field_name}")
        print(f"[DOM] 已识别 {field_name} 选择器: {selector}")
        return selector

    def fill_score(self, score):
        if self.mode == "dom":
            try:
                self._fill_score_dom(score)
            except Exception as e:
                print(f"DOM 填分错误：{e}")
            return

        time.sleep(1)
        try:
            if self.score_input_pos:
                pyautogui.click(self.score_input_pos)
                time.sleep(0.12)
                # 先清空原值，避免和已有分数拼接
                pyautogui.hotkey("ctrl", "a")
                pyautogui.press("backspace")
                pyautogui.typewrite(str(score))
                time.sleep(0.08)
                pyautogui.press("tab")

                if self.submit_btn_pos:
                    pyautogui.click(self.submit_btn_pos)
                    time.sleep(0.6)

                    if self.next_btn_pos:
                        pyautogui.click(self.next_btn_pos)
        except Exception as e:
            print(f"填分错误：{e}")

    def close(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
