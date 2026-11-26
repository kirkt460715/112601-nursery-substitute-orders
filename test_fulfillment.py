"""补植履约规则测试：替代苗源、分车签收、苗源凭证、错批处置与保活责任。"""

import json
import unittest

from fulfillment import DomainError, Fulfillment


REGISTER = {
    "sites": {
        "hill-a": {"eligible": ["src-7"], "quantity": 75},
        "hill-b": {"eligible": ["src-7", "src-8"], "quantity": 90},
        "hill-c": {"eligible": ["src-7", "src-8"], "quantity": 60},
    },
    "stocks": {
        "batch-A": {"source": "src-7", "available": 60},
        "batch-B": {"source": "src-8", "available": 45},
    },
    "orders": [
        {"id": "o-a", "site": "hill-a", "original": "batch-A"},
        {"id": "o-b", "site": "hill-b", "original": "batch-A"},
    ],
    "agreement": {"warranty_days": 120, "start": "actual_planting"},
    "bids": [
        {"id": "b-blocked", "order": "o-a", "batch": "batch-B", "quantity": 35},
        {"id": "b-b", "order": "o-b", "batch": "batch-B", "quantity": 40},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.f = Fulfillment(json.loads(json.dumps(REGISTER)), clock=lambda: self.now)

    def tick(self, amount=1):
        self.now += amount

    def assertCode(self, code, call, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception


class OrderAndEligibilityTest(Base):
    def test_order_cannot_exceed_site_scope(self):
        self.assertCode("outside_site_scope", self.f.open_order, "hill-a", 76)
        result = self.f.open_order("hill-a", 40)
        self.assertEqual(result["order"]["requested"], 40)
        self.assertCode("unknown_site", self.f.open_order, "hill-z", 1)

    def test_substitution_only_for_eligible_site(self):
        review = self.f.review_substitution("o-a")
        batch_b = next(c for c in review["candidates"] if c["batch"]["id"] == "batch-B")
        self.assertFalse(batch_b["eligible"])
        self.assertCode("source_not_eligible", self.f.submit_bid, "o-a", "batch-B", 10)
        # 同一替代批次对 hill-b 是许可的。
        self.assertTrue(self.f.is_eligible("hill-b", "batch-B"))

    def test_bid_cannot_cover_more_than_remaining_need(self):
        self.f.open_order("hill-a", 20)
        self.assertCode("quantity_exceeded", self.f.submit_bid, "o-a", "batch-A", 80)


class SubstitutionTest(Base):
    def test_rejection_leaves_other_site_untouched(self):
        self.f.decide_bid("b-blocked", "reject")
        self.assertEqual(self.f.batch_totals("batch-B")["remaining"], 45)
        self.assertEqual(self.f.decide_bid("b-b", "approve")["batch"]["remaining"], 5)

    def test_second_approval_sees_remaining_after_first(self):
        # 两块地同时申请同一替代供给。
        order_c = self.f.open_order("hill-c", 20)["order"]["id"]
        bid_c = self.f.submit_bid(order_c, "batch-B", 20)["bid"]["id"]
        self.assertEqual(self.f.batch_totals("batch-B")["available"], 45)

        self.f.decide_bid("b-b", "approve", quantity=40)
        review = self.f.review_substitution(order_c)
        batch_b = next(c for c in review["candidates"] if c["batch"]["id"] == "batch-B")
        self.assertEqual(batch_b["batch"]["remaining"], 5)
        self.assertEqual(
            [c["bid"] for c in batch_b["competing_bids"]], ["b-b"]
        )

        # 剩余只够 5 株，批准 20 株应被拒并说明竞争。
        rejected = self.assertCode("stock_shortfall", self.f.decide_bid, bid_c, "approve")
        self.assertEqual(rejected.details["remaining"], 5)
        self.assertEqual(rejected.details["competing_bids"][0]["order"], "o-b")

        self.f.decide_bid(bid_c, "approve", quantity=5)
        self.assertEqual(self.f.batch_totals("batch-B")["remaining"], 0)

    def test_cannot_approve_more_than_offered(self):
        self.assertCode("quantity_exceeded", self.f.decide_bid, "b-b", "approve", quantity=41)


class DeliveryAndReceiptTest(Base):
    def test_receipt_only_against_matching_vehicle(self):
        self.f.decide_bid("b-b", "approve")
        load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.assertCode(
            "vehicle_mismatch", self.f.receive_load, load["id"], "truck-9", 10, "foreman"
        )
        result = self.f.receive_load(load["id"], "truck-6", 10, "foreman")
        self.assertEqual(result["load"]["outstanding"], 30)
        self.assertEqual(result["receipt"]["accepted"], 10)

    def test_receipt_cannot_exceed_arrival_or_outstanding(self):
        self.f.decide_bid("b-b", "approve")
        load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.assertCode(
            "quantity_exceeded", self.f.receive_load, load["id"], "truck-6", 20, "f", seen=19
        )
        self.f.receive_load(load["id"], "truck-6", 30, "f")
        self.assertCode(
            "quantity_exceeded", self.f.receive_load, load["id"], "truck-6", 11, "f"
        )

    def test_partial_delivery_returns_unused_commitment(self):
        self.f.decide_bid("b-b", "approve")
        load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.f.receive_load(load["id"], "truck-6", 38, "f")
        reduced = self.f.reduce_load(load["id"], 38, note="苗圃少交 2 株")
        self.assertEqual(reduced["released_to_commitment"], 2)
        totals = self.f.batch_totals("batch-B")
        self.assertEqual(totals["available"], 7)       # 45 - 38 已交
        self.assertEqual(totals["committed"], 2)       # 同一个承诺还占着未交的 2 株

        # 未交的 2 株另派一车，仍属这份已批准的承诺。
        second = self.f.announce_load("o-b", "batch-B", 2, "truck-7")["load"]
        self.assertEqual(second["bid"], "b-b")
        self.assertEqual(second["vehicle"], "truck-7")

    def test_closing_load_returns_commitment_to_batch(self):
        self.f.decide_bid("b-b", "approve")
        load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.f.receive_load(load["id"], "truck-6", 38, "f")
        self.f.close_load(load["id"])
        totals = self.f.batch_totals("batch-B")
        self.assertEqual((totals["available"], totals["committed"], totals["delivered"]), (7, 0, 38))

    def test_load_requires_approved_commitment(self):
        self.assertCode(
            "no_approved_commitment", self.f.announce_load, "o-b", "batch-B", 5, "truck-1"
        )

    def test_vehicle_required(self):
        self.f.decide_bid("b-b", "approve")
        self.assertCode("vehicle_required", self.f.announce_load, "o-b", "batch-B", 5, " ")


class ProofAndCustodyTest(Base):
    def setUp(self):
        super().setUp()
        self.f.decide_bid("b-b", "approve")
        self.load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.receipt = self.f.receive_load(
            self.load["id"], "truck-6", 38, "foreman", seen=38
        )["receipt"]

    def test_missing_proof_stays_custody_not_conforming(self):
        self.assertEqual(self.receipt["proof_status"], "pending_proof")
        self.assertEqual(self.receipt["contract_status"], "custody_hold")
        kinds = {gap["kind"]: gap for gap in self.f.gaps("o-b")}
        self.assertEqual(kinds["eligibility_proof"]["receipts"], [self.receipt["id"]])

    def test_stock_receipt_is_not_contract_compliance(self):
        self.f.register_proof("batch-B", "proof-B", covers=[self.load["id"]])
        # 补齐凭证不自动覆盖已签认记录。
        self.assertEqual(
            self.f.list_receipts(order_id="o-b")[0]["contract_status"], "custody_hold"
        )
        # 仍待使用的数量可以重核。
        recheck = self.f.recheck("p-1")
        self.assertEqual(recheck["pending_loads"][0]["outstanding"], 2)
        self.assertEqual(recheck["unused_quantities"][0]["unused"], 38)
        # 未作处置前不能发起保活。
        self.f.record_planting(self.receipt["id"], "row-1", 20, planted_at=self.now)
        self.f.countersign(self.receipt["id"], "nursery-wang")
        self.assertCode("eligibility_missing", self.f.submit_claim, self.receipt["id"])

    def test_proof_cannot_be_applied_to_another_truck(self):
        self.assertCode("unknown_proof", self.f.receive_load, self.load["id"], "truck-6", 1, "f", proof_id="p-missing")
        self.f.register_proof("batch-B", "proof-B", covers=[self.load["id"]])
        self.f.reduce_load(self.load["id"], 38)   # 苗圃少交的 2 株另派一车
        other = self.f.announce_load("o-b", "batch-B", 2, "truck-7")["load"]
        self.assertCode(
            "proof_load_mismatch",
            self.f.receive_load,
            other["id"],
            "truck-7",
            2,
            "f",
            proof_id="p-1",
        )
        # 凭证所属来源必须与批次登记来源一致，不能用 batch-B 的来源给 batch-A 出证。
        self.assertCode("proof_source_mismatch", self.f.register_proof, "batch-A", "proof-X", source="src-8")
        self.assertCode("proof_source_mismatch", self.f.register_proof, "batch-B", "proof-Y", source="src-7")

    def test_new_proof_recheck_keeps_signed_source(self):
        # 首次收苗时已带凭证，之后补的产地证明不能改写这一车。
        proof = self.f.register_proof("batch-B", "proof-B")["proof"]
        receipt = self.f.receive_load(
            self.load["id"], "truck-6", 2, "f", proof_id=proof["id"]
        )["receipt"]
        self.assertEqual(receipt["proof_id"], proof["id"])
        self.assertEqual(receipt["contract_status"], "conforming")

    def test_mark_conforming_only_through_resolution(self):
        self.f.register_proof("batch-B", "proof-B", covers=[self.load["id"]])
        self.f.record_planting(self.receipt["id"], "row-1", 10, planted_at=self.now)
        self.f.countersign(self.receipt["id"], "nursery-wang")
        resolved = self.f.resolve_receipt(self.receipt["id"], "continue_care")
        self.assertEqual(resolved["receipt"]["resolution"]["decision"], "continue_care")
        self.assertEqual(self.f.warranty(self.receipt["id"])["status"], "active")


class CustodyResolutionTest(Base):
    def setUp(self):
        super().setUp()
        self.f.decide_bid("b-b", "approve")
        self.load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.receipt = self.f.receive_load(
            self.load["id"], "truck-6", 38, "foreman", seen=38
        )["receipt"]
        self.f.countersign(self.receipt["id"], "nursery-wang")

    def test_continue_care_requires_proof(self):
        self.assertCode(
            "proof_missing", self.f.resolve_receipt, self.receipt["id"], "continue_care"
        )

    def test_return_stock_reduces_accepted(self):
        self.assertCode(
            "quantity_exceeded", self.f.resolve_receipt, self.receipt["id"], "return_stock", quantity=100
        )
        result = self.f.resolve_receipt(self.receipt["id"], "return_stock", quantity=10)
        self.assertEqual(result["receipt"]["returned"], 10)
        self.assertEqual(result["warranty"]["planted"], 0)
        self.assertCode("already_resolved", self.f.resolve_receipt, self.receipt["id"], "top_up")

    def test_top_up_proposes_follow_up_order(self):
        result = self.f.resolve_receipt(self.receipt["id"], "top_up", quantity=2)
        follow_up = result["receipt"]["resolution"]["follow_up_order"]
        self.assertEqual(follow_up["site"], "hill-b")
        self.assertEqual(follow_up["quantity"], 2)
        self.assertEqual(follow_up["follow_up_of"], "o-b")


class WarrantyTest(Base):
    def setUp(self):
        super().setUp()
        self.f.decide_bid("b-b", "approve")
        self.load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]

    def _conforming_receipt(self, accepted=40):
        proof = self.f.register_proof("batch-B", f"proof-{accepted}", covers=[self.load["id"]])["proof"]
        receipt = self.f.receive_load(
            self.load["id"], "truck-6", accepted, "foreman", proof_id=proof["id"]
        )["receipt"]
        self.f.countersign(receipt["id"], "nursery-wang")
        return receipt

    def test_warranty_starts_at_actual_planting(self):
        receipt = self._conforming_receipt(20)
        self.f.record_planting(receipt["id"], "row-1", 20, planted_at=2000)
        status = self.f.warranty(receipt["id"], as_of=2050)
        self.assertEqual((status["start"], status["end"]), (2000, 2120))
        self.assertEqual(status["status"], "active")
        self.assertTrue(status["claimable"])
        self.assertEqual(self.f.warranty(receipt["id"], as_of=2121)["status"], "expired")

    def test_claim_requires_both_signatures(self):
        receipt = self.f.receive_load(self.load["id"], "truck-6", 10, "foreman")["receipt"]
        self.f.record_planting(receipt["id"], "row-1", 10)
        # 无凭证
        self.assertCode("eligibility_missing", self.f.submit_claim, receipt["id"])
        self.f.register_proof("batch-B", "proof-B", covers=[self.load["id"]])
        self.f.resolve_receipt(receipt["id"], "continue_care")
        self.assertCode("care_handover_missing", self.f.submit_claim, receipt["id"])
        self.f.countersign(receipt["id"], "nursery-wang")
        claim = self.f.submit_claim(receipt["id"])
        self.assertEqual(claim["claim"]["site"], "hill-b")

    def test_normal_lot_keeps_warranty_despite_other_lot_dispute(self):
        receipt = self._conforming_receipt(30)
        self.f.record_planting(receipt["id"], "row-1", 30, planted_at=2000)
        claim = self.f.submit_claim(receipt["id"], as_of=2010)
        self.assertEqual(claim["warranty"]["status"], "active")

        # 同地段后来出现替代批争议的另一车。
        self.f.reduce_load(self.load["id"], 30)
        other = self.f.announce_load("o-b", "batch-B", 2, "truck-9")["load"]
        disputed = self.f.receive_load(other["id"], "truck-9", 2, "foreman")["receipt"]
        self.f.countersign(disputed["id"], "nursery-wang")
        self.f.record_planting(disputed["id"], "row-2", 2, planted_at=2012)
        self.f.report_mismatch(disputed["id"], "batch-A", note="标签不符")

        self.assertCode("dispute_unresolved", self.f.submit_claim, disputed["id"])
        # 正常部分仍能发起，整单不被算作逾保。
        self.f.submit_claim(receipt["id"], as_of=2020)
        summary = self.f.order_warranty("o-b")
        statuses = {lot["receipt"]: lot["warranty"]["status"] for lot in summary["lots"]}
        self.assertEqual(statuses[receipt["id"]], "active")
        self.assertEqual(statuses[disputed["id"]], "disputed_under_review")
        self.assertFalse(summary["documents"]["overdue_by_document"])

    def test_dispute_keeps_planting_at_original_row(self):
        receipt = self._conforming_receipt(10)
        self.f.record_planting(receipt["id"], "row-1", 10, planted_at=2000)
        self.assertEqual(
            self.f.list_plantings(site="hill-b")[-1]["flagged"], None
        )
        result = self.f.report_mismatch(receipt["id"], "batch-A")
        self.assertEqual(result["plantings"][0]["row"], "row-1")
        self.assertEqual(result["plantings"][0]["flagged"], "batch_dispute")
        self.assertEqual(result["receipt"]["dispute"]["plantings_retained"], [result["plantings"][0]["id"]])

    def test_not_planted_has_no_warranty(self):
        receipt = self._conforming_receipt(10)
        status = self.f.warranty(receipt["id"])
        self.assertEqual(status["status"], "not_planted")
        self.assertCode("not_planted", self.f.submit_claim, receipt["id"])

    def test_planting_cannot_exceed_receipt_or_precede_it(self):
        receipt = self._conforming_receipt(10)
        self.assertCode("quantity_exceeded", self.f.record_planting, receipt["id"], "row-1", 11)
        self.assertCode(
            "planted_before_receipt",
            self.f.record_planting,
            receipt["id"],
            "row-1",
            5,
            planted_at=receipt["received_at"] - 1,
        )


class GapAndFollowUpTest(Base):
    def test_gaps_name_each_missing_kind(self):
        gaps = {gap["kind"] for gap in self.f.gaps("o-b")}
        self.assertEqual(gaps, {"supply_commitment"})

        self.f.decide_bid("b-b", "approve")
        self.assertIn("physical_delivery", {gap["kind"] for gap in self.f.gaps("o-b")})

        load = self.f.announce_load("o-b", "batch-B", 40, "truck-6")["load"]
        self.f.receive_load(load["id"], "truck-6", 10, "foreman")
        gaps = {gap["kind"]: gap for gap in self.f.gaps("o-b")}
        self.assertIn("eligibility_proof", gaps)
        self.assertIn("care_handover", gaps)
        self.assertIn("supply_commitment", gaps)   # 剩余 50 株仍缺承诺

    def test_next_round_order_links_back(self):
        first = self.f.open_order("hill-c", 20)
        again = self.f.open_order("hill-c", 20, follow_up_of=first["order"]["id"])
        self.assertEqual(again["order"]["follow_up_of"], first["order"]["id"])

    def test_close_releases_undelivered_commitment(self):
        self.f.decide_bid("b-b", "approve")
        self.f.announce_load("o-b", "batch-B", 10, "truck-6")
        self.f.receive_load("l-1", "truck-6", 10, "foreman")
        self.f.close_order("o-b")
        self.assertEqual(self.f.batch_totals("batch-B")["committed"], 0)
        self.assertEqual(self.f.batch_totals("batch-B")["available"], 35)
        self.assertCode("order_closed", self.f.announce_load, "o-b", "batch-B", 1, "truck-2")


if __name__ == "__main__":
    unittest.main()
