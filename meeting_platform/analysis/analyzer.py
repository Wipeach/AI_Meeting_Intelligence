"""会议内容 AI 分析（支持长文本 map-reduce 分块，保证不漏内容）。

策略：
- 原文较短（≤ CHUNK_SIZE）→ 单次整体分析。
- 原文较长 → 切成多块，每块单独提取（map），再合并去重并综合摘要（reduce）。
  这样无论多长的会议都能完整覆盖，不会因上下文窗口截断而丢失内容。
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from meeting_platform.analysis.llm import LLMClient, MockLLMClient, OpenAICompatibleClient
from meeting_platform.core.models import (
    ActionItem,
    ActionItemStatus,
    AnalysisResult,
    Meeting,
    MeetingStatus,
    utc_now_iso,
)
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.term_dict import TermDict, get_term_dict

log = logging.getLogger("meeting_analyzer")

# 单次调用字符上限：≤此长度一次性整篇分析（质量最好、最快）；超过才切块走 map-reduce。
# DeepSeek 128K 上下文：5 万字 ≈ 6-7 万 tokens，加 prompt+输出仍远低于上限。
SINGLE_CALL_LIMIT = int(os.getenv("ANALYSIS_SINGLE_CALL_LIMIT", "50000"))

# 切块字符数：仅当原文 > SINGLE_CALL_LIMIT 时启用，切成多大一块。
# 取 3 万字 → 8 万字最多 3 块，兼顾上下文与单次输出长度。
CHUNK_SIZE = int(os.getenv("ANALYSIS_CHUNK_SIZE", "30000"))

# 跨会议并发分析线程数（LLM 客户端无状态、线程安全；store 写有锁）。可用环境变量覆盖。
ANALYZE_WORKERS = max(1, int(os.getenv("ANALYZE_WORKERS", "5")))

# 统一的 JSON 输出结构
# 统一的 JSON 输出结构（四项字段不变；summary 升级为整篇智能纪要，action_items 的 owner/due 兜底文案收紧）
_SCHEMA = """{
  "meeting_topic": "整场会议的核心议题名(8-15汉字)：概括这场会议真正在讨论什么，禁用'XX的快速会议/周会/例会'等占位名",
  "summary": "完整智能会议纪要(Markdown)：一、会议摘要(3-6条)；二、关键结论(与 decisions 同源)；三、议题纪要(每议题:讨论要点+会议结论+后续事项)；四、待确认/未决事项。待办不在此复述，由 action_items 输出。",
  "topics": ["议题名称1(8-18汉字)", "议题名称2"],
  "decisions": ["关键结论1：已确定的最终结果", "关键结论2"],
  "action_items": [
    {"content": "督办事项：动作+对象+关键范围+交付结果", "owner": "责任人；无法判断填'待明确'", "due": "截止；只填原文明示时间，无则填'—'"}
  ]
}"""

# ---------------------------------------------------------------------------
# 以下三段（_PM_ROLE / _ACTION_RULES / _ACTION_REFINE）来自地产项目督办提取
# 专用提示词，注入到 SYSTEM / CHUNK / MERGE 三个 prompt，提升待办精度。
# summary / topics / decisions 的提取约束保持不变。
# ---------------------------------------------------------------------------

# 角色升级：会议纪要助手 + 资深地产 PMO 双重身份
_PM_ROLE = """你兼具「会议纪要分析助手」与「资深地产项目经理 / PMO」双重身份。
你熟悉房地产开发项目从投资、定位、设计、报批报建、成本、招采、工程建设、营销、交付到运营的全过程，
也熟悉地产项目中的多方协同关系：甲方项目团队、设计管理、工程、成本、招采、营销、运营，
以及建筑设计院、方案公司、室内、景观、幕墙、结构、机电、顾问单位、总包、分包、供应商等。

你有两项核心任务：
1. 把冗长、口语化、多人交叉讨论的逐字稿，整理成一份「飞书妙记风格」的结构化智能会议纪要（写入 summary）：
   让一个没有参加会议的人，3～5 分钟内看懂——这次会议讨论了什么、最重要的结论是什么、每个议题讨论到什么程度、
   哪些问题仍没解决、接下来谁需要做什么。
