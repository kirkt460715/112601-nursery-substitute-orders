"""补植苗木的履约状态机。

把一次补植申领拆成几件可以分别核对的事实：

* 申领（order）——施工方按地段提出，数量受地段许可范围限制；
* 供货承诺（bid）——苗圃按自身实际批次提交，替代批次只能用于该地段许可的来源；
* 发车（load）——由已批准的承诺派车，车辆是现场签收的唯一对应物；
* 收苗（receipt）——班组只签对应车辆，签收量不超过到场量与该车待收量；
* 苗源凭证（proof）——按批次登记，不能套给别的车，也不能回写已签认的来源；
* 种植（planting）——已种下的事实留在原种植点，错批由现场决定后续处置；
* 保活（warranty）——按实际种植日计算，按每一车（receipt）分别成立。

本模块只做状态与规则，不涉及 HTTP；时间值由调用方给入或取自可注入的时钟。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class DomainError(Exception):
    """带稳定错误码的业务拒绝。"""

    STATUS_BY_CODE = {
        "not_found": 404,
        "unknown_site": 404,
        "unknown_batch": 404,
        "unknown_order": 404,
        "unknown_bid": 404,
        "unknown_load": 404,
        "unknown_receipt": 404,
        "unknown_proof": 404,
        "order_closed": 409,
        "load_closed": 409,
        "already_closed": 409,
        "already_signed": 409,
        "already_resolved": 409,
        "bid_not_pending": 409,
        "citation_conflict": 409,
        "desk_not_configured": 503,
    }

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status or self.STATUS_BY_CODE.get(code, 422)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# ---------------------------------------------------------------------------
# 登记对象


@dataclass
class Site:
    id: str
    eligible: tuple[str, ...]
    quantity: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "eligible": list(self.eligible), "quantity": self.quantity}


@dataclass
class Batch:
    id: str
    source: str
    available: int
    committed: int = 0
    delivered: int = 0

    @property
    def remaining(self) -> int:
        """尚未被供货承诺占用的现存数量。"""
        return self.available - self.committed

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "available": self.available,
            "committed": self.committed,
            "delivered": self.delivered,
            "remaining": self.remaining,
        }


@dataclass
class Order:
    id: str
    site: str
    original: str
    requested: int
    status: str = "open"
    follow_up_of: str | None = None
    created_at: int = 0
    closed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "site": self.site,
            "original": self.original,
            "requested": self.requested,
            "status": self.status,
            "follow_up_of": self.follow_up_of,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }


@dataclass
class Bid:
    id: str
    order: str
    batch: str
    quantity: int
    original_quantity: int
    status: str = "pending"  # pending / approved / rejected
    decided_at: int | None = None
    delivered: int = 0   # 已签收（原始数量，用于计算剩余申领）
    released: int = 0    # 已释放（未交足的承诺部分）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "batch": self.batch,
            "quantity": self.quantity,
            "original_quantity": self.original_quantity,
            "status": self.status,
            "decided_at": self.decided_at,
            "delivered": self.delivered,
            "released": self.released,
        }


@dataclass
class Load:
    id: str
    order: str
    bid: str
    batch: str
    quantity: int
    vehicle: str
    announced_at: int
    status: str = "announced"  # announced / received / closed
    received: int = 0
    closed_at: int | None = None

    @property
    def outstanding(self) -> int:
        return self.quantity - self.received

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "bid": self.bid,
            "batch": self.batch,
            "quantity": self.quantity,
            "vehicle": self.vehicle,
            "announced_at": self.announced_at,
            "status": self.status,
            "received": self.received,
            "outstanding": self.outstanding,
            "closed_at": self.closed_at,
        }


@dataclass
class Proof:
    id: str
    batch: str
    source: str
    citation: str
    received_at: int
    covers: set[str] = field(default_factory=set)  # 留空表示覆盖该批次全部车次

    def covers_load(self, load: Load) -> bool:
        return load.batch == self.batch and (not self.covers or load.id in self.covers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch": self.batch,
            "source": self.source,
            "citation": self.citation,
            "received_at": self.received_at,
            "covers": sorted(self.covers),
        }


@dataclass
class Receipt:
    id: str
    load: str
    order: str
    site: str
    batch: str
    vehicle: str
    seen: int
    accepted: int
    field_signer: str
    received_at: int
    nursery_signer: str | None = None
    proof_id: str | None = None
    proof_status: str = "pending_proof"  # verified / pending_proof
    contract_status: str = "custody_hold"  # conforming / custody_hold
    returned: int = 0
    resolution: dict[str, Any] | None = None
    dispute: dict[str, Any] | None = None
    partner: dict[str, Any] | None = None

    @property
    def fully_signed(self) -> bool:
        return bool(self.field_signer and self.nursery_signer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "load": self.load,
            "order": self.order,
            "site": self.site,
            "batch": self.batch,
            "vehicle": self.vehicle,
            "seen": self.seen,
            "accepted": self.accepted,
            "returned": self.returned,
            "field_signer": self.field_signer,
            "nursery_signer": self.nursery_signer,
            "fully_signed": self.fully_signed,
            "proof_id": self.proof_id,
            "proof_status": self.proof_status,
            "contract_status": self.contract_status,
            "resolution": self.resolution,
            "dispute": self.dispute,
            "partner": self.partner,
            "received_at": self.received_at,
        }


@dataclass
class Planting:
    id: str
    receipt: str
    site: str
    row: str
    count: int
    planted_at: int
    flagged: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "receipt": self.receipt,
            "site": self.site,
            "row": self.row,
            "count": self.count,
            "planted_at": self.planted_at,
            "flagged": self.flagged,
        }


@dataclass
class Claim:
    id: str
    receipt: str
    order: str
    site: str
    as_of: int
    warranty_start: int
    warranty_end: int
    note: str | None = None
    status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "receipt": self.receipt,
            "order": self.order,
            "site": self.site,
            "as_of": self.as_of,
            "warranty_start": self.warranty_start,
            "warranty_end": self.warranty_end,
            "note": self.note,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# 履约服务


# ---------------------------------------------------------------------------
# 快照反序列化（供持久化使用）


def _take(raw: dict[str, Any], cls, drop: tuple[str, ...] = ()):
    fields = {name: raw[name] for name in cls.__dataclass_fields__ if name in raw and name not in drop}
    return cls(**fields)


def _order_from(raw):
    return _take(raw, Order)


def _bid_from(raw):
    return _take(raw, Bid)


def _load_from(raw):
    return _take(raw, Load, drop=("outstanding",))


def _receipt_from(raw):
    return _take(raw, Receipt, drop=("fully_signed",))


def _planting_from(raw):
    return _take(raw, Planting)


def _proof_from(raw):
    data = _take(raw, Proof)
    data.covers = set(raw.get("covers") or ())
    return data


def _claim_from(raw):
    return _take(raw, Claim)


class Fulfillment:
    """一次补植履约的全部状态。"""

    def __init__(
        self,
        register: dict[str, Any],
        clock: Callable[[], int] | None = None,
    ):
        self.clock = clock or (lambda: int(time.time()))
        self.sites: dict[str, Site] = {}
        self.batches: dict[str, Batch] = {}
        self.orders: dict[str, Order] = {}
        self.bids: dict[str, Bid] = {}
        self.loads: dict[str, Load] = {}
        self.receipts: dict[str, Receipt] = {}
        self.plantings: dict[str, Planting] = {}
        self.proofs: dict[str, Proof] = {}
        self.claims: dict[str, Claim] = {}
        self.citations: dict[str, str] = {}  # citation -> proof id

        self._load_register(register)

    # -- 快照 ---------------------------------------------------------------

    _COLLECTIONS = (
        "orders",
        "bids",
        "loads",
        "receipts",
        "plantings",
        "proofs",
        "claims",
    )

    def snapshot(self) -> dict[str, Any]:
        """导出可变状态；地段与批次登记以 register 为准，不在此重复。"""
        data: dict[str, Any] = {
            "warranty_days": self.warranty_days,
            "warranty_basis": self.warranty_basis,
            "batches": {key: batch.to_dict() for key, batch in self.batches.items()},
            "citations": dict(self.citations),
        }
        for name in self._COLLECTIONS:
            data[name] = {
                key: value.to_dict() for key, value in getattr(self, name).items()
            }
        for proof in self.proofs.values():
            data["proofs"][proof.id]["covers"] = sorted(proof.covers)
        return data

    def restore(self, snapshot: dict[str, Any]) -> None:
        """从快照恢复；只覆盖登记之外的履约状态。"""
        for batch_id, raw in snapshot.get("batches", {}).items():
            batch = self.batches.get(batch_id)
            if batch is None:
                continue
            batch.available = int(raw["available"])
            batch.committed = int(raw["committed"])
            batch.delivered = int(raw["delivered"])
        self.warranty_days = int(snapshot.get("warranty_days", self.warranty_days))
        self.warranty_basis = snapshot.get("warranty_basis", self.warranty_basis)

        self.orders = {
            key: _order_from(raw) for key, raw in snapshot.get("orders", {}).items()
        }
        self.bids = {key: _bid_from(raw) for key, raw in snapshot.get("bids", {}).items()}
        self.loads = {key: _load_from(raw) for key, raw in snapshot.get("loads", {}).items()}
        self.receipts = {
            key: _receipt_from(raw) for key, raw in snapshot.get("receipts", {}).items()
        }
        self.plantings = {
            key: _planting_from(raw) for key, raw in snapshot.get("plantings", {}).items()
        }
        self.proofs = {key: _proof_from(raw) for key, raw in snapshot.get("proofs", {}).items()}
        self.claims = {key: _claim_from(raw) for key, raw in snapshot.get("claims", {}).items()}
        self.citations = dict(snapshot.get("citations", {}))

    # -- 载入 ---------------------------------------------------------------

    def _load_register(self, register: dict[str, Any]) -> None:
        for site_id, raw in register.get("sites", {}).items():
            self.sites[site_id] = Site(
                id=site_id,
                eligible=tuple(raw.get("eligible", ())),
                quantity=int(raw["quantity"]),
            )
        for batch_id, raw in register.get("stocks", {}).items():
            self.batches[batch_id] = Batch(
                id=batch_id, source=raw["source"], available=int(raw["available"])
            )
        agreement = register.get("agreement", {})
        self.warranty_days = int(agreement.get("warranty_days", 120))
        self.warranty_basis = agreement.get("start", "actual_planting")

        for raw in register.get("orders", []):
            site = self.sites.get(raw["site"])
            if site is None:
                raise ValueError(f"订单引用了未登记的地段: {raw['site']}")
            if raw["original"] not in self.batches:
                raise ValueError(f"订单引用了未登记的批次: {raw['original']}")
            self.orders[raw["id"]] = Order(
                id=raw["id"],
                site=raw["site"],
                original=raw["original"],
                requested=int(raw.get("quantity", site.quantity)),
                created_at=int(raw.get("created_at", 0)),
            )
        for raw in register.get("bids", []):
            order = self.orders.get(raw["order"])
            if order is None:
                raise ValueError(f"供货承诺引用了未登记的申领: {raw['order']}")
            if raw["batch"] not in self.batches:
                raise ValueError(f"供货承诺引用了未登记的批次: {raw['batch']}")
            quantity = int(raw["quantity"])
            if quantity <= 0:
                raise ValueError("供货承诺数量应为正数")
            bid_id = raw.get("id") or self._new_id("b", self.bids)
            self.bids[bid_id] = Bid(
                id=bid_id,
                order=order.id,
                batch=raw["batch"],
                quantity=quantity,
                original_quantity=quantity,
            )

    # -- 小工具 -------------------------------------------------------------

    @staticmethod
    def _new_id(prefix: str, collection: dict[str, Any]) -> str:
        index = 1
        while f"{prefix}-{index}" in collection:
            index += 1
        return f"{prefix}-{index}"

    @staticmethod
    def _require(collection: dict[str, Any], key: str, code: str, label: str) -> Any:
        try:
            return collection[key]
        except KeyError:
            raise DomainError(code, f"未登记的{label}: {key}", {"id": key}) from None

    def _site(self, site_id: str) -> Site:
        return self._require(self.sites, site_id, "unknown_site", "地段")

    def _batch(self, batch_id: str) -> Batch:
        return self._require(self.batches, batch_id, "unknown_batch", "批次")

    def _order(self, order_id: str) -> Order:
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        if order.status != "open":
            raise DomainError("order_closed", f"申领 {order_id} 已关闭", {"order": order_id})
        return order

    def _load(self, load_id: str) -> Load:
        return self._require(self.loads, load_id, "unknown_load", "车次")

    def _receipt(self, receipt_id: str) -> Receipt:
        return self._require(self.receipts, receipt_id, "unknown_receipt", "收苗记录")

    def is_eligible(self, site_id: str, batch_id: str) -> bool:
        site = self._site(site_id)
        batch = self._batch(batch_id)
        return not site.eligible or batch.source in site.eligible

    def _assert_eligible(self, site_id: str, batch_id: str) -> None:
        if not self.is_eligible(site_id, batch_id):
            site = self._site(site_id)
            batch = self._batch(batch_id)
            raise DomainError(
                "source_not_eligible",
                f"地段 {site_id} 许可的来源不含 {batch.source}（批次 {batch_id}）",
                {
                    "site": site_id,
                    "batch": batch_id,
                    "source": batch.source,
                    "eligible": list(site.eligible),
                },
            )

    # -- 数量核算 -----------------------------------------------------------

    def order_totals(self, order_id: str) -> dict[str, int]:
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        approvals = [
            bid
            for bid in self.bids.values()
            if bid.order == order.id and bid.status == "approved"
        ]
        committed = sum(bid.quantity - bid.delivered - bid.released for bid in approvals)
        accepted = sum(r.accepted for r in self.receipts.values() if r.order == order.id)
        returned = sum(r.returned for r in self.receipts.values() if r.order == order.id)
        planted = sum(p.count for p in self.plantings.values() if self.receipts[p.receipt].order == order.id)
        delivered = accepted - returned
        return {
            "requested": order.requested,
            "approved": sum(bid.quantity for bid in approvals),
            "committed": committed,
            "delivered": delivered,
            "planted": planted,
            "remaining_need": order.requested - committed - delivered,
        }

    def _remaining_need(self, order: Order) -> int:
        return self.order_totals(order.id)["remaining_need"]

    def batch_totals(self, batch_id: str) -> dict[str, int]:
        batch = self._batch(batch_id)
        return {
            "available": batch.available,
            "committed": batch.committed,
            "delivered": batch.delivered,
            "remaining": batch.remaining,
        }

    def _capacity(self, bid: Bid, exclude_load: str | None = None) -> int:
        """该承诺还能再派出多少株：批准数量减去已签收、已释放和已派车未收的部分。"""
        outstanding = sum(
            load.outstanding
            for load in self.loads.values()
            if load.bid == bid.id and load.status != "closed" and load.id != exclude_load
        )
        return bid.quantity - bid.delivered - bid.released - outstanding

    def _allocate(self, bid: Bid, quantity: int, exclude_load: str | None = None) -> bool:
        return self._capacity(bid, exclude_load) >= quantity

    def _release(self, bid: Bid, quantity: int) -> None:
        if quantity <= 0:
            return
        batch = self._batch(bid.batch)
        batch.committed -= quantity
        bid.released += quantity

    # -- 申领 ---------------------------------------------------------------

    def open_order(
        self,
        site_id: str,
        quantity: int | None = None,
        follow_up_of: str | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        site = self._site(site_id)
        requested = site.quantity if quantity is None else int(quantity)
        if requested <= 0:
            raise DomainError("bad_quantity", "申领数量应为正数", {"quantity": requested})
        if requested > site.quantity:
            raise DomainError(
                "outside_site_scope",
                f"地段 {site_id} 的许可数量为 {site.quantity}，申领 {requested} 超出范围",
                {"site": site_id, "scope": site.quantity, "requested": requested},
            )
        if follow_up_of is not None and follow_up_of not in self.orders:
            raise DomainError("unknown_order", f"未登记的申领: {follow_up_of}", {"id": follow_up_of})
        original = site.eligible[0] if site.eligible else next(iter(self.batches))
        candidates = [b for b in self.batches.values() if b.source == original]
        if not candidates:
            candidates = list(self.batches.values())
        if not candidates:
            raise DomainError("no_batch", "没有可用的苗源批次")
        order = Order(
            id=self._new_id("o", self.orders),
            site=site_id,
            original=candidates[0].id,
            requested=requested,
            follow_up_of=follow_up_of,
            created_at=self.clock() if at is None else at,
        )
        self.orders[order.id] = order
        return self.order_status(order.id)

    def list_orders(self) -> list[dict[str, Any]]:
        return [self.order_status(order_id) for order_id in self.orders]

    def order_status(self, order_id: str) -> dict[str, Any]:
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        site = self._site(order.site)
        totals = self.order_totals(order.id)
        bids = [bid.to_dict() for bid in self.bids.values() if bid.order == order.id]
        loads = [load.to_dict() for load in self.loads.values() if load.order == order.id]
        receipts = [r.to_dict() for r in self.receipts.values() if r.order == order.id]
        plantings = [
            p.to_dict()
            for p in self.plantings.values()
            if self.receipts[p.receipt].order == order.id
        ]
        return {
            "order": order.to_dict(),
            "site": site.to_dict(),
            "totals": totals,
            "bids": bids,
            "loads": loads,
            "receipts": receipts,
            "plantings": plantings,
            "gaps": self.gaps(order.id),
            "next_actions": self.next_actions(order.id),
        }

    def close_order(self, order_id: str, note: str | None = None) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise DomainError("unknown_order", f"未登记的申领: {order_id}", {"id": order_id})
        if order.status == "closed":
            raise DomainError("already_closed", f"申领 {order_id} 已关闭", {"order": order_id})
        for bid in list(self.bids.values()):
            if bid.order != order.id or bid.status != "approved":
                continue
            for load in list(self.loads.values()):
                if load.bid == bid.id and load.status == "announced" and load.outstanding:
                    self._release(bid, load.outstanding)
                    load.status = "closed"
                    load.closed_at = self.clock()
            self._release(bid, self._capacity(bid))
        order.status = "closed"
        order.closed_at = self.clock()
        result = self.order_status(order.id)
        result["closed_note"] = note
        return result

    # -- 供货承诺与替代 -----------------------------------------------------

    def submit_bid(
        self, order_id: str, batch_id: str, quantity: int, at: int | None = None
    ) -> dict[str, Any]:
        order = self._order(order_id)
        self._batch(batch_id)
        quantity = int(quantity)
        if quantity <= 0:
            raise DomainError("bad_quantity", "供货承诺数量应为正数", {"quantity": quantity})
        self._assert_eligible(order.site, batch_id)
        remaining = self._remaining_need(order)
        if quantity > remaining:
            raise DomainError(
                "quantity_exceeded",
                f"申领 {order_id} 尚缺 {remaining} 株，承诺 {quantity} 株超出",
                {"order": order_id, "remaining_need": remaining, "quantity": quantity},
            )
        bid = Bid(
            id=self._new_id("b", self.bids),
            order=order.id,
            batch=batch_id,
            quantity=quantity,
            original_quantity=quantity,
        )
        self.bids[bid.id] = bid
        return {
            "bid": bid.to_dict(),
            "review": self.review_substitution(order.id, batch_id),
        }

    def list_bids(self, order_id: str | None = None) -> list[dict[str, Any]]:
        if order_id is not None:
            self._require(self.orders, order_id, "unknown_order", "申领")
        return [
            bid.to_dict()
            for bid in self.bids.values()
            if order_id is None or bid.order == order_id
        ]

    def review_substitution(
        self, order_id: str, batch_id: str | None = None
    ) -> dict[str, Any]:
        """换苗确认前的现场视图：真实供给、其他申领的占用、本地段发车安排。"""
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        totals = self.order_totals(order.id)
        candidates = []
        for batch in self.batches.values():
            proofs = [p for p in self.proofs.values() if p.batch == batch.id]
            competing = [
                {
                    "bid": bid.id,
                    "order": bid.order,
                    "site": self.orders[bid.order].site,
                    "quantity": bid.quantity,
                    "status": bid.status,
                }
                for bid in self.bids.values()
                if bid.batch == batch.id
                and bid.order != order.id
                and bid.status != "rejected"
                and self.is_eligible(self.orders[bid.order].site, batch.id)
            ]
            candidates.append(
                {
                    "batch": batch.to_dict(),
                    "eligible": self.is_eligible(order.site, batch.id),
                    "proofs": [p.citation for p in proofs],
                    "competing_bids": competing,
                }
            )
        return {
            "order": order.to_dict(),
            "site": self._site(order.site).to_dict(),
            "totals": totals,
            "candidates": candidates,
            "dispatch": [
                load.to_dict()
                for load in self.loads.values()
                if load.order == order.id and load.status != "closed"
            ],
            "bids": [b.to_dict() for b in self.bids.values() if b.order == order.id],
        }

    def decide_bid(
        self,
        bid_id: str,
        decision: str,
        quantity: int | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        bid = self._require(self.bids, bid_id, "unknown_bid", "供货承诺")
        if bid.status != "pending":
            raise DomainError(
                "bid_not_pending",
                f"供货承诺 {bid_id} 已处理为 {bid.status}",
                {"bid": bid_id, "status": bid.status},
            )
        order = self._order(bid.order)
        if decision == "reject":
            bid.status = "rejected"
            bid.decided_at = self.clock() if at is None else at
            return {"bid": bid.to_dict(), "order": self.order_status(order.id)}
        if decision != "approve":
            raise DomainError("bad_decision", "决定应为 approve 或 reject", {"decision": decision})

        approved = bid.quantity if quantity is None else int(quantity)
        if approved <= 0 or approved > bid.quantity:
            raise DomainError(
                "quantity_exceeded" if approved > bid.quantity else "bad_quantity",
                f"批准数量应在 1..{bid.quantity} 之间",
                {"bid": bid_id, "quantity": approved, "offered": bid.quantity},
            )
        self._assert_eligible(order.site, bid.batch)
        remaining = self._remaining_need(order)
        if approved > remaining:
            raise DomainError(
                "quantity_exceeded",
                f"申领 {order.id} 尚缺 {remaining} 株，批准 {approved} 株超出",
                {"order": order.id, "remaining_need": remaining, "quantity": approved},
            )
        batch = self._batch(bid.batch)
        if approved > batch.remaining:
            raise DomainError(
                "stock_shortfall",
                f"批次 {batch.id} 当前可承诺 {batch.remaining} 株，本次批准 {approved} 株不足",
                {
                    "batch": batch.id,
                    "available": batch.available,
                    "committed": batch.committed,
                    "remaining": batch.remaining,
                    "requested": approved,
                    "competing_bids": [
                        {"bid": other.id, "order": other.order, "quantity": other.quantity}
                        for other in self.bids.values()
                        if other.batch == batch.id
                        and other.order != order.id
                        and other.status == "approved"
                    ],
                },
            )
        bid.quantity = approved
        bid.status = "approved"
        bid.decided_at = self.clock() if at is None else at
        batch.committed += approved
        return {
            "bid": bid.to_dict(),
            "batch": batch.to_dict(),
            "order": self.order_status(order.id),
        }

    # -- 发车与收苗 ---------------------------------------------------------

    def announce_load(
        self,
        order_id: str,
        batch_id: str,
        quantity: int,
        vehicle: str,
        at: int | None = None,
    ) -> dict[str, Any]:
        order = self._order(order_id)
        self._batch(batch_id)
        self._assert_eligible(order.site, batch_id)
        quantity = int(quantity)
        if quantity <= 0:
            raise DomainError("bad_quantity", "发车数量应为正数", {"quantity": quantity})
        if not vehicle or not str(vehicle).strip():
            raise DomainError("vehicle_required", "发车必须登记车辆")
        candidates = [
            bid
            for bid in self.bids.values()
            if bid.order == order.id and bid.batch == batch_id and bid.status == "approved"
        ]
        candidates.sort(key=lambda bid: bid.decided_at if bid.decided_at is not None else 0)
        # 先给还没派车的额度独立的车；已登记车次优先占用剩余额度。
        for bid in candidates:
            if self._allocate(bid, quantity):
                load = Load(
                    id=self._new_id("l", self.loads),
                    order=order.id,
                    bid=bid.id,
                    batch=batch_id,
                    quantity=quantity,
                    vehicle=str(vehicle),
                    announced_at=self.clock() if at is None else at,
                )
                self.loads[load.id] = load
                return {"load": load.to_dict(), "bid": bid.to_dict()}
        available = sum(self._capacity(bid) for bid in candidates)
        raise DomainError(
            "no_approved_commitment",
            f"申领 {order_id} 在批次 {batch_id} 上未被批准的发车额度不足",
            {
                "order": order_id,
                "batch": batch_id,
                "requested": quantity,
                "approved_outstanding": available,
                "approved_bids": [bid.id for bid in candidates],
            },
        )

    def list_loads(self, order_id: str | None = None, site: str | None = None) -> list[dict[str, Any]]:
        result = []
        for load in self.loads.values():
            order = self.orders[load.order]
            if order_id is not None and load.order != order_id:
                continue
            if site is not None and order.site != site:
                continue
            result.append({**load.to_dict(), "site": order.site})
        return result

    def close_load(self, load_id: str, note: str | None = None) -> dict[str, Any]:
        load = self._load(load_id)
        if load.status == "closed":
            raise DomainError("already_closed", f"车次 {load_id} 已关闭", {"load": load_id})
        bid = self.bids[load.bid]
        self._release(bid, load.outstanding)
        load.status = "closed"
        load.closed_at = self.clock()
        return {"load": load.to_dict(), "note": note, "order": self.order_status(load.order)}

    def reduce_load(self, load_id: str, quantity: int, note: str | None = None) -> dict[str, Any]:
        """苗圃少交一部分车：只下调尚未签收的额度，已签收部分不动。

        下调出的额度仍留在原供货承诺内，可另派一车，不会退回批次库存。
        """
        load = self._load(load_id)
        if load.status == "closed":
            raise DomainError("load_closed", f"车次 {load_id} 已关闭", {"load": load_id})
        quantity = int(quantity)
        if quantity < load.received:
            raise DomainError(
                "quantity_below_received",
                f"车次 {load_id} 已签收 {load.received} 株，不能下调到 {quantity} 株",
                {"load": load_id, "received": load.received, "quantity": quantity},
            )
        if quantity >= load.quantity:
            raise DomainError(
                "not_a_reduction",
                f"车次 {load_id} 当前登记 {load.quantity} 株，下调数量应为更小值",
                {"load": load_id, "quantity": load.quantity},
            )
        load.quantity = quantity
        if load.outstanding == 0:
            load.status = "received"
        return {
            "load": load.to_dict(),
            "released_to_commitment": self._capacity(self.bids[load.bid]),
            "note": note,
        }

    def receive_load(
        self,
        load_id: str,
        vehicle: str,
        accepted: int,
        field_signer: str,
        seen: int | None = None,
        nursery_signer: str | None = None,
        proof_id: str | None = None,
        partner: dict[str, Any] | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        """班组签收。只认对应车辆与签收数量。"""
        load = self._load(load_id)
        if load.status == "closed":
            raise DomainError("load_closed", f"车次 {load_id} 已关闭", {"load": load_id})
        order = self._require(self.orders, load.order, "unknown_order", "申领")
        if vehicle is None or str(vehicle) != load.vehicle:
            raise DomainError(
                "vehicle_mismatch",
                f"车次 {load_id} 对应车辆 {load.vehicle}，签收车辆为 {vehicle}",
                {"load": load_id, "expected": load.vehicle, "received": vehicle},
            )
        accepted = int(accepted)
        seen = accepted if seen is None else int(seen)
        if accepted <= 0:
            raise DomainError("bad_quantity", "签收数量应为正数", {"quantity": accepted})
        if seen < accepted:
            raise DomainError(
                "quantity_exceeded",
                f"到场 {seen} 株，不能签收 {accepted} 株",
                {"seen": seen, "accepted": accepted},
            )
        outstanding = load.outstanding
        if accepted > outstanding:
            raise DomainError(
                "quantity_exceeded",
                f"车次 {load_id} 待收 {outstanding} 株，签收 {accepted} 株超出",
                {"load": load_id, "outstanding": outstanding, "accepted": accepted},
            )
        if not field_signer or not str(field_signer).strip():
            raise DomainError("signer_required", "签收必须由现场班组签认")

        checked_partner = self._check_partner(load, partner, seen, accepted)

        if proof_id is not None:
            proof = self._require(self.proofs, proof_id, "unknown_proof", "苗源凭证")
            if proof.batch != load.batch:
                raise DomainError(
                    "proof_batch_mismatch",
                    f"凭证 {proof_id} 属于批次 {proof.batch}，不能用于批次 {load.batch} 的车次",
                    {"proof": proof_id, "proof_batch": proof.batch, "load_batch": load.batch},
                )
            if not proof.covers_load(load):
                raise DomainError(
                    "proof_load_mismatch",
                    f"凭证 {proof_id} 未覆盖车次 {load_id}",
                    {"proof": proof_id, "load": load_id, "covers": sorted(proof.covers)},
                )
            used = proof
        else:
            used = next(
                (
                    proof
                    for proof in self.proofs.values()
                    if proof.covers_load(load)
                ),
                None,
            )

        verified = used is not None
        receipt = Receipt(
            id=self._new_id("r", self.receipts),
            load=load.id,
            order=load.order,
            site=order.site,
            batch=load.batch,
            vehicle=load.vehicle,
            seen=seen,
            accepted=accepted,
            field_signer=str(field_signer),
            received_at=self.clock() if at is None else at,
            nursery_signer=nursery_signer,
            proof_id=used.id if used else None,
            proof_status="verified" if verified else "pending_proof",
            contract_status="conforming" if verified else "custody_hold",
            partner=checked_partner,
        )
        self.receipts[receipt.id] = receipt

        load.received += accepted
        if load.outstanding == 0:
            load.status = "received"
        bid = self.bids[load.bid]
        bid.delivered += accepted
        batch = self._batch(load.batch)
        batch.committed -= accepted
        batch.available -= accepted
        batch.delivered += accepted
        return {"receipt": receipt.to_dict(), "load": load.to_dict(), "batch": batch.to_dict()}

    def _check_partner(
        self,
        load: Load,
        partner: dict[str, Any] | None,
        seen: int,
        accepted: int,
    ) -> dict[str, Any] | None:
        """核对合作方独立回执；回执只作为佐证，不替代签收。"""
        if partner is None:
            return None
        status = partner.get("status", "ok")
        if status != "ok":
            # 未取得回执：如实记录，不能当作已核对，也不能当作已符合合同。
            return {"desk": partner.get("desk"), "action": partner.get("action"), "status": status}
        fact = partner.get("fact") or {}
        reported_vehicle = fact.get("load", fact.get("vehicle"))
        if reported_vehicle is not None and str(reported_vehicle) != load.vehicle:
            raise DomainError(
                "partner_vehicle_mismatch",
                f"合作方回执对应车辆 {reported_vehicle}，与车次车辆 {load.vehicle} 不符",
                {"load": load.id, "expected": load.vehicle, "partner": reported_vehicle},
            )
        partner_seen = fact.get("seen")
        if partner_seen is not None and accepted > int(partner_seen):
            raise DomainError(
                "partner_quantity_conflict",
                f"合作方回执记录到场 {partner_seen} 株，不能签收 {accepted} 株",
                {"seen": int(partner_seen), "accepted": accepted},
            )
        return {
            "desk": partner.get("desk"),
            "action": partner.get("action"),
            "status": status,
            "load": reported_vehicle,
            "seen": int(partner_seen) if partner_seen is not None else None,
            "accepted": fact.get("accepted"),
        }

    def list_receipts(self, order_id: str | None = None, site: str | None = None) -> list[dict[str, Any]]:
        result = []
        for receipt in self.receipts.values():
            if order_id is not None and receipt.order != order_id:
                continue
            if site is not None and receipt.site != site:
                continue
            result.append(receipt.to_dict())
        return result

    def countersign(self, receipt_id: str, nursery_signer: str, at: int | None = None) -> dict[str, Any]:
        receipt = self._receipt(receipt_id)
        if not nursery_signer or not str(nursery_signer).strip():
            raise DomainError("signer_required", "会签必须由苗圃方签认")
        if receipt.nursery_signer:
            raise DomainError(
                "already_signed",
                f"收苗记录 {receipt_id} 已有苗圃签认",
                {"receipt": receipt_id, "nursery_signer": receipt.nursery_signer},
            )
        receipt.nursery_signer = str(nursery_signer)
        return {"receipt": receipt.to_dict(), "warranty": self.warranty(receipt_id)}

    # -- 苗源凭证 -----------------------------------------------------------

    def register_proof(
        self,
        batch_id: str,
        citation: str,
        source: str | None = None,
        covers: Iterable[str] | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        batch = self._batch(batch_id)
        source = batch.source if source is None else str(source)
        if source != batch.source:
            raise DomainError(
                "proof_source_mismatch",
                f"凭证来源 {source} 与批次 {batch_id} 的登记来源 {batch.source} 不符",
                {"batch": batch_id, "source": source, "expected": batch.source},
            )
        citation = str(citation or "").strip()
        if not citation:
            raise DomainError("citation_required", "凭证必须注明编号")
        if citation in self.citations:
            existing = self.proofs[self.citations[citation]]
            if existing.batch == batch_id and existing.source == source:
                return {"proof": existing.to_dict(), "recheck": self.recheck(existing.id)}
            raise DomainError(
                "citation_conflict",
                f"凭证编号 {citation} 已用于批次 {existing.batch}",
                {"citation": citation, "batch": existing.batch},
            )
        cover_ids: set[str] = set()
        for load_id in covers or ():
            load = self._load(load_id)
            if load.batch != batch_id:
                raise DomainError(
                    "proof_load_mismatch",
                    f"凭证属于批次 {batch_id}，不能套给批次 {load.batch} 的车次 {load_id}",
                    {"proof_batch": batch_id, "load": load_id, "load_batch": load.batch},
                )
            cover_ids.add(load_id)
        proof = Proof(
            id=self._new_id("p", self.proofs),
            batch=batch_id,
            source=source,
            citation=citation,
            received_at=self.clock() if at is None else at,
            covers=cover_ids,
        )
        self.proofs[proof.id] = proof
        self.citations[citation] = proof.id
        return {"proof": proof.to_dict(), "recheck": self.recheck(proof.id)}

    def list_proofs(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        return [
            proof.to_dict()
            for proof in self.proofs.values()
            if batch_id is None or proof.batch == batch_id
        ]

    def recheck(self, proof_id: str) -> dict[str, Any]:
        """接到补齐证明后可重核的范围：未验批次、待收车次和尚待使用的数量。"""
        proof = self._require(self.proofs, proof_id, "unknown_proof", "苗源凭证")
        pending_loads = [
            {**load.to_dict(), "site": self.orders[load.order].site}
            for load in self.loads.values()
            if load.batch == proof.batch
            and load.status == "announced"
            and load.outstanding
        ]
        custody = [r.to_dict() for r in self.receipts.values() if r.batch == proof.batch and r.contract_status == "custody_hold"]
        undelivered = []
        for batch_id, batch in self.batches.items():
            if batch_id == proof.batch:
                undelivered.append({"batch": batch_id, "undelivered_committed": batch.committed, "available": batch.available})
        unused = []
        for receipt in self.receipts.values():
            if receipt.batch != proof.batch:
                continue
            planted = sum(p.count for p in self.plantings.values() if p.receipt == receipt.id)
            unused.append(
                {
                    "receipt": receipt.id,
                    "site": receipt.site,
                    "unused": receipt.accepted - receipt.returned - planted,
                }
            )
        return {
            "proof": proof.to_dict(),
            "pending_loads": pending_loads,
            "custody_receipts": custody,
            "batch_supply": undelivered,
            "unused_quantities": unused,
            "next_actions": [
                action
                for action in (
                    "receive_load" if pending_loads else None,
                    "resolve_custody" if custody else None,
                )
                if action
            ],
        }

    # -- 种植与错批 ---------------------------------------------------------

    def record_planting(
        self,
        receipt_id: str,
        row: str,
        count: int,
        planted_at: int | None = None,
        site: str | None = None,
    ) -> dict[str, Any]:
        receipt = self._receipt(receipt_id)
        count = int(count)
        if count <= 0:
            raise DomainError("bad_quantity", "种植数量应为正数", {"quantity": count})
        if site is not None and site != receipt.site:
            raise DomainError(
                "site_mismatch",
                f"收苗记录属于地段 {receipt.site}，不能种到 {site}",
                {"receipt": receipt_id, "site": receipt.site, "planted_site": site},
            )
        if not str(row or "").strip():
            raise DomainError("row_required", "种植必须登记种植点")
        planted = sum(p.count for p in self.plantings.values() if p.receipt == receipt_id)
        available = receipt.accepted - receipt.returned - planted
        if count > available:
            raise DomainError(
                "quantity_exceeded",
                f"收苗记录 {receipt_id} 尚可种 {available} 株，本次 {count} 株超出",
                {"receipt": receipt_id, "available": available, "count": count},
            )
        at = self.clock() if planted_at is None else int(planted_at)
        if at < receipt.received_at:
            raise DomainError(
                "planted_before_receipt",
                "种植时间不能早于收苗时间",
                {"planted_at": at, "received_at": receipt.received_at},
            )
        flagged = None
        if receipt.contract_status != "conforming" or receipt.dispute:
            flagged = "source_unverified"
        planting = Planting(
            id=self._new_id("g", self.plantings),
            receipt=receipt_id,
            site=receipt.site,
            row=str(row),
            count=count,
            planted_at=at,
            flagged=flagged,
        )
        self.plantings[planting.id] = planting
        return {"planting": planting.to_dict(), "warranty": self.warranty(receipt_id)}

    def list_plantings(self, site: str | None = None, order_id: str | None = None) -> list[dict[str, Any]]:
        result = []
        for planting in self.plantings.values():
            receipt = self.receipts[planting.receipt]
            if site is not None and planting.site != site:
                continue
            if order_id is not None and receipt.order != order_id:
                continue
            result.append({**planting.to_dict(), "order": receipt.order, "batch": receipt.batch})
        return result

    def report_mismatch(
        self, receipt_id: str, observed_batch: str, note: str | None = None, at: int | None = None
    ) -> dict[str, Any]:
        """发现错批：已种记录留在原种植点，只登记争议与后续处置。"""
        receipt = self._receipt(receipt_id)
        self._batch(observed_batch)
        if observed_batch == receipt.batch:
            raise DomainError(
                "not_a_mismatch",
                f"发现的批次与签收批次一致（{observed_batch}）",
                {"receipt": receipt_id, "batch": receipt.batch},
            )
        if receipt.resolution:
            raise DomainError(
                "already_resolved",
                f"收苗记录 {receipt_id} 已处置，不能再登记错批",
                {"receipt": receipt_id},
            )
        receipt.dispute = {
            "observed_batch": observed_batch,
            "note": note,
            "reported_at": self.clock() if at is None else at,
            "plantings_retained": [
                p.id for p in self.plantings.values() if p.receipt == receipt_id
            ],
        }
        # 已种记录留在原种植点，只加标记，不改种植点、不改批次。
        for planting in self.plantings.values():
            if planting.receipt == receipt_id and planting.flagged is None:
                planting.flagged = "batch_dispute"
        return {
            "receipt": receipt.to_dict(),
            "plantings": [p.to_dict() for p in self.plantings.values() if p.receipt == receipt_id],
            "warranty": self.warranty(receipt_id),
        }

    def resolve_receipt(
        self,
        receipt_id: str,
        decision: str,
        quantity: int | None = None,
        note: str | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        """现场处置：继续养护、补领或退料。"""
        receipt = self._receipt(receipt_id)
        if receipt.resolution:
            raise DomainError(
                "already_resolved",
                f"收苗记录 {receipt_id} 已处置为 {receipt.resolution['decision']}",
                {"receipt": receipt_id, "resolution": receipt.resolution},
            )
        planted = sum(p.count for p in self.plantings.values() if p.receipt == receipt_id)
        unused = receipt.accepted - receipt.returned - planted
        record: dict[str, Any] = {
            "decision": decision,
            "note": note,
            "at": self.clock() if at is None else at,
        }
        if decision == "continue_care":
            if receipt.contract_status == "conforming" and not receipt.dispute:
                raise DomainError(
                    "nothing_to_resolve",
                    f"收苗记录 {receipt_id} 已符合合同",
                    {"receipt": receipt_id},
                )
            proof = next((p for p in self.proofs.values() if p.batch == receipt.batch), None)
            if proof is None:
                raise DomainError(
                    "proof_missing",
                    f"批次 {receipt.batch} 尚无补齐的苗源凭证，不能按继续养护处置",
                    {"receipt": receipt_id, "batch": receipt.batch},
                )
            record["proof_id"] = proof.id
        elif decision == "top_up":
            order = self.orders[receipt.order]
            unused = receipt.accepted - receipt.returned - planted
            shortfall = unused if unused > 0 else 0
            if receipt.dispute:
                shortfall = max(shortfall, receipt.accepted - receipt.returned)
            requested = int(quantity) if quantity is not None else shortfall
            if requested <= 0:
                raise DomainError(
                    "bad_quantity",
                    f"收苗记录 {receipt_id} 没有可补领的数量，需指定数量",
                    {"receipt": receipt_id, "quantity": requested},
                )
            record["requested_quantity"] = requested
            record["follow_up_order"] = {
                "site": receipt.site,
                "quantity": requested,
                "follow_up_of": order.id,
                "outcome": "pending_acceptance",
                "note": "补领由施工方按地段重新申领后，苗圃另提交供货承诺",
            }
        elif decision == "return_stock":
            quantity = unused if quantity is None else int(quantity)
            if quantity <= 0:
                raise DomainError(
                    "bad_quantity", "退料数量应为正数", {"quantity": quantity}
                )
            if quantity > unused:
                raise DomainError(
                    "quantity_exceeded",
                    f"收苗记录 {receipt_id} 可退 {unused} 株，本次 {quantity} 株超出",
                    {"receipt": receipt_id, "available": unused, "quantity": quantity},
                )
            batch = self._batch(receipt.batch)
            receipt.returned += quantity
            if receipt.contract_status == "conforming":
                batch.delivered -= quantity
                batch.available += quantity
            record["quantity"] = quantity
        else:
            raise DomainError(
                "bad_decision",
                "处置应为 continue_care、top_up 或 return_stock",
                {"decision": decision},
            )
        receipt.resolution = record
        return {
            "receipt": receipt.to_dict(),
            "warranty": self.warranty(receipt_id),
            "batch": self._batch(receipt.batch).to_dict(),
        }

    # -- 保活 ---------------------------------------------------------------

    def warranty(self, receipt_id: str, as_of: int | None = None) -> dict[str, Any]:
        """按实际种植日与双方签认的供货关联，判断这一车苗的保活责任。"""
        receipt = self._receipt(receipt_id)
        plantings = [p for p in self.plantings.values() if p.receipt == receipt_id]
        as_of = self.clock() if as_of is None else int(as_of)
        base: dict[str, Any] = {
            "receipt": receipt_id,
            "order": receipt.order,
            "site": receipt.site,
            "batch": receipt.batch,
            "basis": self.warranty_basis,
            "planted": sum(p.count for p in plantings),
            "as_of": as_of,
        }
        if not plantings:
            return {**base, "status": "not_planted", "claimable": False, "start": None, "end": None}
        start = min(p.planted_at for p in plantings)
        end = start + self.warranty_days
        base.update({"start": start, "end": end})
        if receipt.dispute and not receipt.resolution:
            return {**base, "status": "disputed_under_review", "claimable": False}
        source_ok = receipt.contract_status == "conforming" or (
            receipt.resolution and receipt.resolution["decision"] == "continue_care"
        )
        if not source_ok:
            return {**base, "status": "pending_source", "claimable": False}
        if not receipt.fully_signed:
            return {**base, "status": "awaiting_countersign", "claimable": False}
        if as_of > end:
            return {
                **base,
                "status": "expired",
                "claimable": False,
                "days_over": as_of - end,
            }
        return {
            **base,
            "status": "active",
            "claimable": True,
            "days_remaining": end - as_of,
        }

    def submit_claim(
        self, receipt_id: str, as_of: int | None = None, note: str | None = None
    ) -> dict[str, Any]:
        """按车发起补植养护；同地段其他车的争议不影响这一车。"""
        receipt = self._receipt(receipt_id)
        status = self.warranty(receipt_id, as_of=as_of)
        if status["status"] == "not_planted":
            raise DomainError("not_planted", "尚未种植，不能发起保活", {"receipt": receipt_id})
        if status["status"] == "disputed_under_review":
            raise DomainError(
                "dispute_unresolved",
                "该车存在错批争议，待现场处置后再发起",
                {"receipt": receipt_id, "dispute": receipt.dispute},
            )
        if status["status"] == "awaiting_countersign":
            raise DomainError(
                "care_handover_missing",
                "供货关联尚未双方签认，保活责任未成立",
                {"receipt": receipt_id, "missing": "nursery_signer"},
            )
        if status["status"] == "pending_source":
            raise DomainError(
                "eligibility_missing",
                f"批次 {receipt.batch} 缺少苗源凭证，不能发起保活",
                {"receipt": receipt_id, "missing": "origin_proof"},
            )
        if status["status"] == "expired":
            raise DomainError(
                "warranty_expired",
                f"该车保活已于 {status['end']} 到期（实际种植日 {status['start']}）",
                {
                    "receipt": receipt_id,
                    "start": status["start"],
                    "end": status["end"],
                    "as_of": status["as_of"],
                },
            )
        claim = Claim(
            id=self._new_id("c", self.claims),
            receipt=receipt_id,
            order=receipt.order,
            site=receipt.site,
            as_of=status["as_of"],
            warranty_start=status["start"],
            warranty_end=status["end"],
            note=note,
        )
        self.claims[claim.id] = claim
        return {"claim": claim.to_dict(), "warranty": status}

    def list_claims(self, order_id: str | None = None) -> list[dict[str, Any]]:
        return [
            claim.to_dict()
            for claim in self.claims.values()
            if order_id is None or claim.order == order_id
        ]

    def order_warranty(self, order_id: str) -> dict[str, Any]:
        """按车列示保活状态，不把整张申领合成一个到期判断。"""
        self._require(self.orders, order_id, "unknown_order", "申领")
        lots = []
        for receipt in self.receipts.values():
            if receipt.order != order_id:
                continue
            lots.append(
                {
                    "receipt": receipt.id,
                    "batch": receipt.batch,
                    "accepted": receipt.accepted,
                    "contract_status": receipt.contract_status,
                    "warranty": self.warranty(receipt.id),
                }
            )
        return {
            "order": order_id,
            "lots": lots,
            "claims": self.list_claims(order_id),
            # 整单不设单一到期日；任何一车都不因其他车的争议被算作逾保。
            "documents": {
                "overdue_by_document": False,
                "note": "保活按每一车分别计算",
            },
        }

    # -- 反查缺口 -----------------------------------------------------------

    def gaps(self, order_id: str) -> list[dict[str, Any]]:
        """缺的是供货承诺、资格证明、实物交割还是养护接手。"""
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        totals = self.order_totals(order_id)
        found: list[dict[str, Any]] = []

        pending_bids = [
            bid for bid in self.bids.values() if bid.order == order_id and bid.status == "pending"
        ]
        if totals["remaining_need"] > 0 and not pending_bids:
            found.append(
                {
                    "kind": "supply_commitment",
                    "status": "missing",
                    "quantity": totals["remaining_need"],
                    "detail": "尚无苗圃供货承诺覆盖剩余申领数量",
                }
            )
        elif pending_bids:
            found.append(
                {
                    "kind": "supply_commitment",
                    "status": "awaiting_decision",
                    "quantity": sum(b.quantity for b in pending_bids),
                    "bids": [b.id for b in pending_bids],
                }
            )

        custody = [
            r
            for r in self.receipts.values()
            if r.order == order_id
            and r.contract_status == "custody_hold"
            and not r.resolution
            and not r.dispute
        ]
        if custody:
            found.append(
                {
                    "kind": "eligibility_proof",
                    "status": "missing",
                    "quantity": sum(r.accepted - r.returned for r in custody),
                    "receipts": [r.id for r in custody],
                    "batches": sorted({r.batch for r in custody}),
                }
            )

        announced = [
            load
            for load in self.loads.values()
            if load.order == order_id and load.status == "announced" and load.outstanding
        ]
        if totals["committed"] > 0 or announced:
            found.append(
                {
                    "kind": "physical_delivery",
                    "status": "awaiting_receipt" if announced else "missing",
                    "quantity": totals["committed"],
                    "loads": [load.id for load in announced],
                }
            )

        unsigned = [r for r in self.receipts.values() if r.order == order_id and not r.fully_signed]
        unplanted = []
        for receipt in self.receipts.values():
            if receipt.order != order_id:
                continue
            planted = sum(p.count for p in self.plantings.values() if p.receipt == receipt.id)
            unused = receipt.accepted - receipt.returned - planted
            if unused > 0:
                unplanted.append({"receipt": receipt.id, "quantity": unused})
        if unsigned or unplanted:
            found.append(
                {
                    "kind": "care_handover",
                    "status": "missing",
                    "unsigned_receipts": [r.id for r in unsigned],
                    "unplanted": unplanted,
                }
            )
        return found

    def next_actions(self, order_id: str) -> list[str]:
        order = self._require(self.orders, order_id, "unknown_order", "申领")
        if order.status == "closed":
            return ["open_follow_up_order"]
        actions: list[str] = []
        totals = self.order_totals(order_id)
        if any(b.order == order_id and b.status == "pending" for b in self.bids.values()):
            actions.append("decide_bid")
        if totals["remaining_need"] > 0 and "decide_bid" not in actions:
            actions.append("submit_bid")
        if any(
            b.order == order_id and b.status == "approved" and self._capacity(b) > 0
            for b in self.bids.values()
        ):
            actions.append("announce_load")
        if any(load.order == order_id and load.status == "announced" and load.outstanding for load in self.loads.values()):
            actions.append("receive_load")
        if any(r.order == order_id and not r.fully_signed for r in self.receipts.values()):
            actions.append("countersign")
        if any(r.order == order_id and r.contract_status == "custody_hold" for r in self.receipts.values()):
            actions.append("register_proof")
        for receipt in self.receipts.values():
            if receipt.order != order_id:
                continue
            planted = sum(p.count for p in self.plantings.values() if p.receipt == receipt.id)
            if receipt.accepted - receipt.returned - planted > 0:
                actions.append("record_planting")
                break
        if any(r.order == order_id and r.dispute and not r.resolution for r in self.receipts.values()):
            actions.append("resolve_receipt")
        if any(
            r.order == order_id
            and (r.resolution or r.contract_status == "conforming")
            and r.fully_signed
            and not r.dispute
            and not any(c.receipt == r.id for c in self.claims.values())
            and self.warranty(r.id)["status"] == "active"
            for r in self.receipts.values()
        ):
            actions.append("submit_claim")
        if totals["remaining_need"] <= 0 and not any(
            load.order == order_id and load.outstanding for load in self.loads.values()
        ):
            actions.append("close_order")
        actions.append("open_follow_up_order")
        return actions

    # -- 汇总 ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "sites": [site.to_dict() for site in self.sites.values()],
            "batches": [batch.to_dict() for batch in self.batches.values()],
            "orders": [
                {"id": order.id, "site": order.site, "status": order.status}
                for order in self.orders.values()
            ],
            "counts": {
                "bids": len(self.bids),
                "loads": len(self.loads),
                "receipts": len(self.receipts),
                "plantings": len(self.plantings),
                "proofs": len(self.proofs),
                "claims": len(self.claims),
            },
        }
