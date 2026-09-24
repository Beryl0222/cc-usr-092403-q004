"""海外发行主线的领域规则测试。

覆盖:
- 发布批次冻结页面版本、史料许可、发行窗口、合作方授权与市场规则版本;
- 禁运/许可收缩只撤回命中的市场与素材, 其他市场不停;
- 既有批次保留原清单, 仅追加撤回通知、合作方回执与替代版本;
- 页面更正/规则变更/迟到回执乱序到达时按生效时间重算;
- 旧消息不复活已撤回(且已更正)内容, 同批次重放不重复通知;
- 导出只含当前范围确实允许的材料;
- 任一批次可反查冻结依据与每个合作方最后确认到的步骤。
"""

import unittest
from datetime import date, datetime

from domain import (
    License,
    PermissionDenied,
    StaleVersionError,
    StateError,
    ReviewSystem,
)

MGR = {"actor": "王经理", "role": "海外发行经理"}
EDITOR = {"actor": "王编辑", "role": "编辑"}
PARTNER = {"actor": "环球影业对接人", "role": "合作方"}
SCOPES = ("国内", "北美", "欧洲")


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = [datetime(2026, 9, 24, 9, 0, 0)]
        self.system = ReviewSystem(clock=lambda: self.clock[0])
        self.page, self.source = self._publishable_page("第3页 伏击", SCOPES)
        for market in ("北美", "欧洲"):
            self.system.create_market_rule(market=market, note="初版规则", **MGR)
            self.system.create_window(
                market=market, opens_at=date(2026, 9, 1),
                closes_at=date(2026, 10, 31), **MGR)
        self.partner = self.system.create_partner(
            name="环球影业", markets=("北美", "欧洲"), **MGR)
        self.pid = self.partner.id

    # ----- 夹具 -----

    def _publishable_page(self, title, scopes):
        source = self.system.register_source(
            title=f"{title}史料", citation="《八路军战史》第12页",
            license=License(frozenset(scopes)), **EDITOR)
        segment = self.system.create_segment(
            title=title, text="拂晓进入伏击阵地。", actor="学员甲")
        design = self.system.create_design(
            name="连长", brief="三十岁。", actor="学员甲")
        page = self.system.create_page(
            title=title,
            script_refs={segment.id: 1}, design_refs={design.id: 1},
            source_refs={source.id: 1}, actor="学员甲")
        for target in ("待评审", "精稿中", "可出版"):
            self.system.transition_page(page.id, target, **EDITOR)
        return page, source

    def _freeze_publish(self, markets=("北美", "欧洲")):
        batch = self.system.create_release_batch(
            markets=list(markets), partner_ids=[self.pid], **MGR)
        self.system.publish_batch(batch.id, **MGR)
        return batch

    def _line(self, batch, market):
        return next(l for l in batch.lines
                    if l.market == market and l.page_id == self.page.id)

    def _advance(self, day):
        self.clock[0] = datetime(day.year, day.month, day.day, 9, 0, 0)

    # ----- 批次冻结 -----

    def test_batch_freezes_versions_and_license_snapshot(self):
        batch = self._freeze_publish()
        trace = self.system.batch_trace(batch.id)
        # 冻结了窗口、规则、合作方授权版本
        self.assertEqual(trace["frozen_window"]["北美"]["version"], 1)
        self.assertEqual(trace["frozen_rules"]["北美"]["version"], 1)
        self.assertEqual(trace["frozen_grants"][self.pid]["version"], 1)
        line = self._line(batch, "北美")
        self.assertEqual(line.page_version, 1)
        # 每条素材线冻结了所采用页面版本下的史料许可快照
        snap = line.source_licenses[self.source.id]
        self.assertEqual(snap["revision"], 1)
        self.assertEqual(set(snap["publication_scopes"]), set(SCOPES))

        # 此后史料修订(许可变化), 冻结清单与快照不被改写
        self.system.revise_source(
            self.source.id, citation="新版出处",
            license=License(frozenset(("国内",))), base_revision=1, **EDITOR)
        trace = self.system.batch_trace(batch.id)
        frozen = next(l for l in trace["lines"] if l["line_id"] == line.id)
        self.assertEqual(frozen["frozen_page_version"], 1)
        self.assertEqual(
            frozen["source_licenses"][self.source.id]["revision"], 1)

    def test_freeze_excludes_unauthorized_or_blocked_material(self):
        # 合作方授权不含该市场: 冻结时即排除并留原因
        other = self.system.create_partner(
            name="欧陆发行", markets=("欧洲",), **MGR)
        batch = self.system.create_release_batch(
            markets=["北美", "欧洲"], partner_ids=[other.id], **MGR)
        # 仅欧洲授权成立, 北美被排除
        self.assertEqual(len(batch.lines), 1)
        self.assertEqual(batch.lines[0].market, "欧洲")
        self.assertIn(f"{other.id}:北美:{self.page.id}", batch.freeze_exclusions)
        reasons = batch.freeze_exclusions[f"{other.id}:北美:{self.page.id}"]
        self.assertTrue(any(r["type"] == "合作方授权" for r in reasons))

    def test_window_not_open_keeps_line_pending(self):
        self.system.create_window(
            market="东南亚", opens_at=date(2026, 10, 1),
            closes_at=date(2026, 11, 1), **MGR)
        self.system.create_market_rule(market="东南亚", note="r", **MGR)
        self.system.new_partner_version(
            self.pid, markets=("北美", "欧洲", "东南亚"),
            base_version=1, **MGR)
        # 史料许可也需含该市场
        self.system.revise_source(
            self.source.id, citation="c",
            license=License(frozenset(SCOPES + ("东南亚",))),
            base_revision=1, **EDITOR)
        # 引用新史料修订需先更正页面, 否则依赖陈旧
        self.system.new_page_version(
            self.page.id,
            script_refs={sid: 1 for sid in self.page.current.script_refs},
            design_refs={did: 1 for did in self.page.current.design_refs},
            source_refs={self.source.id: 2}, actor="学员甲", base_version=1)
        batch = self.system.create_release_batch(
            markets=["东南亚"], partner_ids=[self.pid], **MGR)
        line = batch.lines[0]
        self.assertEqual(line.status, "待发布")
        self.system.publish_batch(batch.id, on=date(2026, 9, 24), **MGR)
        self.assertEqual(line.status, "待发布")  # 窗口未开
        self._advance(date(2026, 10, 1))
        self.system.publish_batch(batch.id, **MGR)
        self.assertEqual(line.status, "已发布")

    # ----- 禁运: 只撤回命中市场, 其他市场不停 -----

    def test_embargo_after_download_withdraws_only_hit_market(self):
        batch = self._freeze_publish()
        na, eu = self._line(batch, "北美"), self._line(batch, "欧洲")
        # 合作方在禁运前已下载交付物
        self.system.record_receipt(
            batch.id, na.id, step="已下载",
            effective_at=date(2026, 9, 23), **PARTNER)

        self._advance(date(2026, 9, 25))
        self.system.issue_embargo(
            market="北美", reason="一纸地区禁运通知",
            effective_at=date(2026, 9, 25), **MGR)

        self.assertEqual(na.status, "已撤回")
        self.assertEqual(eu.status, "已发布")  # 其他市场不停
        # 追加了撤回通知, 冻结的素材线本身仍在清单中
        types = [n.type for n in batch.notices]
        self.assertEqual(types.count("撤回通知"), 1)
        withdraw = next(n for n in batch.notices if n.type == "撤回通知")
        self.assertEqual(withdraw.line_id, na.id)
        self.assertTrue(any(b["type"] == "禁运" for b in na.barriers))
        # 欧洲市场导出不受影响, 北美导出为空
        self.assertEqual(
            len(self.system.export_release(batch.id, market="欧洲", **MGR)["materials"]),
            1)
        self.assertEqual(
            self.system.export_release(batch.id, market="北美", **MGR)["materials"],
            [])

    def test_embargo_targeting_specific_pages_spares_other_pages(self):
        page2, _ = self._publishable_page("第4页 突围", SCOPES)
        batch = self._freeze_publish()
        line_a = self._line(batch, "北美")
        line_b = next(l for l in batch.lines
                      if l.market == "北美" and l.page_id == page2.id)
        self.system.issue_embargo(
            market="北美", reason="仅个别页禁运",
            effective_at=date(2026, 9, 24), page_ids=[self.page.id], **MGR)
        self.assertEqual(line_a.status, "已撤回")
        self.assertEqual(line_b.status, "已发布")

    # ----- 解禁不复活已更正页面 -----

    def test_lift_embargo_revives_unchanged_material_without_duplicate_notice(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        embargo = self.system.issue_embargo(
            market="北美", reason="临时禁运", effective_at=date(2026, 9, 24),
            **MGR)
        self.assertEqual(na.status, "已撤回")
        notices_before = len(batch.notices)
        self._advance(date(2026, 9, 26))
        self.system.lift_embargo(embargo.id, lifted_at=date(2026, 9, 26), **MGR)
        self.assertEqual(na.status, "已发布")  # 版本未变, 恢复
        # 解禁恢复不产生新通知
        self.assertEqual(len(batch.notices), notices_before)
        # 同批次重放不重复通知
        self.system.recalculate_batch(batch.id, on=date(2026, 9, 26), **MGR)
        self.assertEqual(len(batch.notices), notices_before)

    def test_lift_embargo_does_not_revive_corrected_page(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        embargo = self.system.issue_embargo(
            market="北美", reason="地区禁运", effective_at=date(2026, 9, 24),
            **MGR)
        # 撤回期间页面被更正为 v2: 旧线保持撤回, 不自动上线
        self.system.new_page_version(
            self.page.id,
            script_refs=dict(self.page.current.script_refs),
            design_refs=dict(self.page.current.design_refs),
            source_refs=dict(self.page.current.source_refs),
            actor="学员甲", base_version=1)
        self.assertEqual(na.status, "已撤回")
        self.assertEqual(na.page_version, 1)

        # 解禁: 旧 v1 不得复活
        self._advance(date(2026, 9, 26))
        self.system.lift_embargo(embargo.id, lifted_at=date(2026, 9, 26), **MGR)
        self.assertEqual(na.status, "已撤回")

        # 经理追加采用更正版本的替代线, 旧线转「已替代」
        new_line = self.system.provide_alternative(batch.id, na.id, **MGR)
        self.assertEqual(new_line.page_version, 2)
        self.assertEqual(new_line.replaces, na.id)
        self.assertEqual(na.status, "已替代")
        self.assertEqual(na.replaced_by, new_line.id)
        self.system.publish_batch(batch.id, on=date(2026, 9, 26), **MGR)
        self.assertEqual(new_line.status, "已发布")
        # 新线发出替代通知(仅一次)
        alt = [n for n in batch.notices if n.type == "替代通知"]
        self.assertEqual(len(alt), 1)
        self.assertEqual(alt[0].line_id, new_line.id)
        # 导出只返回当前允许的 v2
        result = self.system.export_release(batch.id, market="北美", **MGR)
        self.assertEqual([m["page_version"] for m in result["materials"]], [2])
        self.assertEqual(
            [m["line_id"] for m in result["materials"]], [new_line.id])

    # ----- 许可收缩 -----

    def test_license_shrink_withdraws_hit_market_and_restore_revives(self):
        batch = self._freeze_publish()
        eu = self._line(batch, "欧洲")
        self.system.revise_source(
            self.source.id, citation="c",
            license=License(frozenset(("国内", "北美"))),
            base_revision=1, **EDITOR)
        self.assertEqual(eu.status, "已撤回")
        result = self.system.export_release(batch.id, market="欧洲", **MGR)
        self.assertIn(eu.id, result["excluded"])
        self.assertTrue(any("出版范围" in r for r in result["excluded"][eu.id]))
        # 许可恢复后, 原版本(未更正)恢复发布
        self.system.revise_source(
            self.source.id, citation="c",
            license=License(frozenset(SCOPES)),
            base_revision=2, **EDITOR)
        self.assertEqual(eu.status, "已发布")

    # ----- 规则变更按生效时间重算 -----

    def test_rule_version_change_withdraws_blocked_page(self):
        batch = self._freeze_publish()
        eu = self._line(batch, "欧洲")
        rule = next(r for r in self.system.market_rules.values()
                    if r.market == "欧洲")
        self.system.new_rule_version(
            rule.id, note="新规封禁该页", blocked_pages=[self.page.id],
            base_version=1, effective_at=date(2026, 9, 24), **MGR)
        self.assertEqual(eu.status, "已撤回")
        self.assertTrue(any(b["type"] == "市场规则" for b in eu.barriers))

    def test_stale_rule_version_rejected(self):
        rule = next(r for r in self.system.market_rules.values()
                    if r.market == "欧洲")
        with self.assertRaises(StaleVersionError):
            self.system.new_rule_version(
                rule.id, note="x", base_version=99, **MGR)

    def test_future_effective_rule_does_not_withdraw_before_its_time(self):
        batch = self._freeze_publish()
        eu = self._line(batch, "欧洲")
        rule = next(r for r in self.system.market_rules.values()
                    if r.market == "欧洲")
        self.system.new_rule_version(
            rule.id, note="未来才封禁", blocked_pages=[self.page.id],
            base_version=1, effective_at=date(2026, 10, 15), **MGR)
        self.assertEqual(eu.status, "已发布")  # 尚未生效
        self.system.recalculate_batch(batch.id, on=date(2026, 10, 15), **MGR)
        self.assertEqual(eu.status, "已撤回")

    # ----- 合作方授权收缩 -----

    def test_partner_grant_shrink_withdraws_removed_market(self):
        batch = self._freeze_publish()
        eu = self._line(batch, "欧洲")
        self.system.new_partner_version(
            self.pid, markets=("北美",), base_version=1,
            effective_at=date(2026, 9, 24), **MGR)
        self.assertEqual(eu.status, "已撤回")
        self.assertTrue(any(b["type"] == "合作方授权" for b in eu.barriers))

    # ----- 乱序/迟到回执: 标记但不改状态, 不复活 -----

    def test_late_receipts_flagged_and_never_change_status(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        embargo = self.system.issue_embargo(
            market="北美", reason="禁运", effective_at=date(2026, 9, 24),
            **MGR)
        # 撤回确认先到
        confirm = self.system.record_receipt(
            batch.id, na.id, step="撤回已确认",
            effective_at=date(2026, 9, 25), **PARTNER)
        # 早先的"已下载"回执迟到
        late = self.system.record_receipt(
            batch.id, na.id, step="已下载",
            effective_at=date(2026, 9, 23), **PARTNER)
        self.assertFalse(confirm.late)
        self.assertTrue(late.late)
        self.assertEqual(na.status, "已撤回")
        # 推进到解禁日: 迟到的旧"已下载"回执不影响状态, 恢复仅由壁垒清除决定
        self._advance(date(2026, 9, 26))
        self.system.lift_embargo(embargo.id, lifted_at=date(2026, 9, 26), **MGR)
        self.assertEqual(na.status, "已发布")  # 版本未变, 解禁恢复
        # 重放同一回执不重复登记
        again = self.system.record_receipt(
            batch.id, na.id, step="已下载",
            effective_at=date(2026, 9, 23), **PARTNER)
        self.assertEqual(again.id, late.id)
        self.assertEqual(len(na.receipts), 2)

    # ----- 手工追加替代版本 -----

    def test_provide_alternative_only_for_withdrawn_corrected_line(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        self.system.issue_embargo(
            market="北美", reason="禁运", effective_at=date(2026, 9, 24),
            **MGR)
        # 尚无更正版本时不能追加替代
        with self.assertRaises(Exception):
            self.system.provide_alternative(batch.id, na.id, **MGR)
        self.system.new_page_version(
            self.page.id,
            script_refs=dict(self.page.current.script_refs),
            design_refs=dict(self.page.current.design_refs),
            source_refs=dict(self.page.current.source_refs),
            actor="学员甲", base_version=1)
        # 更正后旧线仍保持撤回(不自动复活), 由经理显式追加替代
        self.assertEqual(na.status, "已撤回")
        new_line = self.system.provide_alternative(batch.id, na.id, **MGR)
        self.assertEqual(na.status, "已替代")
        self.assertEqual(new_line.replaces, na.id)
        self.assertEqual(new_line.page_version, 2)

    def test_provide_alternative_on_withdrawn_uncorrected_then_correct(self):
        # 许可收缩导致撤回(页面未更正): 更正后允许手工追加替代线
        batch = self._freeze_publish()
        eu = self._line(batch, "欧洲")
        self.system.revise_source(
            self.source.id, citation="c",
            license=License(frozenset(("国内", "北美"))),
            base_revision=1, **EDITOR)
        self.assertEqual(eu.status, "已撤回")
        self.system.new_page_version(
            self.page.id,
            script_refs=dict(self.page.current.script_refs),
            design_refs=dict(self.page.current.design_refs),
            source_refs=dict(self.page.current.source_refs),
            actor="学员甲", base_version=1)
        # 注意: 许可仍不含欧洲, 新线即使替代也不能发布, 直到许可恢复
        alternative = self.system.provide_alternative(batch.id, eu.id, **MGR)
        self.assertEqual(eu.status, "已替代")
        self.assertEqual(alternative.replaces, eu.id)
        self.assertEqual(alternative.page_version, 2)

    def test_pending_line_corrected_before_publish_sends_publish_notice(self):
        # 冻结前把北美窗口改到未来: 素材保持待发布
        window = next(w for w in self.system.windows.values() if w.market == "北美")
        self.system.new_window_version(
            window.id, opens_at=date(2026, 10, 1), closes_at=date(2026, 11, 1),
            base_version=1, **MGR)
        batch = self.system.create_release_batch(
            markets=["北美"], partner_ids=[self.pid], **MGR)
        line = self._line(batch, "北美")
        self.assertEqual(line.status, "待发布")
        # 待发布期间更正为 v2: 自动改用新版本
        self.system.new_page_version(
            self.page.id,
            script_refs=dict(self.page.current.script_refs),
            design_refs=dict(self.page.current.design_refs),
            source_refs=dict(self.page.current.source_refs),
            actor="学员甲", base_version=1)
        self.assertEqual(line.status, "已替代")
        fresh = next(l for l in batch.lines if l.replaces == line.id)
        self.assertEqual(fresh.page_version, 2)
        self.assertEqual(fresh.status, "待发布")
        self._advance(date(2026, 10, 1))
        self.system.publish_batch(batch.id, **MGR)
        self.assertEqual(fresh.status, "已发布")
        # 合作方从未收到旧版, 首发只发"发布通知", 不发"替代通知"
        self.assertTrue(any(n.type == "发布通知" and n.line_id == fresh.id
                            for n in batch.notices))
        self.assertFalse(any(n.type == "替代通知" for n in batch.notices))

    # ----- 导出: 仅当前范围允许的材料 -----

    def test_export_excludes_withdrawn_and_pending_with_reasons(self):
        batch = self._freeze_publish()
        na, eu = self._line(batch, "北美"), self._line(batch, "欧洲")
        self.system.issue_embargo(
            market="北美", reason="禁运", effective_at=date(2026, 9, 24),
            **MGR)
        result = self.system.export_release(batch.id, market="北美", **MGR)
        self.assertEqual(result["materials"], [])
        self.assertIn(na.id, result["excluded"])
        # 撤回期间页面更正, 旧线仍撤回并继续被排除(不复活)
        self.system.new_page_version(
            self.page.id,
            script_refs=dict(self.page.current.script_refs),
            design_refs=dict(self.page.current.design_refs),
            source_refs=dict(self.page.current.source_refs),
            actor="学员甲", base_version=1)
        result = self.system.export_release(batch.id, market="北美", **MGR)
        self.assertNotIn(na.id, result["materials"])
        self.assertIn(na.id, result["excluded"])
        # 经理追加替代后, 旧线转为「已替代」, 彻底不进入导出
        new_line = self.system.provide_alternative(batch.id, na.id, **MGR)
        result = self.system.export_release(batch.id, market="北美", **MGR)
        self.assertNotIn(na.id, result["materials"])
        self.assertNotIn(na.id, result["excluded"])
        self.assertIn(new_line.id, result["excluded"])  # 新线尚未发布

    # ----- 反查: 冻结依据与合作方确认进度 -----

    def test_batch_trace_reports_basis_and_partner_last_step(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        self.system.record_receipt(
            batch.id, na.id, step="已接收",
            effective_at=date(2026, 9, 22), **PARTNER)
        self.system.record_receipt(
            batch.id, na.id, step="已下载",
            effective_at=date(2026, 9, 23), **PARTNER)
        trace = self.system.batch_trace(batch.id)
        # 合作方最后确认到的步骤
        partner = next(p for p in trace["partners"]
                       if p["partner_id"] == self.pid)
        self.assertEqual(partner["last_step"], "已下载")
        line_view = next(l for l in trace["lines"] if l["line_id"] == na.id)
        self.assertEqual(line_view["last_step"], "已下载")
        self.assertEqual(len(line_view["receipts"]), 2)
        # 规则依据反查: 冻结的规则 id 与市场规则集一致
        rule_id = next(r.id for r in self.system.market_rules.values()
                       if r.market == "北美")
        self.assertEqual(trace["frozen_rules"]["北美"]["id"], rule_id)
        self.assertEqual(trace["frozen_grants"][self.pid]["id"], self.pid)
        self.assertIn("frozen_window", trace)
        self.assertEqual(trace["frozen_window"]["北美"]["id"],
                         batch.window["北美"]["id"])

    # ----- 权限 -----

    def test_release_operations_require_release_role(self):
        with self.assertRaises(PermissionDenied):
            self.system.create_market_rule(
                market="X", note="x", actor="学员甲", role="学员")
        with self.assertRaises(PermissionDenied):
            self.system.create_release_batch(
                markets=["北美"], partner_ids=[self.pid],
                actor="学员甲", role="学员")
        with self.assertRaises(PermissionDenied):
            self.system.issue_embargo(
                market="北美", reason="x", effective_at=date(2026, 9, 24),
                actor="学员甲", role="学员")
        with self.assertRaises(PermissionDenied):
            self.system.export_release(
                "RL-x", market="北美", actor="学员甲", role="学员")

    def test_partner_role_may_only_record_receipts(self):
        batch = self._freeze_publish()
        na = self._line(batch, "北美")
        receipt = self.system.record_receipt(
            batch.id, na.id, step="已接收", **PARTNER)
        self.assertEqual(receipt.step, "已接收")
        with self.assertRaises(PermissionDenied):
            self.system.issue_embargo(
                market="北美", reason="x", effective_at=date(2026, 9, 24),
                **PARTNER)


if __name__ == "__main__":
    unittest.main()