2. 从转写原文中识别真正需要会后持续跟踪、催办、闭环的督办事项（写入 action_items），最终进入地产项目督办系统。

两项任务的共同准则：准确优先、宁缺毋滥、责任清晰、成果明确、便于催办；
提炼而非转录，按议题组织而非按发言人，结论优先而非过程优先，事实优先不脑补，重要内容详细、普通内容压缩。"""

# 待办提取核心规则（三段共用：判断什么算督办 + 责任人 + 截止 + 禁止臆造）
_ACTION_RULES = """
## 待办（action_items）提取规则 —— 地产项目专用

### 什么叫督办事项
必须是「会议结束后仍需某人/某单位继续执行，并最终形成明确结果」的事项。
核心判断：会后项目经理是否需要找责任人问「这个事完成了吗？成果在哪？」——需要才是督办。

### 六类信息必须区分（只有后两类才是督办候选）
1. 会议结论（如「采用A方案」「该位置取消」「成本目标确定」）→ 归 decisions，不进 action_items；
   仅当会议进一步要求「据此修改图纸/模型/清单并重新提交」时，才生成督办。
2. 问题（图纸冲突、成本偏高、空间不足等）→ 仅当有人被要求确认/修改/测算/补图/反馈时才成督办。
3. 建议与讨论（「是不是可以…」「我建议…」「后面再看看」）→ 未形成明确行动安排的，不得生成督办。
4. 已完成事项（现场已查到/已确认/已发送/已解决）→ 不再生成督办。
5. 待决策事项（要求某人再研究/比选/测算/提方案/向领导或政府确认）→ 可生成督办。
6. 明确会后行动（修改图纸/更新模型/补充节点/提资/发正式版/重新报价/补测算/报审/与施工或政府沟通）→ 主要督办候选。

### 常见督办动作（仅作识别参考，出现关键词 ≠ 一定是督办，须结合上下文判断）
设计类（修改/深化/补充/出图/提资/复核/更新模型）、工程类（整改/核查/样板/复测/报验/验收）、
成本类（测算/核价/限额/方案比选）、招采类（招标/询价/定标/采购计划）、报建类（报审/报规/取证/回复审查意见）、
营销类（更新货值/调价/展示方案/开放节点）、运营类（计划调整/节点确认/跨部门协调/专题会/会议纪要）。

### 责任人（owner）识别优先级
1. 明确承诺（「我来改」「我回去确认」）→ 说话人；
2. 明确指派并接受（A：「王工你补一下」 B：「好的」）→ 王工；
3. 明确指定单位（「设计院重新出图」）→ 设计院；
4. 上下文明确责任界面 → 对应专业/单位。
注意区分：提出问题的人 / 要求别人做事的人 / 主持人 ≠ 责任人。
无法判断时 owner 一律填「待明确」，禁止猜测具体人员。

### 截止时间（due）
只用原文明示的时间（今天/明天/本周/下周/会后/出图前/开放前/招标前/8月15日前/尽快等）。
原文未明示时 due 一律填「—」，禁止臆造日期。

### 转写容错与禁止创造
原文来自语音转写，可能有人名错字、术语错字、重复、口语、中断、多人抢话；
可修正明显语气词和断句，理解真实含义。但禁止自行创造：数字、面积、楼栋、尺寸、日期、责任人、未形成的方案。不确定的信息不要猜。
没有明确督办事项时，action_items 返回空数组 []。
"""

# 合并 / 拆分 / 颗粒度 / 二次校验（MERGE 全局精加工主战场；SYSTEM 也用）
_ACTION_REFINE = """
## 合并 / 拆分 / 颗粒度（最终精加工）—— 合并优先
总原则：默认倾向合并。当拿不准该拆还是该合时，一律合并为一条。只有满足下列明确的拆分条件时才拆。

