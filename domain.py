"""连环画创作审稿的领域模型与规则。

五条主线:
1. 版本互引: 脚本段落、人物设定、分镜页均按版本演进, 页面版本记录其采用的依赖版本,
   依赖陈旧即不可交付。
2. 专业签署: 党史专家/作家/画家等只签署自己专业范围内的意见。
3. 冲突会审: 结论冲突的已采纳意见进入联合会审, 重大事实未关闭不得转入精稿;
   学员可提交有依据的异议重开议题。
4. 授权检查: 素材授权、保密期、出版范围随页面版本在交付与批量导出时检查。
5. 全程追溯: 任一画格可反查采用的史料、文字版本与决定人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count


# ---------- 错误 ----------


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""


class StaleVersionError(DomainError):
    """基于陈旧版本的修改, 需刷新后重试。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


# ---------- 角色与状态词汇 ----------

EXPERT_SCOPES = {
    "党史专家": "史实",
    "军史专家": "史实",
    "作家": "文字",
    "文学编辑": "文字",
    "画家": "画面",
    "美术编辑": "画面",
}
EDITOR_ROLES = {"编辑"}
RELEASE_MANAGER = "海外发行经理"
# 可执行发行/导出类操作的角色
RELEASE_ROLES = {"编辑", RELEASE_MANAGER}
TRAINEE = "学员"

OPINION_STATES = ("提出", "采纳", "驳回", "撤回")
ISSUE_STATES = ("待会审", "已关闭")
PAGE_STATES = ("构思中", "待评审", "联合会审", "精稿中", "可出版")
PAGE_TRANSITIONS = {
    "构思中": {"待评审"},
    "待评审": {"联合会审", "精稿中"},
    "联合会审": {"精稿中"},
    "精稿中": {"可出版"},
    "可出版": set(),
}


# ---------- 实体 ----------


@dataclass(frozen=True)
class License:
    """素材授权: 出版范围、授权截止日、保密期截止日。"""

    publication_scopes: frozenset
    expires_at: date | None = None
    confidential_until: date | None = None

    def problems(self, scope: str | None, on: date) -> list[str]:
        problems = []
        if self.expires_at is not None and on > self.expires_at:
            problems.append(f"授权已于{self.expires_at.isoformat()}过期")
        if self.confidential_until is not None and on <= self.confidential_until:
            problems.append(f"保密期至{self.confidential_until.isoformat()}届满")
        if scope is not None and scope not in self.publication_scopes:
            problems.append(f"出版范围不含「{scope}」")
        return problems


@dataclass
class HistoricalSource:
    id: str
    title: str
    citation: str
    revision: int
    license: License
    # 每个修订号冻结的许可快照, 供发布批次反查"采用的史料许可"
    license_history: list = field(default_factory=list)
    history: list = field(default_factory=list)


@dataclass
class ScriptVersion:
    version: int
    text: str
    author: str
    created_at: datetime


@dataclass
class ScriptSegment:
    id: str
    title: str
    versions: list = field(default_factory=list)

    @property
    def current(self) -> ScriptVersion:
        return self.versions[-1]


@dataclass
class CharacterDesign:
    id: str
    name: str
    versions: list = field(default_factory=list)  # 元素为 dict(version, brief, author, created_at)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class Panel:
    id: str
    index: int
    sketch_ref: str
    page_version: int


@dataclass
class PageVersion:
    version: int
    script_refs: dict  # 脚本段落 id -> 采用的版本号
    design_refs: dict  # 人物设定 id -> 采用的版本号
    source_refs: dict  # 史料 id -> 采用的修订号
    panels: list = field(default_factory=list)
    author: str = ""
    created_at: datetime | None = None


@dataclass
class Page:
    id: str
    title: str
    status: str = "构思中"
    versions: list = field(default_factory=list)
    history: list = field(default_factory=list)

    @property
    def current(self) -> PageVersion:
        return self.versions[-1]


@dataclass
class Opinion:
    id: str
    target_kind: str  # page / script / design / source
    target_id: str
    target_version: int
    scope: str  # 史实 / 文字 / 画面
    stance: str  # 结论标识, 用于冲突检测
    content: str
    author: str
    role: str
    state: str = "提出"
    decided_by: str | None = None
    outdated: bool = False  # 针对的版本已被更新(乱序点评)
    history: list = field(default_factory=list)


@dataclass
class Objection:
    id: str
    author: str
    content: str
    evidence: str  # 依据的史料 id
    created_at: datetime


@dataclass
class Issue:
    id: str
    target_kind: str
    target_id: str
    scope: str
    subject: str
    is_major_fact: bool
    opinion_ids: list = field(default_factory=list)
    state: str = "待会审"
    decision: str | None = None
    decided_by: str | None = None
    objections: list = field(default_factory=list)
    history: list = field(default_factory=list)


# ---------- 海外发行 ----------
#
# 版本化依据(发行窗口/合作方授权/市场规则)都带 effective_at, 乱序到达时按
# 生效时间取版本; 发布批次(ReleaseBatch)冻结当时的页面版本、史料许可快照与
# 各项依据版本, 此后禁运/许可收缩只追加撤回通知与替代版本, 绝不改写冻结清单。

PARTNER_ROLE = "合作方"
RELEASE_LINE_STATES = ("待发布", "已发布", "已撤回", "已替代")
# 合作方回执步骤(按推进顺序); 迟到的回执允许乱序, 但不能改变素材状态
RECEIPT_STEPS = ("已接收", "已下载", "撤回已确认", "替代已确认")
NOTICE_TYPES = ("发布通知", "撤回通知", "替代通知")


@dataclass
class MarketRuleSet:
    """某市场的规则集, 按版本演进; 版本可封禁特定素材。"""

    id: str
    market: str
    # 元素: {version, blocked_pages:frozenset, note, effective_at}
    versions: list = field(default_factory=list)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class PartnerGrant:
    """合作方授权: 授权市场集合与到期日, 按版本演进。"""

    id: str
    name: str
    # 元素: {version, markets:frozenset, expires_at:date|None, effective_at}
    versions: list = field(default_factory=list)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class ReleaseWindow:
    """某市场的发行窗口, 按版本演进(可改期)。"""

    id: str
    market: str
    # 元素: {version, opens_at:date, closes_at:date, effective_at}
    versions: list = field(default_factory=list)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class EmbargoNotice:
    """地区禁运通知: 自 effective_at 命中某市场的素材(None 表示整市场), lifted_at 解禁。"""

    id: str
    market: str
    page_ids: frozenset | None
    reason: str
    effective_at: date
    lifted_at: date | None = None
    issued_by: str = ""
    history: list = field(default_factory=list)

    def active(self, on: date) -> bool:
        return self.effective_at <= on and (
            self.lifted_at is None or on < self.lifted_at)

    def hits(self, page_id: str) -> bool:
        return self.page_ids is None or page_id in self.page_ids


