"""许可证清单。

把每个要评估的组件的上游许可证、锁定版本和分发结论列成一张表。
结论只有三种：能评估、要审阅、不能随包分发。这不是法律意见，是让决定有依据。
"""

from extensions.manifest import LICENSE_LABELS


def rows(manifests):
    """按「必须审阅的排前面」给出清单行。"""
    order = {"blocked": 0, "review-needed": 1, "unknown": 2, "approved": 3}
    items = []
    for manifest in manifests:
        items.append(
            {
                "id": manifest.id,
                "name": manifest.name,
                "kind": manifest.kind,
                "homepage": manifest.homepage,
                "pinned": manifest.pinned,
                "license": manifest.license or "未声明",
                "license_status": manifest.license_status,
                "license_label": LICENSE_LABELS.get(manifest.license_status, manifest.license_status),
                "bundled": bool(manifest.bundled),
                "bundle_blocked": manifest.bundle_blocked(),
            }
        )
    items.sort(key=lambda item: (order.get(item["license_status"], 9), item["name"]))
    return items


def blocked_from_bundle(manifests):
    return [item for item in rows(manifests) if item["bundle_blocked"]]


def review_pending(manifests):
    return [item for item in rows(manifests) if item["license_status"] in ("review-needed", "unknown")]


def notice_text(manifests, product="Blossom"):
    """生成一份 NOTICE。没审过的一律写「不得随包分发」，不留模糊地带。"""
    lines = [
        "%s 第三方组件清单" % product,
        "生成时间：%s" % __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "说明：本清单只记录已评估的上游组件。标注「不得随包分发」的组件，",
        "在完成许可证审阅之前不会链接进主程序，也不会复制进发行包。",
        "",
    ]
    for item in rows(manifests):
        lines.append("- %s（%s）" % (item["name"], item["id"]))
        lines.append("  来源：%s" % (item["homepage"] or "本项目自带"))
        lines.append("  锁定版本：%s" % (item["pinned"] or "不适用"))
        lines.append("  许可证：%s（%s）" % (item["license"], item["license_label"]))
        lines.append("  分发结论：%s" % (item["bundle_blocked"] or ("已随包内置" if item["bundled"] else "可评估，随包需再确认")))
        lines.append("")
    return "\n".join(lines)