### 合并原则（优先执行）
- 一个督办事项 = 一个独立、可验收的成果。
- 同一责任人针对同一成果的一系列连续动作（如调色/调尺寸/改模型/出图/发确认）→ 合并为一条，用顿号或括号列举各子动作。
- 同一责任人做的【同类调研 / 收集 / 查找 / 整理】动作（典型如「找养老案例」「找康养案例」「找参考项目」「收集配套案例」）→ 必须合并为一条，用列举方式写出各子项，禁止按子主题拆成多条。
- 多条围绕同一交付物（同一份图 / 同一份报告 / 同一个模型）的修改意见 → 合并为一条。

### 拆分原则（仅当满足以下任一条件才拆，否则合并）
- 责任人或责任单位确实不同；
- 或成果不同、可分别独立验收；
- 或存在明确的前后置依赖（A 先完成、B 才能开展，如「建筑出图 → 室内标吊顶 → 幕墙深化」），按责任主体拆分。
注意：「主题相近但同一人做同一成果」不构成拆分理由；仅为让清单"更详细"而拆分是禁止的。

### 其他要求
- 结论 vs 成果修改：督办的是「按会议结论去改图/改模型/改清单」，而非「确认某结论」。例：最终定「山墙只保留沿街部分」，真正的督办是「按该范围更新山墙方案图」。
- 版本信息保留：若原文明确指定版本（0804版/报规版/施工图版/SU模型/CAD/T3/汇报版/招标版等），任务描述必须带版本；版本未明示不得自行补充。
- 成果交付对象保留：若做完后需发给另一专业继续深化（如「更新墙身节点并发幕墙深化」），应保留交付对象。
- 颗粒度：每条须为「动作 + 对象 + 关键范围 + 交付结果」，禁止「调整图纸」「优化方案」「处理一下结构问题」「核对一下」等无法验收的空泛项。
- 不过度细化：目标不是记录所有工作动作，而是项目经理真正需要追踪的关键闭环事项。

## 二次校验（输出前对每条逐项检查，不满足则删除或改写）
1. 原文是否真的要求完成？（否→删）2. 会议现场是否已解决？（已解决→删）3. 是否只是拍板结论而无后续动作？（是→删，或并入 decisions）
4. 是否存在责任主体？（无且无法判断→删；owner 仅在确有后续动作时保留「待明确」）5. 以后能否判断「做完了吗」？（不能→删）
6. 是否与其他事项同属一个成果？（是→合并）7. 是否是讨论中的旧方案、后续已被否定/替代？（是→删）8. 项目经理是否真的需要催办？（否→删）

## 高精度原则
采用高精度、低误报模式：宁可遗漏少量弱任务，也不要把大量讨论内容误识别成督办。
一场 2 小时会议讨论了 30 个问题，不代表就要输出 30 条待办。没有明确督办事项时，action_items 返回空数组 []。
"""

# 智能会议纪要（summary 字段）生成规则 —— 飞书妙记 / 安克 AI 录音豆风格
_MINUTES_RULES = """
## 智能会议纪要（summary 字段）生成规则

summary 字段不是 3-5 句短摘要，而是一份**完整的结构化智能会议纪要**，用 Markdown 输出。

### 总体原则
- 提炼而非转录；按议题组织而非按发言人；结论优先而非过程优先；保留关键讨论逻辑而非只剩一句结论；事实优先不脑补。
- 重要内容详细、普通内容压缩。禁止流水账（"张三提出…李四认为…王五表示…"），必须重组为"议题 + 讨论 + 结论 + 后续"。

