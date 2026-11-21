"""差异报告领域服务：数据资产的结构化差异对比、版本间变更汇总和差异评估。

接收变更捕获的事件自动生成差异报告。资产版本回滚后，本资产的未结报告
按字段版本快照重锚回滚比较基线、继续核对；已签发报告的前后置冻结，
复现时仍按原先的两版取值。
"""
from __future__ import annotations

import re
import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DiffReportService(BaseModuleService):
    MODULE = "diff_report"
    ENTITY = "差异记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "比较对象", "比较维度", "差异字段", "源值", "目标值", "差异等级", "报告时间"]
    REQUIRED_FIELDS = ["记录编号", "比较对象", "比较维度"]
    STATUS_ORDER = ["待生成", "已生成", "已确认", "已忽略"]
    ACTION_RULES = {"生成报告": "已生成", "确认差异": "已确认", "忽略差异": "已忽略"}
    NEGATIVE_ACTIONS = ["忽略差异"]

    # 未结报告：仍可继续核对，回滚时参与基线重锚
    UNSETTLED_STATUSES = ("待生成", "已生成")
    # 已签发报告：前后值冻结，回滚不得改写
    SIGNED_STATUSES = ("已确认", "已忽略")

    def generate_report(self, subject: str, dimension: str, field: str, from_val: Any,
                        to_val: Any, level: str = "修改") -> dict[str, Any]:
        """生成差异报告。"""
        report_id = store.next_id(self.MODULE)
        report = {
            "id": report_id,
            "记录编号": f"DIFF-{report_id:04d}",
            "比较对象": subject,
            "比较维度": dimension,
            "差异字段": field,
            "源值": str(from_val),
            "目标值": str(to_val),
            "差异等级": level,
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(report)

        self.emit_event("diff_generated", {
            "report_id": report_id,
            "subject": subject,
            "level": level,
        })

        return report

    def confirm_diff(self, report_id: int) -> dict[str, Any] | None:
        """确认差异：未结报告按当前（可能已被回滚重锚的）基线冻结。

        与资产回退冲突时未结报告仍可确认；已签发报告幂等返回，不再改写前后值。
        """
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None
        if report.get("status") in self.SIGNED_STATUSES:
            return report
        baseline = self.resolve_baseline(report)
        entry, _ = self.run_action(report_id, "确认差异")
        if entry is not None:
            entry["pending"] = False
            entry["确认基线"] = baseline["基线标识"] if baseline else "report-local"
            entry["确认时差异"] = self._field_delta(entry.get("源值", ""), entry.get("目标值", ""))
        return entry

    # ── 回滚比较基线 ──────────────────────────────────────────

    def anchor_rollback_baseline(self, asset: str, snapshot: dict[str, Any],
                                 rollback_code: str = "") -> dict[str, Any]:
        """资产恢复后，把本资产未结报告的比较基线重锚到恢复版本。

        只按字段版本快照改写本资产未结报告的基线端（源值），实物端（目标值）
        保留；已签发报告与其它资产的报告保持原样。
        """
        field_values = snapshot.get("字段快照") or {}
        baseline_code = f"{snapshot.get('快照编号', '')}@{snapshot.get('版本标签', '')}"
        anchored: list[str] = []
        skipped_signed: list[str] = []
        for report in store.rows(self.MODULE):
            if str(report.get("比较对象", "")) != asset:
                continue
            if report.get("status") in self.SIGNED_STATUSES:
                skipped_signed.append(str(report.get("记录编号", "")))
                continue
            field = str(report.get("差异字段", "") or "")
            restored = field_values.get(field)
            if restored is None:
                snap_field = str(snapshot.get("差异字段", "") or "")
                if field in ("", "-", "记录") or (field and field == snap_field):
                    restored = snapshot.get("行数记录")
            if restored is not None:
                report["源值"] = str(restored)
            report["回滚基线"] = baseline_code
            report["回滚编号"] = rollback_code
            report["pending"] = True
            report["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            anchored.append(str(report.get("记录编号", "")))
        return {
            "目标资产": asset,
            "版本标签": snapshot.get("版本标签", ""),
            "回滚基线": baseline_code,
            "重锚报告": anchored,
            "跳过已签报告": skipped_signed,
        }

    def resolve_baseline(self, report_or_id: Any) -> dict[str, Any] | None:
        """沿着历史取数链路定位差异报告的比较基线。

        链路：diff_report →（回滚基线 / change_capture）→ version_history。
        """
        report = self._coerce_report(report_or_id)
        if report is None:
            return None
        chain: list[dict[str, Any]] = [{
            "环节": "diff_report",
            "记录编号": report.get("记录编号", ""),
            "状态": report.get("status", ""),
        }]
        asset = str(report.get("比较对象", ""))

        # 已签发：冻结的两端即签发时的两版，按原先两版复现
        if report.get("status") in self.SIGNED_STATUSES:
            chain.append({"环节": "签发冻结", "说明": "已签发报告按原先两版复现，前后值不再改写"})
            return {
                "记录编号": report.get("记录编号", ""),
                "基线标识": report.get("确认基线") or "签发冻结",
                "源值": report.get("源值", ""),
                "目标值": report.get("目标值", ""),
                "链路": chain,
            }

        # 回滚基线：未结报告被回滚重锚后的当前基线
        if report.get("回滚基线"):
            chain.append({
                "环节": "version_history",
                "回滚基线": report.get("回滚基线", ""),
                "回滚编号": report.get("回滚编号", ""),
            })
            return {
                "记录编号": report.get("记录编号", ""),
                "基线标识": report.get("回滚基线", ""),
                "源值": report.get("源值", ""),
                "目标值": report.get("目标值", ""),
                "链路": chain,
            }

        # 变更捕获链路：change_id → 变更记录 → 资产版本快照
        change_id = report.get("change_id")
        if change_id is not None:
            change = store.find("change_capture", int(change_id))
            if change is not None:
                chain.append({
                    "环节": "change_capture",
                    "记录编号": change.get("记录编号", ""),
                    "变更时间": change.get("变更时间", ""),
                })
                versions = self._asset_versions(asset)
                chain.append({"环节": "version_history", "版本数": len(versions)})
                return {
                    "记录编号": report.get("记录编号", ""),
                    "基线标识": f"change#{change_id}",
                    "源值": change.get("变更前值", ""),
                    "目标值": change.get("变更后值", ""),
                    "链路": chain,
                }

        # 退化：报告自身记录的两端
        chain.append({"环节": "report-local", "说明": "无外部基线，按报告自身前后值"})
        return {
            "记录编号": report.get("记录编号", ""),
            "基线标识": "report-local",
            "源值": report.get("源值", ""),
            "目标值": report.get("目标值", ""),
            "链路": chain,
        }

    def reproduce_report(self, report_or_id: Any) -> dict[str, Any] | None:
        """复现差异报告的比较结果：已签发按原先两版，未结按当前基线。"""
        report = self._coerce_report(report_or_id)
        if report is None:
            return None
        baseline = self.resolve_baseline(report)
        signed = report.get("status") in self.SIGNED_STATUSES
        return {
            "记录编号": report.get("记录编号", ""),
            "状态": report.get("status", ""),
            "比较对象": report.get("比较对象", ""),
            "差异字段": report.get("差异字段", ""),
            "源值": report.get("源值", ""),
            "目标值": report.get("目标值", ""),
            "差异": self._field_delta(report.get("源值", ""), report.get("目标值", "")),
            "复现方式": "签发冻结" if signed else "当前基线",
            "基线": baseline,
        }

    def pending_reconciliations(self, asset: str | None = None) -> list[dict[str, Any]]:
        """核对待办：未结报告及其当前比较基线，随回滚与确认结果更新。"""
        todos: list[dict[str, Any]] = []
        for report in store.rows(self.MODULE):
            if report.get("status") not in self.UNSETTLED_STATUSES:
                continue
            if asset is not None and str(report.get("比较对象", "")) != asset:
                continue
            todos.append({
                "id": report.get("id"),
                "记录编号": report.get("记录编号", ""),
                "比较对象": report.get("比较对象", ""),
                "差异字段": report.get("差异字段", ""),
                "源值": report.get("源值", ""),
                "目标值": report.get("目标值", ""),
                "回滚基线": report.get("回滚基线", ""),
                "status": report.get("status", ""),
            })
        return todos

    # ── 兼容读取 ──────────────────────────────────────────────

    def find_report(self, key: Any) -> dict[str, Any] | None:
        """按 id 或比较编号读取差异报告，兼容历史比较编号格式。

        支持：id（如 3）、完整编号（DIFF-0003 / DIFF-CHG-0003，大小写不敏感）、
        以及尾部带数字的简写（如 DIFF-3、diff-chg-3）。
        """
        if key is None:
            return None
        text = str(key).strip()
        if not text:
            return None
        if text.isdigit():
            return store.find(self.MODULE, int(text))
        code = text.upper()
        for row in store.rows(self.MODULE):
            if str(row.get("记录编号", "")).upper() == code:
                return row
        match = re.search(r"(\d+)$", code)
        if match:
            return store.find(self.MODULE, int(match.group(1)))
        return None

    def get_reports_by_target(self, target: str) -> list[dict[str, Any]]:
        """获取指定对象的差异报告。"""
        return store.find_by_field(self.MODULE, "比较对象", target)

    # ── 内部工具 ──────────────────────────────────────────────

    def _coerce_report(self, report_or_id: Any) -> dict[str, Any] | None:
        if isinstance(report_or_id, dict):
            return report_or_id
        return self.find_report(report_or_id)

    def _asset_versions(self, asset: str) -> list[dict[str, Any]]:
        versions = [
            row for row in store.rows("version_history")
            if str(row.get("目标资产", "")) == asset
        ]
        versions.sort(key=lambda v: str(v.get("快照时间", "")))
        return versions

    @staticmethod
    def _field_delta(from_val: Any, to_val: Any) -> str:
        """计算两端字段差异：数值型给带符号差值，否则给前后对照。"""
        try:
            delta = float(str(to_val)) - float(str(from_val))
        except (TypeError, ValueError):
            return f"{from_val}→{to_val}"
        return f"{delta:+g}"
