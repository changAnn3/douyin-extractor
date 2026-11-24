# 抖音视频 / 图文文案提取工具

一个本地运行的网页工具：粘贴抖音分享链接或分享口令，自动下载视频或图文作品，并提取四类文案：

- **视频标题**
- **摘要文案**（MiMo 文本模型生成）
- **口播文案**（MiMo ASR 转写）
- **画面文字**（本地 RapidOCR 识别视频抽帧 / 图文图片）

界面基于 Gradio，所有文件保存在本地 `output/`，不上传原始媒体。

> 仅用于个人学习、研究与内容备份。请遵守抖音用户协议与著作权规定，不要将下载内容用于二次传播或商业用途。

---

## 功能特性

- **链接解析**：支持分享口令、`v.douyin.com` 短链、`/video/<id>`、`/note/<id>`、带 `modal_id` 的主页/搜索页链接。
- **视频下载**：优先 yt-dlp；抖音 web detail 接口被风控拦截时，自动切换到分享页 SSR 数据路径，获取无水印播放地址。
- **图文支持**：识别图文作品的 `images`，下载全部原图，逐张 OCR。
- **口播转写**：音频按 **180 秒**分块调用 MiMo ASR，块失败自动二分重试；避免超长单次音频只返回部分内容。
- **画面文字**：ffmpeg 抽帧（最多 120 帧）+ 本地 RapidOCR，按时间轴去重合并；顶部水印噪声过滤。
- **摘要生成**：综合标题、作者文案、口播转写、画面文字，生成 100–160 字、总—分结构摘要；不足 100 字自动重生成一次。
- **长视频保障**：流式下载、网络重试、OCR 帧数固定上限、ASR 自动分块，时长增长不会导致整体失败。
- **容错降级**：无音轨、OCR 失败、摘要失败只记录提示，不中断整体流程；未配置 API Key 时仍完成下载与 OCR。
- **历史记录**：界面扫描 `output/` 下所有结果，下拉选择任意历史作品并加载查看。
- **本地存储**：`result.json` + `result.md`，保留视频/图片/音频。

## 目录结构

```
.
├─ app.py            # Gradio 网页入口
├─ pipeline.py       # 流程编排（链接→下载→音频→ASR→OCR→摘要→保存）
├─ config.py         # .env 解析、配置单例、代理清理
├─ extractor.py      # 链接/作品 ID 解析、短链跳转
├─ downloader.py     # yt-dlp + 分享页 SSR 备用下载、图文图片下载
├─ audio.py          # 时长探测、音轨抽取、静音点切块
├─ asr.py            # MiMo ASR 请求与重试
├─ ocr.py            # 抽帧、RapidOCR、时间轴合并
├─ summarizer.py     # MiMo 文本模型摘要
├─ storage.py        # result.json / result.md 写入
├─ requirements.txt
├─ .env.example      # 复制为 .env 后填写配置
└─ output/           # 运行时生成（已在 .gitignore 中）
```

## 环境要求

