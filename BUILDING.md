# 从源码运行和构建

作者：霜星。Windows 64 位，Python 3.9–3.11；本地便携包使用 Python 3.9.7。

```powershell
python -m pip install -r requirements.txt
python scripts/fetch_dependencies.py
python app.py
```

`DanmakuFactory.exe` 是随源码保留的官方 MIT 工具。`libmpv-2.dll` 不进入 Git 历史，下载脚本从本项目 Release 取得未修改的官方 LGPL 原始归档并校验 SHA256。

```powershell
python -m unittest discover -v
python -m pip install PyInstaller==6.22.3
python scripts/build_windows.py
```

输出为 `dist/BiliDanmakuDesk/`，须将整个文件夹一起分发。构建脚本嵌入原创 ICO、运行时 PNG 和第三方许可。

从链接进行声音匹配另需 FFmpeg 与 ffprobe 可执行程序位于 PATH；它们不随包分发。手工核对、删减标记和 ASS 导出不依赖 FFmpeg。

`assets/icon.svg` 是原创矢量图标，PNG 与 ICO 为对应导出件。不能只替换 EXE 的图标而遗漏 Tk 窗口图标。

自动测试覆盖转换与映射，不等于全片播放或全格式验证；具体实机记录见 [VALIDATION.md](VALIDATION.md)。
