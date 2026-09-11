# douyin_transcript —— 本地提取抖音文字稿

输入抖音链接，输出视频口播的文字稿。全程本地处理：优先抓取平台自带的自动字幕（最快最准），拿不到字幕时下载音频后用 faster-whisper 在本机转写，不使用任何外部 ASR 服务，音频不离开本机。

## 两种使用方式

### 分发版（给他人用，开箱即用）

运行 `python build_windows.py` 生成 `dist/douyin_transcript/抖音文字稿工具-win64.zip`（约 550 MB，内含 exe、运行库与 small 模型）。接收方解压后无需安装 Python：

1. 复制抖音分享链接
2. 双击 `douyin_transcript.exe`（自动读剪贴板）
3. 转写完成自动用记事本打开文字稿，文件在 `output\` 目录

### 源码版（本机开发）

```powershell
# 基本用法（支持短链 v.douyin.com/xxx 和含链接的整段分享文本；不传链接则读剪贴板）
python tools/douyin_transcript/douyin_transcript.py "https://v.douyin.com/xxxxxx/"

# 同时输出带时间轴的 SRT，并在 TXT 顶部附加标题/作者/链接
python tools/douyin_transcript/douyin_transcript.py "<链接>" --srt --with-meta
```

文字稿 TXT 默认输出到 `tools/douyin_transcript/output/`，媒体文件缓存在 `tools/douyin_transcript/cache/`（同一视频重复提取时直接复用；`--no-cache` 可强制重新下载）。

## 选项

| 选项 | 说明 |
| --- | --- |
| （不带链接运行） | 自动读取剪贴板中的链接 |
| `-o, --output` | 输出目录 |
| `--srt` | 同时输出带时间轴的 SRT 文件 |
| `--with-meta` | TXT 顶部附加标题、作者、视频链接、提取时间 |
| `--model` | tiny / base / small（默认）/ medium / large-v3，或本地模型目录 |
| `--language` | 口播语言，默认 zh，`auto` 为自动检测 |
| `--force-asr` | 跳过平台字幕，强制本地转写 |
| `--no-cache` | 忽略已缓存的媒体文件 |
| `--no-open` | 完成后不自动打开文字稿 |
| `--notion` | 把文字稿推送为 Notion 笔记页面（见下节配置） |
| `--classify` | 转写完成后自动按内容领域分类归档（见下节「内容领域自动分类」） |
| `--segment` | 转写完成后调用 MiniMax 语义分段（防改写校验，见下节「语义分段」） |
| `--notion-token` / `--notion-parent` | 临时指定 Notion 集成密钥与父页面，覆盖配置 |
| `--env-proxy` | 使用系统代理环境变量（默认强制直连，避免本机失效代理干扰） |

## 内容领域自动分类（classify_transcript.py）

接入中转站 MiniMax（复用 `.env.local` 的 `MINIMAX_SEARCH_API_KEY` / `MINIMAX_SEARCH_API_BASE_URL`，模型默认 `MiniMax-M3`），用**固定提示词**（`classify_config.json`，版本化、可审计）把文字稿判定到唯一的内容领域，然后自动归档：

```powershell
# 对 output 根目录下所有未归档文稿分类（dry-run 只看结果不动文件）
python tools/douyin_transcript/classify_transcript.py --dry-run
python tools/douyin_transcript/classify_transcript.py

# 新视频一步到位：转写 + 分类归档
python tools/douyin_transcript/douyin_transcript.py "<链接>" --classify
```

- 归档结果：文稿与同名 SRT 移入 `output\<类别>\` 子目录，分类记录追加到 `output\classifications.jsonl`
- 固定类别（`classify_config.json`）：AI与技术 / 财经商业 / 自媒体与个人成长 / 教育与职场 / 社会时事 / 生活娱乐 / 其他；修改类别或提示词只需改该配置文件
- token 消耗：每篇约取前 2500 字送检（约 1700 token），只在分类时产生，转写本身仍为零成本

## 语义分段（segment_transcript.py）

Whisper 输出缺少自然分段，本工具用中转站 MiniMax 按话题重新分段并可加 `## ` 小标题，**带防改写完整性校验**：模型输出剥掉标题与空白后必须与原文逐字相等，否则自动重试（最多 `maxAttempts` 次），全部失败则保留原文件——原文一字不动是有程序保证的。

