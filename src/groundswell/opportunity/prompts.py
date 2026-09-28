"""漏斗 prompt。修改后递增 PROMPT_VERSION：input_hash 包含版本号，新版本会重新分析。"""

from __future__ import annotations

from groundswell.opportunity.schemas import DIMENSIONS

PROMPT_VERSION = "opp-v1"

SCREEN_SYSTEM = (
    "你是早期投资人兼连续创业者，擅长从互联网热点里判断是否存在小团队可切入的商业机会。"
    "你只做快速初筛：宁可放过，不要把纯新闻、八卦、体育比分、政治事件当成商机。"
)

SCREEN_TEMPLATE = """区域：{region}（{region_name}）
下面是该区域近期起飞的话题，每个给出名称、类别、关键词、增长指标与代表标题。

{topics}

对每个话题判断：是否可能蕴含 2–5 人团队可以切入的商业机会（新需求、未满足的痛点、正在形成的新市场、可复制的海外模式）？
输出 JSON：{{"topics": [{{"id": 整数, "keep": true/false, "score": 0-10, "reason": "≤40字"}}]}}，每个 id 都要输出。"""

DEEP_SYSTEM = (
    "你是一名严谨的商业分析师，使用经典创业方法论评估互联网热点背后的商机。"
    "硬性规则：\n"
    "1. 所有判断必须引用下方【证据】中的条目（用 item_id），不得编造数据、公司或用户原话；\n"
    "2. 证据不足以支持商机时，is_opportunity=false，并在 counter_arguments 说明原因；\n"
    "3. 必须结合目标区域的渠道、支付、监管情况给出具体可执行的建议，而不是泛泛而谈；\n"
    "4. 必须给出反方论点（为什么这可能不是好机会）；\n"
    "5. 只输出 JSON。"
)

DEEP_TEMPLATE = """# 目标区域
{region}（{region_name}）
- 主要渠道：{channels}
- 支付方式：{payments}
- 监管要点：{regulation}
- 备注：{notes}

# 话题
名称：{name}
类别：{category}
摘要：{summary}
关键词：{keywords}
起源区域：{origin}；活跃区域：{active_regions}
扩散路径：{spread}
慢变量（长期趋势）：{slow_vars}
历史相似话题：{similar}

# 增长数据（近 72 小时，按区域）
{stats}

# 二次验证（如有）
{validations}

# 证据（只能引用这些 item_id）
{evidence}

# 方法论检查清单（回答有证据支持的问题，并在 principles_applied 中列出对应 id、在 tags 中列出对应标签）
{principles}

# 输出 JSON 字段
- is_opportunity: bool
- summary: 一句话商机描述（≤80 字，写清楚“给谁、解决什么、为什么现在”）
- scores: {dimensions}（每项 0–10）
- tags: 方法论标签（来自清单的 tag）
- principles_applied: 方法论 id 列表
- why_now: 为什么是现在（引用增长数据或证据）
- distribution_path: 在 {region_name} 的获客路径（具体到渠道）
- product_shapes: 1–3 个具体产品形态（例如“面向 X 的 Y 小程序，按月订阅 Z 元”）
- counter_arguments: 1–3 条反方论点
- known_players: 已知玩家（仅限证据或常识中确定存在的产品/公司，不确定就留空）
- durability: one_off | seasonal | structural | unknown
- evidence: [{{"item_id": 证据中的 id, "kind": "pain|demand|growth|willingness_to_pay|competitor|regulation|other", "quote": "从证据中摘录的原文"}}]，至少 2 条
"""

GAP_STATES = {"absent": "几乎没有讨论", "lagging": "有少量讨论但尚未起飞"}

GAP_TEMPLATE = """
# 区域空白（本条是跨区信息差分析）
该话题已在 {broke} 显著起飞，但在 {region_name}{state}。证据主要来自其他区域。
重点回答：这是“还没人做”（信息差，可搬运 / 本地化），还是“结构性做不了”（监管、支付、渠道、文化、基础设施、需求本身不存在）？
- 结构性障碍：is_opportunity=false，并在 counter_arguments 写明具体障碍；
- 信息差：summary 写清要本地化什么，distribution_path 给出 {region_name} 的冷启动渠道，regional_fit 按本地可行性打分。
"""


def dimensions_text() -> str:
    return "；".join(f"{k}={desc}" for k, (desc, _) in DIMENSIONS.items())
