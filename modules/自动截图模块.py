#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动截图模块
负责题目截图区域选择与截图保存。
"""

import tkinter as tk

import pyautogui
from PIL import ImageGrab, ImageTk


class ScreenshotTool:
    """截图工具类"""

    def __init__(self):
        # 选区用“归一化比例”存储，避免 Windows 缩放/多屏导致的坐标偏移
        # 格式：(l, t, r, b) 均为 0~1 之间的浮点数
        self.selected_region_norm = None
        self.debug_print = False

    def select_region_interactive(self, root):
        """交互式选择截图区域"""
        selection_window = tk.Toplevel(root)
        selection_window.attributes("-fullscreen", True)
        selection_window.attributes("-topmost", True)
        selection_window.title("拖拽框选截图区域（Esc 取消）")

        canvas = tk.Canvas(selection_window, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        # 在同一张“虚拟屏幕全域”截图上框选，然后记录成“比例坐标”
        # 优先使用 ImageGrab(all_screens=True) 解决多屏/DPI 缩放下范围不一致的问题
        try:
            screen_shot = ImageGrab.grab(all_screens=True)
        except Exception:
            screen_shot = pyautogui.screenshot()
        img_w, img_h = screen_shot.size
        if self.debug_print:
            print(f"全屏截图尺寸: {img_w}x{img_h}")
        screen_image = ImageTk.PhotoImage(screen_shot)
        self.screen_image_ref = screen_image
        canvas.create_image(0, 0, anchor=tk.NW, image=screen_image)

        start_x, start_y = None, None
        current_rect = None

        def _canvas_to_img(x, y):
            # Tk 事件坐标可能是“逻辑像素”，截图是“物理像素”，用比例映射更稳
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            ix = int(x * (img_w / cw))
            iy = int(y * (img_h / ch))
            ix = max(0, min(img_w - 1, ix))
            iy = max(0, min(img_h - 1, iy))
            return ix, iy

        def on_mouse_down(event):
            nonlocal start_x, start_y, current_rect
            start_x, start_y = event.x, event.y
            current_rect = canvas.create_rectangle(
                start_x, start_y, start_x, start_y, outline="red", width=2, dash=(5, 5)
            )

        def on_mouse_drag(event):
            nonlocal current_rect
            if current_rect and start_x is not None:
                canvas.coords(current_rect, start_x, start_y, event.x, event.y)

        def on_mouse_up(event):
            nonlocal start_x, start_y
            if start_x is not None:
                x1, y1 = _canvas_to_img(start_x, start_y)
                x2, y2 = _canvas_to_img(event.x, event.y)
                left, right = (x1, x2) if x1 <= x2 else (x2, x1)
                top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)

                # 存为归一化比例，后续对不同分辨率截图也能正确裁剪
                self.selected_region_norm = (
                    left / img_w,
                    top / img_h,
                    right / img_w,
                    bottom / img_h,
                )
                if self.debug_print:
                    print(f"选区(截图像素): ({left},{top})-({right},{bottom}) / 图片 {img_w}x{img_h}")
                    print(f"选区(归一化): {self.selected_region_norm}")
                selection_window.destroy()

        def on_esc(event):
            selection_window.destroy()

        canvas.bind("<Button-1>", on_mouse_down)
        canvas.bind("<B1-Motion>", on_mouse_drag)
        canvas.bind("<ButtonRelease-1>", on_mouse_up)
        selection_window.bind("<Escape>", on_esc)

        cancel_btn = tk.Button(selection_window, text="取消", command=selection_window.destroy)
        cancel_btn.place(x=10, y=10)
        return True

    def capture_selected_region(self):
        """截取用户选择的区域"""
        if self.selected_region_norm:
            # 每次都重新截“虚拟屏幕全域”，然后按比例裁剪，避免 DPI/多屏坐标误差
            try:
                full = ImageGrab.grab(all_screens=True)
            except Exception:
                full = pyautogui.screenshot()
            w, h = full.size
            l, t, r, b = self.selected_region_norm
            left = int(max(0, min(w - 1, l * w)))
            top = int(max(0, min(h - 1, t * h)))
            right = int(max(1, min(w, r * w)))
            bottom = int(max(1, min(h, b * h)))

            if right <= left:
                right = min(w, left + 1)
            if bottom <= top:
                bottom = min(h, top + 1)

            if self.debug_print:
                print(f"裁剪区域(截图像素): ({left},{top})-({right},{bottom}) / {w}x{h}")
            return full.crop((left, top, right, bottom))
        return None

    def capture_current_question(self):
        """截取当前屏幕中的题目区域"""
        if self.selected_region_norm:
            if self.debug_print:
                print(f"使用选择的区域截图(归一化): {self.selected_region_norm}")
            return self.capture_selected_region()
        screen_width, screen_height = pyautogui.size()
        question_area = (100, 100, screen_width - 100, screen_height - 200)
        if self.debug_print:
            print(f"使用默认区域截图: {question_area}")
        return pyautogui.screenshot(region=question_area)

    def enable_debug(self):
        self.debug_print = True

    def save_image(self, image, filename):
        image.save(filename)
        return filename
