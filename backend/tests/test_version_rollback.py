"""版本回滚与差异报告基线测试。

场景锚点：库存量 60 → 59 → 65 三版快照，当前有一张未签的差异单和一张
已签的月结单。恢复第一版后，未签单仍能核对实物差异，月结单按当时两端
复现原来的盘差。

关键原则：
- 每个测试使用唯一的资产编号避免相互干扰
- 使用 store.reset() 确保每个测试干净启动
- 通过业务字段查找新建的记录，不硬编码 entry_id
"""
import app.store as store_mod
from app.services.change_capture import ChangeCaptureService
from app.services.diff_report import DiffReportService
from app.services.version_history import VersionHistoryService


def _fresh():
    """重置存储并返回服务实例字典。"""
    store_mod.store.reset()
    return {
        "version_history": VersionHistoryService(),
        "diff_report": DiffReportService(),
        "change_capture": ChangeCaptureService(),
    }


def _make_inventory_versions(s, asset, quantities=("60", "59", "65")):
    """为资产建立库存量相继版本快照，返回快照行列表。"""
    versions = []
    for idx, qty in enumerate(quantities, start=1):
        result = s["version_history"].create_version(
            asset, f"v{idx}", "盘点快照",
            row_count=qty, field_values={"库存量": qty},
        )
        versions.append(result["version"])
    return versions


# ══════════════════════════════════════════════════════════════════
# 资产恢复与回滚比较基线
# ══════════════════════════════════════════════════════════════════

class TestRestoreFormsRollbackBaseline:

    def test_restore_first_version_forms_baseline(self):
        """恢复第一版：版本状态只切本资产，未签单重锚基线，已签月结单不动。"""
        s = _fresh()
        asset = "INV-RB-001"
        v1, v2, v3 = _make_inventory_versions(s, asset)
        # 未签差异单：v2 → v3 的库存差异
        unsigned = s["diff_report"].generate_report(asset, "库存盘点", "库存量", "59", "65")
        # 已签月结单：v1 → v2 的盘差
        monthly = s["diff_report"].generate_report(asset, "月结盘差", "库存量", "60", "59")
        s["diff_report"].confirm_diff(monthly["id"])

        result = s["version_history"].restore_version(v1["id"])
        assert result["applied"] is True
        assert result["rollback"]["目标资产"] == asset

        # 版本状态切换只发生在本资产三版上
        assert s["version_history"].get_entry(v1["id"])["status"] == "当前版本"
        assert s["version_history"].get_entry(v2["id"])["status"] == "历史版本"
        assert s["version_history"].get_entry(v3["id"])["status"] == "历史版本"

        # 未签单重锚到回滚基线：基线端回到 60，实物端 65 保留，仍可核对
        after = s["diff_report"].get_entry(unsigned["id"])
        assert after["源值"] == "60"
        assert after["目标值"] == "65"
        assert after["回滚基线"].endswith("@v1")
        assert after["status"] == "已生成"
        assert after["pending"] is True

        # 已签月结单前后值不变、不带回滚基线
        signed = s["diff_report"].get_entry(monthly["id"])
        assert signed["源值"] == "60"
        assert signed["目标值"] == "59"
        assert "回滚基线" not in signed

    def test_unsigned_confirmable_after_restore_and_monthly_reproduces(self):
        """恢复第一版后：未签单仍能核对实物差异，月结单按当时两端复现盘差。"""
        s = _fresh()
        asset = "INV-RB-002"
        v1, _, _ = _make_inventory_versions(s, asset)
        unsigned = s["diff_report"].generate_report(asset, "库存盘点", "库存量", "59", "65")
        monthly = s["diff_report"].generate_report(asset, "月结盘差", "库存量", "60", "59")
        s["diff_report"].confirm_diff(monthly["id"])

        s["version_history"].restore_version(v1["id"])

        # 未签单仍能核对实物差异：确认后按回滚基线冻结
        confirmed = s["diff_report"].confirm_diff(unsigned["id"])
        assert confirmed["status"] == "已确认"
        assert confirmed["确认基线"].endswith("@v1")
        assert confirmed["确认时差异"] == "+5"  # 实物 65 - 恢复后账面 60
        assert confirmed["pending"] is False

        # 月结单按当时两端重新得到原来的盘差（60 → 59，盘差 -1）
        replay = s["diff_report"].reproduce_report(monthly["id"])
        assert replay["复现方式"] == "签发冻结"
        assert replay["源值"] == "60"
        assert replay["目标值"] == "59"
        assert replay["差异"] == "-1"


