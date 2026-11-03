# 统一会议管理平台核心（meeting_platform）

把飞书妙记、腾讯会议等不同来源的会议，归一化成**同一个数据模型**，
并以「双 URL」形态统一存储，为后续的 AI 分析、督办、前端展示打底。

## 核心思想

无论会议来自哪里，最终都变成一个 `Meeting`，且都带两个 URL：

| 字段 | 含义 | 飞书妙记 | 腾讯会议 |
|------|------|----------|----------|
| `content_url` | 纪要 / 转写 | 飞书文档链接 | 纪要上传 OSS 后的链接 |
| `media_url` | 音视频 | 飞书妙记链接 | 录制上传 OSS 后的链接 |

下游模块只认这两个 URL + 元数据，不再关心来源差异。

## 目录结构

```text
meeting_platform/
├── __init__.py     # 对外导出
├── models.py       # 统一数据模型 Meeting + 枚举 + AI 分析 / 待办子结构
├── store.py        # 存储层（JSON 实现，接口可换 SQLite）
├── adapters.py     # 各数据源 → Meeting 的归一化适配器
├── oss.py          # 阿里云 OSS 上传工具（腾讯录制转 URL）
├── llm.py          # LLM 客户端（OpenAI 兼容 + Mock）
├── analyzer.py     # AI 分析：转写原文 → 摘要/议题/决议/待办
├── scheduler.py    # 定时调度：每天凌晨爬昨天 + 按需采集今天
├── text_extract.py # 纪要文本提取（PDF/DOCX → txt）
├── oss.py          # OSS 上传（线程安全）
├── seed_demo.py    # 灌入演示数据
├── collectors/     # 数据源采集器
│   ├── __init__.py
│   └── tencent.py  # 腾讯会议：多线程采集 视频+纪要 → OSS → 入库（去重）
├── web/            # 统一仪表盘
│   ├── __init__.py
│   └── app.py      # Flask 仪表盘 + JSON API + 定时/按需采集
├── example.py      # 可运行 demo
└── README.md
```

## 数据模型（Meeting）

- **身份**：`meeting_id`（内部 uuid）、`source`、`source_ref`（源内 ID，去重依据）
- **元信息**：`title` / `subject` / `start_time` / `end_time` / `owner` / `participants`
- **双 URL**：`content_url` / `media_url`
- **内容**：`raw_content`（转写原文，供 AI）
- **流水线**：`status`（captured → analyzed → archived）、`analysis`（AI 结果）
- **时间戳**：`captured_at` / `updated_at`

`analysis` 内含 `summary / topics / decisions / action_items`，
其中 `action_items`（待办）是后续「督办」环节的载体，带 `owner / due / status`。

## 存储层（MeetingStore）

- `upsert(meeting)` —— 新增 / 更新，自动维护时间戳
- `get(meeting_id)` —— 按内部 ID 取
- `find_by_source_ref(source, source_ref)` —— 按来源 + 源内 ID 定位，**采集去重用**
- `list(source=, status=)` —— 列表，支持按来源 / 状态过滤
- `delete(meeting_id)` —— 删除

默认 JSON 实现（`data/meetings.json`）。接口稳定，将来换 SQLite 只需新增一个实现同方法的类。

## 适配器（adapters）

- `build_meeting_from_feishu_minutes(...)` —— 飞书妙记 → Meeting
- `build_meeting_from_tencent_meeting(...)` —— 腾讯会议 → Meeting

**新增来源（如钉钉、Zoom）只需加一个 `build_meeting_from_xxx` 函数。**

## 运行 demo

```bash
cd AI_Meeting_Intelligence
python -m meeting_platform.example
```

会在 `data/meetings.json` 生成两条不同来源的会议，演示双 URL 形态与去重。

## 腾讯会议采集（已接入 OSS）

配置 `.env`（参考根目录 `.env.example`）后，一行采集最近 7 天录制并入库：

```python
from meeting_platform.store import MeetingStore
from meeting_platform.oss import OSSUploader
from meeting_platform.collectors import collect_tencent_meetings

store = MeetingStore("data/meetings.json")
result = collect_tencent_meetings(store, days=7, uploader=OSSUploader())
print(result)  # {'created': n, 'skipped': n, 'failed': n, 'items': [...]}
```

每个录制会：下载视频/纪要 → 上传 OSS → 以双 URL 形态写入统一库，已采集的自动去重。
依赖：`pip install oss2 requests`。

## AI 分析（转写 → 结构化）

把已采集会议的 `raw_content` 抽成 摘要 / 议题 / 决议 / 待办，回填 `analysis` 字段：

```python
from meeting_platform.store import MeetingStore
from meeting_platform.analyzer import analyze_pending
from meeting_platform.llm import OpenAICompatibleClient  # 或 MockLLMClient 无 Key 演示

store = MeetingStore("data/meetings.json")
result = analyze_pending(store, llm=OpenAICompatibleClient())  # 配置 LLM_* 环境变量
print(result)  # {'analyzed': n, 'skipped': n, 'failed': n, 'items': [...]}
```

- 单条：`analyze_meeting(meeting, llm=...)`，会自动把状态推进到 `analyzed`。
- 无 API Key 时用 `MockLLMClient()` 即可跑通整条链路，便于周五演示。
- 抽出的 `action_items`（待办）即后续「督办」环节的数据源。

## 统一仪表盘（Web）

一键启动可视化平台（默认端口 5050，避免与飞书 8080 冲突）：

```bash
cd AI_Meeting_Intelligence
python -m meeting_platform.seed_demo      # 先灌入演示数据（5 条会议）
python -m meeting_platform.web.app        # 启动仪表盘
```

打开 http://127.0.0.1:5050 即可看到：

- 顶部统计：会议总数 / 各来源数 / 待办合计
- 会议列表：来源徽标（飞书妙记 / 腾讯会议）、状态、**双 URL 入口**（纪要 / 录制）
- 详情面板：AI 摘要、议题、决议、**待办表**（责任人 / 截止 / 状态）
- 操作：单条「一键 AI 分析」、顶部「一键分析全部」（无 Key 自动用 Mock，演示零门槛）

API：`GET /api/meetings`、`GET /api/meetings/<id>`、`POST /api/meetings/<id>/analyze`、`POST /api/analyze-all`、`POST /api/crawl`、`GET /api/crawl/status`。

### 定时采集与实时更新

- **每日凌晨自动采集**：`MeetingScheduler`（[scheduler.py](scheduler.py)）在 Web 服务进程内常驻，
  每天 02:00 自动爬取**昨天**的腾讯会议（视频+纪要→OSS）并分析。随仪表盘启动自动生效。
- **网页「实时更新」按钮**：点击后后台采集**今天**的内容，页面轮询状态、完成后自动刷新。
- 相关 API：`POST /api/crawl`（触发今天的采集）、`GET /api/crawl/status`（查询采集进度）。
- 按自然日精确取数：`day_range('20260802')` → 该日 `[00:00, 次日00:00)` 时间窗。

## 在整体链路中的位置

```text
飞书妙记 ──┐                                ┌── AI 分析（议题/决议/待办）
           ├─→ collectors/adapters ─→ Meeting ──┼── 督办（待办跟踪）
腾讯会议 ──┘  (下载→OSS→双URL)     ↑          └── 统一前端展示
                                store
```

本包已提供：统一模型、存储、飞书/腾讯适配器、腾讯采集器（含 OSS）。
待补：飞书采集器对接既有 feishu_oauth_app、AI 分析、督办、统一前端。
