# 文件夹自动转知识库

这是一个本地运行的 EPUB/PDF → Markdown → Obsidian/RAG 知识库工具。GitHub 仓库只保存程序和配置模板；书籍原文件、生成的 Markdown、SQLite 索引和日志均属于本机数据，不应提交。

## 安装依赖

```bash
brew install poppler
# 扫描版 PDF 的自动 OCR：
brew install ocrmypdf tesseract-lang
# 本地语义索引和知识库问答：
brew install ollama
```

## 安装本地 AI 模型

文件监听、EPUB/PDF 转换、Markdown 拆分和关键词索引不依赖 AI 模型。要使用语义检索和知识库问答，还需要在本机运行 [Ollama](https://ollama.com/) 并下载两个模型：

- `bge-m3`：把文章转换为语义向量，用于按含义检索。
- `qwen3:4b`：根据检索到的原文生成回答。

安装 Ollama 后启动服务：

```bash
# 作为 macOS 后台服务运行
brew services start ollama

# 下载项目的默认模型（只需执行一次）
ollama pull bge-m3
ollama pull qwen3:4b

# 确认模型已经安装
ollama list
```

如果没有使用 Homebrew 服务，也可以单独打开一个终端运行：

```bash
ollama serve
```

默认配置为：

```text
Ollama 地址：http://127.0.0.1:11434
向量模型：bge-m3
问答模型：qwen3:4b
```

可以在启动程序前通过环境变量更换地址或模型：

```bash
export OLLAMA_HOST=http://127.0.0.1:11434
export WANWEI_EMBED_MODEL=bge-m3
export WANWEI_CHAT_MODEL=qwen3:4b
```

如果先处理了书籍、后来才安装向量模型，可补建缺失的语义向量：

```bash
python3 indexer.py --embed-missing
```

## 使用

```bash
# 进入你克隆后的项目目录（默认目录名为 knowledge-ingest）
cd knowledge-ingest
python3 ingest.py
```

然后把 `.pdf` 或 `.epub` 放进 `inbox/`。程序会把原文件移到 `original/`，把分章节 Markdown 写入 `output/`。停止监听按 `Ctrl-C`。

PDF 会先尝试直接提取文字，并结合页数计算文字密度。判断为纯扫描或图文混合 PDF 时，会自动启动中英文 OCR、纠偏和页面方向校正；普通文字 PDF 不运行 OCR。

只处理一次现有文件：

```bash
python3 ingest.py --once
```

把 `output/` 用 Obsidian 打开即可搜索。程序不会联网，也不会上传文件。

## 从 GitHub 克隆后初始化

```bash
git clone https://github.com/wuzhi7/knowledge-ingest.git
cd knowledge-ingest
mkdir -p inbox original output index logs .tmp
```

本项目的 Python 脚本只使用标准库；PDF 功能需要 `poppler`，扫描 PDF 的 OCR 另外需要 `ocrmypdf` 和 `tesseract-lang`。语义索引和问答服务需要本机运行 Ollama；不设置环境变量时使用上面列出的默认地址和模型。

## 启动问答服务

确保 Ollama 已启动且模型已下载，然后运行：

```bash
python3 qa_server.py
```

服务默认监听 `http://127.0.0.1:8765`。可用下面的命令检查服务和索引状态：

```bash
curl http://127.0.0.1:8765/health
```

首次使用时，把书籍放入 `inbox/`，不要把 `original/`、`output/`、`index/` 或 `logs/` 上传到 GitHub。

## 登录后自动监听（可选）

```bash
mkdir -p ~/Library/LaunchAgents
sed "s#__PROJECT_DIR__#$PWD#g" com.knowledge-ingest.watcher.plist.example \
  > ~/Library/LaunchAgents/com.knowledge-ingest.watcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.knowledge-ingest.watcher.plist
```

以后每次登录都会自动运行，不必保留终端窗口。日志位于 `logs/`。

如需停用：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.knowledge-ingest.watcher.plist
```

## 持续更新项目

程序优化在源码目录中进行，书籍数据在本机目录中独立保存：

```bash
git checkout -b fix/说明
# 修改 ingest.py、indexer.py、qa_server.py 或文档
python3 -m py_compile ingest.py indexer.py qa_server.py
git add .
git commit -m "说明本次改动"
git push -u origin fix/说明
```

在 GitHub 上通过 Pull Request 合并到 `main`。另一台电脑更新程序时执行 `git pull`，再重新运行监听器；本地书籍和索引不会因为 Git 更新而进入仓库。

## 发布前检查

```bash
git status --short
git check-ignore -v original output index logs inbox
git ls-files
```

确认 `git ls-files` 中没有 `.epub`、`.pdf`、生成的 `.md`、`knowledge.db` 或日志文件后，再推送到 GitHub。书籍内容还可能受版权限制，建议只在自己有权使用的设备上保存和处理。
