"""Qoder 技能市场：只读客户端。

只做三件事：列清单、查详情、取说明文件正文。不下载可执行文件，不解压，
不运行任何东西。网络不通、超时、返回不是 JSON，一律如实报错。

公开接口（2026-10-14 实测，不需要账号）：
- 列表  /api/v1/marketplace/skills?page=N&page_size=M
- 搜索  /api/v1/marketplace/skills?keyword=xxx   （keyword 是唯一有效的搜索参数）
- 详情  /api/v1/marketplace/skills/{id}/detail
- 正文  /api/v1/marketplace/skills/{id}/files?path=%2FSKILL.md

api.qoder.com、插件市场等入口要鉴权，这里不用，也不去碰。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://qoder.com"
LIST_PATH = "/api/v1/marketplace/skills"
DEFAULT_TIMEOUT = 20
MAX_BYTES = 2 * 1024 * 1024


class QoderError(Exception):
    """拿不到就说拿不到，别拿猜的内容顶上去。"""


def _get(url, timeout=DEFAULT_TIMEOUT, opener=None):
    request = urllib.request.Request(url, headers={"User-Agent": "Blossom/1.0 (skill inspector)"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise QoderError("接口返回 %s：%s" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise QoderError("连不上：%s" % exc.reason)
    except Exception as exc:  # 超时、DNS、代理都会走到这里
        raise QoderError("请求失败：%s" % exc)
    if len(raw) > MAX_BYTES:
        raise QoderError("返回内容过大，已截断不处理：%s" % url)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise QoderError("返回的不是 JSON：%s（%s）" % (url, exc))


def skill_id_from_url(url):
    """从市场链接里抠出技能 id。抠不出来就返回空串，不猜。"""
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "skill_id", "skillId"):
        if query.get(key):
            return str(query[key][0]).strip()
    # 也认形如 /marketplace/skills/official_xxx 的路径写法
    parts = [item for item in parsed.path.split("/") if item]
    for index, part in enumerate(parts):
        if part in ("skill", "skills") and index + 1 < len(parts):
            return parts[index + 1].strip()
    return ""


def list_skills(page=1, page_size=20, keyword="", timeout=DEFAULT_TIMEOUT, opener=None):
    """列一页技能。返回原始 JSON，字段名以接口为准，这里不加工。"""
    params = {"page": int(page), "page_size": int(page_size)}
    if keyword:
        params["keyword"] = str(keyword)
    url = BASE + LIST_PATH + "?" + urllib.parse.urlencode(params)
    return _get(url, timeout=timeout, opener=opener)


def search(keyword, page=1, page_size=20, timeout=DEFAULT_TIMEOUT, opener=None):
    return list_skills(page=page, page_size=page_size, keyword=keyword, timeout=timeout, opener=opener)


def detail(skill_id, timeout=DEFAULT_TIMEOUT, opener=None):
    """查一个技能的详情。返回原始 JSON。"""
    ident = str(skill_id or "").strip()
    if not ident:
        raise QoderError("缺少技能 id")
    url = "%s%s/%s/detail" % (BASE, LIST_PATH, urllib.parse.quote(ident, safe=""))
    return _get(url, timeout=timeout, opener=opener)


def skill_file(skill_id, path="/SKILL.md", timeout=DEFAULT_TIMEOUT, opener=None):
    """取说明文件正文。只用于读取，不落盘执行。"""
    ident = str(skill_id or "").strip()
    if not ident:
        raise QoderError("缺少技能 id")
    url = "%s%s/%s/files?%s" % (
        BASE,
        LIST_PATH,
        urllib.parse.quote(ident, safe=""),
        urllib.parse.urlencode({"path": str(path or "/SKILL.md")}),
    )
    return _get(url, timeout=timeout, opener=opener)


def file_text(payload):
    """从 files 接口的返回里取正文。取不到返回空串。"""
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("content", "text", "body"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


def tree_paths(node):
    """把详情里的 file_tree 摊平成相对路径列表。只列文件，不列目录。"""
    found = []

    def walk(item):
        if isinstance(item, dict):
            kind = str(item.get("type") or "")
            path = str(item.get("path") or "")
            if kind == "file" and path:
                found.append(path.lstrip("/"))
            for child in item.get("files") or []:
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(node)
    return found


def normalize_detail(payload):
    """把详情压成清单要的那几个字段。缺的留空，不编。"""
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return {}
    return {
        "skill_id": str(data.get("skill_id") or data.get("id") or ""),
        "name": str(data.get("skill_name") or data.get("name") or ""),
        "name_cn": str(data.get("skill_name_cn") or data.get("display_name") or ""),
        "version": str(data.get("version") or ""),
        "author": str(data.get("author_name") or data.get("author") or ""),
        "description": str(data.get("description") or data.get("description_cn") or ""),
        "category": str(data.get("category") or ""),
        "tags": list(data.get("tags") or []),
        "download_url": str(data.get("download_url") or ""),
        "github_repo": str(data.get("github_repo") or ""),
        "updated_at": str(data.get("content_updated_at") or data.get("updated_at") or ""),
        "file_tree": data.get("file_tree") or {},
        "homepage": "%s/zh/marketplace/skill?id=%s" % (BASE, data.get("skill_id") or data.get("id") or ""),
    }
