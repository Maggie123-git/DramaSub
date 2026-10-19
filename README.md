# DramaSub · 短剧出海字幕工具

> **Short-drama subtitles that get every line right. · 短剧出海，台词不出错。**

DramaSub 是面向**短剧出海**场景的 AI 字幕翻译与术语管理工具。它在翻译前先锁定**角色名与人设**、匹配**剧集类型的台词风格**，再把整季短剧本地化到 59 种语言。

DramaSub is an AI subtitle-localization tool built for **short-drama globalization**. It locks **character names and personas**, matches **genre-specific dialogue tone**, and localizes a whole season to 59 languages.

> **声明 / Disclaimer**：本项目基于开源项目 [TermSub](https://github.com/seaweedbeehive/TermSub)（MIT License）二次开发，保留原版权声明与许可证。核心增量见下文 [「我做了什么」](#我做了什么--what-i-built-on-top)。

---

## 目录 · Table of Contents

- [为什么是短剧 · Why short drama](#为什么是短剧--why-short-drama)
- [核心功能 · Features](#核心功能--features)
- [工作流程 · How it works](#工作流程--how-it-works)
- [产品设计 · Product design](#产品设计--product-design)（见 [CASE_STUDY.md](./CASE_STUDY.md)）
- [技术架构 · Architecture](#技术架构--architecture)
- [快速开始 · Quick start](#快速开始--quick-start)
- [API 端点 · API](#api-端点--api)
- [我做了什么 · What I built on top](#我做了什么--what-i-built-on-top)
- [许可证 · License](#许可证--license)

---

## 为什么是短剧 · Why short drama

短剧出海（Short-drama globalization）是内容生产里对**一致性与效率**要求最苛刻的品类：

- **角色多、跨集长**：一部剧几十个角色、几十集，角色名必须在每一集、每一种语言里完全一致——否则观众会"认不出人"。
- **台词风格高度类型化**：甜宠、悬疑、古装、都市的台词语域（register）完全不同，通用的"逐句翻译"会丢掉类型感。
- **人设与称谓敏感**：古装的「公子/姑娘/大人」、甜宠的昵称、悬疑的潜台词，翻错一个称谓就出戏。
- **批量与时效**：发行方要在多个市场快速上架，人工字幕成本高、周期长。

通用翻译工具（DeepL、谷歌）能翻句子，但**不懂"这部剧的角色叫什么、这台词该什么语气"**。DramaSub 把"角色名 + 类型风格"提前锁死，再用全剧上下文翻译——这是通用工具跳过的一步。

---

## 核心功能 · Features

| 功能 · Feature | 说明 · Description |
|---|---|
| **角色名锁定 · Character-name locking** | 分析阶段自动提取每个角色的姓名、译名、人设、性别与关系，落库为「角色（Character）」术语，翻译时作为硬约束——主角在每场戏都叫同一个名字。 |
| **剧集类型预设 · Genre presets** | 上传时选择类型（甜宠/悬疑/古装/都市/奇幻/科幻/通用），自动套用对应的台词语域、称谓与术语锁定策略。 |
| **全剧上下文 · Whole-season context** | 先通读整集转写稿再翻译，语气、风格、称谓从头到尾保持一致。 |
| **术语审核闭环 · Glossary review loop** | AI 抽取关键术语 + 角色名，**人审核确认后**才进入翻译——这是其他工具跳过、却决定质量的一步。 |
| **字幕卡片编辑 · Editable subtitle cards** | 逐卡审核、编辑、拆分、合并、全局查找替换，再导出。 |
| **59 语言 + RTL** | 支持英语、西班牙语、阿拉伯语、波斯语、日语、韩语等 59 种语言，含完整的从右到左（RTL）渲染。 |
| **多格式导出 · Export** | SRT / WebVTT / TXT / JSON，适配 NLE、播放器与流媒体平台。 |

---

## 工作流程 · How it works

```
上传分集 ──▶ FFmpeg 抽音轨 ──▶ Whisper 转录 ──▶ 上下文分析（类型预设 + 角色/术语抽取）
                                                        │
                                                        ▼
                                          术语/角色审核（人工锁定）
                                                        │
                                                        ▼
                              滑动窗口翻译（术语表为硬约束）──▶ 字幕卡片编辑 ──▶ 导出
```

1. **上传 · Upload**：选类型（genre）→ 传视频/音频/文本 → Whisper 转录。
2. **审核 · Review**：AI 抽取角色名与关键术语，人确认/修正，锁定译名。
3. **翻译导出 · Translate & export**：全上下文翻译到 59 种语言，导出 SRT/VTT/TXT/JSON。

---

## 产品设计 · Product design

产品决策、取舍与竞品差异见 **[CASE_STUDY.md](./CASE_STUDY.md)**（中文，含英文标题）。这是本项目的核心产品文档。

---

## 技术架构 · Architecture

```
                 FFmpeg              OpenAI Whisper
  上传(Video) ──▶ 抽音轨 ──────────▶ 转写(whisper-1)
                                          │
                                          ▼
                                   分段时间戳 + 转写文本
                                          │
                ┌─────────────────────────▼─────────────────────────┐
                │            PostgreSQL（视频/术语/角色/任务）        │
                └─────────────────────────┬─────────────────────────┘
                                          │
                ┌─────────────────────────▼─────────────────────────┐
                │     Celery Worker + Redis（后台任务 + 进度广播）    │
                └─────────────────────────┬─────────────────────────┘
                                          │
                ┌─────────────────────────▼─────────────────────────┐
                │  统一上下文分析（类型预设 → 角色卡 + 术语 + 风格指南） │
                │  滑动窗口翻译（术语表/角色名为硬约束）                │
                └─────────────────────────┬─────────────────────────┘
                                          ▼
                          术语/角色审核 → 字幕时间轴编辑 → 导出
```

**技术栈 · Stack**：FastAPI + SQLAlchemy 2.0 + PostgreSQL · Celery + Redis（WebSocket 实时进度）· OpenAI Whisper（转写）· Google Gemini（分析/翻译）· 原生 JS + Tailwind（前端）· Alembic（迁移）· Ruff + MyPy + pytest（质量）。

---

## 快速开始 · Quick start

### 前置 · Prerequisites

- Python 3.11+、FFmpeg
- PostgreSQL 14+、Redis（Docker Compose 已内置）

### 1. 配置环境

```bash
cp .env.example .env   # 填入 OPENAI_API_KEY 与 GEMINI_API_KEY
```

### 2. Docker 启动（推荐）

```bash
docker compose up --build
```

### 3. 本地开发

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head

# 终端 A：Celery worker
celery -A app.core.celery_app worker --loglevel=info --concurrency=2
# 终端 B：FastAPI
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000`。API 文档：`/docs`（需 `ENABLE_API_DOCS=true`）。

---

## API 端点 · API

| 端点 · Endpoint | 方法 | 说明 |
|---|---|---|
| `/videos/upload` | POST | 上传文件（`target_language` 必填，新增 `genre` 可选） |
| `/videos/{id}/transcribe` | POST | 排队转写 |
| `/videos/{id}/analyze` | POST | 排队上下文分析（类型预设 + 角色/术语抽取） |
| `/videos/{id}/translate` | POST | 排队翻译（术语表为硬约束） |
| `/videos/{id}/config` | PATCH | 更新源/目标语言、`genre`、`skip_glossary` |
| `/videos/{id}/segments/*` | PATCH/POST/DELETE | 字幕卡片编辑（更新/拆分/增删/查找替换） |
| `/export/{id}/{format}` | GET | 导出 `srt` / `vtt` / `txt` / `json` |
| `/ws/videos/{id}` | WS | WebSocket 实时进度 |

---

## 我做了什么 · What I built on top

在原 TermSub（MIT）之上，本项目的增量：

1. **产品重新定位**：从通用"视频翻译 + 术语管理"重定位为**短剧出海字幕工具**。
2. **剧集类型预设（Genre presets）**：把原项目中未启用的死字段 `domain` 改造为 `genre`，新增枚举（甜宠/悬疑/古装/都市/奇幻/科幻）、Alembic 迁移、上传/配置 API，并接入上下文分析 prompt——让类型真正影响翻译风格。
3. **角色卡（Character cards）**：扩展分析 prompt，新增 `characters` 数组（姓名/译名/人设/性别/关系），落库为 `Character` 术语并在前端以「角色」标签高亮，翻译时作为硬约束锁定角色名。
4. **双语产品文档**：README（中英双语）+ 产品设计案例（CASE_STUDY.md）+ 用户指南。

详见 [CASE_STUDY.md](./CASE_STUDY.md) 的「增量清单」。

---

## 许可证 · License

本项目沿用 [MIT License](./LICENSE)（版权归原 TermSub 作者）。新增代码同样以 MIT 授权。

- 原项目：[github.com/seaweedbeehive/TermSub](https://github.com/seaweedbeehive/TermSub)
- 本派生项目的作者在此之上完成了产品定位、功能与文档的增量工作。
