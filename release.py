"""海外发行与禁运撤回: 发布批次、市场规则、禁运、合作方回执。

四条主线:
1. 批次冻结: 发布批次冻结所采用的页面版本、史料许可(修订号与条款)、发行窗口、
   合作方授权与市场规则版本; 之后不改写原清单, 只追加撤回通知、合作方回执与替代版本。
2. 生效时间重算: 禁运/解禁、许可修订、规则版本、替代版本均携带生效时间; 乱序到达时
   未完结批次按生效时间重算每个 (市场, 页面) 单元的撤回状态——某时刻单元被撤回,
   当且仅当该时刻存在生效中的撤回原因(命中的禁运/不再覆盖窗口的许可/禁用素材的规则)。
   因此旧消息不会复活已撤回内容: 所有原因都消除, 单元才可恢复。
   生效时间相同的事件按全局事件序号(到达顺序)裁决, 保证重算结果确定。
3. 更正不误恢复: 解禁时若页面已被更正(冻结版本陈旧)且未追加替代版本, 单元保持已撤回;
   追加替代版本后按替代版本恢复(通知类型为「替换」)。
4. 幂等通知: 通知按 (批次, 市场, 页面, 撤回区间, 合作方) 的确定性键去重,
   重放同一批次或同一消息(message_id)不会产生重复通知。

导出与反查: export_delivery 只返回当前范围确实允许的材料(单元状态、页面状态、
事实争议、当前许可、当前禁运与规则、合作方授权逐项检查); batch_trace 反查冻结清单、
许可与规则依据、通知/替代/回执, 以及每个合作方最后确认到了哪一步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count

from domain import (
    DomainError,
    NotFoundError,
    PermissionDenied,
    ReviewSystem,
    StateError,
)

DISTRIBUTION_ROLES = {"发行经理"}

BATCH_STATES = ("发布中", "已完结")
NOTICE_KINDS = ("撤回", "恢复", "替换")
RECEIPT_STEPS = ("已下载", "已收讫", "已下架", "已替换")
ACK_STEPS = ("已收讫", "已下架", "已替换")  # 可用于确认撤回通知的回执步骤

# 截点 = (生效时刻, 全局事件序号); 事件在截点"之后"视为已生效。
# 同一时刻的多个事件按序号(到达顺序)依次生效, 重算结果与到达顺序无关、确定可重放。
CUTOFF_INF = float("inf")


# ---------- 词汇与实体 ----------


@dataclass(frozen=True)
class Cause:
    """某一截点命中 (市场, 页面) 单元的一条撤回原因。"""

    kind: str      # 禁运 / 许可 / 规则
    detail: str    # 人类可读的依据
    base: str      # 原因身份(同一原因的反复出现视为同一原因)
    event_id: str  # 触发本次出现的事件标识, 用于通知幂等键


@dataclass
class MarketRuleVersion:
    version: int
    effective_at: datetime
    seq: int
    banned_sources: frozenset  # 该市场禁用的史料
    note: str
    author: str


@dataclass
class MarketRule:
    market: str
    versions: list = field(default_factory=list)  # MarketRuleVersion


@dataclass
class Embargo:
    id: str
    market: str
    source_ids: frozenset  # 空集 = 命中该市场全部素材
    reason: str
    effective_at: datetime
    seq: int
    lifted_at: datetime | None = None
    lift_seq: int | None = None
    history: list = field(default_factory=list)

    def active_at(self, cutoff) -> bool:
        if (self.effective_at, self.seq) > cutoff:
            return False
        return self.lifted_at is None or (self.lifted_at, self.lift_seq) > cutoff


@dataclass(frozen=True)
class PartnerGrant:
    """合作方授权: 允许的市场与有效期。"""

    partner: str
    markets: frozenset
    valid_from: date
    valid_to: date | None = None

    def covers(self, market: str, on: date) -> bool:
        if market not in self.markets:
            return False
        return self.valid_from <= on and (
            self.valid_to is None or on <= self.valid_to)


@dataclass
class BatchEntry:
    """冻结清单中的一页: 页面版本与其采用的依赖。"""

    page_id: str
    page_version: int
    script_refs: dict
    source_refs: dict


@dataclass
class Replacement:
    """追加到批次的替代版本(页面更正后改用新版本交付)。"""

    id: str
    page_id: str
    page_version: int
    reason: str
    effective_at: datetime
    seq: int
    recorded_at: datetime


@dataclass
class Notice:
    """发给合作方的通知(撤回/恢复/替换); key 为幂等键。"""

    id: str
    key: str
    kind: str
    batch_id: str
    partner: str
    market: str
    page_id: str
    page_version: int
    causes: list
    effective_at: datetime
    recorded_at: datetime


@dataclass
class Receipt:
    """合作方回执; effective_at 为合作方实际完成该步的时间(可迟到补录)。"""

    id: str
    partner: str
    step: str
    notice_id: str | None
    effective_at: datetime
    recorded_at: datetime


@dataclass
class CellState:
    """(市场, 页面) 单元的当前状态(由重算得出, 不手工修改)。"""

    market: str
    page_id: str
    state: str           # 可用 / 已撤回 / 已恢复
    pointer_version: int  # 当前应交付的页面版本(替代版本优先于冻结版本)
    causes: list = field(default_factory=list)   # 当前生效的撤回原因(描述)
    hold_reason: str | None = None  # 原因已消除但暂不能恢复的解释


@dataclass
class ReleaseBatch:
    id: str
    title: str
    markets: tuple
    window_start: date
    window_end: date
    status: str
    created_at: datetime
    created_by: str
    # 冻结清单(创建后不改写)
    entries: list
    license_snapshot: dict   # source_id -> {"revision": int, "license": License}
    rule_versions: dict      # market -> 冻结时生效的市场规则版本号或 None
    partner_grants: dict     # partner -> PartnerGrant
    # 追加区(只增不改)
    replacements: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    cells: dict = field(default_factory=dict)  # "market|page_id" -> CellState
    history: list = field(default_factory=list)


# ---------- 发行系统 ----------


class ReleaseSystem:
    """海外发行领域服务; 依附于审稿系统, 监听其变更以重算未完结批次。"""

    def __init__(self, review: ReviewSystem, clock=None):
        self.review = review
        self._clock = clock or review._clock  # 与审稿域共享时钟, 便于测试注入
        self._seq = count(1)
        self.rules: dict[str, MarketRule] = {}
        self.embargoes: dict[str, Embargo] = {}
        self.batches: dict[str, ReleaseBatch] = {}
        self._messages: dict[str, object] = {}  # message_id -> 已处理结果(重放去重)
        review.add_listener(self._on_review_change)

    # ----- 基础设施 -----

    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._now().date()

    def _now_cutoff(self):
        """当前截点: 此刻之前(含同刻)到达的事件全部生效。"""
        return (self._now(), CUTOFF_INF)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    @staticmethod
    def _require_distribution(role: str):
        if role not in DISTRIBUTION_ROLES:
            raise PermissionDenied(f"角色「{role}」无权执行发行操作")

    def _get(self, table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFoundError(f"{kind}不存在: {key}") from None

    def _dedup(self, message_id: str | None):
        """消息重放去重: 已处理过则返回原结果, 未见过返回 None。"""
        if message_id is None:
            return None
        return self._messages.get(message_id)

    def _remember(self, message_id: str | None, result):
        if message_id is not None:
            self._messages[message_id] = result

    # ----- 市场规则 -----

    def register_market_rule(self, *, market: str, banned_sources=(), note: str = "",
                             effective_at: datetime | None = None,
                             actor: str, role: str, message_id: str | None = None):
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        for source_id in banned_sources:
            self._get(self.review.sources, source_id, "史料")
        rule = self.rules.get(market)
        if rule is None:
            rule = self.rules[market] = MarketRule(market)
        rule.versions.append(MarketRuleVersion(
            version=len(rule.versions) + 1,
            effective_at=effective_at or self._now(),
            seq=self.review.next_event_seq(),
            banned_sources=frozenset(banned_sources),
            note=note, author=actor,
        ))
        self._remember(message_id, rule)
        self._recompute_all()
        return rule

    def _rule_version_at(self, market: str, cutoff) -> MarketRuleVersion | None:
        rule = self.rules.get(market)
        if rule is None:
            return None
        best = None
        for version in rule.versions:
            key = (version.effective_at, version.seq)
            if key <= cutoff and (best is None or key > best[0]):
                best = (key, version)
        return best[1] if best is not None else None

    # ----- 禁运与解禁 -----

    def register_embargo(self, *, market: str, source_ids=(), reason: str = "",
                         effective_at: datetime | None = None,
                         actor: str, role: str, message_id: str | None = None) -> Embargo:
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        for source_id in source_ids:
            self._get(self.review.sources, source_id, "史料")
        embargo = Embargo(
            id=self._new_id("EMB"), market=market,
            source_ids=frozenset(source_ids), reason=reason,
            effective_at=effective_at or self._now(),
            seq=self.review.next_event_seq(),
        )
        embargo.history.append(
            f"{self._now().isoformat()} {actor} 登记禁运"
            f"(生效{embargo.effective_at.isoformat()}): {reason}")
        self.embargoes[embargo.id] = embargo
        self._remember(message_id, embargo)
        self._recompute_all()
        return embargo

    def lift_embargo(self, embargo_id: str, *, effective_at: datetime | None = None,
                     actor: str, role: str, message_id: str | None = None) -> Embargo:
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        embargo = self._get(self.embargoes, embargo_id, "禁运通知")
        if embargo.lifted_at is not None:
            raise StateError(f"禁运{embargo_id}已解除, 不能重复解禁")
        effective_at = effective_at or self._now()
        if effective_at < embargo.effective_at:
            raise DomainError("解禁生效时间不能早于禁运生效时间")
        embargo.lifted_at = effective_at
        embargo.lift_seq = self.review.next_event_seq()
        embargo.history.append(
            f"{self._now().isoformat()} {actor} 解除禁运(生效{effective_at.isoformat()})")
        self._remember(message_id, embargo)
        self._recompute_all()
        return embargo

    # ----- 发布批次 -----

    def create_release_batch(self, *, title: str, markets: list,
                             window_start: date, window_end: date,
                             page_ids: list, partner_grants: list,
                             actor: str, role: str,
                             message_id: str | None = None) -> ReleaseBatch:
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        markets = list(dict.fromkeys(markets or []))
        if not markets:
            raise DomainError("发布批次必须至少包含一个市场")
        if window_end < window_start:
            raise DomainError("发行窗口结束日期不能早于开始日期")
        if not page_ids:
            raise DomainError("发布批次必须至少包含一个页面")
        grants = {}
        for grant in partner_grants or []:
            if grant.partner in grants:
                raise DomainError(f"合作方「{grant.partner}」的授权重复")
            outside = sorted(set(grant.markets) - set(markets))
            if outside:
                raise DomainError(
                    f"合作方「{grant.partner}」的授权市场{outside}不在批次市场范围内")
            if grant.valid_from is None:
                raise DomainError(f"合作方「{grant.partner}」的授权缺少生效日期")
            if grant.valid_from > window_start:
                raise DomainError(
                    f"合作方「{grant.partner}」的授权生效日晚于发行窗口开始")
            if grant.valid_to is not None and grant.valid_to < window_start:
                raise DomainError(
                    f"合作方「{grant.partner}」的授权在发行窗口开始前已失效")
            grants[grant.partner] = grant
        now = self._now()
        now_cutoff = (now, CUTOFF_INF)
        problems = []
        entries = []
        license_snapshot = {}
        for page_id in dict.fromkeys(page_ids):
            page = self._get(self.review.pages, page_id, "分镜页")
            if page.status != "可出版":
                problems.append(f"页面{page_id}状态为「{page.status}」, 未达到可出版")
                continue
            current = page.current
            for market in markets:
                blockers = self.review.page_blockers(page_id, scope=market, on=window_start)
                problems.extend(f"页面{page_id}@{market}: {b}" for b in blockers)
                for source_id in current.source_refs:
                    lic = self.review.sources[source_id].license
                    problems.extend(
                        f"页面{page_id}@{market} 史料{source_id}: {p}"
                        for p in lic.problems(market, window_end))
                causes = self._embargo_causes(market, current.source_refs, now_cutoff) \
                    + self._rule_causes(market, current.source_refs, now_cutoff)
                problems.extend(f"页面{page_id}@{market}: {c.detail}" for c in causes)
            entries.append(BatchEntry(
                page_id=page_id, page_version=current.version,
                script_refs=dict(current.script_refs),
                source_refs=dict(current.source_refs)))
            for source_id in current.source_refs:
                source = self.review.sources[source_id]
                license_snapshot[source_id] = {
                    "revision": source.revision, "license": source.license}
        if problems:
            raise DomainError("发布批次校验未通过: " + "; ".join(problems))
        batch = ReleaseBatch(
            id=self._new_id("REL"), title=title, markets=tuple(markets),
            window_start=window_start, window_end=window_end,
            status="发布中", created_at=now, created_by=actor,
            entries=entries, license_snapshot=license_snapshot,
            rule_versions={
                m: (v.version if (v := self._rule_version_at(m, now_cutoff)) else None)
                for m in markets
            },
            partner_grants=grants,
        )
        batch.history.append(
            f"{now.isoformat()} {actor} 创建发布批次, "
            f"冻结{len(entries)}个页面/{len(license_snapshot)}项史料许可")
        self.batches[batch.id] = batch
        self._remember(message_id, batch)
        self._recompute(batch)
        return batch

    def append_replacement(self, batch_id: str, *, page_id: str, page_version: int,
                           reason: str = "", effective_at: datetime | None = None,
                           actor: str, role: str,
                           message_id: str | None = None) -> Replacement:
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        batch = self._get(self.batches, batch_id, "发布批次")
        if batch.status != "发布中":
            raise StateError(f"批次{batch_id}已完结, 不能追加替代版本")
        entry = next((e for e in batch.entries if e.page_id == page_id), None)
        if entry is None:
            raise NotFoundError(f"页面{page_id}不在批次{batch_id}的冻结清单内")
        page = self._get(self.review.pages, page_id, "分镜页")
        if not any(v.version == page_version for v in page.versions):
            raise NotFoundError(f"分镜页{page_id}没有版本v{page_version}")
        now_cutoff = self._now_cutoff()
        pointer = self._pointer_at(batch, entry, now_cutoff)
        if page_version == pointer:
            raise DomainError(f"页面{page_id}当前交付版本已是v{page_version}")
        replacement = Replacement(
            id=self._new_id("RP"), page_id=page_id, page_version=page_version,
            reason=reason, effective_at=effective_at or self._now(),
            seq=self.review.next_event_seq(), recorded_at=self._now())
        batch.replacements.append(replacement)
        batch.history.append(
            f"{self._now().isoformat()} {actor} 追加替代版本: "
            f"页面{page_id}改用v{page_version}")
        self._remember(message_id, replacement)
        self._recompute(batch)
        return replacement

    def record_receipt(self, batch_id: str, *, partner: str, step: str,
                       notice_id: str | None = None,
                       effective_at: datetime | None = None,
                       actor: str, role: str,
                       message_id: str | None = None) -> Receipt:
        """登记合作方回执; 已完结批次的迟到回执也可补录(只影响确认进度)。"""
        self._require_distribution(role)
        if (seen := self._dedup(message_id)) is not None:
            return seen
        batch = self._get(self.batches, batch_id, "发布批次")
        if partner not in batch.partner_grants:
            raise PermissionDenied(f"合作方「{partner}」不在批次{batch_id}的授权名单内")
        if step not in RECEIPT_STEPS:
            raise DomainError(f"未知回执步骤「{step}」, 可选: {RECEIPT_STEPS}")
        if notice_id is not None:
            notice = next((n for n in batch.notices if n.id == notice_id), None)
            if notice is None:
                raise NotFoundError(f"通知{notice_id}不属于批次{batch_id}")
            if notice.partner != partner:
                raise PermissionDenied(f"通知{notice_id}属于合作方「{notice.partner}」")
        now = self._now()
        receipt = Receipt(
            id=self._new_id("RC"), partner=partner, step=step,
            notice_id=notice_id, effective_at=effective_at or now,
            recorded_at=now)
        batch.receipts.append(receipt)
        batch.history.append(
            f"{now.isoformat()} {actor} 登记合作方「{partner}」回执: {step}")
        self._remember(message_id, receipt)
        return receipt

    def close_batch(self, batch_id: str, *, actor: str, role: str) -> ReleaseBatch:
        """完结批次: 所有撤回通知均须已被合作方确认; 完结后不再重算。"""
        self._require_distribution(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        if batch.status != "发布中":
            raise StateError(f"批次{batch_id}已完结, 不能重复完结")
        self._recompute(batch)
        pending = [n.id for n in batch.notices
                   if n.kind == "撤回" and not self._acknowledged(batch, n)]
        if pending:
            raise StateError(
                "以下撤回通知尚未收到合作方确认, 不能完结: " + ", ".join(pending))
        batch.status = "已完结"
        batch.history.append(f"{self._now().isoformat()} {actor} 完结批次")
        return batch

    @staticmethod
    def _acknowledged(batch: ReleaseBatch, notice: Notice) -> bool:
        return any(r.notice_id == notice.id and r.step in ACK_STEPS
                   for r in batch.receipts)

    # ----- 撤回原因(纯函数: 某截点某市场某素材集合的命中情况) -----

    def _embargo_causes(self, market: str, source_refs: dict, cutoff) -> list[Cause]:
        causes = []
        for embargo in self.embargoes.values():
            if embargo.market != market or not embargo.active_at(cutoff):
                continue
            if embargo.source_ids and not (embargo.source_ids & source_refs.keys()):
                continue
            causes.append(Cause(
                "禁运", f"禁运{embargo.id}: {embargo.reason}",
                base=f"embargo:{embargo.id}", event_id=f"embargo:{embargo.id}"))
        return causes

    def _license_causes(self, window_start: date, window_end: date,
                        market: str, source_refs: dict, cutoff) -> list[Cause]:
        causes = []
        for source_id in source_refs:
            current = self._license_at(source_id, cutoff)
            if current is None:
                continue
            revision, lic = current
            if lic.problems(market, window_start) or lic.problems(market, window_end):
                causes.append(Cause(
                    "许可", f"史料{source_id}许可不再覆盖市场「{market}」的发行窗口",
                    base=f"license:{source_id}",
                    event_id=f"license:{source_id}@r{revision}"))
        return causes

    def _rule_causes(self, market: str, source_refs: dict, cutoff) -> list[Cause]:
        version = self._rule_version_at(market, cutoff)
        if version is None:
            return []
        return [
            Cause("规则",
                  f"市场「{market}」规则v{version.version}禁用史料{source_id}",
                  base=f"rule:{market}:{source_id}",
                  event_id=f"rule:{market}:v{version.version}:{source_id}")
            for source_id in source_refs
            if source_id in version.banned_sources
        ]

    def _causes_at(self, batch: ReleaseBatch, market: str, source_refs: dict,
                   cutoff) -> list[Cause]:
        return (
            self._embargo_causes(market, source_refs, cutoff)
            + self._license_causes(batch.window_start, batch.window_end,
                                   market, source_refs, cutoff)
            + self._rule_causes(market, source_refs, cutoff)
        )

    def _license_at(self, source_id: str, cutoff):
        """截点之前生效的 (修订号, 许可); 按 (生效时间, 事件序号) 取最新。"""
        best = None
        for entry in self.review.source_revision_log.get(source_id, []):
            key = (entry["effective_at"], entry["seq"])
            if key <= cutoff and (best is None or key > best[0]):
                best = (key, entry)
        if best is None:
            return None
        return best[1]["revision"], best[1]["license"]

    # ----- 重算: 按 (生效时间, 事件序号) 折叠每个单元的撤回状态 -----

    def _on_review_change(self):
        self._recompute_all()

    def _recompute_all(self):
        for batch in self.batches.values():
            self._recompute(batch)

    def _recompute(self, batch: ReleaseBatch):
        if batch.status != "发布中":
            return  # 已完结批次不再重算, 保留原清单与既有记录
        now_cutoff = self._now_cutoff()
        for market in batch.markets:
            for entry in batch.entries:
                batch.cells[f"{market}|{entry.page_id}"] = self._fold_cell(
                    batch, market, entry, now_cutoff)

    def _pointer_at(self, batch: ReleaseBatch, entry: BatchEntry, cutoff) -> int:
        """截点处应交付的页面版本: 生效的替代版本优先, 否则冻结版本。"""
        best = None
        for repl in batch.replacements:
            if repl.page_id != entry.page_id:
                continue
            key = (repl.effective_at, repl.seq)
            if key <= cutoff and (best is None or key > best[0]):
                best = (key, repl)
        return best[1].page_version if best is not None else entry.page_version

    def _version_sources(self, entry: BatchEntry, page_version: int) -> dict:
        if page_version == entry.page_version:
            return entry.source_refs
        page = self.review.pages[entry.page_id]
        version = next((v for v in page.versions if v.version == page_version), None)
        return version.source_refs if version is not None else entry.source_refs

    def _fold_cell(self, batch: ReleaseBatch, market: str,
                   entry: BatchEntry, now_cutoff) -> CellState:
        # 可能影响原因集合的截点: 创建、现在、替代版本、许可修订、禁运/解禁、规则版本
        cutoffs = {(batch.created_at, -1), now_cutoff}
        relevant_sources = set(entry.source_refs)
        for repl in batch.replacements:
            if repl.page_id == entry.page_id:
                cutoffs.add((repl.effective_at, repl.seq))
                relevant_sources |= set(self._version_sources(entry, repl.page_version))
        for source_id in relevant_sources:
            for log_entry in self.review.source_revision_log.get(source_id, []):
                cutoffs.add((log_entry["effective_at"], log_entry["seq"]))
        for embargo in self.embargoes.values():
            if embargo.market == market:
                cutoffs.add((embargo.effective_at, embargo.seq))
                if embargo.lifted_at is not None:
                    cutoffs.add((embargo.lifted_at, embargo.lift_seq))
        rule = self.rules.get(market)
        if rule is not None:
            for version in rule.versions:
                cutoffs.add((version.effective_at, version.seq))
        ordered = sorted(c for c in cutoffs if c <= now_cutoff)

        # 折叠撤回区间: 原因集合从空变为非空即开始一个撤回区间, 清空即结束
        episodes = []
        open_episode = None
        prev_bases = set()
        for cutoff in ordered:
            sources = self._version_sources(
                entry, self._pointer_at(batch, entry, cutoff))
            causes = self._causes_at(batch, market, sources, cutoff)
            bases = {c.base for c in causes}
            if bases and not prev_bases:
                open_episode = {
                    "start": cutoff,
                    "causes": [c for c in causes if c.base not in prev_bases],
                }
                episodes.append(open_episode)
            elif not bases and prev_bases and open_episode is not None:
                open_episode["end"] = cutoff
                open_episode = None
            prev_bases = bases

        pointer = self._pointer_at(batch, entry, now_cutoff)
        now_causes = self._causes_at(
            batch, market, self._version_sources(entry, pointer), now_cutoff)
        hold = None
        if now_causes:
            state = "已撤回"
        elif episodes:
            # 原因已全部消除, 但恢复不得误用已被更正的冻结版本, 且须通过当前检查
            hold = self._restore_hold(batch, market, entry, pointer, now_cutoff)
            state = "已撤回" if hold else "已恢复"
        else:
            state = "可用"

        for index, episode in enumerate(episodes):
            suffix = "+".join(sorted(c.event_id for c in episode["causes"]))
            base_key = f"{batch.id}|{market}|{entry.page_id}|{suffix}"
            self._ensure_notices(
                batch, kind="撤回", market=market, page_id=entry.page_id,
                page_version=self._pointer_at(batch, entry, episode["start"]),
                causes=episode["causes"], effective_at=episode["start"][0],
                base_key=f"{base_key}|撤回")
            if "end" not in episode:
                continue
            is_final = index == len(episodes) - 1
            if is_final:
                if hold:
                    continue  # 恢复被搁置(如页面已更正待替代版本), 暂不通知
                restored_version, restored_at = pointer, self._now()
            else:
                restored_version = self._pointer_at(batch, entry, episode["end"])
                restored_at = episode["end"][0]
            kind = "恢复" if restored_version == entry.page_version else "替换"
            self._ensure_notices(
                batch, kind=kind, market=market, page_id=entry.page_id,
                page_version=restored_version, causes=[],
                effective_at=restored_at, base_key=f"{base_key}|恢复")

        return CellState(
            market=market, page_id=entry.page_id, state=state,
            pointer_version=pointer,
            causes=[c.detail for c in now_causes], hold_reason=hold)

    def _restore_hold(self, batch: ReleaseBatch, market: str, entry: BatchEntry,
                      pointer: int, now_cutoff) -> str | None:
        """原因已消除时仍阻止恢复的原因; 返回 None 表示可以恢复。"""
        page = self.review.pages[entry.page_id]
        has_replacement = any(r.page_id == entry.page_id for r in batch.replacements)
        if not has_replacement and page.current.version != entry.page_version:
            return (f"页面已更正至v{page.current.version}, "
                    f"冻结版本v{entry.page_version}已陈旧, 需追加替代版本后方可恢复")
        if page.status != "可出版":
            return f"页面状态为「{page.status}」, 未达到可出版"
        issues = self.review.open_fact_issues(entry.page_id)
        if issues:
            return "事实争议未关闭: " + ", ".join(issue.id for issue in issues)
        today = self._today()
        for source_id in self._version_sources(entry, pointer):
            current = self._license_at(source_id, now_cutoff)
            if current is None:
                continue
            problems = current[1].problems(market, today)
            if problems:
                return f"史料{source_id}: " + "; ".join(problems)
        return None

    def _ensure_notices(self, batch: ReleaseBatch, *, kind: str, market: str,
                        page_id: str, page_version: int, causes: list,
                        effective_at: datetime, base_key: str):
        """按确定性键为每个相关合作方生成通知; 键已存在则跳过(重放不重复)。"""
        for partner, grant in batch.partner_grants.items():
            if market not in grant.markets:
                continue
            key = f"{base_key}|{partner}"
            if any(n.key == key for n in batch.notices):
                continue
            batch.notices.append(Notice(
                id=self._new_id("WN"), key=key, kind=kind, batch_id=batch.id,
                partner=partner, market=market, page_id=page_id,
                page_version=page_version,
                causes=[{"kind": c.kind, "detail": c.detail, "event_id": c.event_id}
                        for c in causes],
                effective_at=effective_at, recorded_at=self._now()))

    # ----- 导出与反查 -----

    def export_delivery(self, batch_id: str, *, partner: str, market: str,
                        actor: str, role: str, on: date | None = None) -> dict:
        """向合作方交付: 只返回当前范围确实允许的材料, 被排除的页面附原因。"""
        self._require_distribution(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        on = on or self._today()
        if market not in batch.markets:
            raise NotFoundError(f"市场「{market}」不在批次{batch_id}范围内")
        grant = batch.partner_grants.get(partner)
        if grant is None or not grant.covers(market, on):
            raise PermissionDenied(
                f"合作方「{partner}」未获得市场「{market}」在{on.isoformat()}的发行授权")
        self._recompute(batch)
        now_cutoff = self._now_cutoff()
        exported, excluded = [], {}
        for entry in batch.entries:
            cell = batch.cells[f"{market}|{entry.page_id}"]
            page = self.review.pages[entry.page_id]
            problems = []
            if cell.state == "已撤回":
                reason = "素材已撤回"
                if cell.causes:
                    reason += ": " + "; ".join(cell.causes)
                if cell.hold_reason:
                    reason += f"({cell.hold_reason})"
                problems.append(reason)
            else:
                # 以当前范围为准再查一次(已完结批次的单元状态可能滞后)
                sources = self._version_sources(entry, cell.pointer_version)
                problems.extend(
                    c.detail for c in self._causes_at(batch, market, sources,
                                                      now_cutoff))
            if page.status != "可出版":
                problems.append(f"页面状态为「{page.status}」, 未达到可出版")
            problems.extend(
                f"事实争议未关闭: {issue.id}"
                for issue in self.review.open_fact_issues(entry.page_id))
            for source_id in self._version_sources(entry, cell.pointer_version):
                source = self.review.sources[source_id]
                problems.extend(
                    f"史料{source_id}: {p}"
                    for p in source.license.problems(market, on))
            if problems:
                excluded[entry.page_id] = problems
                continue
            version = next(v for v in page.versions
                           if v.version == cell.pointer_version)
            exported.append({
                "page_id": entry.page_id,
                "title": page.title,
                "page_version": version.version,
                "frozen_version": entry.page_version,
                "panels": [
                    {"id": p.id, "index": p.index, "sketch_ref": p.sketch_ref}
                    for p in version.panels
                ],
                "sources": [
                    {"source_id": sid,
                     "citation": self.review.sources[sid].citation,
                     "revision": rev}
                    for sid, rev in version.source_refs.items()
                ],
                "scripts": [
                    {"segment_id": sid, "version": v}
                    for sid, v in version.script_refs.items()
                ],
            })
        if exported and not any(r.partner == partner for r in batch.receipts):
            batch.receipts.append(Receipt(
                id=self._new_id("RC"), partner=partner, step="已下载",
                notice_id=None, effective_at=self._now(), recorded_at=self._now()))
        return {
            "batch_id": batch.id, "market": market, "partner": partner,
            "window": {"start": batch.window_start.isoformat(),
                       "end": batch.window_end.isoformat()},
            "exported_at": self._now().isoformat(), "exported_by": actor,
            "pages": exported, "excluded": excluded,
        }

    def batch_trace(self, batch_id: str) -> dict:
        """反查批次: 冻结的页面/许可/规则依据、通知与替代、每个合作方的确认进度。"""
        batch = self._get(self.batches, batch_id, "发布批次")
        self._recompute(batch)
        now_cutoff = self._now_cutoff()
        cells = []
        for market in batch.markets:
            for entry in batch.entries:
                cell = batch.cells[f"{market}|{entry.page_id}"]
                cells.append({
                    "market": market, "page_id": entry.page_id,
                    "state": cell.state, "pointer_version": cell.pointer_version,
                    "causes": list(cell.causes), "hold_reason": cell.hold_reason,
                })
        return {
            "batch_id": batch.id, "title": batch.title, "status": batch.status,
            "markets": list(batch.markets),
            "window": {"start": batch.window_start.isoformat(),
                       "end": batch.window_end.isoformat()},
            "created_at": batch.created_at.isoformat(),
            "created_by": batch.created_by,
            "entries": [{
                "page_id": e.page_id,
                "frozen_version": e.page_version,
                "pointer_version": self._pointer_at(batch, e, now_cutoff),
                "script_refs": dict(e.script_refs),
                "source_refs": dict(e.source_refs),
            } for e in batch.entries],
            "licenses": {
                sid: {
                    "revision": snap["revision"],
                    "publication_scopes": sorted(snap["license"].publication_scopes),
                    "expires_at": (snap["license"].expires_at.isoformat()
                                   if snap["license"].expires_at else None),
                    "confidential_until": (
                        snap["license"].confidential_until.isoformat()
                        if snap["license"].confidential_until else None),
                }
                for sid, snap in batch.license_snapshot.items()
            },
            "rule_versions": dict(batch.rule_versions),
            "current_rule_versions": {
                m: (v.version if (v := self._rule_version_at(m, now_cutoff)) else None)
                for m in batch.markets
            },
            "partner_grants": [{
                "partner": g.partner, "markets": sorted(g.markets),
                "valid_from": g.valid_from.isoformat(),
                "valid_to": g.valid_to.isoformat() if g.valid_to else None,
            } for g in batch.partner_grants.values()],
            "cells": cells,
            "notices": [{
                "notice_id": n.id, "kind": n.kind, "partner": n.partner,
                "market": n.market, "page_id": n.page_id,
                "page_version": n.page_version, "causes": n.causes,
                "effective_at": n.effective_at.isoformat(),
                "recorded_at": n.recorded_at.isoformat(),
            } for n in batch.notices],
            "replacements": [{
                "replacement_id": r.id, "page_id": r.page_id,
                "page_version": r.page_version, "reason": r.reason,
                "effective_at": r.effective_at.isoformat(),
                "recorded_at": r.recorded_at.isoformat(),
            } for r in batch.replacements],
            "partners": [{
                "partner": partner,
                "last_step": progress["last_step"],
                "last_step_effective_at": (
                    progress["effective_at"].isoformat()
                    if progress["effective_at"] else None),
                "receipts": [{
                    "receipt_id": r.id, "step": r.step, "notice_id": r.notice_id,
                    "effective_at": r.effective_at.isoformat(),
                    "recorded_at": r.recorded_at.isoformat(),
                } for r in progress["receipts"]],
            } for partner, progress in self._partner_progress(batch).items()],
            "history": list(batch.history),
        }

    @staticmethod
    def _partner_progress(batch: ReleaseBatch) -> dict:
        """每个合作方最后确认到了哪一步: 按 (生效时间, 到达顺序) 取最新回执。"""
        progress = {}
        for partner in batch.partner_grants:
            best, best_key = None, None
            for position, receipt in enumerate(batch.receipts):
                if receipt.partner != partner:
                    continue
                key = (receipt.effective_at, position)
                if best_key is None or key > best_key:
                    best, best_key = receipt, key
            progress[partner] = {
                "last_step": best.step if best else None,
                "effective_at": best.effective_at if best else None,
                "receipts": [r for r in batch.receipts if r.partner == partner],
            }
        return progress
