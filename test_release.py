"""海外发行与禁运撤回的领域规则测试。"""

import unittest
from datetime import date, datetime

from domain import (
    License,
    PermissionDenied,
    ReviewSystem,
    StateError,
    DomainError,
)
from release import PartnerGrant, ReleaseSystem

EDITOR = {"actor": "王编辑", "role": "编辑"}
MANAGER = {"actor": "林经理", "role": "发行经理"}
START = datetime(2026, 9, 25, 8, 0, 0)
WINDOW = {"window_start": date(2026, 10, 1), "window_end": date(2026, 12, 31)}


class Clock:
    def __init__(self, now):
        self._now = now

    def __call__(self):
        return self._now

    def set(self, now):
        self._now = now


def lic(scopes, expires=None, confidential=None):
    return License(
        publication_scopes=frozenset(scopes),
        expires_at=expires,
        confidential_until=confidential,
    )


class ReleaseSystemTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(START)
        self.review = ReviewSystem(clock=self.clock)
        self.release = ReleaseSystem(self.review, clock=self.clock)
        self.source_a = self.review.register_source(
            title="平型关战斗详报", citation="《八路军战史》第12页",
            license=lic(("东南亚", "欧洲")), **EDITOR)
        self.source_b = self.review.register_source(
            title="老兵回忆录", citation="《回忆》第3页",
            license=lic(("东南亚", "欧洲")), **EDITOR)
        self.segment = self.review.create_segment(
            title="夜袭", text="拂晓进入阵地。", actor="学员甲")
        self.design = self.review.create_design(
            name="连长", brief="三十岁。", actor="学员甲")
        self.page_a = self._publishable_page("第3页 伏击", self.source_a)
        self.page_b = self._publishable_page("第4页 转移", self.source_b)

    def _publishable_page(self, title, source):
        page = self.review.create_page(
            title=title, script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1}, source_refs={source.id: 1},
            actor="学员甲")
        for target in ("待评审", "精稿中", "可出版"):
            self.review.transition_page(page.id, target, **EDITOR)
        return page

    def _grants(self, markets=("东南亚", "欧洲")):
        return [
            PartnerGrant("合作方甲", frozenset(markets), date(2026, 1, 1)),
            PartnerGrant("合作方乙", frozenset(markets), date(2026, 1, 1)),
        ]

    def _batch(self, pages=None, markets=("东南亚", "欧洲"), grants=None):
        return self.release.create_release_batch(
            title="海外首批", markets=list(markets),
            page_ids=[p.id for p in (pages or [self.page_a, self.page_b])],
            partner_grants=grants if grants is not None else self._grants(markets),
            **WINDOW, **MANAGER)

    def _cell(self, batch, market, page):
        return self.release.batches[batch.id].cells[f"{market}|{page.id}"]

    def _notices(self, batch, kind=None):
        notices = self.release.batches[batch.id].notices
        return [n for n in notices if kind is None or n.kind == kind]

    # ----- 批次冻结 -----

    def test_batch_freezes_pages_licenses_window_grants_and_rules(self):
        self.release.register_market_rule(
            market="东南亚", banned_sources=[], note="初版规则", **MANAGER)
        batch = self._batch()
        # 冻结后: 页面更正、许可收缩、规则变更都不改写原清单
        self.review.new_page_version(
            self.page_a.id, script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1}, source_refs={self.source_a.id: 1},
            actor="学员甲", base_version=1)
        self.review.revise_source(
            self.source_a.id, citation="《战史》p12(修订)",
            license=lic(("欧洲",)), base_revision=1, **EDITOR)
        self.release.register_market_rule(
            market="东南亚", banned_sources=[self.source_b.id], note="第二版", **MANAGER)
        frozen = self.release.batches[batch.id]
        self.assertEqual(frozen.entries[0].page_version, 1)
        self.assertEqual(frozen.entries[0].source_refs, {self.source_a.id: 1})
        snapshot = frozen.license_snapshot[self.source_a.id]
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["license"].publication_scopes,
                         frozenset({"东南亚", "欧洲"}))
        self.assertEqual(frozen.rule_versions["东南亚"], 1)
        self.assertEqual(
            self.release.rules["东南亚"].versions[-1].version, 2)  # 现行已是v2
        self.assertEqual(frozen.window_start, date(2026, 10, 1))
        self.assertEqual(frozen.window_end, date(2026, 12, 31))
        self.assertIn("合作方甲", frozen.partner_grants)

    # ----- 禁运: 只改变命中的市场与素材 -----

    def test_embargo_withdraws_only_hit_market_and_materials(self):
        batch = self._batch()
        self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id],
            reason="地区禁运通知", **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        self.assertEqual(self._cell(batch, "东南亚", self.page_b).state, "可用")
        self.assertEqual(self._cell(batch, "欧洲", self.page_a).state, "可用")
        notices = self._notices(batch, "撤回")
        self.assertEqual({n.partner for n in notices}, {"合作方甲", "合作方乙"})
        self.assertEqual({n.page_id for n in notices}, {self.page_a.id})
        self.assertEqual({n.market for n in notices}, {"东南亚"})
        # 既有批次保留原清单, 撤回以追加通知的形式记录
        self.assertEqual(
            [e.page_version for e in self.release.batches[batch.id].entries], [1, 1])

    def test_blanket_embargo_hits_all_materials_in_market(self):
        batch = self._batch()
        self.release.register_embargo(market="东南亚", reason="全面禁运", **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        self.assertEqual(self._cell(batch, "东南亚", self.page_b).state, "已撤回")
        self.assertEqual(self._cell(batch, "欧洲", self.page_a).state, "可用")

    # ----- 许可收缩 -----

    def test_license_contraction_hits_only_lost_markets(self):
        batch = self._batch()
        self.review.revise_source(
            self.source_a.id, citation="《战史》p12(修订)",
            license=lic(("欧洲",)), base_revision=1, **EDITOR)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        self.assertEqual(self._cell(batch, "欧洲", self.page_a).state, "可用")
        self.assertEqual(self._cell(batch, "东南亚", self.page_b).state, "可用")

    # ----- 市场规则变更 -----

    def test_market_rule_change_withdraws_and_restores(self):
        batch = self._batch()
        self.release.register_market_rule(
            market="东南亚", banned_sources=[self.source_a.id],
            note="禁用战报类素材", **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        self.assertEqual(self._cell(batch, "欧洲", self.page_a).state, "可用")
        self.release.register_market_rule(
            market="东南亚", banned_sources=[], note="解除禁用", **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已恢复")

    # ----- 解禁不得误恢复已被更正的页面 -----

    def test_lift_does_not_restore_corrected_page_until_replacement(self):
        batch = self._batch()
        embargo = self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        self.review.new_page_version(
            self.page_a.id, script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1}, source_refs={self.source_a.id: 1},
            actor="学员甲", base_version=1)
        self.release.lift_embargo(embargo.id, **MANAGER)
        cell = self._cell(batch, "东南亚", self.page_a)
        self.assertEqual(cell.state, "已撤回")
        self.assertIn("替代版本", cell.hold_reason)
        self.assertEqual(self._notices(batch, "恢复"), [])
        self.assertEqual(self._notices(batch, "替换"), [])
        # 追加替代版本后按新版本恢复
        self.release.append_replacement(
            batch.id, page_id=self.page_a.id, page_version=2,
            reason="采用更正后的v2", **MANAGER)
        cell = self._cell(batch, "东南亚", self.page_a)
        self.assertEqual(cell.state, "已恢复")
        self.assertEqual(cell.pointer_version, 2)
        replacements = self._notices(batch, "替换")
        self.assertEqual({n.partner for n in replacements}, {"合作方甲", "合作方乙"})
        self.assertEqual({n.page_version for n in replacements}, {2})

    def test_replacement_without_embargoed_source_restores_delivery(self):
        source_c = self.review.register_source(
            title="大事年表", citation="《年表》p1",
            license=lic(("东南亚", "欧洲")), **EDITOR)
        batch = self._batch()
        self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        self.review.new_page_version(
            self.page_a.id, script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1}, source_refs={source_c.id: 1},
            actor="学员甲", base_version=1)
        self.release.append_replacement(
            batch.id, page_id=self.page_a.id, page_version=2,
            reason="替换被禁素材", **MANAGER)
        cell = self._cell(batch, "东南亚", self.page_a)
        self.assertEqual(cell.state, "已恢复")
        self.assertEqual(cell.pointer_version, 2)

    # ----- 乱序到达: 按各自生效时间重算 -----

    def test_out_of_order_events_recomputed_by_effective_time(self):
        batch = self._batch()
        self.clock.set(datetime(2026, 9, 25, 12, 0))
        embargo = self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运",
            effective_at=datetime(2026, 9, 25, 11, 0), **MANAGER)
        self.release.lift_embargo(
            embargo.id, effective_at=datetime(2026, 9, 25, 11, 30), **MANAGER)
        # 许可收缩生效于 11:20, 比解禁更晚到达
        self.review.revise_source(
            self.source_a.id, citation="《战史》p12(修订)",
            license=lic(("欧洲",)), base_revision=1,
            effective_at=datetime(2026, 9, 25, 11, 20), **EDITOR)
        cell = self._cell(batch, "东南亚", self.page_a)
        self.assertEqual(cell.state, "已撤回")  # 解禁不能复活被许可收缩命中的内容
        self.assertTrue(any("许可" in c or "史料" in c for c in cell.causes))
        # 许可恢复(生效 11:40)后所有原因消除, 单元恢复
        self.review.revise_source(
            self.source_a.id, citation="《战史》p12(再修订)",
            license=lic(("东南亚", "欧洲")), base_revision=2,
            effective_at=datetime(2026, 9, 25, 11, 40), **EDITOR)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已恢复")

    def test_stale_message_does_not_revive_withdrawn_content(self):
        batch = self._batch()
        self.clock.set(datetime(2026, 9, 25, 12, 0))
        self.review.revise_source(
            self.source_a.id, citation="收缩", license=lic(("欧洲",)),
            base_revision=1, effective_at=datetime(2026, 9, 25, 11, 0), **EDITOR)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        # 迟到的旧消息(生效时间更早的修订)不得复活已撤回内容
        self.review.revise_source(
            self.source_a.id, citation="迟到的恢复", license=lic(("东南亚", "欧洲")),
            base_revision=2, effective_at=datetime(2026, 9, 25, 10, 30), **EDITOR)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")

    def test_backdated_rule_version_does_not_override_newer_rule(self):
        batch = self._batch()
        self.clock.set(datetime(2026, 9, 25, 12, 0))
        self.release.register_market_rule(
            market="东南亚", banned_sources=[self.source_a.id], note="禁用",
            effective_at=datetime(2026, 9, 25, 11, 0), **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")
        # 生效时间更早的规则版本迟到到达, 不改变现行规则
        self.release.register_market_rule(
            market="东南亚", banned_sources=[], note="迟到的旧版",
            effective_at=datetime(2026, 9, 25, 10, 30), **MANAGER)
        self.assertEqual(self._cell(batch, "东南亚", self.page_a).state, "已撤回")

    # ----- 重放幂等 -----

    def test_replay_does_not_duplicate_notices(self):
        batch = self._batch()
        first = self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运",
            message_id="msg-emb-1", **MANAGER)
        replayed = self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运",
            message_id="msg-emb-1", **MANAGER)
        self.assertIs(first, replayed)
        self.assertEqual(len(self.release.embargoes), 1)
        count = len(self._notices(batch))
        # 无关事件触发整批重算, 通知数量不变
        self.release.register_market_rule(market="美洲", note="无关市场", **MANAGER)
        self.assertEqual(len(self._notices(batch)), count)

    # ----- 导出: 只返回当前范围确实允许的材料 -----

    def test_export_returns_only_currently_allowed_materials(self):
        batch = self._batch()
        embargo = self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        result = self.release.export_delivery(
            batch.id, partner="合作方甲", market="东南亚", **MANAGER)
        self.assertEqual([p["page_id"] for p in result["pages"]], [self.page_b.id])
        self.assertIn(self.page_a.id, result["excluded"])
        self.assertTrue(any("撤回" in r for r in result["excluded"][self.page_a.id]))
        # 其他市场不受影响
        other = self.release.export_delivery(
            batch.id, partner="合作方甲", market="欧洲", **MANAGER)
        self.assertEqual({p["page_id"] for p in other["pages"]},
                         {self.page_a.id, self.page_b.id})
        # 解禁后恢复导出
        self.clock.set(datetime(2026, 9, 25, 9, 0))
        self.release.lift_embargo(embargo.id, **MANAGER)
        result = self.release.export_delivery(
            batch.id, partner="合作方甲", market="东南亚", **MANAGER)
        self.assertEqual({p["page_id"] for p in result["pages"]},
                         {self.page_a.id, self.page_b.id})
        # 首次成功导出自动登记「已下载」
        trace = self.release.batch_trace(batch.id)
        steps = {p["partner"]: p["last_step"] for p in trace["partners"]}
        self.assertEqual(steps["合作方甲"], "已下载")

    def test_export_requires_valid_partner_grant(self):
        grants = [PartnerGrant("合作方甲", frozenset({"东南亚"}),
                               date(2026, 1, 1), date(2026, 10, 31))]
        batch = self._batch(markets=("东南亚",), grants=grants)
        with self.assertRaises(PermissionDenied):
            self.release.export_delivery(
                batch.id, partner="合作方甲", market="东南亚",
                on=date(2026, 11, 15), **MANAGER)
        with self.assertRaises(PermissionDenied):
            self.release.export_delivery(
                batch.id, partner="合作方乙", market="东南亚", **MANAGER)

    # ----- 反查: 冻结依据与合作方确认进度 -----

    def test_batch_trace_recovers_basis_and_partner_progress(self):
        self.release.register_market_rule(
            market="东南亚", banned_sources=[], note="初版规则", **MANAGER)
        batch = self._batch()
        self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        notice = next(n for n in self._notices(batch, "撤回")
                      if n.partner == "合作方甲")
        self.clock.set(datetime(2026, 9, 25, 9, 0))
        self.release.record_receipt(
            batch.id, partner="合作方甲", step="已收讫",
            notice_id=notice.id, **MANAGER)
        self.clock.set(datetime(2026, 9, 25, 10, 0))
        self.release.record_receipt(
            batch.id, partner="合作方甲", step="已下架",
            notice_id=notice.id, **MANAGER)
        # 迟到的旧回执(生效时间更早)不会拉低确认进度
        self.release.record_receipt(
            batch.id, partner="合作方甲", step="已收讫", notice_id=notice.id,
            effective_at=datetime(2026, 9, 25, 8, 30), **MANAGER)
        trace = self.release.batch_trace(batch.id)
        partner = next(p for p in trace["partners"] if p["partner"] == "合作方甲")
        self.assertEqual(partner["last_step"], "已下架")
        self.assertEqual(len(partner["receipts"]), 3)
        # 冻结依据可反查
        self.assertEqual(trace["entries"][0]["frozen_version"], 1)
        self.assertEqual(trace["licenses"][self.source_a.id]["revision"], 1)
        self.assertEqual(trace["rule_versions"]["东南亚"], 1)
        self.assertEqual(trace["status"], "发布中")
        self.assertTrue(any(n["kind"] == "撤回" for n in trace["notices"]))
        cell = next(c for c in trace["cells"]
                    if c["market"] == "东南亚" and c["page_id"] == self.page_a.id)
        self.assertEqual(cell["state"], "已撤回")

    # ----- 完结 -----

    def test_close_batch_requires_acknowledged_withdrawals(self):
        batch = self._batch()
        self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        with self.assertRaises(StateError):
            self.release.close_batch(batch.id, **MANAGER)
        for notice in self._notices(batch, "撤回"):
            self.release.record_receipt(
                batch.id, partner=notice.partner, step="已下架",
                notice_id=notice.id, **MANAGER)
        self.release.close_batch(batch.id, **MANAGER)
        self.assertEqual(self.release.batches[batch.id].status, "已完结")
        # 已完结批次不再重算: 新禁运不改变单元状态, 也不追加通知
        cells_before = {k: v.state for k, v in self.release.batches[batch.id].cells.items()}
        notices_before = len(self._notices(batch))
        self.release.register_embargo(market="东南亚", reason="全面禁运", **MANAGER)
        self.assertEqual(
            {k: v.state for k, v in self.release.batches[batch.id].cells.items()},
            cells_before)
        self.assertEqual(len(self._notices(batch)), notices_before)
        # 迟到回执仍可补录
        self.release.record_receipt(
            batch.id, partner="合作方甲", step="已替换", **MANAGER)
        trace = self.release.batch_trace(batch.id)
        partner = next(p for p in trace["partners"] if p["partner"] == "合作方甲")
        self.assertEqual(partner["last_step"], "已替换")

    # ----- 创建校验与权限 -----

    def test_batch_creation_rejects_undeliverable_or_embargoed_content(self):
        expiring = self.review.register_source(
            title="限时素材", citation="《快报》p1",
            license=lic(("东南亚",), expires=date(2026, 11, 30)), **EDITOR)
        page = self._publishable_page("第5页 转移", expiring)
        with self.assertRaises(DomainError):  # 许可在发行窗口内过期
            self._batch(pages=[page], markets=("东南亚",))
        self.release.register_embargo(
            market="东南亚", source_ids=[self.source_a.id], reason="禁运", **MANAGER)
        with self.assertRaises(DomainError):  # 已生效禁运命中
            self._batch(pages=[self.page_a], markets=("东南亚",))
        draft = self.review.create_page(
            title="未完工", script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1}, source_refs={self.source_b.id: 1},
            actor="学员甲")
        with self.assertRaises(DomainError):  # 未达可出版
            self._batch(pages=[draft], markets=("欧洲",))

    def test_grant_must_fit_batch_markets_and_window(self):
        with self.assertRaises(DomainError):
            self._batch(grants=[PartnerGrant(
                "合作方甲", frozenset({"美洲"}), date(2026, 1, 1))])
        with self.assertRaises(DomainError):
            self._batch(grants=[PartnerGrant(
                "合作方甲", frozenset({"东南亚"}), date(2026, 11, 1))])

    def test_distribution_ops_require_manager_role(self):
        with self.assertRaises(PermissionDenied):
            self.release.create_release_batch(
                title="越权批次", markets=["东南亚"], page_ids=[self.page_a.id],
                partner_grants=[], actor="王编辑", role="编辑", **WINDOW)
        with self.assertRaises(PermissionDenied):
            self.release.register_embargo(
                market="东南亚", actor="王编辑", role="编辑")
        with self.assertRaises(PermissionDenied):
            self.release.record_receipt(
                "REL-1", partner="合作方甲", step="已下架",
                actor="学员甲", role="学员")


if __name__ == "__main__":
    unittest.main()
