# 第三方组件

## NumPy 1.26.4（0.5 新增）

用于本地音频频谱和声音片段匹配。Windows 包已包含运行所需库和许可，源码运行需要安装 requirements.txt。项目与文档：https://numpy.org/doc/1.26/；BSD 及所含依赖许可见 vendor/NumPy-LICENSE.txt 和 Windows/licenses/NumPy-LICENSE.txt。没有修改 NumPy 的代码或二进制。

声音分析使用用户机器上的 FFmpeg，未随包分发 FFmpeg：文档 https://ffmpeg.org/ffmpeg.html。

## DanmakuFactory 1.70 CLI

- 作者：hihkm
- 项目：https://github.com/hihkm/DanmakuFactory
- 发行包：https://github.com/hihkm/DanmakuFactory/releases/tag/v1.70
- 下载文件：DanmakuFactory1.70_Release_CLI.zip
- 工具内文件：vendor/DanmakuFactory.exe（官方 DanmakuFactory_REL1.70CLI.exe 的重命名副本）
- 许可：MIT，完整文本见 vendor/DanmakuFactory-LICENSE.txt。
- 调用方式：独立子进程，仅传入弹幕文件和转换参数。

## 桌面运行时

Windows 便携包使用 PyInstaller 打包 Python 3.9.7 和 Tcl/Tk 8.6。运行时许可随 Windows 包提供在 licenses 文件夹中。PyInstaller 仅作为构建工具，采用允许打包应用分发的许可例外；项目：https://github.com/pyinstaller/pyinstaller。

Bili23、KikoPlay、DanmakuPlayer 等仅用于调研，没有向本项目复制其实现。

## libmpv 原生播放引擎（新增）

使用 mpv-player 官方 CI 的 x86_64 LGPL 构建，版本 v0.41.0-dev-gb2c255c13，运行号 37956197985；本工具没有修改其二进制。该版本为开发构建，已在本机电影核对场景测试。

- 项目和源码：https://github.com/mpv-player/mpv/tree/b2c255c13
- 原始下载：https://github.com/mpv-player/mpv/releases/download/git-release/libmpv-v0.41.0-dev-gb2c255c13-37956197985-x86_64-w64-mingw32-lgpl.zip
- 官方构建脚本：https://github.com/mpv-player/mpv/blob/b2c255c13/ci/build-mingw64-full.sh
- 官方构建流程：https://github.com/mpv-player/mpv/blob/b2c255c13/.github/workflows/build.yml
- 依赖构建项目、源码下载和许可：https://github.com/BtbN/FFmpeg-Builds
- mpv/FFmpeg LGPLv3 许可：vendor/mpv-LICENSE.LGPL；LGPL 使用的 GPLv3 基础文本：vendor/mpv-COPYING.GPLv3。
- DLL SHA256：5C595CC00F839B2F2FBA395D80614D80AB938EC03A47F96AD39C18DD0E26355C。

动态链接通过 player.py 的 ctypes 接口，原始接口头文件见 vendor/mpv-headers。允许为调试此库修改而进行逆向工程，并可替换 vendor/libmpv-2.dll（源码运行）或 Windows/_internal/vendor/libmpv-2.dll（便携包）；不会进行签名或版本锁定。

## 固定版本的公开归档

官方 git-release 资产会轮换，旧 DLL 归档目前不再列于最新资产中。本项目 Release 提供 `libmpv-lgpl-pinned.zip`，是原官方归档的未修改副本，含 DLL、接口头文件和原许可。归档 SHA256：`f310c39054cdef39715f6debeeb04ad4f3acdee0cee72dec67564ec6efd4ea88`，DLL 校验值见上文。

DanmakuFactory.exe SHA256：`d30a05972794b28f2846e5f5e5ff0f7437324a62feabd1ef93d5161b94d9ba44`。它是第三方 MIT 工具，不属于霜星的原创代码或图标。
