# RoughCut / 粗剪拼接

一个 Windows 桌面小工具，用来从单个长视频里粗略选择多个片段，并按片段顺序无重编码拼接导出。

## 功能

- 无重编码剪切/拼接，基于 FFmpeg `-c copy`
- 时间轴缩略图
- 内嵌低帧率预览和红色播放头
- 拖拽创建多个片段
- 拖动片段左右边缘微调开始/结束
- 片段上移、下移、删除
- 导出进度显示和取消导出
- 默认输出 `原文件名_cut.mp4`，MP4 失败时自动尝试 MKV

说明：无重编码剪切受关键帧限制，适合秒级粗剪，不适合帧级精准剪辑。

## 运行

需要 Python 3.10+ 和 FFmpeg。

程序会优先查找：

```text
bin/ffmpeg.exe
bin/ffprobe.exe
bin/ffplay.exe
```

如果没有，再查找系统 PATH 里的 FFmpeg。

启动：

```powershell
python roughcut.py
```

也可以双击：

```text
run.bat
```

## 打包

可用 PyInstaller 打包成便携目录：

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --windowed --onedir --name RoughCutEditor `
  --add-binary "bin\ffmpeg.exe;bin" `
  --add-binary "bin\ffprobe.exe;bin" `
  --add-binary "bin\ffplay.exe;bin" `
  roughcut.py
```

仓库不包含 FFmpeg 二进制文件，因为体积很大。打包前请自行把 FFmpeg 放入 `bin/`。

## 作者

fzlong