- Python 3.9+
- [ffmpeg / ffprobe](https://ffmpeg.org/)（需可执行，加入 PATH 或在 `.env` 写完整路径）
- 可访问抖音与 MiMo API 的网络
- MiMo 云端 API Key（用于 ASR 与摘要）

开发与验证环境：Windows 11 + Python 3.9.6；Linux/macOS 安装 ffmpeg 后同样适用（未做完整跨平台验证）。

## 安装

```bash
git clone <your-repo-url>
cd <repo>

python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
```

RapidOCR 首次运行会加载本地模型，CPU 即可，无需 GPU。

## 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

| 变量 | 说明 |
|---|---|
| `MIMO_API_KEY` | 必填，MiMo 云端 API Key |
| `MIMO_BASE_URL` | 默认 `https://api.xiaomimimo.com/v1`，按实际服务地址填写 |
| `MIMO_ASR_MODEL` | 默认 `mimo-v2.5-asr` |
| `MIMO_TEXT_MODEL` | 默认 `mimo-v2.6-flash` |
| `FFMPEG_BIN` / `FFPROBE_BIN` | 默认 `ffmpeg` / `ffprobe`（走 PATH），也可填绝对路径 |
| `OUTPUT_DIR` | 结果输出目录，默认 `output/` |
| `ASR_CHUNK_SECONDS` | ASR 分块秒数，默认 `180`，不建议调大 |
| `ASR_MAX_BASE64_BYTES` | 单块 base64 上限，默认 10MB |
| `OCR_MAX_FRAMES` | 抽帧上限，默认 `120` |
| `OCR_MIN_SCORE` | OCR 置信度阈值，默认 `0.5` |
| `COOKIES_FILE` | **推荐**，浏览器扩展导出的 `cookies.txt` 路径 |
| `COOKIES_FROM_BROWSER` | `chrome` / `edge`，需先关闭浏览器；新版浏览器 App-Bound 加密可能导致读取失败 |
| `MIMO_PROXY` | 访问 API 时使用的代理，留空直连 |
| `IMPERSONATE` | 可选，yt-dlp 浏览器指纹（需 `curl_cffi`，未安装会自动忽略） |

> 启动时会自动清理系统代理环境变量并设置 `NO_PROXY`，避免 Windows 注册表代理影响下载与本地 Web 服务。

## 获取抖音 Cookies

抖音接口有风控，部分视频需要浏览器 cookie（登录态或设备指纹）。推荐使用谷歌浏览器扩展Get cookies.txt LOCALLY导出的 `cookies.txt`（Netscape 格式）：

1. 在 Chrome / Edge 安装扩展 **Get cookies.txt LOCALLY**。
2. 登录抖音并打开任意视频页面。
3. 点击扩展 → Export（导出当前站点），得到 `cookies.txt`。
4. 把文件保存到项目目录或任意位置，在 `.env` 中配置：
   ```env
   COOKIES_FILE=E:/你的路径/cookies.txt
   ```

**注意**

- `cookies.txt` 包含登录凭据，**不要提交到 Git、不要分享**（已加入 `.gitignore`）。
- 不建议用“复制浏览器 profile / 注册表”等非常规方式读取 cookie：新版 Chromium 的 App-Bound 加密会导致 cookie 被清空。
- Cookie 过期或风控加剧时，重新导出即可。

## 运行

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:7860`（默认仅监听本机）。

界面说明：

- 左侧：链接输入、识别语种（auto/zh/en）、开始提取、加载最近结果、刷新历史列表。
- 右侧：标题、摘要、口播、画面文字；折叠区含视频/图片、音频、`result.json`、`result.md`。
- 历史记录下拉：选择后加载对应作品的结果。
- 页面加载/刷新会自动同步历史列表并显示最新结果。

### 作为库调用

```python
import config, pipeline

settings = config.get_settings()
result = pipeline.run("https://v.douyin.com/xxxx/", "auto", settings)
print(result.title, result.summary, len(result.transcript))
```

## 输出结构

```
output/<作品ID>/
├─ video.mp4            # 视频作品
├─ images/              # 图文作品原图
├─ audio.mp3            # 抽取的音频（视频作品）
├─ video.info.json      # 原始元数据
├─ result.json          # 结构化结果（摘要/口播/画面文字/分段/警告）
└─ result.md            # 可直接阅读的 Markdown 结果
```

`result.json` 主要字段：`aweme_id`、`media_type`（video/images）、`images`、`title`、`caption`、`author`、`duration`、`summary`、`transcript`、`transcript_segments`、`ocr_text`、`ocr_lines`、`models`、`warnings`。

## 长视频说明

- 下载为流式写入，网络异常自动重试。
- ASR 固定按 180 秒分块（每块独立请求）；单块失败会二分重试并保留其余内容。
- OCR 始终最多 120 帧：视频越长，抽帧间隔越大，耗时有上限。
- 摘要输入会截断（口播前 6000 字 + 画面前 4000 字），保证长视频也能得到摘要。

## 常见问题

- **下载提示 “Fresh cookies”（403）**：cookie 失效或风控，重新导出 `cookies.txt`；或稍后重试。
- **页面出现 Error / 结果不更新**：长任务期间浏览器连接可能中断。保持页面在前台；结束后点击“加载最近结果”。
- **口播文案为空**：作品无音轨（图文作品属正常），或 API Key 未配置 / 转写失败（查看“提示”）。
- **摘要偏短或为空**：文本模型为推理模型，会消耗较多 token；可在 `.env` 中更换模型，或增大 `summarizer.py` 中的 `max_tokens`。
- **找不到 ffmpeg**：安装 ffmpeg 并在 `.env` 写绝对路径。
- **代理问题**：工具默认直连；如需代理，使用 `MIMO_PROXY`，或为下载单独配置网络环境。

## 隐私与免责声明

- 原始视频/图片/音频仅保存在本地，不会上传到除 MiMo API 之外的第三方。
- 进行口播转写与摘要时，**音频与相关文本会发送到 MiMo API**，请自行评估隐私影响。
- 本工具不绕过 DRM 或付费内容，仅使用抖音网页自身可获取的媒体地址。
- 使用者需自行确保下载与使用行为符合抖音用户协议、著作权法及当地法律。作者不对使用者的行为承担责任。
- 请勿将本工具用于批量抓取、侵权传播或其他违法用途。

## 参与贡献

欢迎提交 Issue / Pull Request。提交前请确保：

1. 不包含任何 cookie、API Key、`.env` 或 `output/` 内容；
2. 不引入会读取/修改浏览器 cookie 数据库的破坏性实现；
3. 说明测试环境与复现步骤。

## License

[MIT](LICENSE)
