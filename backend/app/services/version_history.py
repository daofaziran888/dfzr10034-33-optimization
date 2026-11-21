"""版本履历领域服务：数据资产的多版本Schema快照、差异对比和回查能力。

维护跨时间点的数据资产版本快照，支持回溯和差异分析。资产恢复（回滚）
时只重算本资产的版本状态与本资产未结报告的比较基线，并登记回滚台账
保证同一回滚重复提交只生效一次。
"""
from __future__ import annotations

import threading
import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.services.diff_report import DiffReportService
from app.store import store

# 同一资产的回退与差异确认可能并发冲突，恢复动作串行化，保证只生效一次
_RESTORE_LOCK = threading.Lock()


class VersionHistoryService(BaseModuleService):
    MODULE = "version_history"
    ENTITY = "版本快照"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["快照编号", "目标资产", "版本标签", "Schema摘要", "行数记录", "创建原因", "差异字段", "快照时间"]
    REQUIRED_FIELDS = ["快照编号", "目标资产", "版本标签"]
    STATUS_ORDER = ["当前版本", "历史版本", "已标注"]
    ACTION_RULES = {"创建快照": "历史版本", "标注版本": "已标注", "恢复版本": "当前版本"}
    NEGATIVE_ACTIONS = []

    # 版本/差异链路上的模块：创建快照时只重算这两张表，不做全量重算
    VERSION_CHAIN_MODULES = ["version_history", "diff_report"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "恢复版本":
            result = self.restore_version(entry_id)
            if result is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            return result["snapshot"], result["message"]
        return super().run_action(entry_id, action)

    def create_version(self, asset_code: str, version_tag: str, reason: str, schema_summary: str = "",
                       row_count: str = "0", diff_fields: str = "",
                       field_values: dict[str, str] | None = None) -> dict[str, Any]:
        """创建资产版本快照，可携带字段级取值（字段快照）。"""
        snap_id = store.next_id(self.MODULE)
        snapshot = {
            "id": snap_id,
            "快照编号": f"VERS-{snap_id:04d}",
            "目标资产": asset_code,
            "版本标签": version_tag,
            "Schema摘要": schema_summary or f"资产{asset_code}快照",
            "行数记录": row_count,
            "创建原因": reason,
            "差异字段": diff_fields,
            "字段快照": dict(field_values or {}),
            "快照时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "历史版本",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(snapshot)

        # 只快照版本/差异链路上的模块，把重算范围收窄到本链路
        store_snapshot = store.create_snapshot(
            f"version-{version_tag}-{asset_code}", self.VERSION_CHAIN_MODULES)

        return {
            "version": snapshot,
            "store_snapshot": store_snapshot,
        }

    def restore_version(self, snapshot_id: int) -> dict[str, Any] | None:
        """恢复资产到指定版本快照，形成回滚比较基线（幂等）。

        重算范围限定为本资产的版本状态与本资产的未结差异报告；已签发报告
        与其它资产的数据保持原样。同一资产恢复到同一版本重复提交只生效一次。
        """
        with _RESTORE_LOCK:
            snapshot = store.find(self.MODULE, snapshot_id)
            if snapshot is None:
                return None
            asset = str(snapshot.get("目标资产", ""))
            version_tag = str(snapshot.get("版本标签", ""))

            # 幂等：同一资产恢复到同一版本只生效一次
            existing = store.find_rollback(asset, version_tag)
            if existing is not None:
                return {
                    "snapshot": snapshot,
                    "rollback": existing,
                    "applied": False,
                    "deduplicated": True,
                    "message": f"资产{asset}已恢复过版本{version_tag}，本次提交未重复生效",
                }

            now = _time.strftime("%Y-%m-%d %H:%M:%S")
            # 限定重算范围：只切换本资产名下的版本状态
            switched: list[int] = []
            for row in store.rows(self.MODULE):
                if str(row.get("目标资产", "")) != asset:
                    continue
                if row is snapshot:
                    row["status"] = "当前版本"
                    row["pending"] = True
                else:
                    row["status"] = "历史版本"
                    row["pending"] = False
                row["updated_at"] = now
                switched.append(int(row.get("id", 0)))

            rollback_code = f"RB-{len(store.rollbacks()) + 1:04d}"

            # 形成回滚比较基线：按字段快照关联本资产的未结报告
            baseline = DiffReportService().anchor_rollback_baseline(
                asset, snapshot, rollback_code)

            rollback = store.record_rollback({
                "回滚编号": rollback_code,
                "目标资产": asset,
                "版本标签": version_tag,
                "快照编号": snapshot.get("快照编号", ""),
                "重算范围": {
                    "版本快照": switched,
                    "重锚报告": baseline["重锚报告"],
                    "跳过已签报告": baseline["跳过已签报告"],
                },
                "回滚时间": now,
            })

            store.log_change(
                self.MODULE, snapshot_id, "恢复版本",
                before=asset,
                after=f"{version_tag}（重锚{len(baseline['重锚报告'])}份未结报告）",
                source="rollback", scope=asset,
            )
            self.emit_event("version_restored", {
                "asset": asset,
                "version_tag": version_tag,
                "rollback_code": rollback_code,
                "anchored_reports": baseline["重锚报告"],
            })

            return {
                "snapshot": snapshot,
                "rollback": rollback,
                "baseline": baseline,
                "applied": True,
                "deduplicated": False,
                "message": (
                    f"资产{asset}已恢复到版本{version_tag}，"
                    f"重锚{len(baseline['重锚报告'])}份未结报告、"
                    f"跳过{len(baseline['跳过已签报告'])}份已签报告"
                ),
            }

    def get_rollback_baseline(self, asset_code: str) -> dict[str, Any] | None:
        """获取资产最近一次回滚形成的比较基线。"""
        records = store.rollbacks(asset_code)
        return records[-1] if records else None

    def annotate_version(self, snapshot_id: int, annotation: str) -> dict[str, Any] | None:
        """标注版本快照。"""
        entry, msg = self.run_action(snapshot_id, "标注版本")
        if entry:
            entry["标注"] = annotation
            entry["标注时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        return entry

    def get_versions_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有版本快照（按时间倒序）。"""
        versions = store.find_by_field(self.MODULE, "目标资产", asset_code)
        versions.sort(key=lambda v: v.get("快照时间", ""), reverse=True)
        return versions

    def get_current_version(self, asset_code: str) -> dict[str, Any] | None:
        """获取资产的当前版本快照（优先取状态为当前版本的快照）。"""
        versions = self.get_versions_by_asset(asset_code)
        for version in versions:
            if version.get("status") == "当前版本":
                return version
        return versions[0] if versions else None
