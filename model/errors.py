"""把不同服务商的报错收成同一套类别，并给出能照做的中文说明。

Agent 只认这里定义的类别，不解析各家 SDK 的异常类型。错误文本在进入界面
之前先过一遍 redact，避免把 Key 或 Authorization 头带出去。
"""

import re

AUTH = "auth"
QUOTA = "quota"
RATE_LIMIT = "rate_limit"
TIMEOUT = "timeout"
NETWORK = "network"
BAD_MODEL = "bad_model"
INVALID = "invalid_request"
SERVER = "server"
UNCONFIGURED = "unconfigured"
OTHER = "other"

LABELS = {
    AUTH: "认证失败",
    QUOTA: "额度不足",
    RATE_LIMIT: "请求限流",
    TIMEOUT: "请求超时",
    NETWORK: "网络错误",
    BAD_MODEL: "模型不可用",
    INVALID: "请求被拒绝",
    SERVER: "服务商异常",
    UNCONFIGURED: "未配置",
    OTHER: "调用失败",
}

HINTS = {
    AUTH: "Key 不对、过期，或这个 Key 没有该模型的权限。到服务商控制台确认后重填。",
    QUOTA: "余额或配额用尽，服务商拒绝了这次请求。充值或换一个 Key。",
    RATE_LIMIT: "请求太频繁被限流了，等一会儿再试。",
    TIMEOUT: "网络慢或服务商没在时限内响应。可以调大超时，或稍后重试。",
    NETWORK: "连不上服务商。检查网络、代理和接口地址是否可访问。",
    BAD_MODEL: "模型 ID 不对：这个 Key 下没有这个模型。去控制台确认名称再填。",
    INVALID: "请求参数或接口协议不匹配。确认地址是 OpenAI 兼容接口，模型 ID 正确。",
    SERVER: "服务商自己的服务器出错了，等几分钟再试。",
    UNCONFIGURED: "还没配置模型。先在「模型与 API 管理」里填好并测试。",
    OTHER: "调用没有成功，具体原因见下面的说明。",
}

# 这些类别重试有意义：等一会儿或换个时刻就可能成功。
RETRYABLE = (TIMEOUT, RATE_LIMIT, NETWORK, SERVER)

_STATUS = {
    401: AUTH,
    402: QUOTA,
    403: AUTH,
    404: BAD_MODEL,
    408: TIMEOUT,
    409: INVALID,
    413: INVALID,
    422: INVALID,
    429: RATE_LIMIT,
}

_HINTS = (
    (AUTH, ("invalid api key", "incorrect api key", "unauthorized", "authentication",
            "invalid_api_key", "no permission", "密钥", "鉴权", "无权")),
    (QUOTA, ("insufficient quota", "exceeded your current quota", "billing",
             "insufficient_quota", "余额", "欠费", "额度")),
    (RATE_LIMIT, ("rate limit", "rate_limit", "too many requests", "429", "限流")),
    (BAD_MODEL, ("model not found", "model_not_found", "does not exist",
                 "invalid model", "unknown model", "模型不存在", "没有这个模型")),
    (TIMEOUT, ("timeout", "timed out", "deadline", "超时")),
    (NETWORK, ("connection", "unreachable", "getaddrinfo", "network", "ssl",
               "proxy", "断网", "连接不上")),
    (SERVER, ("internal server", "server error", "bad gateway", "unavailable",
              "502", "503", "504", "服务商")),
)

_KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*[^\s,;\"']+"),
    re.compile(
        r"""(?ix)
        ((?<![A-Za-z0-9_])["']?
         (?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|apikey|
            authorization|access[_-]?token|refresh[_-]?token|token)
         ["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|(?:bearer|basic|token)\s+[^\s,;]+|[^\s,;]+)
        """
    ),
    re.compile(r"(?i)(key-|token-)[A-Za-z0-9]{12,}"),
)


def status_code(exc):
    """从 SDK 异常里取 HTTP 状态码；取不到返回 0。"""
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return 0


def classify(exc=None, text="", status=None):
    """按状态码优先、文本提示兜底的顺序归类。认不出来就是 other。"""
    code = int(status or 0) or status_code(exc)
    if code in _STATUS:
        return _STATUS[code]
    if 500 <= code <= 599:
        return SERVER
    low = str(text or exc or "").lower()
    if not low:
        return OTHER
    for kind, needles in _HINTS:
        for needle in needles:
            if needle in low:
                return kind
    return OTHER


def label(kind):
    return LABELS.get(str(kind or ""), LABELS[OTHER])


def redact(text):
    """去掉可能混进错误文本里的密钥片段。"""
    body = str(text or "")
    for pattern in _KEY_PATTERNS:
        body = pattern.sub("[已隐藏]", body)
    return body


def friendly(kind, provider="", detail=""):
    """给用户看的一句话：类别 + 怎么办 + 脱敏后的原始说明。"""
    kind = str(kind or OTHER)
    head = LABELS.get(kind, LABELS[OTHER])
    hint = HINTS.get(kind, HINTS[OTHER])
    prefix = ("%s：" % provider) if provider else ""
    line = "%s%s。%s" % (prefix, head, hint)
    body = redact(str(detail or "")).strip().replace("\n", " ")
    if body:
        line += "（%s）" % body[:160]
    return line