# ══════════════════════════════════════════════════════════════════
# 幂等与重算范围
# ══════════════════════════════════════════════════════════════════

class TestRollbackIdempotencyAndScope:

    def test_same_rollback_submitted_twice_applies_once(self):
        """同时提交两次相同回滚只生效一次，且不得再次改写未结报告。"""
        s = _fresh()
        asset = "INV-RB-003"
        v1, _, _ = _make_inventory_versions(s, asset)
        unsigned = s["diff_report"].generate_report(asset, "库存盘点", "库存量", "59", "65")

        first = s["version_history"].restore_version(v1["id"])
        second = s["version_history"].restore_version(v1["id"])

        assert first["applied"] is True
        assert second["applied"] is False
        assert second["deduplicated"] is True
        assert second["rollback"]["回滚编号"] == first["rollback"]["回滚编号"]
        assert len(store_mod.store.rollbacks(asset)) == 1

        # 第一次回滚后报告被核对过程改写，第二次相同回滚不得再次改写
        report = s["diff_report"].get_entry(unsigned["id"])
        report["源值"] = "61"
        again = s["version_history"].restore_version(v1["id"])
        assert again["deduplicated"] is True
        assert s["diff_report"].get_entry(unsigned["id"])["源值"] == "61"

    def test_rollback_scope_limited_to_target_asset(self):
        """回滚不能改写其他资产的版本状态与报告前后值。"""
        s = _fresh()
        asset_a, asset_b = "INV-RB-004A", "INV-RB-004B"
        a1, a2, a3 = _make_inventory_versions(s, asset_a)
        b_versions = _make_inventory_versions(s, asset_b, quantities=("10", "11", "12"))
        report_b = s["diff_report"].generate_report(asset_b, "库存盘点", "库存量", "11", "12")
        signed_b = s["diff_report"].generate_report(asset_b, "月结盘差", "库存量", "10", "11")
        s["diff_report"].confirm_diff(signed_b["id"])

        result = s["version_history"].restore_version(a1["id"])

        # 重算范围只含资产 A 的三版快照
        scope = result["rollback"]["重算范围"]
        assert set(scope["版本快照"]) == {a1["id"], a2["id"], a3["id"]}

        # 资产 B 的版本状态与报告前后值完全不变
        for version in b_versions:
            assert s["version_history"].get_entry(version["id"])["status"] == "历史版本"
        open_b = s["diff_report"].get_entry(report_b["id"])
        assert open_b["源值"] == "11" and open_b["目标值"] == "12"
        assert "回滚基线" not in open_b
        frozen_b = s["diff_report"].get_entry(signed_b["id"])
        assert frozen_b["源值"] == "10" and frozen_b["目标值"] == "11"
        assert store_mod.store.rollbacks(asset_b) == []

    def test_create_version_snapshots_only_version_chain(self):
        """创建快照的存储级重算范围收窄到版本/差异链路。"""
        s = _fresh()
        result = s["version_history"].create_version("INV-RB-006", "v1", "盘点")
        assert result["store_snapshot"]["modules"] == ["version_history", "diff_report"]

    def test_current_version_pointer_follows_restore(self):
        """恢复后当前版本指针指向被恢复的版本。"""
        s = _fresh()
        asset = "INV-RB-007"
        v1, _, _ = _make_inventory_versions(s, asset)
        s["version_history"].restore_version(v1["id"])
        current = s["version_history"].get_current_version(asset)
        assert current["id"] == v1["id"]
        assert current["字段快照"]["库存量"] == "60"


