"""人格测试台：把一套人格文本放进几个场景里，看它怎么回话。

只做预览：不写聊天记录、不写记忆、不改人格设置、不建会话。
没有模型时明确说跑不了，绝不编一段回答冒充结果。
"""

MAX_SCENARIO_CHARS = 200

# 场景：编号、名字、用户那句话、这个场景想看的是什么。
SCENARIOS = (
    ("chat", "普通聊天", "在吗？今天有点无聊。", "看平时说话的口气"),
    ("low", "用户低落", "今天什么都做不好，有点撑不住了。", "看会不会先顾情绪"),
    ("correct", "用户纠错", "刚才那个结论是错的，你弄错了。", "看认错的态度"),
    ("study", "学习辅导", "帮我讲讲什么是递归，我没太懂。", "看讲得多细、有没有例子"),
    ("debug", "代码排错", "我这段 Python 报了 KeyError，帮我看看。", "看会不会先问清上下文"),
    ("taskfail", "任务失败", "你上次说帮我整理文件，结果没弄成。", "看失败后怎么交代"),
    ("remind", "主动提醒", "（你已经连着坐了两个小时没动）", "看主动开口的分寸"),
    ("disagree", "意见分歧", "我觉得这个方案挺好，你非要我改。", "看能不能直说不同意见"),
)

SCENARIO_IDS = tuple(item[0] for item in SCENARIOS)

SYSTEM_HEAD = "你正在一个测试场景里回话。只回这一句，不要解释你在测试。"


def scenarios():
    """给界面用的场景清单。"""
    return tuple(
        {"id": item[0], "name": item[1], "input": item[2], "focus": item[3]}
        for item in SCENARIOS
    )


def scenario(identifier):
    """按编号取场景。找不到返回 None，不编一个。"""
    wanted = str(identifier or "").strip()
    for item in SCENARIOS:
        if item[0] == wanted:
            return {"id": item[0], "name": item[1], "input": item[2], "focus": item[3]}
    return None


def clean_input(text):
    body = " ".join(str(text or "").split())
    return body[:MAX_SCENARIO_CHARS]


def messages_for(note, scenario_input, system_head=SYSTEM_HEAD):
    """按模型接口要求的格式拼两条消息：一条人格，一条场景。"""
    messages = []
    head = clean_input(system_head) if system_head else ""
    if head:
        messages.append({"role": "system", "content": head})
    body = str(note or "").strip()
    if body:
        messages.append({"role": "system", "content": body})
    messages.append({"role": "user", "content": clean_input(scenario_input)})
    return messages


def available(provider):
    """这个模型现在能不能用来跑测试。"""
    if provider is None:
        return False
    checker = getattr(provider, "is_available", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def usage_tokens(result):
    """把用量读成两个整数。读不到就都是 0，不猜。"""
    usage = getattr(result, "usage", None)
    if usage is None:
        return 0, 0
    try:
        return int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0)
    except Exception:
        return 0, 0


def run(provider, note, scenario_input):
    """跑一次预览。返回结果字典，失败时带上真实原因。"""
    if not available(provider):
        return {"ok": False, "error": "没有配置可用的模型，测试跑不了", "content": ""}
    messages = messages_for(note, scenario_input)
    try:
        result = provider.chat(messages)
    except Exception as exc:
        return {"ok": False, "error": "调用模型出错：%s" % exc, "content": ""}
    if result is None:
        return {"ok": False, "error": "模型没有返回内容", "content": ""}
    if not getattr(result, "ok", False):
        return {
            "ok": False,
            "error": str(getattr(result, "error", "") or "模型返回了失败"),
            "content": "",
        }
    prompt_tokens, completion_tokens = usage_tokens(result)
    return {
        "ok": True,
        "content": str(getattr(result, "content", "") or ""),
        "model": str(getattr(result, "model", "") or ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def compare(provider, left_note, right_note, scenario_input):
    """同一句话，两套人格各回一次。左边失败不影响右边。"""
    return {
        "scenario": clean_input(scenario_input),
        "left": run(provider, left_note, scenario_input),
        "right": run(provider, right_note, scenario_input),
    }
