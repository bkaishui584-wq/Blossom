"""更新检查：拿清单里锁定的版本和市场里的当前版本比一比。

只查不装：发现新版本只报出来，不会自动下载、替换或启用任何东西。
网络不通、条目下架、版本没变，都如实说，不编。
"""

import re

PIN_RE = re.compile(r"^(?P<id>[A-Za-z0-9_\-]+)@(?P<version>[^@\s]+)$")
# 市场里的技能 id 长这样：official + 一串字母数字。别的锁定值（比如连接器的 npm 包名）
# 不是市场条目，不去问市场，免得拿一串没意义的 404 当结果。
MARKET_ID_RE = re.compile(r"^official[A-Za-z0-9_]+$")


def parse_pinned(pinned):
    """把 'official_xxx@1.0.0' 拆成 (id, 版本)。拆不出来返回 ('', '')。"""
    match = PIN_RE.match(str(pinned or "").strip())
    if not match:
        return "", ""
    return match.group("id"), match.group("version")


def qoder_entries(manifests):
    """挑出能从市场查版本的条目。只认市场 id 的写法，别的一律不问。"""
    found = []
    for manifest in manifests or ():
        skill_id, version = parse_pinned(getattr(manifest, "pinned", ""))
        if skill_id and version and MARKET_ID_RE.match(skill_id):
            found.append((manifest, skill_id, version))
    return found


def check(client, manifests, timeout=20):
    """逐条问市场当前版本。返回 {checked, pending, errors, skipped}。"""
    entries = qoder_entries(manifests)
    pending, errors = [], []
    for manifest, skill_id, local_version in entries:
        try:
            payload = client.detail(skill_id, timeout=timeout)
        except Exception as exc:
            errors.append({"id": getattr(manifest, "id", skill_id), "reason": str(exc)})
            continue
        info = client.normalize_detail(payload) if hasattr(client, "normalize_detail") else (payload or {})
        remote = str((info or {}).get("version") or "").strip()
        if not remote:
            errors.append({"id": getattr(manifest, "id", skill_id), "reason": "接口没给出当前版本"})
            continue
        if remote != local_version:
            pending.append(
                {
                    "id": getattr(manifest, "id", skill_id),
                    "name": getattr(manifest, "name", skill_id),
                    "skill_id": skill_id,
                    "local": local_version,
                    "remote": remote,
                    "homepage": getattr(manifest, "homepage", ""),
                }
            )
    return {
        "checked": len(entries) - len(errors),
        "pending": pending,
        "errors": errors,
        "skipped": 0,
        "total": len(entries),
    }


def summary_text(result):
    """给界面用的人话总结。"""
    if not result:
        return "没查。"
    total = int(result.get("total") or 0)
    if total == 0:
        return "没有能从市场查版本的条目。"
    pending = result.get("pending") or []
    errors = result.get("errors") or []
    parts = ["查了 %d 条" % total]
    parts.append("有新版本 %d 条" % len(pending) if pending else "都是最新")
    if errors:
        parts.append("没查成 %d 条" % len(errors))
    return "，".join(parts) + "。"