# ══════════════════════════════════════════════════════════════════
# 回退与差异确认的冲突
# ══════════════════════════════════════════════════════════════════

class TestRollbackConfirmConflict:

    def test_rollback_and_confirm_conflict_same_asset(self):
        """同一资产先确认一张、再回滚：已签单冻结，未结单继续工作。"""
        s = _fresh()
        asset = "INV-RB-008"
        v1, _, _ = _make_inventory_versions(s, asset)
        confirmed_first = s["diff_report"].generate_report(asset, "盘点差异A", "库存量", "60", "59")
        s["diff_report"].confirm_diff(confirmed_first["id"])
        still_open = s["diff_report"].generate_report(asset, "盘点差异B", "库存量", "59", "65")

        s["version_history"].restore_version(v1["id"])

        # 先确认的已签单不被回滚改写
        frozen = s["diff_report"].get_entry(confirmed_first["id"])
        assert frozen["源值"] == "60" and frozen["目标值"] == "59"
        assert "回滚基线" not in frozen

        # 未结单重锚后仍可确认，确认时按回滚基线冻结
        open_report = s["diff_report"].get_entry(still_open["id"])
        assert open_report["源值"] == "60"
        done = s["diff_report"].confirm_diff(still_open["id"])
        assert done["status"] == "已确认"
        assert done["确认时差异"] == "+5"

    def test_confirm_signed_report_is_idempotent(self):
        """已签发报告重复确认幂等返回，前后值不被改写。"""
        s = _fresh()
        asset = "INV-RB-009"
        report = s["diff_report"].generate_report(asset, "月结盘差", "库存量", "60", "59")
        s["diff_report"].confirm_diff(report["id"])
        again = s["diff_report"].confirm_diff(report["id"])
        assert again["status"] == "已确认"
        assert again["源值"] == "60" and again["目标值"] == "59"


# ══════════════════════════════════════════════════════════════════
# 历史取数链路、核对待办与兼容读取
# ══════════════════════════════════════════════════════════════════

class TestBaselineChainAndCompat:

    def test_resolve_baseline_follows_change_capture_chain(self):
        """沿着历史取数链路定位差异报告的基线。"""
        s = _fresh()
        asset = "INV-RB-010"
        result = s["change_capture"].capture_change(asset, "库存变更", before="60", after="59")
        diff_id = result["diff_report"]["diff_id"]

        baseline = s["diff_report"].resolve_baseline(diff_id)
        assert baseline["基线标识"] == f"change#{result['change']['id']}"
        assert baseline["源值"] == "60"
        assert baseline["目标值"] == "59"
        stages = [step["环节"] for step in baseline["链路"]]
        assert stages[0] == "diff_report"
        assert "change_capture" in stages
        assert "version_history" in stages

    def test_pending_reconciliations_update_with_rollback(self):
        """核对待办随回滚结果更新，确认后退出待办。"""
        s = _fresh()
        asset = "INV-RB-011"
        v1, _, _ = _make_inventory_versions(s, asset)
        open_report = s["diff_report"].generate_report(asset, "库存盘点", "库存量", "59", "65")
        signed = s["diff_report"].generate_report(asset, "月结盘差", "库存量", "60", "59")
        s["diff_report"].confirm_diff(signed["id"])

        # 待办只含未签单
        todos = s["diff_report"].pending_reconciliations(asset)
        assert [t["记录编号"] for t in todos] == [open_report["记录编号"]]

        # 回滚后待办仍在，且带上回滚基线
        s["version_history"].restore_version(v1["id"])
        todos = s["diff_report"].pending_reconciliations(asset)
        assert len(todos) == 1
        assert todos[0]["回滚基线"].endswith("@v1")
        assert todos[0]["源值"] == "60"

        # 确认后退出待办
        s["diff_report"].confirm_diff(open_report["id"])
        assert s["diff_report"].pending_reconciliations(asset) == []

    def test_find_report_compatible_with_historical_codes(self):
        """读取兼容历史的比较编号：DIFF-xxxx 与 DIFF-CHG-xxxx 等格式。"""
        s = _fresh()
        asset = "INV-RB-012"
        generated = s["diff_report"].generate_report(asset, "库存盘点", "库存量", "59", "65")
        chg = s["change_capture"].capture_change(asset, "库存变更", before="60", after="59")
        chg_report = s["diff_report"].get_entry(chg["diff_report"]["diff_id"])

        svc = s["diff_report"]
        # 完整编号（两种历史格式），大小写不敏感
        assert svc.find_report(generated["记录编号"])["id"] == generated["id"]
        assert svc.find_report(chg_report["记录编号"])["id"] == chg_report["id"]
        assert svc.find_report(generated["记录编号"].lower())["id"] == generated["id"]
        # 纯 id 与尾部数字简写
        assert svc.find_report(generated["id"])["id"] == generated["id"]
        assert svc.find_report(f"DIFF-{generated['id']}")["id"] == generated["id"]
        assert svc.find_report(f"diff-chg-{chg_report['id']}")["id"] == chg_report["id"]
        assert svc.find_report("DIFF-9999") is None


