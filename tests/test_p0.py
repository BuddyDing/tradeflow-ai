import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from config.settings import settings
from tradeflow.compose import compose_system_prompt
from tradeflow.registry import get_spec
from web.app import ChatTurn, app, _import_data_scope, _import_data_user_input
from web import auth, database, store
from web.import_service import (ads_chat_analysis, ads_overview, competitor_rows, customer_overview, detect_report_type,
                                parse_upload, save_import, suggest_mapping)
from web.import_tools import build_import_tools


class TestImportParsing(unittest.TestCase):
    def test_csv_header_detection_and_mapping(self):
        content = ("Amazon report,,,\nCampaign Name,Customer Search Term,Impressions,Clicks,Spend,7 Day Total Sales\n"
                   "Kitchen,kitchen mat,1000,20,12.5,50\n").encode()
        columns, rows = parse_upload("ads.csv", content)
        mapping = suggest_mapping(columns)
        self.assertEqual(mapping["Campaign Name"], "campaign")
        self.assertEqual(mapping["Customer Search Term"], "search_term")
        self.assertEqual(len(rows), 1)
        self.assertEqual(detect_report_type(mapping), "ads_search_terms")

    def test_multisheet_xlsx(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["说明", None])
        ws.append(["SKU", "Stock"])
        ws.append(["A-1", 12])
        buf = io.BytesIO()
        wb.save(buf)
        columns, rows = parse_upload("inventory.xlsx", buf.getvalue())
        self.assertEqual(columns, ["SKU", "Stock"])
        self.assertEqual(rows[0]["SKU"], "A-1")

    def test_xlsx_with_broken_dimension_declaration(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["广告活动名称", "客户搜索词", "展示量", "点击量", "花费", "7天总销售额"])
        ws.append(["Campaign", "silicone mat", 100, 5, 10, 40])
        original = io.BytesIO()
        wb.save(original)

        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original.getvalue())) as source, zipfile.ZipFile(broken, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b'<dimension ref="A1:F2"/>', b'<dimension ref="A1:A1"/>')
                target.writestr(info, data)

        columns, rows = parse_upload("amazon-export.xlsx", broken.getvalue())
        self.assertEqual(len(columns), 6)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["客户搜索词"], "silicone mat")


class TestTenantPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_PATH = Path(self.tmp.name) / "test.db"
        database.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _authed_client(self) -> TestClient:
        """API 端点现在要求登录 session cookie；给 'default' 用户补一个可登录的账号密码，
        登录后返回带 cookie 的 client，user_id 仍是 'default'，不影响其余用例的既有假设。
        TestClient 走 http://testserver（非 https），Secure cookie 不会被回传，
        跟本地用 http 调试时要设 TRADEFLOW_SECURE_COOKIES=0 是同一个道理，这里直接关掉。"""
        original = settings.secure_cookies
        settings.secure_cookies = False
        self.addCleanup(setattr, settings, "secure_cookies", original)
        with database.connect() as db:
            db.execute("UPDATE users SET username=?, password_hash=? WHERE id='default'",
                      ("tester", auth.hash_password("test-pass")))
        client = TestClient(app)
        r = client.post("/api/auth/login", json={"username": "tester", "password": "test-pass"})
        assert r.status_code == 200, r.text
        return client

    def test_opportunities_are_store_scoped(self):
        with database.connect() as db:
            db.execute("INSERT INTO stores(id,user_id,name,marketplace) VALUES('second','default','Second','US')")
        store.add_opp({"key": "x", "name": "One"}, "default", "default")
        self.assertEqual(len(store.list_opps("default", "default")), 1)
        self.assertEqual(store.list_opps("default", "second"), [])

    def test_chat_sessions_persist_messages(self):
        client = self._authed_client()
        created = client.post("/api/chat/sessions", json={"title": "分析广告报表", "agent": "ads"}).json()
        session_id = created["id"]
        self.assertTrue(session_id.startswith("chat_"))
        self.assertEqual(created["title"], "分析广告报表")

        r = client.post(f"/api/chat/sessions/{session_id}/messages",
                        json={"role": "user", "content": "刚才的广告报表怎么看？"})
        self.assertEqual(r.status_code, 200)
        r = client.post(f"/api/chat/sessions/{session_id}/messages",
                        json={"role": "assistant", "content": "先看高 ACOS 搜索词。"})
        self.assertEqual(r.status_code, 200)

        loaded = client.get(f"/api/chat/sessions/{session_id}").json()
        self.assertEqual([m["role"] for m in loaded["messages"]], ["user", "assistant"])
        self.assertIn("高 ACOS", loaded["messages"][1]["content"])

    def test_chat_sessions_reject_store_not_owned_by_user(self):
        # 店铺归属现在是强校验（_current_store）：带一个不属于当前用户的/不存在的店铺 id
        # 必须 403，不能像登录功能上线前那样静默放行或换成别的店铺——那样等于允许把
        # 数据写进别人的店铺，或者用户完全没意识到请求被悄悄改路由到了别处。
        client = self._authed_client()
        headers = {"X-TradeFlow-Store": "store_from_old_page"}
        created = client.post("/api/chat/sessions", json={"title": "旧页面店铺"}, headers=headers)
        self.assertEqual(created.status_code, 403)

        listed = client.get("/api/chat/sessions", headers=headers)
        self.assertEqual(listed.status_code, 403)

    def test_chat_sessions_work_with_owned_store_header(self):
        client = self._authed_client()
        headers = {"X-TradeFlow-Store": "default"}  # 'default' 用户名下真实拥有的那个店铺
        created = client.post("/api/chat/sessions", json={"title": "真实店铺"}, headers=headers)
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["id"]

        listed = client.get("/api/chat/sessions", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["id"], session_id)

        saved = client.post(f"/api/chat/sessions/{session_id}/messages",
                            json={"role": "user", "content": "刷新后还在吗"}, headers=headers)
        self.assertEqual(saved.status_code, 200)

    def test_import_is_durable_and_drives_ads_overview(self):
        mapping = {"Campaign Name": "campaign", "Clicks": "clicks", "Impressions": "impressions",
                   "Spend": "spend", "Sales": "sales", "Orders": "orders",
                   "Customer Search Term": "search_term"}
        rows = [{"Campaign Name": "C1", "Clicks": 10, "Impressions": 100, "Spend": 20,
                 "Sales": 80, "Orders": 2, "Customer Search Term": "mat"}]
        saved = save_import("default", "default", "ads.xlsx", list(mapping), rows, mapping)
        self.assertEqual(saved["row_count"], 1)
        result = ads_overview("default", "default")
        self.assertEqual(result["source"]["filename"], "ads.xlsx")
        self.assertEqual(result["items"][0]["acos"], 0.25)
        self.assertIn("recommendations", result["items"][0])
        self.assertIn(result["items"][0]["severity"], {"crit", "warn", "good"})

    def test_empty_ads_overview_does_not_return_demo_items(self):
        result = ads_overview("default", "default")
        self.assertEqual(result["items"], [])
        self.assertIn("请先导入真实广告搜索词报表", result["error"])

    def test_customer_overview_uses_imported_orders_only(self):
        empty = customer_overview("default", "default")
        self.assertEqual(empty["items"], [])
        self.assertIn("请先导入真实订单报表", empty["error"])

        mapping = {"Order ID": "order_id", "SKU": "sku", "Title": "title",
                   "Buyer": "buyer", "Purchase Date": "purchase_date", "Quantity": "quantity"}
        rows = [
            {"Order ID": "111-1", "SKU": "A-1", "Title": "Silicone Mat",
             "Buyer": "Alice", "Purchase Date": "2026-07-01", "Quantity": 1},
            {"Order ID": "111-2", "SKU": "A-2", "Title": "Kitchen Tongs",
             "Buyer": "Alice", "Purchase Date": "2026-07-08", "Quantity": 1},
        ]
        save_import("default", "default", "orders.xlsx", list(mapping), rows, mapping)
        result = customer_overview("default", "default")
        self.assertEqual(result["source"], "imported_orders")
        self.assertEqual(result["counts"]["all"], 2)
        self.assertEqual(result["counts"]["repeat"], 1)
        self.assertEqual(result["items"][0]["name"], "Alice")

    def test_imported_ads_can_be_analyzed_from_chat(self):
        mapping = {"Campaign Name": "campaign", "Clicks": "clicks", "Impressions": "impressions",
                   "Spend": "spend", "Sales": "sales", "Orders": "orders",
                   "Customer Search Term": "search_term"}
        rows = [
            {"Campaign Name": "C1", "Clicks": 12, "Impressions": 100, "Spend": 15,
             "Sales": 0, "Orders": 0, "Customer Search Term": "bad term"},
            {"Campaign Name": "C2", "Clicks": 8, "Impressions": 100, "Spend": 8,
             "Sales": 100, "Orders": 3, "Customer Search Term": "good term"},
        ]
        save_import("default", "default", "ads.xlsx", list(mapping), rows, mapping)
        reply = ads_chat_analysis("default", "default")
        self.assertIsNotNone(reply)
        self.assertIn("已读取你导入的广告搜索词报表", reply)
        self.assertIn("bad term", reply)
        self.assertIn("good term", reply)

    def test_competitor_import_can_drive_offline_flow(self):
        mapping = {"ASIN": "asin", "Title": "title", "Price": "price", "Rating": "rating"}
        rows = [{"ASIN": "B001", "Title": "Silicone Mat", "Price": 19.99, "Rating": 4.6}]
        saved = save_import("default", "default", "competitors.xlsx", list(mapping), rows, mapping)
        self.assertEqual(saved["report_type"], "competitors")
        self.assertEqual(competitor_rows("default", "default")[0]["asin"], "B001")

    def test_import_tools_can_analyze_non_ads_files(self):
        mapping = {"SKU": "sku", "Quantity": "quantity", "Sales": "sales"}
        rows = [
            {"SKU": "A-1", "Quantity": 2, "Sales": 40},
            {"SKU": "A-1", "Quantity": 3, "Sales": 60},
            {"SKU": "B-2", "Quantity": 1, "Sales": 25},
            {"SKU": 999, "Quantity": "", "Sales": ""},
        ]
        saved = save_import("default", "default", "orders.xlsx", list(mapping), rows, mapping)
        tools = {t.name: t for t in build_import_tools("default", "default")}

        files = json.loads(tools["list_imported_files"].run({}))
        self.assertEqual(files["items"][0]["filename"], "orders.xlsx")

        result = json.loads(tools["aggregate_imported_file"].run({
            "batch_id": saved["id"],
            "group_by": "SKU",
            "metrics": {"Quantity": "sum", "Sales": "sum"},
            "sort_by": "Sales",
        }))
        self.assertEqual(result["groups"][0]["sku"], "A-1")
        self.assertEqual(result["groups"][0]["quantity"], 5)
        self.assertEqual(result["groups"][0]["sales"], 100)