```powershell
# 新视频一步到位：转写 + 分段（可与 --classify、--notion 组合）
python tools/douyin_transcript/douyin_transcript.py "<链接>" --segment

# 存量文稿批量补分段（递归 output 下全部类别文件夹，已分段的自动跳过）
python tools/douyin_transcript/segment_transcript.py
python tools/douyin_transcript/segment_transcript.py --dry-run   # 只看结果不写回
python tools/douyin_transcript/segment_transcript.py --file "<单篇路径>"
```

- 固定提示词在 `segment_config.json`（版本化）；处理记录在 `output/segment_state.json`
- 模型偶尔会“顺手改错别字”被校验拦截，重试后基本都能通过；重试与校验对用户透明
- token 消耗：每篇约 2 倍文字稿长度（长稿按 3000 字分块），几分钱以内

## Notion 笔记同步

有两种方式把文字稿写进 Notion，密钥配置完全相同（四选一，优先级：命令行 > 环境变量 > `notion.local.json` > 项目根 `.env.local`）：

### 方式 A：转写时直接推送（单条）

```powershell
python tools/douyin_transcript/douyin_transcript.py "<链接>" --notion
```

### 方式 B：批量上传已有文稿（upload_to_notion.py）

把 `output\` 里已有的全部文字稿一次性上传为 Notion 子页面。文字稿内容只经磁盘与 Notion API 传输，不经过任何大模型上下文，原文零改动、token 零消耗：

```powershell
# 预览将上传的文件和块数（不需要密钥）
python tools/douyin_transcript/upload_to_notion.py --dry-run

# 正式上传（已上传过的自动跳过，--force 强制重传）
python tools/douyin_transcript/upload_to_notion.py
```

上传记录写在 `upload_state.json`（不提交 Git）；页面结构为：来源文件名 → 分隔线 → 原文段落（超 2000 字符自动分块，超 100 块自动分批）。

### 密钥准备

```ini
# 项目根目录 .env.local（与既有密钥同一处，已被 .gitignore 排除）
NOTION_TOKEN=ntn_xxxxxxxxxxxx
NOTION_PARENT=https://www.notion.so/你的页面-1a2b3c4d5e6f7890abcdef1234567890
```

1. 打开 https://www.notion.so/my-integrations → 新建集成 → 复制 Secret（`NOTION_TOKEN`）。
2. 在目标 Notion 页面右上角 `…` → **连接** → 添加刚建的集成。**这步不能省**，否则集成无权在该页面下建子页面（会报 404/403）。
3. `NOTION_PARENT` 填该页面的链接或页面 ID；笔记会作为它的**子页面**创建。

安全约定：token 只放在未提交的 `.env.local` 或 `notion.local.json`，不写入代码、日志或生成物。

## 依赖

- Python 3.11+，`requests`、`faster-whisper`、`curl_cffi`（curl_cffi 用于模拟浏览器 TLS 指纹通过抖音风控，强烈建议安装）
- Whisper 模型首次使用时自动下载（small 约 464 MB，默认从 hf-mirror.com 镜像拉取；解析顺序：本地目录 > exe 内置 models/ > HF 缓存）
- 不需要 ffmpeg（faster-whisper 内部用 PyAV 解码）

## 打包与集成

- Windows 分发包构建：`python build_windows.py`（详见该文件头部说明；构建产物与 zip 均不提交 Git）。
- 供其他程序（如微信内容处理流程）调用：`python douyin_transcript.py <链接> --no-open`，stdout 为文字稿全文，stderr 为进度；返回码 0 成功、1 失败。

## 已知边界

- 图集（slides）内容没有语音轨道，无法提取。
- 平台自动字幕并非所有视频都有；无字幕时走本地转写，CPU 上耗时约为音频时长的 0.3~1 倍。
- 抖音接口有风控，若返回 ArgusSecurityPlugin 拦截，脚本会自动重试；持续失败时稍后重试或先在浏览器打开一次抖音页面再试。
