from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    HORIZONTAL,
    LEFT,
    RIGHT,
    TOP,
    VERTICAL,
    X,
    Y,
    Button,
    Canvas,
    Entry,
    Frame,
    Label,
    Listbox,
    PhotoImage,
    Scrollbar,
    StringVar,
    Tk,
    Text,
    filedialog,
    messagebox,
    ttk,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None


APP_TITLE = "粗剪拼接 - 无重编码视频片段工具"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".m2ts", ".avi", ".webm", ".wmv", ".mpeg", ".mpg"}
THUMB_W = 320
THUMB_H = 180
PREVIEW_W = 480
PREVIEW_H = 270
MAX_THUMBS = 90
MIN_THUMBS = 12
THUMB_WORKERS = 4
EDGE_HIT_PX = 10


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
    return f"{minutes:02d}:{secs:05.2f}"


def parse_duration(value: str) -> float:
    parts = value.strip().split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise ValueError("时间格式应为 秒、MM:SS 或 HH:MM:SS")


def find_tool(name: str) -> str | None:
    root = Path(__file__).resolve().parent
    candidates = [
        root / "bin" / f"{name}.exe",
        root / f"{name}.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def run_process(args: list[str], log=None) -> subprocess.CompletedProcess:
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)
        if log:
            log(line.rstrip())
    code = process.wait()
    return subprocess.CompletedProcess(args, code, "".join(lines), "")


class RoughCutApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x920")
        self.root.minsize(1040, 780)

        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.ffplay = find_tool("ffplay")

        self.input_path: Path | None = None
        self.duration = 0.0
        self.segments: list[Segment] = []
        self.thumb_images: list = []
        self.thumb_paths: list[Path] = []
        self.thumb_dir: Path | None = None
        self.timeline_width = 1
        self.drag_start_x: float | None = None
        self.drag_current_view_x: float | None = None
        self.drag_rect: int | None = None
        self.drag_mode = "create"
        self.drag_segment_index: int | None = None
        self.drag_edge: str | None = None
        self.autoscroll_job: str | None = None
        self.playhead_time = 0.0
        self.preview_image: PhotoImage | None = None
        self.preview_dir: Path | None = None
        self.preview_request_id = 0
        self.preview_rendering = False
        self.playing = False
        self.play_job: str | None = None
        self.export_cancel = threading.Event()
        self.export_process: subprocess.Popen | None = None

        self.status = StringVar(value="打开一个视频开始。也可以把视频文件拖到 run.bat 上启动。")
        self.file_label = StringVar(value="未打开视频")
        self.info_label = StringVar(value="")
        self.preview_time_var = StringVar(value="00:00.00 / 00:00.00")
        self.start_var = StringVar(value="00:00.00")
        self.end_var = StringVar(value="00:00.00")
        self.output_var = StringVar(value="")

        self._build_ui()
        self._check_tools()
        self.enable_drag_drop()

    def _build_ui(self) -> None:
        top = Frame(self.root, padx=10, pady=8)
        top.pack(side=TOP, fill=X)

        Button(top, text="打开视频", command=self.open_file).pack(side=LEFT)
        Button(top, text="预览所选开头", command=self.preview_selected).pack(side=LEFT, padx=(8, 0))
        self.export_button = Button(top, text="导出 _cut", command=self.export)
        self.export_button.pack(side=LEFT, padx=(8, 0))
        self.cancel_button = Button(top, text="取消导出", command=self.cancel_export, state="disabled")
        self.cancel_button.pack(side=LEFT, padx=(8, 0))
        Label(top, textvariable=self.status, anchor="w").pack(side=LEFT, padx=12, fill=X, expand=True)

        file_frame = Frame(self.root, padx=10)
        file_frame.pack(side=TOP, fill=X)
        Label(file_frame, textvariable=self.file_label, anchor="w").pack(side=TOP, fill=X)
        Label(file_frame, textvariable=self.info_label, anchor="w").pack(side=TOP, fill=X)

        preview_frame = Frame(self.root, padx=10, pady=8)
        preview_frame.pack(side=TOP, fill=X)
        self.preview_canvas = Canvas(
            preview_frame,
            width=PREVIEW_W,
            height=PREVIEW_H,
            bg="#0d1117",
            highlightthickness=1,
            highlightbackground="#444",
        )
        self.preview_canvas.pack(side=LEFT)
        self.preview_canvas.create_text(
            PREVIEW_W // 2,
            PREVIEW_H // 2,
            fill="#d0d7de",
            text="打开视频后显示预览",
        )
        controls = Frame(preview_frame, padx=12)
        controls.pack(side=LEFT, fill=BOTH, expand=True)
        Button(controls, text="播放/暂停", command=self.toggle_play).pack(side=TOP, anchor="w", pady=(0, 6))
        Button(controls, text="后退 1 秒", command=lambda: self.seek_relative(-1.0)).pack(side=TOP, anchor="w", pady=(0, 6))
        Button(controls, text="前进 1 秒", command=lambda: self.seek_relative(1.0)).pack(side=TOP, anchor="w", pady=(0, 6))
        Button(controls, text="跳到所选片段开头", command=self.preview_selected).pack(side=TOP, anchor="w", pady=(0, 6))
        Label(controls, textvariable=self.preview_time_var, anchor="w").pack(side=TOP, fill=X, pady=(8, 0))

        timeline_frame = Frame(self.root, padx=10, pady=8)
        timeline_frame.pack(side=TOP, fill=BOTH, expand=True)

        self.canvas = Canvas(timeline_frame, height=250, bg="#1f2328", highlightthickness=1, highlightbackground="#444")
        self.canvas.pack(side=TOP, fill=BOTH, expand=True)
        xscroll = Scrollbar(timeline_frame, orient=HORIZONTAL, command=self.canvas.xview)
        xscroll.pack(side=TOP, fill=X)
        self.canvas.configure(xscrollcommand=xscroll.set)
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_drag_end)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<Motion>", self.on_canvas_motion)

        middle = Frame(self.root, padx=10, pady=4)
        middle.pack(side=TOP, fill=X)
        Label(middle, text="开始").pack(side=LEFT)
        Entry(middle, textvariable=self.start_var, width=12).pack(side=LEFT, padx=(4, 10))
        Label(middle, text="结束").pack(side=LEFT)
        Entry(middle, textvariable=self.end_var, width=12).pack(side=LEFT, padx=(4, 10))
        Button(middle, text="按输入添加片段", command=self.add_segment_from_inputs).pack(side=LEFT)
        Button(middle, text="删除片段", command=self.delete_selected_segment).pack(side=LEFT, padx=(8, 0))
        Button(middle, text="上移", command=lambda: self.move_selected(-1)).pack(side=LEFT, padx=(8, 0))
        Button(middle, text="下移", command=lambda: self.move_selected(1)).pack(side=LEFT, padx=(4, 0))

        lower = Frame(self.root, padx=10, pady=6)
        lower.pack(side=TOP, fill=BOTH, expand=False)

        list_frame = Frame(lower)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True)
        Label(list_frame, text="导出片段顺序").pack(side=TOP, anchor="w")
        self.segment_list = Listbox(list_frame, height=8)
        self.segment_list.pack(side=LEFT, fill=BOTH, expand=True)
        yscroll = Scrollbar(list_frame, orient=VERTICAL, command=self.segment_list.yview)
        yscroll.pack(side=RIGHT, fill=Y)
        self.segment_list.configure(yscrollcommand=yscroll.set)
        self.segment_list.bind("<<ListboxSelect>>", self.on_segment_select)

        output_frame = Frame(self.root, padx=10, pady=4)
        output_frame.pack(side=TOP, fill=X)
        Label(output_frame, text="输出").pack(side=LEFT)
        Entry(output_frame, textvariable=self.output_var).pack(side=LEFT, fill=X, expand=True, padx=(4, 8))
        Button(output_frame, text="选择", command=self.choose_output).pack(side=RIGHT)
        Label(output_frame, text="fzlong", fg="#8b949e").pack(side=RIGHT, padx=(8, 4))

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(side=TOP, fill=X, padx=10, pady=(4, 0))

        self.log = Text(self.root, height=8, wrap="word")
        self.log.pack(side=BOTTOM, fill=BOTH, padx=10, pady=8)

    def _check_tools(self) -> None:
        missing = [name for name, value in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)) if not value]
        if missing:
            self.status.set("缺少 FFmpeg。请把 ffmpeg.exe/ffprobe.exe 放入本目录 bin，或加入 PATH。")
        else:
            self.status.set("FFmpeg 已就绪。可点击打开视频，也可把视频文件拖入窗口。")

    def enable_drag_drop(self) -> None:
        if DND_FILES is None:
            self.status.set(self.status.get() + " 当前环境未安装 tkinterdnd2，窗口拖放不可用。")
            return
        for widget in (self.root, self.preview_canvas, self.canvas, self.segment_list):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_file_drop)
            except Exception as exc:
                self.log_line(f"拖放注册失败: {exc}")

    def on_file_drop(self, event) -> None:
        paths = self.parse_dropped_paths(event.data)
        for path in paths:
            if path.suffix.lower() in VIDEO_SUFFIXES:
                self.load_video(path)
                return
        if paths:
            messagebox.showinfo("不是视频文件", "请拖入常见视频文件。")

    def parse_dropped_paths(self, data: str) -> list[Path]:
        try:
            items = self.root.tk.splitlist(data)
        except Exception:
            items = [data]
        return [Path(item.strip()) for item in items if item.strip()]

    def log_line(self, text: str) -> None:
        def write() -> None:
            self.log.insert(END, text + "\n")
            self.log.see(END)

        self.root.after(0, write)

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择视频",
            filetypes=[
                ("视频文件", "*.mp4 *.mkv *.mov *.flv *.ts *.m2ts *.avi *.webm *.wmv *.mpeg *.mpg"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.load_video(Path(path))

    def load_video(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror("文件不存在", str(path))
            return
        if not self.ffprobe or not self.ffmpeg:
            messagebox.showerror("FFmpeg 不可用", "请先准备 ffmpeg.exe 和 ffprobe.exe。")
            return

        self.input_path = path
        self.segments.clear()
        self.refresh_segment_list()
        self.file_label.set(str(path))
        self.output_var.set(str(self.default_output_path(path, ".mp4")))
        self.status.set("正在读取视频信息...")
        self.log.delete("1.0", END)

        def worker() -> None:
            try:
                info = self.probe(path)
                self.duration = float(info["format"]["duration"])
                video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
                audio_count = len([s for s in info.get("streams", []) if s.get("codec_type") == "audio"])
                summary = (
                    f"时长 {fmt_time(self.duration)}    "
                    f"{video.get('codec_name', 'unknown')}    "
                    f"{video.get('width', '?')}x{video.get('height', '?')}    "
                    f"音轨 {audio_count}"
                )
                self.root.after(0, lambda: self.info_label.set(summary))
                self.root.after(0, lambda: self.end_var.set(fmt_time(self.duration)))
                self.root.after(0, lambda: self.seek_to(0.0))
                self.generate_thumbnails(path)
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda: messagebox.showerror("打开失败", message))
                self.root.after(0, lambda: self.status.set("打开失败。"))

        threading.Thread(target=worker, daemon=True).start()

    def probe(self, path: Path) -> dict:
        args = [
            self.ffprobe or "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return json.loads(completed.stdout)

    def generate_thumbnails(self, path: Path) -> None:
        self.root.after(0, lambda: self.status.set("正在生成时间轴缩略图..."))
        if self.thumb_dir and self.thumb_dir.exists():
            shutil.rmtree(self.thumb_dir, ignore_errors=True)
        self.thumb_dir = Path(tempfile.mkdtemp(prefix="roughcut_thumbs_"))
        thumb_count = self.thumbnail_count()
        positions = self.thumbnail_positions(thumb_count)
        completed = 0

        with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as executor:
            futures = [
                executor.submit(self.extract_thumbnail, path, seconds, self.thumb_dir / f"thumb_{i:04d}.png")
                for i, seconds in enumerate(positions, start=1)
            ]
            for future in as_completed(futures):
                future.result()
                completed += 1
                if completed == 1 or completed % 5 == 0 or completed == thumb_count:
                    self.root.after(
                        0,
                        lambda done=completed, total=thumb_count: self.status.set(
                            f"正在生成时间轴缩略图... {done}/{total}"
                        ),
                    )

        self.thumb_paths = sorted(self.thumb_dir.glob("thumb_*.png"))
        self.root.after(0, self.load_thumb_images)

    def thumbnail_count(self) -> int:
        if self.duration <= 0:
            return MIN_THUMBS
        if self.duration <= 600:
            return max(MIN_THUMBS, min(MAX_THUMBS, int(self.duration / 5) + 1))
        if self.duration <= 3600:
            return max(MIN_THUMBS, min(72, int(self.duration / 30) + 1))
        return max(MIN_THUMBS, min(72, int(self.duration / 60) + 1))

    def thumbnail_positions(self, count: int) -> list[float]:
        if count <= 1 or self.duration <= 0:
            return [0.0]
        end = max(0.0, self.duration - 0.5)
        return [round(end * i / (count - 1), 2) for i in range(count)]

    def extract_thumbnail(self, path: Path, seconds: float, output: Path) -> None:
        vf = (
            f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
            f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2"
        )
        args = [
            self.ffmpeg or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seconds:.2f}",
            "-i",
            str(path),
            "-an",
            "-frames:v",
            "1",
            "-vf",
            vf,
            str(output),
        ]
        result = run_process(args)
        if result.returncode != 0 or not output.exists():
            raise RuntimeError(f"缩略图生成失败: {fmt_time(seconds)}")

    def load_thumb_images(self) -> None:
        self.thumb_images = []
        for path in self.thumb_paths:
            try:
                self.thumb_images.append(__import__("tkinter").PhotoImage(file=str(path)))
            except Exception as exc:
                self.log_line(f"缩略图读取失败: {path.name}: {exc}")
        self.status.set("拖拽时间轴选择片段；双击时间轴可用 ffplay 预览附近位置。")
        self.draw_timeline()

    def draw_timeline(self) -> None:
        self.canvas.delete("all")
        if not self.thumb_images:
            self.canvas.create_text(20, 70, anchor="w", fill="#d0d7de", text="打开视频后这里会显示缩略图时间轴。")
            return

        self.timeline_width = max(THUMB_W * len(self.thumb_images), 1)
        self.canvas.configure(scrollregion=(0, 0, self.timeline_width, 250))
        for i, image in enumerate(self.thumb_images):
            x = i * THUMB_W
            self.canvas.create_image(x, 0, anchor="nw", image=image)
            time_text = fmt_time(self.duration * i / max(1, len(self.thumb_images)))
            self.canvas.create_text(x + 4, THUMB_H + 14, anchor="w", fill="#d0d7de", text=time_text)

        selected = self.current_selection_index()
        for i, segment in enumerate(self.segments):
            x1 = self.time_to_x(segment.start)
            x2 = self.time_to_x(segment.end)
            color = "#ffcc00" if i == selected else "#2da44e"
            self.canvas.create_rectangle(x1, 0, x2, THUMB_H, outline=color, width=3)
            if i == selected:
                self.canvas.create_rectangle(x1 - EDGE_HIT_PX, 0, x1 + EDGE_HIT_PX, THUMB_H, outline="#ffffff", width=1)
                self.canvas.create_rectangle(x2 - EDGE_HIT_PX, 0, x2 + EDGE_HIT_PX, THUMB_H, outline="#ffffff", width=1)
            self.canvas.create_rectangle(x1, THUMB_H + 30, x2, THUMB_H + 52, outline=color, fill=color, stipple="gray50")
            self.canvas.create_text(x1 + 4, THUMB_H + 41, anchor="w", fill="#ffffff", text=str(i + 1))

        playhead_x = self.time_to_x(self.playhead_time)
        self.canvas.create_line(playhead_x, 0, playhead_x, THUMB_H + 60, fill="#ff4d4f", width=2)
        self.canvas.create_text(playhead_x + 4, 12, anchor="w", fill="#ffb3b3", text=fmt_time(self.playhead_time))

    def time_to_x(self, seconds: float) -> float:
        if self.duration <= 0:
            return 0.0
        return max(0.0, min(self.timeline_width, seconds / self.duration * self.timeline_width))

    def x_to_time(self, x: float) -> float:
        if self.timeline_width <= 0:
            return 0.0
        return max(0.0, min(self.duration, x / self.timeline_width * self.duration))

    def seek_to(self, seconds: float, render: bool = True) -> None:
        self.playhead_time = max(0.0, min(self.duration, seconds))
        self.preview_time_var.set(f"{fmt_time(self.playhead_time)} / {fmt_time(self.duration)}")
        self.draw_timeline()
        if render:
            self.render_preview_async(self.playhead_time)

    def seek_relative(self, delta: float) -> None:
        self.seek_to(self.playhead_time + delta)

    def toggle_play(self) -> None:
        if not self.input_path:
            return
        self.playing = not self.playing
        self.status.set("播放中..." if self.playing else "已暂停。")
        if self.playing:
            self.schedule_play_tick()
        elif self.play_job is not None:
            self.root.after_cancel(self.play_job)
            self.play_job = None

    def schedule_play_tick(self) -> None:
        if not self.playing:
            return
        self.play_job = self.root.after(650, self.play_tick)

    def play_tick(self) -> None:
        self.play_job = None
        if not self.playing:
            return
        if self.playhead_time >= self.duration:
            self.playing = False
            self.status.set("已到视频结尾。")
            return
        self.seek_to(self.playhead_time + 0.65)
        self.schedule_play_tick()

    def render_preview_async(self, seconds: float) -> None:
        if not self.input_path or not self.ffmpeg:
            return
        self.preview_request_id += 1
        request_id = self.preview_request_id
        if self.preview_dir is None or not self.preview_dir.exists():
            self.preview_dir = Path(tempfile.mkdtemp(prefix="roughcut_preview_"))
        output = self.preview_dir / f"preview_{request_id}.png"

        def worker() -> None:
            vf = (
                f"scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
                f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2"
            )
            args = [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{seconds:.2f}",
                "-i",
                str(self.input_path),
                "-an",
                "-frames:v",
                "1",
                "-vf",
                vf,
                str(output),
            ]
            result = run_process(args)
            if result.returncode == 0 and output.exists():
                self.root.after(0, lambda: self.show_preview_image(output, request_id))

        threading.Thread(target=worker, daemon=True).start()

    def show_preview_image(self, path: Path, request_id: int) -> None:
        if request_id != self.preview_request_id:
            return
        try:
            self.preview_image = PhotoImage(file=str(path))
        except Exception as exc:
            self.log_line(f"预览帧读取失败: {exc}")
            return
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(0, 0, anchor="nw", image=self.preview_image)

    def hit_segment_edge(self, x: float) -> tuple[int, str] | None:
        for i, segment in enumerate(self.segments):
            start_x = self.time_to_x(segment.start)
            end_x = self.time_to_x(segment.end)
            if abs(x - start_x) <= EDGE_HIT_PX:
                return i, "start"
            if abs(x - end_x) <= EDGE_HIT_PX:
                return i, "end"
        return None

    def on_canvas_motion(self, event) -> None:
        if not self.input_path or self.drag_start_x is not None:
            return
        x = self.canvas.canvasx(event.x)
        hit = self.hit_segment_edge(x)
        self.canvas.configure(cursor="sb_h_double_arrow" if hit else "")

    def on_drag_start(self, event) -> None:
        if not self.input_path:
            return
        x = self.canvas.canvasx(event.x)
        hit = self.hit_segment_edge(x)
        self.drag_mode = "resize" if hit else "create"
        self.drag_segment_index = hit[0] if hit else None
        self.drag_edge = hit[1] if hit else None
        if hit:
            self.segment_list.selection_clear(0, END)
            self.segment_list.selection_set(hit[0])
            self.refresh_time_inputs_from_segment(hit[0])
        self.drag_start_x = x
        self.drag_current_view_x = event.x
        if self.drag_rect:
            self.canvas.delete(self.drag_rect)
        if self.drag_mode == "create":
            self.drag_rect = self.canvas.create_rectangle(self.drag_start_x, 0, self.drag_start_x, THUMB_H, outline="#58a6ff", width=2)

    def on_drag_move(self, event) -> None:
        if self.drag_start_x is None:
            return
        self.drag_current_view_x = event.x
        if self.drag_mode == "resize":
            self.update_resize_selection()
        else:
            self.update_drag_selection()
        self.update_autoscroll()

    def on_drag_end(self, event) -> None:
        if self.drag_start_x is None:
            return
        self.stop_autoscroll()
        self.drag_current_view_x = event.x
        x = self.clamped_drag_x()
        start = self.x_to_time(min(self.drag_start_x, x))
        end = self.x_to_time(max(self.drag_start_x, x))
        was_resize = self.drag_mode == "resize"
        resize_index = self.drag_segment_index
        self.drag_start_x = None
        self.drag_current_view_x = None
        self.drag_mode = "create"
        self.drag_segment_index = None
        self.drag_edge = None
        if self.drag_rect:
            self.canvas.delete(self.drag_rect)
            self.drag_rect = None
        if was_resize:
            self.refresh_segment_list()
            if resize_index is not None:
                self.segment_list.selection_set(resize_index)
        elif end - start >= 0.8:
            self.add_segment(Segment(round(start, 2), round(end, 2)))
        else:
            self.seek_to(self.x_to_time(x))
        self.draw_timeline()

    def clamped_drag_x(self) -> float:
        view_x = self.drag_current_view_x
        if view_x is None:
            return self.drag_start_x or 0.0
        return max(0.0, min(self.timeline_width, self.canvas.canvasx(view_x)))

    def update_drag_selection(self) -> None:
        if self.drag_start_x is None or not self.drag_rect:
            return
        x = self.clamped_drag_x()
        self.canvas.coords(self.drag_rect, self.drag_start_x, 0, x, THUMB_H)
        start = self.x_to_time(min(self.drag_start_x, x))
        end = self.x_to_time(max(self.drag_start_x, x))
        self.start_var.set(fmt_time(start))
        self.end_var.set(fmt_time(end))

    def update_resize_selection(self) -> None:
        if self.drag_segment_index is None or self.drag_edge is None:
            return
        x = self.clamped_drag_x()
        t = round(self.x_to_time(x), 2)
        segment = self.segments[self.drag_segment_index]
        min_duration = 0.8
        if self.drag_edge == "start":
            segment.start = max(0.0, min(t, segment.end - min_duration))
        else:
            segment.end = min(self.duration, max(t, segment.start + min_duration))
        self.refresh_time_inputs_from_segment(self.drag_segment_index)
        self.draw_timeline()

    def refresh_time_inputs_from_segment(self, index: int) -> None:
        if index < 0 or index >= len(self.segments):
            return
        segment = self.segments[index]
        self.start_var.set(fmt_time(segment.start))
        self.end_var.set(fmt_time(segment.end))

    def update_autoscroll(self) -> None:
        if self.drag_start_x is None or self.drag_current_view_x is None:
            self.stop_autoscroll()
            return
        width = max(1, self.canvas.winfo_width())
        margin = 44
        if self.drag_current_view_x < margin or self.drag_current_view_x > width - margin:
            if self.autoscroll_job is None:
                self.autoscroll_job = self.root.after(35, self.autoscroll_tick)
        else:
            self.stop_autoscroll()

    def autoscroll_tick(self) -> None:
        self.autoscroll_job = None
        if self.drag_start_x is None or self.drag_current_view_x is None:
            return
        width = max(1, self.canvas.winfo_width())
        margin = 44
        step = 0
        if self.drag_current_view_x < margin:
            step = -2
        elif self.drag_current_view_x > width - margin:
            step = 2
        if step:
            self.canvas.xview_scroll(step, "units")
            self.update_drag_selection()
            self.autoscroll_job = self.root.after(35, self.autoscroll_tick)

    def stop_autoscroll(self) -> None:
        if self.autoscroll_job is not None:
            self.root.after_cancel(self.autoscroll_job)
            self.autoscroll_job = None

    def on_double_click(self, event) -> None:
        if not self.input_path:
            return
        seconds = self.x_to_time(self.canvas.canvasx(event.x))
        self.seek_to(seconds)

    def add_segment_from_inputs(self) -> None:
        try:
            start = parse_duration(self.start_var.get())
            end = parse_duration(self.end_var.get())
        except ValueError as exc:
            messagebox.showerror("时间格式错误", str(exc))
            return
        if end <= start:
            messagebox.showerror("片段无效", "结束时间必须大于开始时间。")
            return
        self.add_segment(Segment(round(start, 2), round(min(end, self.duration), 2)))

    def add_segment(self, segment: Segment) -> None:
        self.segments.append(segment)
        self.refresh_segment_list()
        self.segment_list.selection_clear(0, END)
        self.segment_list.selection_set(len(self.segments) - 1)
        self.draw_timeline()

    def refresh_segment_list(self) -> None:
        self.segment_list.delete(0, END)
        for i, segment in enumerate(self.segments, start=1):
            self.segment_list.insert(
                END,
                f"{i:02d}. {fmt_time(segment.start)} - {fmt_time(segment.end)}    {fmt_time(segment.duration)}",
            )

    def current_selection_index(self) -> int | None:
        selected = self.segment_list.curselection()
        if not selected:
            return None
        return int(selected[0])

    def on_segment_select(self, _event=None) -> None:
        index = self.current_selection_index()
        if index is not None:
            self.refresh_time_inputs_from_segment(index)
        self.draw_timeline()

    def delete_selected_segment(self) -> None:
        index = self.current_selection_index()
        if index is None:
            return
        del self.segments[index]
        self.refresh_segment_list()
        self.draw_timeline()

    def move_selected(self, offset: int) -> None:
        index = self.current_selection_index()
        if index is None:
            return
        new_index = index + offset
        if new_index < 0 or new_index >= len(self.segments):
            return
        self.segments[index], self.segments[new_index] = self.segments[new_index], self.segments[index]
        self.refresh_segment_list()
        self.segment_list.selection_set(new_index)
        self.draw_timeline()

    def preview_selected(self) -> None:
        if not self.input_path:
            return
        index = self.current_selection_index()
        if index is None:
            self.seek_to(0)
        else:
            self.seek_to(self.segments[index].start)

    def play_at(self, seconds: float) -> None:
        self.seek_to(seconds)

    def choose_output(self) -> None:
        if not self.input_path:
            return
        path = filedialog.asksaveasfilename(
            title="选择输出文件",
            defaultextension=".mp4",
            initialfile=self.default_output_path(self.input_path, ".mp4").name,
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("所有文件", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def default_output_path(self, input_path: Path, suffix: str) -> Path:
        base = input_path.with_name(input_path.stem + "_cut" + suffix)
        if not base.exists():
            return base
        for i in range(1, 1000):
            candidate = input_path.with_name(f"{input_path.stem}_cut_{i}{suffix}")
            if not candidate.exists():
                return candidate
        return base

    def export(self) -> None:
        if not self.input_path:
            messagebox.showinfo("未打开视频", "请先打开一个视频。")
            return
        if not self.segments:
            messagebox.showinfo("没有片段", "请先在时间轴上拖拽选择片段。")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            output = self.default_output_path(self.input_path, ".mp4")
        else:
            output = Path(output_text)
        output.parent.mkdir(parents=True, exist_ok=True)

        self.export_cancel.clear()
        self.progress.configure(value=0)
        self.export_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("正在无重编码导出...")
        self.log_line("====== 开始导出 ======")

        def worker() -> None:
            try:
                final_path = self.export_segments(output)
                self.root.after(0, lambda: self.progress.configure(value=100))
                self.root.after(0, lambda: self.status.set(f"导出完成: {final_path}"))
                self.root.after(0, lambda: messagebox.showinfo("导出完成", str(final_path)))
            except Exception as exc:
                message = str(exc)
                if self.export_cancel.is_set():
                    self.root.after(0, lambda: self.status.set("导出已取消。"))
                else:
                    self.root.after(0, lambda: self.status.set("导出失败。"))
                    self.root.after(0, lambda: messagebox.showerror("导出失败", message))
            finally:
                self.root.after(0, lambda: self.export_button.configure(state="normal"))
                self.root.after(0, lambda: self.cancel_button.configure(state="disabled"))
                if self.export_cancel.is_set():
                    self.root.after(0, lambda: self.progress.configure(value=0))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_export(self) -> None:
        self.export_cancel.set()
        self.status.set("正在取消导出...")
        process = self.export_process
        if process and process.poll() is None:
            process.terminate()

    def export_segments(self, output: Path) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="roughcut_export_"))
        try:
            list_path = temp_root / "concat.txt"
            input_path = (self.input_path or Path()).as_posix().replace("'", "'\\''")
            with list_path.open("w", encoding="utf-8") as f:
                for i, segment in enumerate(self.segments, start=1):
                    self.log_line(f"[{i}/{len(self.segments)}] 计划片段 {fmt_time(segment.start)} - {fmt_time(segment.end)}")
                    f.write(f"file '{input_path}'\n")
                    f.write(f"inpoint {segment.start:.2f}\n")
                    f.write(f"outpoint {segment.end:.2f}\n")

            preferred = output
            if preferred.suffix.lower() not in {".mp4", ".mkv"}:
                preferred = preferred.with_suffix(".mp4")

            self.log_line("正在单次无重编码导出...")
            result = self.concat_to(list_path, preferred)
            if self.export_cancel.is_set():
                raise RuntimeError("导出已取消。")
            if result.returncode == 0 and preferred.exists():
                return preferred

            fallback = preferred.with_suffix(".mkv")
            if fallback.exists():
                fallback.unlink()
            self.log_line("MP4 输出失败，自动改用 MKV 容器重试...")
            result = self.concat_to(list_path, fallback)
            if self.export_cancel.is_set():
                raise RuntimeError("导出已取消。")
            if result.returncode != 0 or not fallback.exists():
                raise RuntimeError("拼接失败。请查看日志中的 FFmpeg 输出。")
            return fallback
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def concat_to(self, list_path: Path, output: Path) -> subprocess.CompletedProcess:
        total_duration = sum(segment.duration for segment in self.segments)
        args = [
            self.ffmpeg or "ffmpeg",
            "-hide_banner",
            "-y",
            "-nostats",
            "-progress",
            "pipe:1",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-sn",
            "-dn",
            "-map_chapters",
            "-1",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
        ]
        if output.suffix.lower() == ".mp4":
            args += ["-movflags", "+faststart"]
        args.append(str(output))
        return self.run_export_process(args, total_duration)

    def run_export_process(self, args: list[str], total_duration: float) -> subprocess.CompletedProcess:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
        )
        self.export_process = process
        output_lines: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                output_lines.append(line)
                if self.export_cancel.is_set():
                    process.terminate()
                    break
                progress = self.parse_ffmpeg_progress(line, total_duration)
                if progress is not None:
                    self.root.after(0, lambda value=progress: self.progress.configure(value=value))
                    self.root.after(0, lambda value=progress: self.status.set(f"正在无重编码导出... {value:.1f}%"))
                elif line and not line.startswith(("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=", "drop_frames=", "speed=", "progress=")):
                    self.log_line(line)
            code = process.wait()
        finally:
            self.export_process = None
        return subprocess.CompletedProcess(args, code, "\n".join(output_lines), "")

    def parse_ffmpeg_progress(self, line: str, total_duration: float) -> float | None:
        if total_duration <= 0:
            return None
        if line.startswith("out_time_ms="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return None
            return max(0.0, min(100.0, seconds / total_duration * 100))
        match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
        if not match:
            return None
        hours, minutes, seconds_text = match.groups()
        seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds_text)
        return max(0.0, min(100.0, seconds / total_duration * 100))


def main() -> None:
    root = TkinterDnD.Tk() if TkinterDnD is not None else Tk()
    app = RoughCutApp(root)
    if len(sys.argv) > 1:
        first = Path(sys.argv[1])
        root.after(100, lambda: app.load_video(first))
    root.mainloop()


if __name__ == "__main__":
    main()
