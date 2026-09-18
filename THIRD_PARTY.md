# 开源项目与许可证说明

本工具没有复制 LDDC 的 GPL-3.0 源码。以下项目分别作为直接依赖、协议/格式依据或设计参考。

## 直接依赖

- [PySide6](https://code.qt.io/cgit/pyside/pyside-setup.git/)：LGPL-3.0/GPL/商业许可。用于 Qt Widgets 图形界面。
- [Mutagen](https://github.com/quodlibet/mutagen)：GPL-2.0-or-later。用于读取和写入音频标签。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：MIT。用于本地多语言音频识别和词级时间戳。
- [CTranslate2](https://github.com/OpenNMT/CTranslate2)：MIT。faster-whisper 推理引擎。
- [Requests](https://github.com/psf/requests)：Apache-2.0。用于联网歌词查询。
- [OpenCC Python Reimplemented](https://github.com/yichen0831/opencc-python)：Apache-2.0。只在比较阶段将繁简中文归一化，不改写用户歌词。
- [pypinyin](https://github.com/mozillazg/python-pinyin)：MIT。用于将中文识别结果与歌词转换为拼音后进行同音字模糊比较；不会改写歌词文本。
- [NVIDIA CUDA Runtime 12.4、cuBLAS 12.4 与 cuDNN 9.1](https://developer.nvidia.com/cuda-toolkit)：NVIDIA 软件许可。Windows GPU 复核运行库，随同级数据文件夹分发；各包的完整许可证保存在 `GPU运行库\许可证`。

## 服务与协议实现

- [LRCLIB](https://github.com/tranxuanthang/lrclib)：MIT。使用公开 API 查询同步歌词。
- 网易云音乐、QQ 音乐和酷狗适配器使用其公开网页客户端所调用的接口。这些并非稳定的官方开发者 API，可能失效，可在设置中关闭；工具不将它们作为唯一来源。

## 设计参考（未复制源码）

- [LRCGET](https://github.com/tranxuanthang/lrcget)，MIT：参考本地曲库扫描、LRCLIB 查询、同名 LRC 工作流。
- [LDDC](https://github.com/chenmozhijin/LDDC)，GPL-3.0：参考中文多来源搜索、同步歌词格式与 MP3/FLAC/M4A 歌词标签兼容策略。
- [stable-ts](https://github.com/jianfch/stable-ts)，MIT：参考已知文本与 Whisper 时间锚点比对、时间戳细化思路。该仓库已归档，因此不是运行依赖。
- [ctc-forced-aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner)，BSD-2-Clause：参考已知文本强制对齐和置信度门槛设计；默认模型为 CC BY-NC 4.0，本工具未捆绑该模型。
- [WhisperX](https://github.com/m-bain/whisperX)，BSD-2-Clause：参考词级时间戳和对齐质量指标；因运行时过重，未作为默认依赖。
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)：调研了其粤语、歌声及伴奏歌曲识别能力，以及 Qwen3-ForcedAligner 的粤语文本对齐；强制对齐器公开标注的主要任务是语音，尚未验证伴奏歌曲表现，因此未捆绑或声称支持。
- [syncedlyrics](https://github.com/moehmeni/syncedlyrics)，MIT：参考可替换歌词来源适配器设计。
- [ShazamIO](https://github.com/shazamio/ShazamIO)，MIT，以及 [pyacoustid](https://github.com/beetbox/pyacoustid)，MIT：调研了音频指纹身份识别；指纹不能验证歌词时间轴，因此未将其用于校时。

各项目名称及商标归其权利人所有。联网歌词的版权归歌词作者和相应权利人所有，本工具仅面向用户个人本地音乐管理。