# ══════════════════════════════════════════════════════════════════
# 三版相继场景：未结报告跨版本核对
# ══════════════════════════════════════════════════════════════════

class TestUnsettledReportAcrossThreeVersions:

    def test_spanning_report_confirmable_after_restore(self):
        """三个相继版本上的未结报告：恢复第一版后比较确认前后的字段差异。"""
        s = _fresh()
        asset = "INV-RB-013"
        v1, _, _ = _make_inventory_versions(s, asset)
        # 未结报告开在第三版：账面 65 vs 实物 58
        spanning = s["diff_report"].generate_report(asset, "三版对照", "库存量", "65", "58")
        extra = s["diff_report"].generate_report(asset, "复盘", "库存量", "59", "60")

        before = s["diff_report"].reproduce_report(spanning["id"])
        assert before["差异"] == "-7"  # 58 - 65

        s["version_history"].restore_version(v1["id"])

        # 恢复首版后基线端重锚为 60，实物端 58 保留，差异随之更新
        mid = s["diff_report"].get_entry(spanning["id"])
        assert mid["源值"] == "60" and mid["目标值"] == "58"
        assert s["diff_report"].reproduce_report(spanning["id"])["差异"] == "-2"

        # 确认后冻结，复现按签发时两端
        confirmed = s["diff_report"].confirm_diff(spanning["id"])
        assert confirmed["确认时差异"] == "-2"
        after = s["diff_report"].reproduce_report(spanning["id"])
        assert after["复现方式"] == "签发冻结"
        assert after["差异"] == "-2"

        # 仍可继续核对的范围：另一张未结单仍在待办中
        remaining = s["diff_report"].pending_reconciliations(asset)
        assert [t["记录编号"] for t in remaining] == [extra["记录编号"]]
        assert remaining[0]["回滚基线"].endswith("@v1")

    def test_restore_action_via_generic_action_route(self):
        """通过通用动作通道触发恢复版本，重复提交幂等。"""
        s = _fresh()
        asset = "INV-RB-014"
        v1, _, _ = _make_inventory_versions(s, asset)

        entry, message = s["version_history"].run_action(v1["id"], "恢复版本")
        assert entry is not None
        assert entry["status"] == "当前版本"
        assert "已恢复到版本v1" in message

        entry2, message2 = s["version_history"].run_action(v1["id"], "恢复版本")
        assert entry2 is not None
        assert "未重复生效" in message2
        assert len(store_mod.store.rollbacks(asset)) == 1
