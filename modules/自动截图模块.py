#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动截图模块
负责题目截图区域选择与截图保存。
"""

import tkinter as tk

import pyautogui
from PIL import Image, ImageDraw, ImageGrab, ImageTk


class ScreenshotTool:
    """截图工具类"""

    def __init__(self, on_region_selected=None, before_capture=None, after_capture=None):
        # 选区用"归一化比例"存储，避免 Windows 缩放/多屏导致的坐标偏移
        # 格式：(l, t, r, b) 均为 0~1 之间的浮点数
        self.selected_region_norm = None
        self.selected_regions_norm = []  # 多区域选区列表
        self.on_region_selected = on_region_selected
        self.before_capture = before_capture
        self.after_capture = after_capture
        self.debug_print = False

    def _grab_all_screens(self):
        if self.before_capture:
            self.before_capture()
        try:
            try:
                return ImageGrab.grab(all_screens=True)
            except Exception:
                return pyautogui.screenshot()
        finally:
            if self.after_capture:
                self.after_capture()

    def select_region_interactive(self, root):
        """交互式选择截图区域"""
        self.selected_regions_norm = []  # 清除多区域选区
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
                if self.on_region_selected:
                    self.on_region_selected(self.selected_region_norm)
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

    def select_regions_interactive(self, root):
        """交互式选择多个截图区域（用于填空题等多空场景）"""
        self.selected_region_norm = None  # 清除单区域选区
        self.selected_regions_norm = []
        selection_window = tk.Toplevel(root)
        selection_window.attributes("-fullscreen", True)
        selection_window.attributes("-topmost", True)
        selection_window.title("拖拽框选多个答案区域（Enter确认 | Backspace撤销 | Esc取消）")

        canvas = tk.Canvas(selection_window, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        try:
            screen_shot = ImageGrab.grab(all_screens=True)
        except Exception:
            screen_shot = pyautogui.screenshot()
        img_w, img_h = screen_shot.size
        screen_image = ImageTk.PhotoImage(screen_shot)
        self.screen_image_ref = screen_image
        canvas.create_image(0, 0, anchor=tk.NW, image=screen_image)

        start_x, start_y = None, None
        current_rect = None
        drawn_rects = []
        region_colors = ["#FF4444", "#44FF44", "#4444FF", "#FFFF44", "#FF44FF", "#44FFFF"]

        hint_id = canvas.create_text(
            img_w // 2, 30,
            text="拖拽框选答案区域 | Enter确认 | Backspace撤销 | Esc取消",
            fill="white", font=("微软雅黑", 16, "bold"),
        )

        def _canvas_to_img(x, y):
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            ix = int(x * (img_w / cw))
            iy = int(y * (img_h / ch))
            ix = max(0, min(img_w - 1, ix))
            iy = max(0, min(img_h - 1, iy))
            return ix, iy

        def _update_hint():
            canvas.itemconfig(hint_id, text=f"已选 {len(self.selected_regions_norm)} 个区域 | 拖拽继续框选 | Enter确认 | Backspace撤销 | Esc取消")

        def on_mouse_down(event):
            nonlocal start_x, start_y, current_rect
            start_x, start_y = event.x, event.y
            color = region_colors[len(self.selected_regions_norm) % len(region_colors)]
            current_rect = canvas.create_rectangle(
                start_x, start_y, start_x, start_y, outline=color, width=3, dash=(5, 5)
            )

        def on_mouse_drag(event):
            nonlocal current_rect
            if current_rect and start_x is not None:
                canvas.coords(current_rect, start_x, start_y, event.x, event.y)

        def on_mouse_up(event):
            nonlocal start_x, start_y, current_rect
            if start_x is not None:
                x1, y1 = _canvas_to_img(start_x, start_y)
                x2, y2 = _canvas_to_img(event.x, event.y)
                left, right = (x1, x2) if x1 <= x2 else (x2, x1)
                top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)
                if right - left > 5 and bottom - top > 5:
                    region = (left / img_w, top / img_h, right / img_w, bottom / img_h)
                    self.selected_regions_norm.append(region)
                    drawn_rects.append(current_rect)
                    _update_hint()
                else:
                    canvas.delete(current_rect)
                current_rect = None

        def on_enter(event):
            if self.selected_regions_norm:
                if self.on_region_selected:
                    self.on_region_selected(self.selected_regions_norm[0])
                selection_window.destroy()

        def on_backspace(event):
            if self.selected_regions_norm:
                self.selected_regions_norm.pop()
                rect_id = drawn_rects.pop()
                canvas.delete(rect_id)
                _update_hint()

        def on_esc(event):
            self.selected_regions_norm = []
            selection_window.destroy()

        canvas.bind("<Button-1>", on_mouse_down)
        canvas.bind("<B1-Motion>", on_mouse_drag)
        canvas.bind("<ButtonRelease-1>", on_mouse_up)
        selection_window.bind("<Return>", on_enter)
        selection_window.bind("<BackSpace>", on_backspace)
        selection_window.bind("<Escape>", on_esc)

        btn_frame = tk.Frame(selection_window)
        btn_frame.place(x=10, y=10)
        tk.Button(btn_frame, text="确认", command=lambda: on_enter(None)).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="撤销", command=lambda: on_backspace(None)).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="取消", command=lambda: on_esc(None)).pack(side=tk.LEFT, padx=2)
        return True

    def capture_selected_region(self):
        """截取用户选择的单个区域"""
        if self.selected_region_norm:
            # 每次都重新截"虚拟屏幕全域"，然后按比例裁剪，避免 DPI/多屏坐标误差
            full = self._grab_all_screens()
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

    def capture_selected_regions(self):
        """截取多个区域并竖向拼接为一张图，每个区域前加编号标签"""
        if not self.selected_regions_norm:
            return None
        full = self._grab_all_screens()
        w, h = full.size
        crops = []
        for region in self.selected_regions_norm:
            l, t, r, b = region
            left = int(max(0, min(w - 1, l * w)))
            top = int(max(0, min(h - 1, t * h)))
            right = int(max(1, min(w, r * w)))
            bottom = int(max(1, min(h, b * h)))
            if right <= left:
                right = min(w, left + 1)
            if bottom <= top:
                bottom = min(h, top + 1)
            crops.append(full.crop((left, top, right, bottom)))
        if not crops:
            return None
        if len(crops) == 1:
            return crops[0]
        # 加载字体
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("msyh.ttc", 20)
        except Exception:
            font = ImageFont.load_default()
        label_width = 50
        separator_height = 3
        # 竖向拼接，每个区域左侧加编号标签
        max_width = max(c.width for c in crops) + label_width
        total_height = sum(c.height for c in crops) + separator_height * (len(crops) - 1)
        combined = Image.new("RGB", (max_width, total_height), (240, 240, 240))
        draw = ImageDraw.Draw(combined)
        y_offset = 0
        for i, crop in enumerate(crops):
            if crop.mode != "RGB":
                crop = crop.convert("RGB")
            # 绘制编号标签背景
            draw.rectangle([(0, y_offset), (label_width - 1, y_offset + crop.height)], fill=(70, 130, 180))
            # 绘制编号文字
            label = f"空{i + 1}"
            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = len(label) * 12, 20
            tx = (label_width - tw) // 2
            ty = y_offset + (crop.height - th) // 2
            draw.text((tx, ty), label, fill="white", font=font)
            # 粘贴截图区域
            combined.paste(crop, (label_width, y_offset))
            y_offset += crop.height
            if i < len(crops) - 1:
                draw.line([(0, y_offset), (max_width, y_offset)], fill=(180, 180, 180), width=separator_height)
                y_offset += separator_height
        return combined

    def capture_current_question(self):
        """截取当前屏幕中的题目区域"""
        # 优先使用多区域选区
        if self.selected_regions_norm:
            if self.debug_print:
                print(f"使用多区域截图，共 {len(self.selected_regions_norm)} 个区域")
            img = self.capture_selected_regions()
            if img:
                return img
        if self.selected_region_norm:
            if self.debug_print:
                print(f"使用选择的区域截图(归一化): {self.selected_region_norm}")
            return self.capture_selected_region()
        screen_width, screen_height = pyautogui.size()
        question_area = (100, 100, screen_width - 100, screen_height - 200)
        if self.debug_print:
            print(f"使用默认区域截图: {question_area}")
        if self.before_capture:
            self.before_capture()
        try:
            return pyautogui.screenshot(region=question_area)
        finally:
            if self.after_capture:
                self.after_capture()

    def enable_debug(self):
        self.debug_print = True

    def save_image(self, image, filename):
        if filename.lower().endswith((".jpg", ".jpeg")):
            if image.mode in ("RGBA", "P", "LA"):
                image = image.convert("RGB")
            image.save(filename, quality=85)
        else:
            image.save(filename)
        return filename
