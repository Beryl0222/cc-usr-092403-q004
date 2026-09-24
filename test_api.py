"""通过 HTTP 接口验证领域操作的端到端行为。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import ReviewSystem
from service import make_handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ReviewSystem()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, action, payload=None):
        request = Request(
            f"{self.base_url}/api/{action}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _seed_page(self):
        _, source = self.call("register_source", {
            "title": "战斗详报", "citation": "《战史》p12",
            "license": {"publication_scopes": ["国内", "海外"]},
            "actor": "王编辑", "role": "编辑",
        })
        _, segment = self.call("create_segment", {
            "title": "夜袭", "text": "拂晓进入阵地。", "actor": "学员甲",
        })
        _, design = self.call("create_design", {
            "name": "连长", "brief": "三十岁。", "actor": "学员甲",
        })
        _, page = self.call("create_page", {
            "title": "第3页",
            "script_refs": {segment["id"]: 1},
            "design_refs": {design["id"]: 1},
            "source_refs": {source["id"]: 1},
            "actor": "学员甲",
        })
        return source, segment, design, page

    def test_out_of_scope_signature_is_forbidden(self):
        _, _, _, page = self._seed_page()
        status, body = self.call("sign_opinion", {
            "target_kind": "page", "target_id": page["id"], "scope": "史实",
            "stance": "x", "content": "作家越权", "author": "赵作家", "role": "作家",
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["kind"], "PermissionDenied")

    def test_unknown_action_and_bad_payload(self):
        request = Request(f"{self.base_url}/api/nope", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        status, body = self.call("create_segment", {"unexpected": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["kind"], "BadRequest")

    def test_review_flow_to_export_over_http(self):
        source, segment, _, page = self._seed_page()
        _, opinion = self.call("sign_opinion", {
            "target_kind": "page", "target_id": page["id"], "scope": "史实",
            "stance": "日期为9月25日", "content": "依战报",
            "author": "李专家", "role": "党史专家",
        })
        status, _ = self.call("adopt_opinion", {
            "opinion_id": opinion["id"], "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(status, 200)
        for target in ("待评审", "精稿中", "可出版"):
            status, _ = self.call("transition_page", {
                "page_id": page["id"], "target": target,
                "actor": "王编辑", "role": "编辑",
            })
            self.assertEqual(status, 200, target)
        status, result = self.call("export_batch", {
            "scope": "海外", "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(status, 200)
        self.assertEqual([p["page_id"] for p in result["pages"]], [page["id"]])
        self.assertEqual(result["pages"][0]["sources"][0]["source_id"], source["id"])

        # 史料授权过期后, 同一页面立即不可交付且被导出排除。
        status, _ = self.call("revise_source", {
            "source_id": source["id"], "citation": "《战史》p12(修订)",
            "license": {"publication_scopes": ["国内", "海外"],
                        "expires_at": "2020-01-01"},
            "actor": "王编辑", "role": "编辑", "base_revision": 1,
        })
        self.assertEqual(status, 200)
        status, blockers = self.call("page_blockers", {"page_id": page["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(any("过期" in item for item in blockers))
        _, result = self.call("export_batch", {
            "scope": "海外", "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(result["pages"], [])
        self.assertIn(page["id"], result["excluded"])

    def test_stale_version_conflict_maps_to_409(self):
        _, segment, _, _ = self._seed_page()
        self.call("new_segment_version", {
            "segment_id": segment["id"], "text": "第二版",
            "actor": "学员甲", "base_version": 1,
        })
        status, body = self.call("new_segment_version", {
            "segment_id": segment["id"], "text": "第三版",
            "actor": "学员乙", "base_version": 1,
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["kind"], "StaleVersionError")

    # ----- 海外发行: 禁运撤回 / 乱序更正 / 替代 / 导出 / 反查 -----

    def _publishable_page_for_markets(self, markets):
        _, source = self.call("register_source", {
            "title": "战斗详报", "citation": "《战史》p12",
            "license": {"publication_scopes": ["国内", *markets]},
            "actor": "王编辑", "role": "编辑",
        })
        _, segment = self.call("create_segment", {
            "title": "夜袭", "text": "拂晓进入阵地。", "actor": "学员甲",
        })
        _, design = self.call("create_design", {
            "name": "连长", "brief": "三十岁。", "actor": "学员甲",
        })
        _, page = self.call("create_page", {
            "title": "第3页",
            "script_refs": {segment["id"]: 1},
            "design_refs": {design["id"]: 1},
            "source_refs": {source["id"]: 1},
            "actor": "学员甲",
        })
        for target in ("待评审", "精稿中", "可出版"):
            self.call("transition_page", {
                "page_id": page["id"], "target": target,
                "actor": "王编辑", "role": "编辑",
            })
        return source, segment, design, page

    def test_release_embargo_withdraw_alternative_export_trace(self):
        mgr = {"actor": "王经理", "role": "海外发行经理"}
        markets = ["北美", "欧洲"]
        source, segment, design, page = self._publishable_page_for_markets(markets)
        for market in markets:
            self.call("create_market_rule", {"market": market, "note": "初版", **mgr})
            self.call("create_window", {
                "market": market, "opens_at": "2000-01-01",
                "closes_at": "2999-01-01", **mgr,
            })
        _, partner = self.call("create_partner", {
            "name": "环球影业", "markets": markets, **mgr})

        _, batch = self.call("create_release_batch", {
            "markets": markets, "partner_ids": [partner["id"]], **mgr})
        self.assertEqual(len(batch["lines"]), 2)
        self.call("publish_batch", {"batch_id": batch["id"], **mgr})
        _, trace = self.call("batch_trace", {"batch_id": batch["id"]})
        na = next(l for l in trace["lines"] if l["market"] == "北美")
        eu = next(l for l in trace["lines"] if l["market"] == "欧洲")
        self.assertEqual(na["status"], "已发布")
        self.assertEqual(na["frozen_page_version"], 1)
        self.assertEqual(trace["frozen_rules"]["北美"]["version"], 1)
        self.assertEqual(trace["frozen_grants"][partner["id"]]["version"], 1)

        # 合作方已下载后, 一纸禁运(仅北美、仅该页、生效日早已过去)到达
        status, _ = self.call("record_receipt", {
            "batch_id": batch["id"], "line_id": na["line_id"],
            "step": "已下载", "effective_at": "2000-01-01",
            "actor": "对接人", "role": "合作方",
        })
        self.assertEqual(status, 200)
        status, embargo = self.call("issue_embargo", {
            "market": "北美", "reason": "地区禁运通知",
            "effective_at": "2000-01-01", "page_ids": [page["id"]], **mgr})
        self.assertEqual(status, 200)

        _, trace = self.call("batch_trace", {"batch_id": batch["id"]})
        na = next(l for l in trace["lines"] if l["line_id"] == na["line_id"])
        eu = next(l for l in trace["lines"] if l["line_id"] == eu["line_id"])
        self.assertEqual(na["status"], "已撤回")
        self.assertEqual(eu["status"], "已发布")  # 其他市场不停
        self.assertEqual(len([n for n in trace["notices"]
                             if n["type"] == "撤回通知"]), 1)

        # 同批次重放不重复通知
        self.call("recalculate_batch", {"batch_id": batch["id"], **mgr})
        _, trace2 = self.call("batch_trace", {"batch_id": batch["id"]})
        self.assertEqual(len([n for n in trace2["notices"]
                              if n["type"] == "撤回通知"]), 1)

        # 撤回期间页面更正为 v2; 解禁旧 v1 不复活
        self.call("new_page_version", {
            "page_id": page["id"],
            "script_refs": {segment["id"]: 1},
            "design_refs": {design["id"]: 1},
            "source_refs": {source["id"]: 1},
            "actor": "学员甲", "base_version": 1,
        })
        status, _ = self.call("lift_embargo", {
            "embargo_id": embargo["id"], "lifted_at": "2000-01-02", **mgr})
        self.assertEqual(status, 200)
        _, trace = self.call("batch_trace", {"batch_id": batch["id"]})
        na = next(l for l in trace["lines"] if l["line_id"] == na["line_id"])
        self.assertEqual(na["status"], "已撤回")  # 已更正, 不复活

        # 经理追加采用 v2 的替代版本, 旧线转「已替代」
        status, alt = self.call("provide_alternative", {
            "batch_id": batch["id"], "line_id": na["line_id"], **mgr})
        self.assertEqual(status, 200)
        self.assertEqual(alt["page_version"], 2)
        self.assertEqual(alt["replaces"], na["line_id"])
        self.call("publish_batch", {"batch_id": batch["id"], **mgr})

        # 迟到的合作方撤回确认回执: 只记录, 不改状态
        status, late = self.call("record_receipt", {
            "batch_id": batch["id"], "line_id": na["line_id"],
            "step": "撤回已确认", "effective_at": "1999-12-31",
            "actor": "对接人", "role": "合作方",
        })
        self.assertEqual(status, 200)
        self.assertTrue(late["late"])

        # 导出: 北美只返回 v2 替代材料, 欧洲仍为原 v1
        status, exported_na = self.call("export_release", {
            "batch_id": batch["id"], "market": "北美", **mgr})
        self.assertEqual(status, 200)
        self.assertEqual([m["page_version"] for m in exported_na["materials"]], [2])
        self.assertEqual(exported_na["materials"][0]["line_id"], alt["id"])
        _, exported_eu = self.call("export_release", {
            "batch_id": batch["id"], "market": "欧洲", **mgr})
        self.assertEqual([m["page_version"] for m in exported_eu["materials"]], [1])

        # 反查: 合作方最后确认步骤 + 冻结依据
        _, trace = self.call("batch_trace", {"batch_id": batch["id"]})
        partner_view = next(p for p in trace["partners"]
                            if p["partner_id"] == partner["id"])
        self.assertEqual(partner_view["last_step"], "撤回已确认")
        self.assertEqual(trace["frozen_window"]["北美"]["version"], 1)
        self.assertEqual(trace["frozen_rules"]["欧洲"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