### 输出结构（严格按此 Markdown 结构写入 summary）
```
# 会议纪要

## 一、会议摘要
（3-6 条，每条表达一个完整核心信息：为什么开/解决了哪些问题/哪些方案已定/哪些未决/下一阶段最关键工作。不要把议题重新罗列一遍。）

## 二、关键结论
（只记录真正已确定的结果：采用某方案/某范围保留/某做法取消/材料调整/最终尺寸/哪个版本为依据/各专业统一原则。写成明确陈述句，禁写"建议/可能/再看看"。此节内容必须与 decisions 数组完全一致。无重要结论可省略本节。）

## 三、议题纪要
（按"讨论对象发生明显变化"智能划分议题，不机械按时间/发言人切分；同一问题反复出现合并到同一议题；章节数由实际内容决定，不强行凑数。每个议题：）

### N. 【议题名称】
（标题 8-18 汉字，一眼知道这部分在聊什么。如原文含时间戳且能可靠识别，标注 **时间：00:12:30–00:25:40**；否则不标。）

**讨论要点**
（2-5 条：保留问题背景/关键矛盾/不同方案/必要专业判断/影响决策的数据/专业接口关系/为什么这样决定；删寒暄、重复、情绪化、跑题。对重要问题保留"问题→判断→决策"逻辑，而非一句结论。）

**会议结论**
（1-3 条；无结论则写"暂未形成最终结论"。）

**后续事项**
（该议题明确的会后动作；无则不强行增加。）

（继续下一个议题……）

## 四、待确认 / 未决事项
（会议结束后仍没完全解决的问题：样板待确认/结构可实施性待复核/某尺寸待现场复测/政府审批口径待确认/A-B方案未最终选择。注意与待办区分——"栏杆节点尚未确定"是待确认；"辉辉会后提供3个节点方案"是待办。不得把普通讨论塞进来。）
```
注意：「待办事项」不在 summary 内重复列出，由 action_items 数组单独输出；二者须完全同源一致。

### 议题标题规则
- 标题让人一眼知道这部分在聊什么，8-18 汉字。
- 推荐：`162户型点位及精装图调整` `最新建筑底图与精装送审衔接` `外立面材料及分缝方案` `屋面排水与防水节点`。
- 禁用：`关于设计的问题` `第三个问题` `图纸讨论` `其他事项`。

### 必须保留的地产关键信息（影响执行则不得过度压缩）
楼栋（6号楼）、户型（143/162/167）、位置（首层大堂/标准层/西立面/主卧/阳台）、版本（0804版/7月24日版/报审版/实施版）、
尺寸（φ150/1.3m/350-400mm）、材料（ST03/不锈钢/天然石材/铝板）、节点时间（送审/发标/样板开放/结构封闭/施工图出图）。

### 版本冲突与被推翻的观点
- 出现"我手上是旧版/这个已经改了/之前说A现在改成B"时：明确【当前有效版本】及相比上版的【变化】；未确认哪个有效→放入"待确认/未决事项"，不自行判断。
- 方案经历 提出A→提出B→否定A→改B→最终定C：结论写 C；A/B 若代表重要方案比较可在"讨论要点"简述；禁止把已被推翻的方案写进"会议结论"。

### 区分「会议结论」与「待办」
- "162户型小夜灯采用带五孔插座小夜灯"是结论（→decisions）；"潘晋宇今天按此原则改图并发群"是待办（→action_items）。两者都要体现，不要只记一个。

### 发言人处理
- 不按发言人组织纪要。仅在以下情况保留发言人：明确责任、关键专业判断、明确决策人意见。避免大量"张三认为/李四提出/王五表示"。

### 转写容错
- 原文来自语音识别，可能有人名/术语/同音字/数字/断句/抢话错误。可修语气词、断句、重复；不得擅自改人名、数字、楼栋、户型、面积、日期、材料编号、专业参数；无法可靠判断时用原文或标注"待确认"。

### 信息压缩比例（参考，不机械）
- 60 分钟会议约 1500-3000 字；90-120 分钟约 2500-5000 字。简单议题短写、重要议题详写；不因会议长就一定写很长。

### 语言风格
- 专业、简洁、客观、项目化。用"会议明确…/经讨论…/当前…/最终确定…/后续按…执行/该事项仍需…"。忌"大家热烈讨论/与会人员一致认为/在友好氛围中"等公文套话。