@dataclass
class Receipt:
    """合作方回执: 确认到了哪一步。"""

    id: str
    partner_id: str
    line_id: str
    step: str
    effective_at: date
    recorded_at: datetime
    late: bool = False


@dataclass
class ReleaseLine:
    """批次内一条素材线: 合作方 × 市场 × 页面, 冻结页面版本与史料许可快照。"""

    id: str
    partner_id: str
    market: str
    page_id: str
    page_version: int
    # source_id -> {revision, publication_scopes, expires_at, confidential_until}
    source_licenses: dict
    status: str = "待发布"
    # 是否曾对外发布过(决定替代线首发时是"发布通知"还是"替代通知")
    ever_published: bool = False
    # 历次命中的壁垒(禁运/许可收缩/规则/授权/退回), 仅追加, 供反查依据
    barriers: list = field(default_factory=list)
    replaces: str | None = None       # 本线替代的旧线
    replaced_by: str | None = None    # 更正后由哪条新线替代
    receipts: list = field(default_factory=list)


@dataclass
class ReleaseNotification:
    """追加到批次的通知; key 去重, 同一批次重放不重复通知。"""

    id: str
    key: str
    type: str
    line_id: str
    partner_id: str
    market: str
    page_id: str
    reason: str
    effective_at: date
    created_at: datetime


@dataclass
class ReleaseBatch:
    """发布批次: 冻结清单 + 只增不改的通知/回执/替代追加。"""

    id: str
    markets: list
    frozen_at: date
    created_at: datetime
    created_by: str
    window_id: str
    # 冻结快照
    window: dict            # {id, version, opens_at, closes_at}
    rules: dict             # market -> {id, version, note, blocked_pages}
    grants: dict            # partner_id -> {id, name, version, markets, expires_at}
    lines: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    notice_keys: set = field(default_factory=set)
    # 冻结时未纳入的 (market, page_id) -> 原因
    freeze_exclusions: dict = field(default_factory=dict)
    history: list = field(default_factory=list)


# ---------- 审稿系统 ----------


