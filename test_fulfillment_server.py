"""履约后端 HTTP 契约测试，包括与资料台的联调。"""

from http.client import HTTPConnection
import json
from threading import Thread
from unittest import TestCase

from evidence_desk import EvidenceDesk
from fulfillment_server import DeskClient, FulfillmentServer


REGISTER = {
    "sites": {"hill-b": {"eligible": ["src-7", "src-8"], "quantity": 90}},
    "stocks": {
        "batch-A": {"source": "src-7", "available": 60},
        "batch-B": {"source": "src-8", "available": 45},
    },
    "orders": [{"id": "o-b", "site": "hill-b", "original": "batch-A"}],
    "agreement": {"warranty_days": 120, "start": "actual_planting"},
    "bids": [{"id": "b-b", "order": "o-b", "batch": "batch-B", "quantity": 40}],
}


def serve(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return thread


class ServerTest(TestCase):
    with_desk = True

    def setUp(self):
        self.clock = 1000
        self.desk = None
        if self.with_desk:
            self.desk = EvidenceDesk(0)
            self.addCleanup(self.desk.server_close)
            self.addCleanup(self.desk.shutdown)
            serve(self.desk)
        self.server = FulfillmentServer(
            0,
            json.loads(json.dumps(REGISTER)),
            desk=DeskClient("127.0.0.1", self.desk.server_port) if self.desk else None,
            clock=lambda: self.clock,
        )
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        serve(self.server)

    def call(self, method, route, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, route, body=payload, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, route):
        return self.call("GET", route)

    def post(self, route, body=None):
        return self.call("POST", route, body or {})

    def prepare_load(self, vehicle="truck-6", quantity=40):
        status, _ = self.post("/bids/b-b/decide", {"decision": "approve"})
        self.assertEqual(status, 200)
        status, load = self.post(
            "/loads", {"order": "o-b", "batch": "batch-B", "quantity": quantity, "vehicle": vehicle}
        )
        self.assertEqual(status, 200)
        return load["load"]


class FulfillmentHttpTest(ServerTest):
    def test_unknown_route_and_error_shape(self):
        status, body = self.get("/nope")
        self.assertEqual((status, body["error"]), (404, "not_found"))
        status, body = self.post("/orders", {"site": "hill-z", "quantity": 1})
        self.assertEqual((status, body["error"]), (404, "unknown_site"))
        status, body = self.post("/orders", {"site": "hill-b", "quantity": 999})
        self.assertEqual((status, body["error"]), (422, "outside_site_scope"))
        status, body = self.call("POST", "/orders", None)
        self.assertEqual((status, body["error"]), (404, "unknown_site"))

    def test_invalid_json_is_rejected(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("POST", "/orders", body=b"{", headers={"Content-Length": "1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 422)
            self.assertEqual(json.loads(response.read())["error"], "invalid_json")
        finally:
            connection.close()

    def test_receipt_flow_through_http(self):
        load = self.prepare_load()
        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {"vehicle": "truck-9", "accepted": 5, "field_signer": "foreman"},
        )
        self.assertEqual((status, body["error"]), (422, "vehicle_mismatch"))

        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {"vehicle": "truck-6", "accepted": 38, "seen": 38, "field_signer": "foreman"},
        )
        self.assertEqual(status, 200)
        receipt = body["receipt"]
        self.assertEqual(receipt["contract_status"], "custody_hold")

        status, gaps = self.get("/orders/o-b/gaps")
        self.assertEqual(status, 200)
        self.assertIn("eligibility_proof", {gap["kind"] for gap in gaps["gaps"]})

        status, body = self.post(
            "/proofs", {"batch": "batch-B", "citation": "proof-B", "covers": [load["id"]]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["proof"]["citation"], "proof-B")

        status, body = self.post(f"/receipts/{receipt['id']}/countersign", {"nursery_signer": "wang"})
        self.assertEqual(status, 200)
        status, body = self.post(
            f"/receipts/{receipt['id']}/plantings", {"row": "row-c", "count": 20, "planted_at": 1100}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["planting"]["row"], "row-c")

        status, warranty = self.get(f"/receipts/{receipt['id']}/warranty?as_of=1200")
        self.assertEqual(status, 200)
        self.assertEqual(warranty["status"], "pending_source")

        status, body = self.post(f"/receipts/{receipt['id']}/resolve", {"decision": "continue_care"})
        self.assertEqual(status, 200)
        status, warranty = self.get(f"/receipts/{receipt['id']}/warranty?as_of=1200")
        self.assertEqual((status, warranty["status"]), (200, "active"))

        status, body = self.post(f"/receipts/{receipt['id']}/claims", {"as_of": 1200})
        self.assertEqual(status, 200)
        self.assertEqual(body["claim"]["warranty_end"], 1220)

    def test_partner_evidence_flows_through_desk(self):
        load = self.prepare_load()
        # 第一次查询资料台返回 503，不影响签收，但也不作为已验凭证。
        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {
                "vehicle": "truck-6",
                "accepted": 38,
                "seen": 38,
                "field_signer": "foreman",
                "partner": {"action": "read-load", "desk": "dispatch-desk"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["receipt"]["partner"]["status"], "temporarily_unavailable")
        self.assertEqual(body["receipt"]["contract_status"], "custody_hold")

        # 第二次取得实际回执：到场 38 株、未接受，签收 38 株与回执一致。
        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {
                "vehicle": "truck-6",
                "accepted": 2,
                "seen": 38,
                "field_signer": "foreman",
                "partner": {"action": "read-load", "desk": "dispatch-desk"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["receipt"]["partner"]["status"], "ok")
        self.assertIs(body["receipt"]["partner"]["accepted"], False)

    def test_partner_quantity_conflict_blocks_signature(self):
        load = self.prepare_load()
        # 先取得回执，避免落在那次计划内的 503 上。
        status, _ = self.get("/desk/partner?action=read-load&desk=dispatch-desk")
        self.assertEqual(status, 200)
        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {
                "vehicle": "truck-6",
                "accepted": 39,
                "field_signer": "foreman",
                "partner": {"action": "read-load", "desk": "dispatch-desk"},
            },
        )
        self.assertEqual((status, body["error"]), (422, "partner_quantity_conflict"))


class NoDeskTest(ServerTest):
    with_desk = False

    def test_desk_routes_report_configuration_gap(self):
        status, body = self.get("/desk/partner?action=read-load&desk=dispatch-desk")
        self.assertEqual((status, body["error"]), (503, "desk_not_configured"))

    def test_receipt_works_without_desk(self):
        load = self.prepare_load()
        status, body = self.post(
            f"/loads/{load['id']}/receive",
            {"vehicle": "truck-6", "accepted": 1, "field_signer": "foreman"},
        )
        self.assertEqual((status, body["receipt"]["partner"]), (200, None))


class PersistenceTest(TestCase):
    def test_state_survives_restart(self):
        import tempfile
        from pathlib import Path

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = Path(folder.name) / "state.json"

        first = FulfillmentServer(0, json.loads(json.dumps(REGISTER)), clock=lambda: 1000, state_path=state)
        serve(first)
        try:
            connection = HTTPConnection("127.0.0.1", first.server_port, timeout=3)
            body = json.dumps({"decision": "approve"}).encode()
            connection.request("POST", "/bids/b-b/decide", body=body)
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
        finally:
            first.shutdown()
            first.server_close()

        second = FulfillmentServer(0, json.loads(json.dumps(REGISTER)), clock=lambda: 1000, state_path=state)
        serve(second)
        self.addCleanup(second.server_close)
        self.addCleanup(second.shutdown)
        connection = HTTPConnection("127.0.0.1", second.server_port, timeout=3)
        try:
            connection.request("GET", "/orders/o-b")
            data = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        self.assertEqual(data["bids"][0]["status"], "approved")
        self.assertEqual(data["totals"]["committed"], 40)


class MessageArrivalTest(ServerTest):
    def test_message_endpoint_reads_desk_arrivals(self):
        status, body = self.get("/desk/messages?sequence=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["message"]["source"], "nursery")
        self.assertEqual(body["message"]["facts"]["load"], "truck-6")
        status, body = self.get("/desk/messages?sequence=99")
        self.assertEqual((status, body["error"]), (404, "unknown_message"))