### 原文溯源
- 如原文含时间戳：每个主要议题尽量保留一个讨论时间范围；仅对重要决策/重大争议/关键待办/易产生责任争议的内容标注时间（如 `[00:42:30]`），不要每句都标。不得捏造时间戳。
"""

SYSTEM_PROMPT = _PM_ROLE + """

给定一场会议的完整转写原文，请提取并严格按下面的 JSON 结构输出（只输出 JSON，不要任何额外说明）：

""" + _SCHEMA + """

## summary / topics / decisions 规则
- meeting_topic：用 8-15 个汉字概括这场会议的核心议题（如「康养项目选址方案讨论」「三期外立面材质定样」），必须是体现会议真实内容的准确名称，禁止用「XX的快速会议/周会/例会/会议」等占位名。
- summary：整篇结构化智能会议纪要（Markdown），严格遵循下面的纪要生成规则。
- topics：议题名称列表（8-18 汉字，与 summary「三、议题纪要」中各议题标题一致）。
- decisions：会议关键结论（与 summary「二、关键结论」完全同源一致）。
- 全部用中文，只根据原文事实，不编造；原文没有的就返回空数组/空字符串。
""" + _MINUTES_RULES + """
## 待办（action_items）规则
""" + _ACTION_RULES + _ACTION_REFINE

CHUNK_PROMPT = _PM_ROLE + """

下面是某场会议的【一个片段】（整场会议被切成了多段）。请只提取【本片段中明确出现】的内容，
不要推测整场会议。严格按下面的 JSON 结构输出（只输出 JSON）：

""" + _SCHEMA + """

## 本片段要求
- meeting_topic：本片段能看出的会议核心议题；片段信息不足时返回空字符串（最终议题名由合并阶段产出）。
- summary / topics / decisions：只覆盖本片段内容，没有的返回空数组。
- 待办（action_items）：只提取本片段中明确的候选会后行动，不做跨段合并，不做全局二次校验（合并与校验留到后续 reduce 阶段）。

""" + _ACTION_RULES + """
（本阶段为候选提取；合并 / 拆分 / 颗粒度 / 二次校验等全局规则在后续合并阶段执行，本片段无需应用。）
全部用中文。"""

MERGE_PROMPT = _PM_ROLE + """

一场长会议被分成了多段分别提取，下面是各段的提取结果（JSON）。
请合并成【整场会议】的最终结果，严格按下面的 JSON 结构输出（只输出 JSON）：

""" + _SCHEMA + """

## 合并要求
- meeting_topic：综合各段信息，为【整场会议】产出 8-15 汉字的核心议题名（体现会议真实内容，禁用占位名）。
- summary：基于各段摘要 + 议题 + 结论，撰写【整场会议】的完整结构化智能会议纪要（Markdown），严格遵循下面的纪要生成规则。
- topics：合并去重为议题名称列表（8-18 汉字，与纪要「三、议题纪要」各议题标题一致）。
- decisions：合并去重（同一结论只保留一条，表述更完整；与纪要「二、关键结论」同源一致）。
- 待办（action_items）：在整场会议视角下合并、拆分、去重、二次校验——这是最终精加工，须严格遵循下面的地产督办规则。