class TestPromptAndTools(unittest.TestCase):
    def test_base_prompt_has_p0_guardrails(self):
        prompt = compose_system_prompt("listing")
        self.assertIn("数据不足", prompt)
        self.assertIn("compliance_gate", prompt)
        self.assertIn("不得使用示例或 Mock 数据冒充真实结果", prompt)

    def test_market_agents_have_real_query_tools(self):
        for name in ("listing", "teardown", "market", "selection"):
            tools = {t.name for t in get_spec(name).tools}
            self.assertIn("search_products", tools)

    def test_import_tool_schema_exposes_object_arguments(self):
        tools = {t.name: t for t in build_import_tools("default", "default")}
        sample_props = tools["sample_imported_rows"].parameters["properties"]
        aggregate_props = tools["aggregate_imported_file"].parameters["properties"]
        self.assertEqual(sample_props["filters"]["type"], "object")
        self.assertEqual(aggregate_props["filters"]["type"], "object")
        self.assertEqual(aggregate_props["metrics"]["type"], "object")

    def test_import_agent_scopes_transaction_queries_to_orders(self):
        history = [
            ChatTurn(role="user", content="刚才分析广告报表，找高 ACOS 搜索词"),
            ChatTurn(role="assistant", content="已基于广告搜索词报表分析。"),
        ]
        message = "分析我刚刚上传的商品交易数据，看下现在商品的问题在哪里，要怎么优化"
        user_input = _import_data_user_input(message, history)
        self.assertEqual(_import_data_scope(message, history), "orders")
        self.assertIn("report_type='orders'", user_input)
        self.assertIn("不要调用或分析 ads_search_terms", user_input)
        self.assertIn("不要主动提 ACOS", user_input)

    def test_import_agent_scopes_ads_queries_to_ads_report(self):
        message = "分析刚导入的广告搜索词报表，找出高 ACOS 的词"
        user_input = _import_data_user_input(message, [])
        self.assertEqual(_import_data_scope(message, []), "ads_search_terms")
        self.assertIn("report_type='ads_search_terms'", user_input)
        self.assertIn("不要把订单交易文件混入结论", user_input)


if __name__ == "__main__":
    unittest.main()
