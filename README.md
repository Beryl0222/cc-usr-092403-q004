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

## 发行与禁运(release.py)

- **批次冻结**:发布批次冻结所采用的页面版本、史料许可(修订号与条款)、发行窗口、
  合作方授权与市场规则版本;冻结后不改写原清单,只追加撤回通知、合作方回执与替代版本。
- **禁运与许可收缩**:禁运按市场+素材命中,许可收缩只影响失去覆盖的市场与引用该史料的
  页面;其他市场与素材不受影响,既有批次保留原清单。
- **生效时间重算**:禁运/解禁、许可修订(`revise_source` 可带 `effective_at`)、规则版本、
  替代版本均携带生效时间;乱序到达时未完结批次按生效时间重算每个 (市场, 页面) 单元——
  某时刻单元被撤回,当且仅当该时刻存在生效中的撤回原因,因此旧消息不会复活已撤回内容。
- **更正不误恢复**:解禁时若页面已被更正(冻结版本陈旧)且未追加替代版本,单元保持
  已撤回;追加替代版本后按替代版本恢复(通知类型为「替换」)。
- **幂等通知**:通知按 (批次, 市场, 页面, 撤回区间, 合作方) 的确定性键去重;
  各发行操作接受 `message_id`,重放同一消息或重算同一批次不会产生重复通知。
- **导出与反查**:`export_delivery` 只返回当前范围确实允许的材料(单元状态、页面状态、
  事实争议、当前许可、当前禁运与规则、合作方授权逐项检查),首次成功导出自动登记
  合作方「已下载」回执;`batch_trace` 反查冻结清单、许可与规则依据、通知/替代/回执,
  以及每个合作方最后确认到了哪一步(迟到回执按生效时间归位,不会拉低进度)。
- **完结**:`close_batch` 要求所有撤回通知均已被合作方确认(已收讫/已下架/已替换);
  已完结批次不再重算,迟到的回执仍可补录。发行操作仅「发行经理」角色可执行。

## HTTP 接口

`POST /api/<action>`,JSON 请求与响应。action 与领域方法一一对应:
`register_source`、`revise_source`、`create_segment`、`new_segment_version`、
`create_design`、`new_design_version`、`create_page`、`new_page_version`、
`add_panel`、`sign_opinion`、`adopt_opinion`、`reject_opinion`、
`withdraw_opinion`、`delete_opinion`、`close_issue`、`submit_objection`、
`transition_page`、`page_blockers`、`panel_trace`、`export_batch`,
以及发行域的 `register_market_rule`、`register_embargo`、`lift_embargo`、
`create_release_batch`、`append_replacement`、`record_receipt`、`close_batch`、
`export_delivery`、`batch_trace`。

错误映射:404 对象不存在,403 越权,409 版本冲突,400 其他领域规则或请求格式错误。

`fixtures/domain.json` 保存领域名词和状态样例,便于接口联调时保持一致语义。

## 运行与检查

```bash
python3 service.py --check
npm test
python3 -m compileall -q .
```