""" + _MINUTES_RULES + "\n" + _ACTION_RULES + _ACTION_REFINE + """
全部用中文。"""


def analyze_meeting(meeting: Meeting, *, llm: Optional[LLMClient] = None) -> AnalysisResult:
    """分析单条会议：短则整体分析，长则 map-reduce 分块，保证完整不漏。

    分析前先做术语纠错：确定类规则直接替换原文与标题（raw_content 永远保留原文，
    修正结果存 corrected_content，AI 分析与后续督办均基于修正后文本）；
    语境类规则追加到各 prompt 由 LLM 结合上下文判断。
    词表不可用时跳过纠错，分析照常进行。
    """
    if not meeting.raw_content.strip():
        raise RuntimeError(f"会议 {meeting.meeting_id} 没有转写原文，无法分析")

    llm = llm or OpenAICompatibleClient()

    # —— 术语纠错（从原文重新计算，词表更新后重分析即用最新规则）——
    content = meeting.raw_content
    corrections = 0
    term_dict: Optional[TermDict] = None
    try:
        term_dict = get_term_dict()
    except Exception:  # noqa: BLE001
        log.warning("获取纠错词表失败，本次分析不做术语修正", exc_info=True)
    if term_dict is not None:
        corrected, n_text = term_dict.correct(meeting.raw_content)
        new_title, n_title = term_dict.correct_title(meeting.title)
        corrections = n_text + n_title
        meeting.corrected_content = corrected
        meeting.title = new_title
        content = corrected
        if corrections:
            log.info("会议 %s 术语纠错：%d 处", meeting.meeting_id, corrections)

    extra_system = term_dict.prompt_section() if term_dict else ""

    if len(content) <= SINGLE_CALL_LIMIT:
        analysis = _analyze_single(meeting, llm, content, extra_system)
    else:
        chunks = _split_text(content, CHUNK_SIZE)
        analysis = _analyze_chunked(meeting, llm, extra_system, chunks)

    if term_dict is not None:
        _strip_annotations(analysis, term_dict)
    analysis.corrections = corrections
    meeting.analysis = analysis
    meeting.status = MeetingStatus.ANALYZED
    return analysis


def _analyze_single(meeting: Meeting, llm: LLMClient, content: str, extra_system: str = "") -> AnalysisResult:
    raw = llm.chat_json(SYSTEM_PROMPT + extra_system, _user_msg(meeting, content))
    return _to_analysis(raw)


def _strip_annotations(analysis: AnalysisResult, td: "TermDict") -> None:
    """兜底清除所有输出字段里「标准词(错误词)」式残留标注，只保留标准词。"""
    analysis.summary = td.strip_annotations(analysis.summary)
    analysis.meeting_topic = td.strip_annotations(analysis.meeting_topic)
    analysis.topics = [td.strip_annotations(t) for t in analysis.topics]
    analysis.decisions = [td.strip_annotations(d) for d in analysis.decisions]
    for item in analysis.action_items:
        item.content = td.strip_annotations(item.content)
        item.owner = td.strip_annotations(item.owner)
        item.due = td.strip_annotations(item.due)


def _analyze_chunked(
    meeting: Meeting, llm: LLMClient, extra_system: str, chunks: List[str]
) -> AnalysisResult:
    """map：每块单独提取；reduce：合并去重 + 综合摘要。"""
    n = len(chunks)

    def _one(idx: int, chunk: str) -> AnalysisResult:
        extra = f"\n（这是第 {idx + 1}/{n} 段，可能不完整，请只提取本段出现的内容）"
        raw = llm.chat_json(CHUNK_PROMPT + extra_system, _user_msg(meeting, chunk, extra))
        return _to_analysis(raw)

    # 分块 map 阶段并发：LLM 调用为 I/O 密集且客户端线程安全（requests.post 无共享状态 + 429 退避）
    workers = min(n, max(1, int(os.getenv("ANALYSIS_CHUNK_WORKERS", "4"))))
    if workers == 1:
        partials = [_one(i, c) for i, c in enumerate(chunks)]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chunk") as pool:
            partials = list(pool.map(lambda ic: _one(ic[0], ic[1]), enumerate(chunks)))

    merge_payload = {
        "会议标题": meeting.title or "未知",
        "各段议题名": [p.meeting_topic for p in partials if p.meeting_topic],
        "分段摘要": [p.summary for p in partials],
        "各段议题": [t for p in partials for t in p.topics],
        "各段决议": [d for p in partials for d in p.decisions],
        "各段待办": [
            {"content": ai.content, "owner": ai.owner, "due": ai.due}
            for p in partials for ai in p.action_items
        ],
    }
    merged = llm.chat_json(MERGE_PROMPT + extra_system, json.dumps(merge_payload, ensure_ascii=False))
    return _to_analysis(merged)


def _user_msg(meeting: Meeting, content: str, extra: str = "") -> str:
    return (
        f"会议标题：{meeting.title or '未知'}\n"
        f"来源：{meeting.source.value}\n"
        f"参会人：{', '.join(meeting.participants) or '未知'}{extra}\n\n"
        f"转写原文：\n{content}"
    )


def _split_text(text: str, max_chars: int) -> List[str]:
    """按行切分，尽量在换行处断块；单行超长则硬切。不丢任何字符。"""
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for line in lines:
        if len(line) > max_chars:  # 单行超长，硬切
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            for i in range(0, len(line), max_chars):
                chunks.append(line[i:i + max_chars])
            continue
        if cur and cur_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def analyze_pending(
    store: MeetingStore,
    *,
    llm: Optional[LLMClient] = None,
    limit: Optional[int] = None,
    max_workers: Optional[int] = None,
    source: Optional[MeetingSource] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    on_progress=None,
) -> Dict[str, Any]:
    """批量【并发】分析所有「已采集但未分析」的会议（有转写原文的）。

    - source 指定时只分析该来源（腾讯/飞书），否则全部。
    - 跨会议多线程并发（默认 ANALYZE_WORKERS 路），大幅缩短整体耗时。
    - 单个共享 LLM 客户端（无状态、线程安全）；store.upsert 自带类级锁，并发写安全。
    - 单条失败不影响其它；on_progress(done, total) 用于回传进度。
    """
    llm = llm or OpenAICompatibleClient()
    all_captured = store.list(status=MeetingStatus.CAPTURED, source=source, date_from=date_from, date_to=date_to)
    targets = [item for item in all_captured if item.raw_content.strip()]
    if limit:
        targets = targets[:limit]

    workers = max(1, max_workers or ANALYZE_WORKERS)
    analyzed, failed = 0, 0
    items: List[Dict[str, Any]] = []
    total = len(targets)
    done = 0

    def _task(meeting: Meeting):
        try:
            analyze_meeting(meeting, llm=llm)  # 原地修改 meeting（analysis/status）
            return meeting, None
        except Exception as exc:  # noqa: BLE001
            return meeting, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_task, m): m for m in targets}
        for fut in as_completed(future_map):
            meeting, exc = fut.result()
            done += 1
            if exc is None:
                store.upsert(meeting)
                analyzed += 1
                items.append({"meeting_id": meeting.meeting_id, "title": meeting.title, "analyzed": True})
            else:
                failed += 1
                items.append({"meeting_id": meeting.meeting_id, "error": str(exc)[:150]})
            if on_progress:
                on_progress(done, total)

    skipped = len(all_captured) - total
    return {"analyzed": analyzed, "skipped": skipped, "failed": failed, "items": items}


def _clean_str(v: Any) -> str:
    """LLM 返回的值安全转字符串：None/缺失 → ""，避免出现字面量 "None"。"""
    return str(v).strip() if v is not None else ""


def _to_analysis(raw: Dict[str, Any]) -> AnalysisResult:
    if not isinstance(raw, dict):
        raise RuntimeError(f"LLM 返回非 JSON 对象：{str(raw)[:200]}")
    action_items = [
        ActionItem(
            content=_clean_str(item.get("content", "")) if isinstance(item, dict) else _clean_str(item),
            owner=_clean_str(item.get("owner", "")) if isinstance(item, dict) else "",
            due=_clean_str(item.get("due", "")) if isinstance(item, dict) else "",
            status=ActionItemStatus.OPEN,
        )
        for item in (raw.get("action_items") or [])
        if (item if isinstance(item, str) else (_clean_str(item.get("content", "")) if isinstance(item, dict) else ""))
    ]
    return AnalysisResult(
        summary=_clean_str(raw.get("summary", "")),
        meeting_topic=_clean_str(raw.get("meeting_topic", "")),
        topics=[_clean_str(t) for t in (raw.get("topics") or []) if _clean_str(t)],
        decisions=[_clean_str(d) for d in (raw.get("decisions") or []) if _clean_str(d)],
        action_items=action_items,
        analyzed_at=utc_now_iso(),
    )