class ReviewSystem:
    """内存中的审稿领域服务; clock 可注入以便测试。"""

    def __init__(self, clock=None):
        self._clock = clock or datetime.now
        self._seq = count(1)
        self.sources: dict[str, HistoricalSource] = {}
        self.segments: dict[str, ScriptSegment] = {}
        self.designs: dict[str, CharacterDesign] = {}
        self.pages: dict[str, Page] = {}
        self.opinions: dict[str, Opinion] = {}
        self.issues: dict[str, Issue] = {}
        # 海外发行主线
        self.market_rules: dict[str, MarketRuleSet] = {}
        self.partners: dict[str, PartnerGrant] = {}
        self.windows: dict[str, ReleaseWindow] = {}
        self.embargoes: dict[str, EmbargoNotice] = {}
        self.batches: dict[str, ReleaseBatch] = {}

    # ----- 基础设施 -----

    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._now().date()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    @staticmethod
    def _require_editor(role: str):
        if role not in EDITOR_ROLES:
            raise PermissionDenied(f"角色「{role}」无权执行编辑操作")

    def _get(self, table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFoundError(f"{kind}不存在: {key}") from None

    # ----- 史料与授权 -----

    def register_source(self, *, title: str, citation: str, license: License,
                        actor: str, role: str) -> HistoricalSource:
        self._require_editor(role)
        source = HistoricalSource(
            id=self._new_id("SRC"), title=title, citation=citation,
            revision=1, license=license,
            license_history=[(1, license)],
        )
        source.history.append(f"{self._now().isoformat()} {actor} 登记史料")
        self.sources[source.id] = source
        return source

    def revise_source(self, source_id: str, *, citation: str, license: License,
                      actor: str, role: str, base_revision: int) -> HistoricalSource:
        self._require_editor(role)
        source = self._get(self.sources, source_id, "史料")
        if base_revision != source.revision:
            raise StaleVersionError(
                f"史料{source_id}当前修订为r{source.revision}, 基于r{base_revision}的修改被拒绝")
        source.citation = citation
        source.license = license
        source.revision += 1
        source.license_history.append((source.revision, license))
        source.history.append(f"{self._now().isoformat()} {actor} 修订至r{source.revision}")
        # 许可收缩/扩张: 重算引用了该史料的发布批次, 既有批次冻结清单不变
        self._recalc_batches(on=self._today())
        return source

    # ----- 脚本段落 -----

    def create_segment(self, *, title: str, text: str, actor: str) -> ScriptSegment:
        segment = ScriptSegment(id=self._new_id("SEG"), title=title)
        segment.versions.append(ScriptVersion(1, text, actor, self._now()))
        self.segments[segment.id] = segment
        return segment

    def new_segment_version(self, segment_id: str, *, text: str, actor: str,
                            base_version: int) -> ScriptVersion:
        segment = self._get(self.segments, segment_id, "脚本段落")
        if base_version != segment.current.version:
            raise StaleVersionError(
                f"脚本段落{segment_id}当前为v{segment.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        version = ScriptVersion(segment.current.version + 1, text, actor, self._now())
        segment.versions.append(version)
        return version

    # ----- 人物设定 -----

    def create_design(self, *, name: str, brief: str, actor: str) -> CharacterDesign:
        design = CharacterDesign(id=self._new_id("DSN"), name=name)
        design.versions.append({
            "version": 1, "brief": brief, "author": actor,
            "created_at": self._now(),
        })
        self.designs[design.id] = design
        return design

    def new_design_version(self, design_id: str, *, brief: str, actor: str,
                           base_version: int) -> dict:
        design = self._get(self.designs, design_id, "人物设定")
        if base_version != design.current["version"]:
            raise StaleVersionError(
                f"人物设定{design_id}当前为v{design.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        version = {
            "version": design.current["version"] + 1, "brief": brief,
            "author": actor, "created_at": self._now(),
        }
        design.versions.append(version)
        return version

    # ----- 分镜页 -----

    def create_page(self, *, title: str, script_refs: dict, design_refs: dict,
                    source_refs: dict, actor: str) -> Page:
        page = Page(id=self._new_id("PG"), title=title)
        self.pages[page.id] = page
        self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)
        return page

    def _validate_refs(self, script_refs, design_refs, source_refs):
        for seg_id, version in script_refs.items():
            segment = self._get(self.segments, seg_id, "脚本段落")
            if not 1 <= version <= segment.current.version:
                raise NotFoundError(f"脚本段落{seg_id}没有版本v{version}")
        for design_id, version in design_refs.items():
            design = self._get(self.designs, design_id, "人物设定")
            if not 1 <= version <= design.current["version"]:
                raise NotFoundError(f"人物设定{design_id}没有版本v{version}")
        for source_id, revision in source_refs.items():
            source = self._get(self.sources, source_id, "史料")
            if not 1 <= revision <= source.revision:
                raise NotFoundError(f"史料{source_id}没有修订r{revision}")

    def _append_page_version(self, page: Page, *, script_refs, design_refs,
                             source_refs, actor) -> PageVersion:
        self._validate_refs(script_refs, design_refs, source_refs)
        version = PageVersion(
            version=len(page.versions) + 1,
            script_refs=dict(script_refs),
            design_refs=dict(design_refs),
            source_refs=dict(source_refs),
            author=actor,
            created_at=self._now(),
        )
        page.versions.append(version)
        page.history.append(
            f"{self._now().isoformat()} {actor} 提交页面v{version.version}")
        return version

    def new_page_version(self, page_id: str, *, script_refs: dict, design_refs: dict,
                         source_refs: dict, actor: str, base_version: int) -> PageVersion:
        page = self._get(self.pages, page_id, "分镜页")
        if base_version != page.current.version:
            raise StaleVersionError(
                f"分镜页{page_id}当前为v{page.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        version = self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)
        # 页面更正: 按当日重算尚未完成的发布, 旧版本素材不得复活
        self._recalc_batches(page_id=page_id, on=self._today())
        return version

    def add_panel(self, page_id: str, *, page_version: int, index: int,
                  sketch_ref: str, actor: str) -> Panel:
        page = self._get(self.pages, page_id, "分镜页")
        version = next((v for v in page.versions if v.version == page_version), None)
        if version is None:
            raise NotFoundError(f"分镜页{page_id}没有版本v{page_version}")
        panel = Panel(
            id=self._new_id("PN"), index=index,
            sketch_ref=sketch_ref, page_version=page_version,
        )
        version.panels.append(panel)
        page.history.append(
            f"{self._now().isoformat()} {actor} 在v{page_version}添加画格{panel.id}")
        return panel

    # ----- 专业签署与意见生命周期 -----

    def _target_version(self, target_kind: str, target_id: str) -> int:
        if target_kind == "page":
            return self._get(self.pages, target_id, "分镜页").current.version
        if target_kind == "script":
            return self._get(self.segments, target_id, "脚本段落").current.version
        if target_kind == "design":
            return self._get(self.designs, target_id, "人物设定").current["version"]
        if target_kind == "source":
            return self._get(self.sources, target_id, "史料").revision
        raise DomainError(f"未知的点评对象类型: {target_kind}")

    def sign_opinion(self, *, target_kind: str, target_id: str, scope: str,
                     stance: str, content: str, author: str, role: str,
                     target_version: int | None = None) -> Opinion:
        if role not in EXPERT_SCOPES:
            raise PermissionDenied(f"角色「{role}」不能签署专业意见")
        if EXPERT_SCOPES[role] != scope:
            raise PermissionDenied(
                f"{role}只能签署「{EXPERT_SCOPES[role]}」范围内的意见, 不能签署「{scope}」")
        current = self._target_version(target_kind, target_id)
        if target_version is None:
            target_version = current
        elif not 1 <= target_version <= current:
            raise NotFoundError(f"{target_kind}{target_id}没有版本v{target_version}")
        opinion = Opinion(
            id=self._new_id("OP"), target_kind=target_kind, target_id=target_id,
            target_version=target_version, scope=scope, stance=stance,
            content=content, author=author, role=role,
            outdated=(target_version != current),
        )
        opinion.history.append(f"{self._now().isoformat()} {author}({role}) 提出")
        self.opinions[opinion.id] = opinion
        return opinion

    def _opinion(self, opinion_id: str) -> Opinion:
        return self._get(self.opinions, opinion_id, "意见")

    def adopt_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能采纳")
        opinion.state = "采纳"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 采纳")
        self._detect_conflict(opinion)
        return opinion

    def reject_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能驳回")
        opinion.state = "驳回"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 驳回")
        return opinion

    def withdraw_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        opinion = self._opinion(opinion_id)
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以撤回意见")
        if opinion.state != "采纳":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能撤回")
        opinion.state = "撤回"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 撤回(原已采纳, 记录留档)")
        return opinion

    def delete_opinion(self, opinion_id: str, *, actor: str, role: str) -> None:
        opinion = self._opinion(opinion_id)
        if opinion.state in ("采纳", "撤回"):
            raise PermissionDenied(
                f"意见{opinion_id}曾被采纳(当前「{opinion.state}」), 任何人不得删除, 记录留档")
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以删除未采纳的意见")
        del self.opinions[opinion_id]

    # ----- 冲突会审 -----

    def _detect_conflict(self, opinion: Opinion) -> Issue | None:
        peers = [
            other for other in self.opinions.values()
            if other.id != opinion.id
            and other.state == "采纳"
            and other.target_kind == opinion.target_kind
            and other.target_id == opinion.target_id
            and other.scope == opinion.scope
            and other.stance != opinion.stance
        ]
        if not peers:
            return None
        for issue in self.issues.values():
            if (issue.target_kind, issue.target_id, issue.scope) == (
                    opinion.target_kind, opinion.target_id, opinion.scope) \
                    and issue.state == "待会审":
                issue.opinion_ids.append(opinion.id)
                issue.history.append(
                    f"{self._now().isoformat()} 意见{opinion.id}并入会审")
                return issue
        issue = Issue(
            id=self._new_id("IS"),
            target_kind=opinion.target_kind,
            target_id=opinion.target_id,
            scope=opinion.scope,
            subject=f"{opinion.target_kind}:{opinion.target_id} 的{opinion.scope}结论冲突",
            is_major_fact=(opinion.scope == "史实"),
            opinion_ids=[p.id for p in peers] + [opinion.id],
        )
        issue.history.append(f"{self._now().isoformat()} 冲突成立, 进入联合会审")
        self.issues[issue.id] = issue
        if opinion.target_kind == "page":
            page = self.pages[opinion.target_id]
            if page.status == "待评审":
                page.status = "联合会审"
                page.history.append(
                    f"{self._now().isoformat()} 因议题{issue.id}转入联合会审")
        return issue

    def close_issue(self, issue_id: str, *, decision: str, actor: str,
                    role: str) -> Issue:
        self._require_editor(role)
        issue = self._get(self.issues, issue_id, "议题")
        if issue.state != "待会审":
            raise StateError(f"议题{issue_id}已关闭, 不能重复关闭")
        if not decision:
            raise DomainError("关闭议题必须给出会审结论")
        issue.state = "已关闭"
        issue.decision = decision
        issue.decided_by = actor
        issue.history.append(f"{self._now().isoformat()} {actor} 关闭: {decision}")
        return issue

    def submit_objection(self, issue_id: str, *, content: str, evidence: str,
                         actor: str, role: str) -> Objection:
        if role != TRAINEE:
            raise PermissionDenied(f"角色「{role}」不能提交学员异议")
        issue = self._get(self.issues, issue_id, "议题")
        if not evidence:
            raise DomainError("异议必须附史料依据")
        self._get(self.sources, evidence, "史料")  # 依据必须指向已登记史料
        objection = Objection(
            id=self._new_id("OBJ"), author=actor, content=content,
            evidence=evidence, created_at=self._now(),
        )
        issue.objections.append(objection)
        if issue.state == "已关闭":
            issue.state = "待会审"
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交有依据异议, 议题重开")
            if issue.is_major_fact and issue.target_kind == "page":
                page = self.pages[issue.target_id]
                if page.status in ("精稿中", "可出版"):
                    page.status = "联合会审"
                    page.history.append(
                        f"{self._now().isoformat()} 重大事实议题{issue.id}重开, 退回联合会审")
        else:
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交异议{objection.id}")
        return objection

    # ----- 页面状态机与交付门禁 -----

    def _open_issues_for_page(self, page: Page, *, major_only: bool = False,
                              fact_only: bool = False,
                              version: PageVersion | None = None) -> list:
        ref = version or page.current
        targets = {("page", page.id)}
        targets |= {("script", sid) for sid in ref.script_refs}
        targets |= {("design", did) for did in ref.design_refs}
        targets |= {("source", sid) for sid in ref.source_refs}
        result = []
        for issue in self.issues.values():
            if issue.state != "待会审":
                continue
            if (issue.target_kind, issue.target_id) not in targets:
                continue
            if major_only and not issue.is_major_fact:
                continue
            if fact_only and issue.scope != "史实":
                continue
            result.append(issue)
        return result

    def _stale_dependencies(self, page: Page) -> list[str]:
        current = page.current
        problems = []
        for seg_id, version in current.script_refs.items():
            latest = self.segments[seg_id].current.version
            if version != latest:
                problems.append(f"脚本段落{seg_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for design_id, version in current.design_refs.items():
            latest = self.designs[design_id].current["version"]
            if version != latest:
                problems.append(f"人物设定{design_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for source_id, revision in current.source_refs.items():
            latest = self.sources[source_id].revision
            if revision != latest:
                problems.append(f"史料{source_id}依赖陈旧: 采用r{revision}, 当前r{latest}")
        return problems

    def page_blockers(self, page_id: str, *, scope: str | None = None,
                      on: date | None = None) -> list[str]:
        """返回页面当前不可交付的原因; 空列表表示可交付。"""
        page = self._get(self.pages, page_id, "分镜页")
        on = on or self._today()
        problems = [
            f"事实争议未关闭: {issue.id}({issue.subject})"
            for issue in self._open_issues_for_page(page, fact_only=True)
        ]
        problems.extend(self._stale_dependencies(page))
        for source_id in page.current.source_refs:
            source = self.sources[source_id]
            problems.extend(
                f"史料{source_id}: {problem}"
                for problem in source.license.problems(scope, on)
            )
        return problems

    def deliverable(self, page_id: str, *, scope: str | None = None,
                    on: date | None = None) -> bool:
        return not self.page_blockers(page_id, scope=scope, on=on)

    def transition_page(self, page_id: str, target: str, *, actor: str,
                        role: str) -> Page:
        self._require_editor(role)
        page = self._get(self.pages, page_id, "分镜页")
        if target not in PAGE_TRANSITIONS.get(page.status, set()):
            raise StateError(f"页面{page_id}不能从「{page.status}」转入「{target}」")
        if target == "精稿中":
            majors = self._open_issues_for_page(page, major_only=True)
            if majors:
                raise StateError(
                    "重大事实未关闭, 不能转入精稿: "
                    + ", ".join(issue.id for issue in majors))
        if target == "可出版":
            blockers = self.page_blockers(page_id)
            if blockers:
                raise StateError("页面不可交付, 不能转入可出版: " + "; ".join(blockers))
        page.status = target
        page.history.append(f"{self._now().isoformat()} {actor} 转入「{target}」")
        return page

    # ----- 追溯与导出 -----

    def panel_trace(self, page_id: str, panel_id: str) -> dict:
        page = self._get(self.pages, page_id, "分镜页")
        version = next(
            (v for v in page.versions if any(p.id == panel_id for p in v.panels)), None)
        if version is None:
            raise NotFoundError(f"画格{panel_id}不属于分镜页{page_id}")
        panel = next(p for p in version.panels if p.id == panel_id)
        scripts = []
        for seg_id, seg_version in version.script_refs.items():
            segment = self.segments[seg_id]
            text = segment.versions[seg_version - 1].text
            scripts.append({
                "segment_id": seg_id, "version": seg_version, "text": text,
                "latest_version": segment.current.version,
            })
        sources = []
        for source_id, revision in version.source_refs.items():
            source = self.sources[source_id]
            sources.append({
                "source_id": source_id, "title": source.title,
                "citation": source.citation, "revision": revision,
                "latest_revision": source.revision,
            })
        opinions = [
            {
                "opinion_id": o.id, "scope": o.scope, "stance": o.stance,
                "state": o.state, "author": o.author, "role": o.role,
                "decided_by": o.decided_by, "outdated": o.outdated,
            }
            for o in self.opinions.values()
            if o.target_kind == "page" and o.target_id == page_id
        ]
        issues = [
            {
                "issue_id": i.id, "subject": i.subject, "scope": i.scope,
                "state": i.state, "is_major_fact": i.is_major_fact,
                "decision": i.decision, "decided_by": i.decided_by,
            }
            for i in self.issues.values()
            if i.target_kind == "page" and i.target_id == page_id
        ]
        return {
            "page_id": page_id,
            "page_version": version.version,
            "panel": {"id": panel.id, "index": panel.index,
                      "sketch_ref": panel.sketch_ref},
            "scripts": scripts,
            "sources": sources,
            "opinions": opinions,
            "issues": issues,
            "deciders": sorted({
                x for x in [o.decided_by for o in self.opinions.values()
                            if o.target_kind == "page" and o.target_id == page_id]
                + [i.decided_by for i in self.issues.values()
                   if i.target_kind == "page" and i.target_id == page_id]
                if x
            }),
        }

    def export_batch(self, *, scope: str, page_ids: list | None = None,
                     actor: str, role: str, on: date | None = None) -> dict:
        """批量导出: 只包含当下获准的内容, 被排除的页面附原因。"""
        self._require_editor(role)
        on = on or self._today()
        if page_ids is None:
            candidates = [p for p in self.pages.values() if p.status == "可出版"]
        else:
            candidates = [self._get(self.pages, pid, "分镜页") for pid in page_ids]
        exported, excluded = [], {}
        for page in candidates:
            problems = []
            if page.status != "可出版":
                problems.append(f"页面状态为「{page.status}」, 未达到可出版")
            problems.extend(self.page_blockers(page.id, scope=scope, on=on))
            if problems:
                excluded[page.id] = problems
                continue
            version = page.current
            exported.append({
                "page_id": page.id,
                "title": page.title,
                "page_version": version.version,
                "panels": [
                    {"id": p.id, "index": p.index, "sketch_ref": p.sketch_ref}
                    for p in version.panels
                ],
                "sources": [
                    {
                        "source_id": sid,
                        "citation": self.sources[sid].citation,
                        "revision": rev,
                    }
                    for sid, rev in version.source_refs.items()
                ],
                "scripts": [
                    {"segment_id": sid, "version": v}
                    for sid, v in version.script_refs.items()
                ],
            })
        return {
            "scope": scope,
            "exported_at": self._now().isoformat(),
            "exported_by": actor,
            "pages": exported,
            "excluded": excluded,
        }

    # ============================================================
    # 海外发行: 版本化依据 / 禁运 / 冻结批次 / 撤回与替代 / 乱序重算
    # ============================================================

    def _require_release(self, role: str):
        if role not in RELEASE_ROLES:
            raise PermissionDenied(f"角色「{role}」无权执行海外发行操作")

    @staticmethod
    def _version_at(versions: list, on: date) -> dict | None:
        """按生效时间取版本: 生效日 <= on 的最大版本; 全部未生效则 None。"""
        chosen = None
        for ver in versions:
            if ver["effective_at"] <= on and (
                    chosen is None or ver["version"] > chosen["version"]):
                chosen = ver
        return chosen

    # ----- 市场规则 / 合作方授权 / 发行窗口(均版本化, 带生效时间) -----

    def create_market_rule(self, *, market: str, note: str = "",
                           blocked_pages=(), effective_at: date | None = None,
                           actor: str, role: str) -> MarketRuleSet:
        self._require_release(role)
        if any(r.market == market for r in self.market_rules.values()):
            raise DomainError(f"市场「{market}」已存在规则集, 请改用新版本")
        rule = MarketRuleSet(id=self._new_id("MR"), market=market)
        rule.versions.append({
            "id": rule.id, "version": 1, "note": note,
            "blocked_pages": frozenset(blocked_pages),
            "effective_at": effective_at or self._today(),
        })
        self.market_rules[rule.id] = rule
        return rule

    def new_rule_version(self, rule_id: str, *, note: str = "", blocked_pages=(),
                         effective_at: date | None = None, base_version: int,
                         actor: str, role: str) -> dict:
        self._require_release(role)
        rule = self._get(self.market_rules, rule_id, "市场规则")
        if base_version != rule.current["version"]:
            raise StaleVersionError(
                f"市场规则{rule_id}当前为v{rule.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        for pid in blocked_pages:
            self._get(self.pages, pid, "分镜页")
        version = {
            "id": rule.id, "version": base_version + 1, "note": note,
            "blocked_pages": frozenset(blocked_pages),
            "effective_at": effective_at or self._today(),
        }
        rule.versions.append(version)
        if version["effective_at"] <= self._today():
            self._recalc_batches(market=rule.market, on=self._today())
        return version

    def create_partner(self, *, name: str, markets=(), expires_at: date | None = None,
                       effective_at: date | None = None, actor: str,
                       role: str) -> PartnerGrant:
        self._require_release(role)
        partner = PartnerGrant(id=self._new_id("PT"), name=name)
        partner.versions.append({
            "id": partner.id, "name": name, "version": 1,
            "markets": frozenset(markets), "expires_at": expires_at,
            "effective_at": effective_at or self._today(),
        })
        self.partners[partner.id] = partner
        return partner

    def new_partner_version(self, partner_id: str, *, markets=(),
                            expires_at: date | None = None,
                            effective_at: date | None = None, base_version: int,
                            actor: str, role: str) -> dict:
        self._require_release(role)
        partner = self._get(self.partners, partner_id, "合作方")
        if base_version != partner.current["version"]:
            raise StaleVersionError(
                f"合作方授权{partner_id}当前为v{partner.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        version = {
            "id": partner.id, "name": partner.name,
            "version": base_version + 1, "markets": frozenset(markets),
            "expires_at": expires_at,
            "effective_at": effective_at or self._today(),
        }
        partner.versions.append(version)
        if version["effective_at"] <= self._today():
            self._recalc_batches(partner_id=partner_id, on=self._today())
        return version

    def create_window(self, *, market: str, opens_at: date, closes_at: date,
                      effective_at: date | None = None, actor: str,
                      role: str) -> ReleaseWindow:
        self._require_release(role)
        if closes_at < opens_at:
            raise DomainError("发行窗口关闭日不能早于开启日")
        window = ReleaseWindow(id=self._new_id("RW"), market=market)
        window.versions.append({
            "id": window.id, "version": 1, "opens_at": opens_at,
            "closes_at": closes_at,
            "effective_at": effective_at or self._today(),
        })
        self.windows[window.id] = window
        return window

    def new_window_version(self, window_id: str, *, opens_at: date, closes_at: date,
                           effective_at: date | None = None, base_version: int,
                           actor: str, role: str) -> dict:
        self._require_release(role)
        window = self._get(self.windows, window_id, "发行窗口")
        if base_version != window.current["version"]:
            raise StaleVersionError(
                f"发行窗口{window_id}当前为v{window.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        if closes_at < opens_at:
            raise DomainError("发行窗口关闭日不能早于开启日")
        version = {
            "id": window.id, "version": base_version + 1,
            "opens_at": opens_at, "closes_at": closes_at,
            "effective_at": effective_at or self._today(),
        }
        window.versions.append(version)
        return version

    # ----- 禁运通知 -----

    def issue_embargo(self, *, market: str, reason: str, effective_at: date,
                      page_ids=None, actor: str, role: str) -> EmbargoNotice:
        self._require_release(role)
        embargo = EmbargoNotice(
            id=self._new_id("EB"), market=market,
            page_ids=(frozenset(page_ids) if page_ids is not None else None),
            reason=reason, effective_at=effective_at, issued_by=actor,
        )
        embargo.history.append(
            f"{self._now().isoformat()} {actor} 发布禁运, {effective_at.isoformat()}起生效")
        self.embargoes[embargo.id] = embargo
        if effective_at <= self._today():
            self._recalc_batches(market=market, on=self._today())
        return embargo

    def lift_embargo(self, embargo_id: str, *, lifted_at: date | None = None,
                     actor: str, role: str) -> EmbargoNotice:
        self._require_release(role)
        embargo = self._get(self.embargoes, embargo_id, "禁运通知")
        lifted_at = lifted_at or self._today()
        if lifted_at < embargo.effective_at:
            raise DomainError("解禁日不能早于禁运生效日")
        embargo.lifted_at = lifted_at
        embargo.history.append(
            f"{self._now().isoformat()} {actor} 解禁, {lifted_at.isoformat()}起解除")
        if lifted_at <= self._today():
            self._recalc_batches(market=embargo.market, on=self._today())
        return embargo

    # ----- 发布批次: 冻结清单 -----

    @staticmethod
    def _license_snapshot(source: HistoricalSource) -> dict:
        lic = source.license
        return {
            "revision": source.revision,
            "publication_scopes": frozenset(lic.publication_scopes),
            "expires_at": lic.expires_at,
            "confidential_until": lic.confidential_until,
        }

    def _window_for(self, market: str, on: date, window_id: str | None) -> dict:
        candidates = []
        for window in self.windows.values():
            if window.market != market:
                continue
            if window_id is not None and window.id != window_id:
                continue
            version = self._version_at(window.versions, on)
            if version is not None:
                candidates.append(version)
        if not candidates:
            raise DomainError(f"市场「{market}」在{on.isoformat()}没有生效的发行窗口")
        if window_id is None and len({v["id"] for v in candidates}) > 1:
            raise DomainError(f"市场「{market}」存在多个发行窗口, 须用 window_ids 指定")
        return max(candidates, key=lambda v: v["version"])

    def create_release_batch(self, *, markets, partner_ids, page_ids=None,
                             window_ids=None, actor: str, role: str,
                             on: date | None = None) -> ReleaseBatch:
        """冻结页面版本、史料许可、发行窗口、合作方授权与市场规则版本形成批次。"""
        self._require_release(role)
        on = on or self._today()
        window_ids = window_ids or {}
        partners = [self._get(self.partners, pid, "合作方") for pid in partner_ids]
        if page_ids is None:
            pages = [p for p in self.pages.values() if p.status == "可出版"]
        else:
            pages = [self._get(self.pages, pid, "分镜页") for pid in page_ids]

        frozen_rules, frozen_windows = {}, {}
        for market in markets:
            rule = next((r for r in self.market_rules.values() if r.market == market), None)
            if rule is None:
                raise DomainError(f"市场「{market}」没有市场规则集")
            frozen_rules[market] = self._version_at(rule.versions, on)
            if frozen_rules[market] is None:
                raise DomainError(f"市场「{market}」的规则在{on.isoformat()}尚未生效")
            win = self._window_for(market, on, window_ids.get(market))
            frozen_windows[market] = {
                "id": win["id"], "version": win["version"],
                "opens_at": win["opens_at"], "closes_at": win["closes_at"],
            }

        frozen_grants = {}
        for partner in partners:
            grant_v = self._version_at(partner.versions, on) or partner.current
            frozen_grants[partner.id] = {
                "id": partner.id, "name": partner.name,
                "version": grant_v["version"], "markets": grant_v["markets"],
                "expires_at": grant_v["expires_at"],
                "effective_at": grant_v["effective_at"],
            }

        batch = ReleaseBatch(
            id=self._new_id("RL"), markets=list(markets), frozen_at=on,
            created_at=self._now(), created_by=actor,
            window_id=",".join(frozen_windows[m]["id"] for m in markets),
            window=frozen_windows,
            rules={
                m: {
                    "id": frozen_rules[m]["id"], "version": frozen_rules[m]["version"],
                    "note": frozen_rules[m]["note"],
                    "blocked_pages": frozen_rules[m]["blocked_pages"],
                }
                for m in markets
            },
            grants=frozen_grants,
        )

        for partner in partners:
            grant_v = self._version_at(partner.versions, on) or partner.current
            for market in markets:
                rule_v = frozen_rules[market]
                for page in pages:
                    key = f"{partner.id}:{market}:{page.id}"
                    barriers = self._line_barriers(
                        partner.id, market, page, page.current.version,
                        on=on, rule_v=rule_v, grant_v=grant_v)
                    if barriers:
                        batch.freeze_exclusions[key] = barriers
                        continue
                    batch.lines.append(self._make_line(
                        batch, partner, market, page, replaces=None))
        batch.history.append(
            f"{self._now().isoformat()} {actor} 冻结批次, "
            f"{len(batch.lines)}条素材线, 窗口={batch.window_id}")
        self.batches[batch.id] = batch
        return batch

    def _make_line(self, batch: ReleaseBatch, partner: PartnerGrant, market: str,
                   page: Page, *, replaces: str | None) -> ReleaseLine:
        return ReleaseLine(
            id=self._new_id("LN"), partner_id=partner.id, market=market,
            page_id=page.id, page_version=page.current.version,
            source_licenses={
                sid: self._license_snapshot(self.sources[sid])
                for sid in page.current.source_refs
            },
            replaces=replaces,
        )

    # ----- 壁垒评估: 禁运 / 规则 / 授权 / 许可收缩 / 事实争议 -----

    def _line_barriers(self, partner_id: str, market: str, page: Page,
                       page_version: int, *, on: date, rule_v, grant_v) -> list:
        barriers = []
        for embargo in self.embargoes.values():
            if embargo.market == market and embargo.active(on) \
                    and embargo.hits(page.id):
                barriers.append({
                    "type": "禁运", "ref": embargo.id, "reason": embargo.reason,
                    "effective_at": embargo.effective_at,
                    "lifted_at": embargo.lifted_at,
                })
        if rule_v is not None and page.id in rule_v["blocked_pages"]:
            barriers.append({
                "type": "市场规则", "ref": rule_v["id"],
                "rule_version": rule_v["version"], "reason": rule_v["note"],
            })
        if grant_v is None:
            barriers.append({"type": "合作方授权", "ref": partner_id,
                             "reason": "授权在该日尚未生效"})
        elif market not in grant_v["markets"]:
            barriers.append({"type": "合作方授权", "ref": partner_id,
                             "reason": f"授权范围不含市场「{market}」"})
        elif grant_v["expires_at"] is not None and on > grant_v["expires_at"]:
            barriers.append({"type": "合作方授权", "ref": partner_id,
                             "reason": f"授权已于{grant_v['expires_at'].isoformat()}到期"})
        if page.status != "可出版":
            barriers.append({"type": "页面状态", "ref": page.id,
                             "reason": f"页面状态为「{page.status}」, 未达到可出版"})
        version = next((v for v in page.versions if v.version == page_version), None)
        if version is None:
            barriers.append({"type": "页面更正", "ref": page.id,
                             "reason": f"冻结的v{page_version}已不存在"})
        else:
            for issue in self._open_issues_for_page(page, fact_only=True,
                                                    version=version):
                barriers.append({"type": "事实争议", "ref": issue.id,
                                 "reason": issue.subject})
            # 许可收缩: 以史料当前许可判断冻结素材是否仍允许进入该市场
            for source_id in version.source_refs:
                for problem in self.sources[source_id].license.problems(market, on):
                    barriers.append({"type": "史料许可", "ref": source_id,
                                     "reason": problem})
        return barriers

    @staticmethod
    def _barrier_key(barrier: dict) -> tuple:
        return barrier["type"], barrier["ref"]

    def _emit_notice(self, batch: ReleaseBatch, line: ReleaseLine,
                     notice_type: str, reason: str, effective_at: date,
                     dedup: str | None = None):
        key = f"{notice_type}:{dedup or line.id}"
        if key in batch.notice_keys:
            return None  # 同一批次重放不重复通知
        notice = ReleaseNotification(
            id=self._new_id("NT"), key=key, type=notice_type, line_id=line.id,
            partner_id=line.partner_id, market=line.market, page_id=line.page_id,
            reason=reason, effective_at=effective_at, created_at=self._now(),
        )
        batch.notices.append(notice)
        batch.notice_keys.add(key)
        return notice

    def _replace_line(self, batch: ReleaseBatch, line: ReleaseLine,
                      on: date) -> ReleaseLine:
        """页面已更正: 旧线冻结为「已替代」, 追加一条采用当前版本的新线。"""
        page = self._get(self.pages, line.page_id, "分镜页")
        partner = self._get(self.partners, line.partner_id, "合作方")
        new_line = self._make_line(batch, partner, line.market, page,
                                   replaces=line.id)
        batch.lines.append(new_line)
        line.replaced_by = new_line.id
        line.status = "已替代"
        batch.history.append(
            f"{self._now().isoformat()} 页面{line.page_id}已更正至"
            f"v{new_line.page_version}: 线{line.id}由线{new_line.id}替代, 旧内容不复活")
        return new_line

    def _recalc_batch(self, batch: ReleaseBatch, on: date):
        """按 on 时点的各依据版本重算未完成(及已发布/已撤回)的素材线。"""
        for line in list(batch.lines):
            if line.status == "已替代":
                continue
            page = self._get(self.pages, line.page_id, "分镜页")
            corrected = page.current.version != line.page_version

            # 尚未发出的素材遇到页面更正: 直接改用更正后的新版本;
            # 已撤回的素材即使后来更正也保持撤回, 由经理显式追加替代版本,
            # 解禁或许可恢复时旧版本绝不复活。
            if corrected and line.status == "待发布":
                self._replace_line(batch, line, on)
                continue

            partner = self._get(self.partners, line.partner_id, "合作方")
            frozen_rule = batch.rules[line.market]
            rule_set = self.market_rules.get(frozen_rule["id"])
            rule_v = self._version_at(rule_set.versions, on) if rule_set else None
            grant_v = self._version_at(partner.versions, on)
            barriers = self._line_barriers(
                line.partner_id, line.market, page, line.page_version,
                on=on, rule_v=rule_v, grant_v=grant_v)

            if line.status == "已发布":
                if barriers:
                    line.status = "已撤回"
                    known = {self._barrier_key(b) for b in line.barriers}
                    fresh = []
                    for barrier in barriers:
                        if self._barrier_key(barrier) not in known:
                            line.barriers.append(barrier)
                            fresh.append(barrier)
                    # 每个新命中的壁垒各发一条撤回通知; 重放时壁垒已留档, 不再通知
                    for barrier in fresh:
                        cause = f"{barrier['type']}:{barrier['reason']}"
                        self._emit_notice(
                            batch, line, "撤回通知", cause, on,
                            dedup=f"{line.id}:{barrier['type']}:{barrier['ref']}")
                    batch.history.append(
                        f"{self._now().isoformat()} 线{line.id}("
                        f"{line.market}/{line.page_id} v{line.page_version})撤回: "
                        + "; ".join(f"{b['type']}:{b['reason']}" for b in barriers))
            elif line.status == "已撤回":
                # 页面已更正的撤回线永不自动复活, 须由经理显式追加替代版本;
                # 仅版本未变且壁垒清除、窗口仍开时才恢复原素材。
                if not barriers and not corrected:
                    window = batch.window[line.market]
                    window_open = window["opens_at"] <= on <= window["closes_at"]
                    if window_open:
                        line.status = "已发布"
                        line.ever_published = True
                        batch.history.append(
                            f"{self._now().isoformat()} 壁垒清除, 线{line.id}"
                            f"(v{line.page_version}仍为当前版本)恢复发布, 不重复通知")
            elif line.status == "待发布":
                # 留档当前壁垒, 供反查为何尚未发出(状态不变, 待壁垒清除后发布)
                line.barriers = barriers

    def _recalc_batches(self, *, market: str | None = None,
                        partner_id: str | None = None, page_id: str | None = None,
                        on: date | None = None):
        on = on or self._today()
        for batch in self.batches.values():
            if market is not None and market not in batch.markets:
                continue
            hit = any(
                line for line in batch.lines
                if (partner_id is None or line.partner_id == partner_id)
                and (page_id is None or line.page_id == page_id))
            if (partner_id is not None or page_id is not None) and not hit:
                continue
            self._recalc_batch(batch, on)

    def recalculate_batch(self, batch_id: str, *, on: date | None = None,
                          actor: str, role: str) -> dict:
        self._require_release(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        self._recalc_batch(batch, on or self._today())
        return self.batch_trace(batch_id)

    def publish_batch(self, batch_id: str, *, actor: str, role: str,
                      on: date | None = None) -> dict:
        """推进窗口已开、壁垒已清的待发布线; 通知按线幂等。"""
        self._require_release(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        on = on or self._today()
        self._recalc_batch(batch, on)
        published = []
        for line in list(batch.lines):
            if line.status != "待发布":
                continue
            page = self._get(self.pages, line.page_id, "分镜页")
            partner = self._get(self.partners, line.partner_id, "合作方")
            rule_v = self._version_at(
                self.market_rules[batch.rules[line.market]["id"]].versions, on)
            grant_v = self._version_at(partner.versions, on)
            barriers = self._line_barriers(
                line.partner_id, line.market, page, line.page_version,
                on=on, rule_v=rule_v, grant_v=grant_v)
            window = batch.window[line.market]
            if barriers or not (window["opens_at"] <= on <= window["closes_at"]):
                continue
            line.status = "已发布"
            line.ever_published = True
            notice_type = "发布通知"
            if line.replaces:
                replaced = next((l for l in batch.lines if l.id == line.replaces), None)
                if replaced is not None and replaced.ever_published:
                    # 合作方此前确已收到被替代的旧版, 才发"替代通知"
                    notice_type = "替代通知"
            reason = (f"采用更正后v{line.page_version}" if notice_type == "替代通知"
                      else f"窗口{window['id']}v{window['version']}内发布")
            self._emit_notice(batch, line, notice_type, reason, on)
            published.append(line.id)
        batch.history.append(
            f"{self._now().isoformat()} {actor} 推进发布: {len(published)}条")
        return {"batch_id": batch.id, "published": published}

    def provide_alternative(self, batch_id: str, line_id: str, *,
                            actor: str, role: str) -> ReleaseLine:
        """为已撤回素材追加采用当前(更正后)页面版本的替代线, 旧线保持撤回。"""
        self._require_release(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        old = self._get_line(batch, line_id)
        if old.status != "已撤回":
            raise StateError(f"素材线{line_id}当前为「{old.status}」, 仅已撤回线可追加替代版本")
        page = self._get(self.pages, old.page_id, "分镜页")
        partner = self._get(self.partners, old.partner_id, "合作方")
        if page.current.version == old.page_version and not old.replaced_by:
            raise DomainError("页面尚无更正版本, 无需替代")
        line = self._make_line(batch, partner, old.market, page, replaces=old.id)
        batch.lines.append(line)
        old.replaced_by = line.id
        old.status = "已替代"
        batch.history.append(
            f"{self._now().isoformat()} {actor} 为撤回线{old.id}追加替代线{line.id}"
            f"(v{line.page_version}), 旧内容标记已替代且不复活")
        return line

    # ----- 合作方回执(乱序/迟到) -----

    def _get_line(self, batch: ReleaseBatch, line_id: str) -> ReleaseLine:
        line = next((l for l in batch.lines if l.id == line_id), None)
        if line is None:
            raise NotFoundError(f"批次{batch.id}内没有素材线{line_id}")
        return line

    def record_receipt(self, batch_id: str, line_id: str, *, step: str,
                       effective_at: date | None = None, actor: str,
                       role: str) -> Receipt:
        if role not in RELEASE_ROLES and role != PARTNER_ROLE:
            raise PermissionDenied(f"角色「{role}」不能登记合作方回执")
        if step not in RECEIPT_STEPS:
            raise DomainError(f"未知回执步骤: {step}")
        batch = self._get(self.batches, batch_id, "发布批次")
        line = self._get_line(batch, line_id)
        on = effective_at or self._today()
        for existing in line.receipts:
            if existing.step == step and existing.effective_at == on:
                return existing  # 同一回执重放: 不重复登记
        new_index = RECEIPT_STEPS.index(step)
        late = any(
            existing.effective_at > on
            or RECEIPT_STEPS.index(existing.step) > new_index
            for existing in line.receipts
        )
        receipt = Receipt(
            id=self._new_id("RC"), partner_id=line.partner_id, line_id=line.id,
            step=step, effective_at=on, recorded_at=self._now(), late=late,
        )
        line.receipts.append(receipt)
        # 回执只记录确认进度, 绝不改变素材线状态(迟到回执不能复活撤回内容)
        return receipt

    @staticmethod
    def _last_step(receipts: list) -> str | None:
        if not receipts:
            return None
        ordered = sorted(
            receipts, key=lambda r: (RECEIPT_STEPS.index(r.step), r.effective_at))
        return ordered[-1].step

    # ----- 批次反查与导出 -----

    def _line_view(self, batch: ReleaseBatch, line: ReleaseLine) -> dict:
        page = self.pages.get(line.page_id)
        partner = self.partners.get(line.partner_id)
        return {
            "line_id": line.id,
            "partner_id": line.partner_id,
            "partner_name": partner.name if partner else None,
            "market": line.market,
            "page_id": line.page_id,
            "page_title": page.title if page else None,
            "frozen_page_version": line.page_version,
            "current_page_version": page.current.version if page else None,
            "source_licenses": line.source_licenses,
            "status": line.status,
            "replaces": line.replaces,
            "replaced_by": line.replaced_by,
            "barriers": line.barriers,
            "last_step": self._last_step(line.receipts),
            "receipts": [
                {
                    "receipt_id": r.id, "step": r.step,
                    "effective_at": r.effective_at, "late": r.late,
                }
                for r in sorted(line.receipts, key=lambda x: x.effective_at)
            ],
        }

    def batch_trace(self, batch_id: str) -> dict:
        """反查批次冻结依据, 以及每个合作方/每条素材线最后确认到了哪一步。"""
        batch = self._get(self.batches, batch_id, "发布批次")
        lines = [self._line_view(batch, l) for l in batch.lines]
        partners = []
        for partner_id in batch.grants:
            partner_lines = [l for l in lines if l["partner_id"] == partner_id]
            receipts = [r for l in batch.lines if l.partner_id == partner_id
                        for r in l.receipts]
            partners.append({
                "partner_id": partner_id,
                "name": batch.grants[partner_id]["name"],
                "frozen_grant": batch.grants[partner_id],
                "last_step": self._last_step(receipts),
                "lines": [
                    {
                        "line_id": l["line_id"], "market": l["market"],
                        "page_id": l["page_id"],
                        "frozen_page_version": l["frozen_page_version"],
                        "status": l["status"], "last_step": l["last_step"],
                    }
                    for l in partner_lines
                ],
            })
        return {
            "batch_id": batch.id,
            "frozen_at": batch.frozen_at,
            "created_by": batch.created_by,
            "frozen_window": batch.window,
            "frozen_rules": batch.rules,
            "frozen_grants": batch.grants,
            "freeze_exclusions": batch.freeze_exclusions,
            "lines": lines,
            "partners": partners,
            "notices": [
                {
                    "notice_id": n.id, "type": n.type, "line_id": n.line_id,
                    "partner_id": n.partner_id, "market": n.market,
                    "page_id": n.page_id, "reason": n.reason,
                    "effective_at": n.effective_at,
                }
                for n in batch.notices
            ],
            "history": batch.history,
        }

    def export_release(self, batch_id: str, *, market: str,
                       actor: str, role: str, on: date | None = None) -> dict:
        """导出某市场当前确实允许的材料; 撤回/替代/壁垒未清的一律排除并附原因。"""
        self._require_release(role)
        batch = self._get(self.batches, batch_id, "发布批次")
        on = on or self._today()
        self._recalc_batch(batch, on)
        exported, excluded = [], {}
        for line in batch.lines:
            if line.market != market or line.status == "已替代":
                continue
            page = self._get(self.pages, line.page_id, "分镜页")
            partner = self._get(self.partners, line.partner_id, "合作方")
            rule_v = self._version_at(
                self.market_rules[batch.rules[market]["id"]].versions, on)
            grant_v = self._version_at(partner.versions, on)
            barriers = self._line_barriers(
                line.partner_id, market, page, line.page_version,
                on=on, rule_v=rule_v, grant_v=grant_v)
            if line.status != "已发布" or barriers:
                reasons = [f"素材线状态为「{line.status}」"] if line.status != "已发布" else []
                reasons.extend(f"{b['type']}: {b['reason']}" for b in barriers)
                excluded[line.id] = reasons
                continue
            exported.append({
                "line_id": line.id,
                "partner_id": line.partner_id,
                "page_id": line.page_id,
                "title": page.title,
                "page_version": line.page_version,
                "panels": [
                    {"id": p.id, "index": p.index, "sketch_ref": p.sketch_ref}
                    for p in page.versions[line.page_version - 1].panels
                ],
                "sources": [
                    {"source_id": sid, **{
                        k: (sorted(val) if isinstance(val, frozenset) else val)
                        for k, val in snap.items()}}
                    for sid, snap in line.source_licenses.items()
                ],
                "rule_basis": {
                    "rule_id": batch.rules[market]["id"],
                    "frozen_version": batch.rules[market]["version"],
                    "effective_version": rule_v["version"] if rule_v else None,
                },
                "grant_basis": {
                    "partner_id": line.partner_id,
                    "frozen_version": batch.grants[line.partner_id]["version"],
                    "effective_version": grant_v["version"] if grant_v else None,
                },
            })
        return {
            "batch_id": batch.id, "market": market,
            "exported_at": self._now().isoformat(), "exported_by": actor,
            "materials": exported, "excluded": excluded,
        }
