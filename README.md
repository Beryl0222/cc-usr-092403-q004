# 连环画创作审稿

维护史料、脚本、分镜和多学科审稿意见之间的版本依赖。

## 运行

- `python3 service.py --check` 检查基础配置。
- `python3 service.py --port 8000` 启动服务;`GET /health` 为健康检查。
- `npm test` 运行全部测试(服务契约、领域规则、HTTP 接口)。

## 领域规则(domain.py)

- **版本互引**:脚本段落、人物设定、分镜页均按版本演进;页面版本记录其采用的
  脚本版本、设定版本与史料修订号,依赖陈旧即不可交付。
- **专业签署**:党史/军史专家(史实)、作家/文学编辑(文字)、画家/美术编辑(画面)
  只签署自己专业范围内的意见;编辑负责采纳、驳回与状态流转。
- **意见留档**:意见状态为 提出 → 采纳/驳回,采纳后可撤回;曾被采纳的意见
  (含已撤回)任何人不得删除。
- **冲突会审**:同一对象同一专业范围内结论冲突的已采纳意见自动生成会审议题,
  页面转入联合会审;重大事实议题未关闭不得转入精稿。学员可提交附史料依据的
  异议,已关闭的议题会被重开,已进入精稿/可出版的页面退回联合会审。
- **授权检查**:素材授权期、保密期、出版范围随页面版本在交付与导出时检查;
  过期授权、事实争议或依赖陈旧的页面明确保持不可交付。
- **批量导出**:`export_batch` 只包含当下获准的内容,被排除的页面附原因。
- **全程追溯**:`panel_trace` 从任一画格反查采用的史料、文字版本、
  采纳/撤回的意见与决定人。
- **并发与乱序**:所有改稿操作携带 `base_version`/`base_revision` 做乐观并发
  校验(`StaleVersionError`);针对旧版本的迟到点评保留并标记 `outdated`。

## 海外发行(domain.py 的发行主线)

- **依据版本化**:市场规则(`create_market_rule`/`new_rule_version`)、合作方授权
  (`create_partner`/`new_partner_version`)、发行窗口(`create_window`/
  `new_window_version`)均按版本演进并带 `effective_at`;取版本时按生效时间选择,
  乱序到达不会让未生效的规则提前作用。
- **批次冻结**:`create_release_batch` 冻结当时的页面版本、每条素材采用的史料许可
  快照、发行窗口版本、各市场规则版本与合作方授权版本。冻结之后既有批次保留原清单,
  禁运或许可收缩只追加撤回通知、合作方回执与替代版本,绝不回写冻结清单。
- **禁运与撤回**:`issue_embargo` 可整市场或仅命中指定页面,只改变命中的市场与素材,
  其他市场继续发行。`lift_embargo` 解禁。撤回按壁垒逐条追加幂等通知。
- **更正与替代**:撤回期间页面被更正(`new_page_version`)时,旧版本素材在解禁/许可
  恢复后也不复活;由发行经理 `provide_alternative` 显式追加采用更正版本的替代线,
  旧线转为「已替代」。尚未发出的素材遇到更正则在重算时直接改用新版本。
- **乱序重算**:页面更正、规则变更、许可收缩、迟到回执按各自生效时间重算尚未完成的
  发布(`recalculate_batch` 或后续操作时自动进行);迟到回执只标记 `late` 并记录,
  不改变素材状态,旧消息不能复活已撤回内容;同一批次重放不重复通知、不重复登记回执。
- **导出**:`export_release` 只返回指定市场当前确实允许的材料,撤回/待发布/壁垒未清
  的素材被排除并附原因,已替代的旧线不再出现。
- **批次反查**:`batch_trace` 反查任一交付批次采用的页面版本、史料许可快照、窗口/规则/
  授权依据,以及每个合作方、每条素材最后确认到了哪一步(已接收/已下载/撤回已确认/
  替代已确认)。

发行类操作限「编辑」或「海外发行经理」;合作方只能 `record_receipt` 登记回执。

## HTTP 接口

`POST /api/<action>`,JSON 请求与响应。action 与领域方法一一对应:
`register_source`、`revise_source`、`create_segment`、`new_segment_version`、
`create_design`、`new_design_version`、`create_page`、`new_page_version`、
`add_panel`、`sign_opinion`、`adopt_opinion`、`reject_opinion`、
`withdraw_opinion`、`delete_opinion`、`close_issue`、`submit_objection`、
`transition_page`、`page_blockers`、`panel_trace`、`export_batch`;
海外发行:`create_market_rule`、`new_rule_version`、`create_partner`、
`new_partner_version`、`create_window`、`new_window_version`、`issue_embargo`、
`lift_embargo`、`create_release_batch`、`publish_batch`、`provide_alternative`、
`record_receipt`、`recalculate_batch`、`batch_trace`、`export_release`。

日期字段(`effective_at`、`opens_at`、`closes_at`、`expires_at`、
`confidential_until`、`lifted_at`、`on`)用 `YYYY-MM-DD` 字符串传递。

错误映射:404 对象不存在,403 越权,409 版本冲突,400 其他领域规则或请求格式错误。

`fixtures/domain.json` 保存领域名词和状态样例,便于接口联调时保持一致语义。

## 运行与检查

```bash
python3 service.py --check
npm test
python3 -m compileall -q .
```
