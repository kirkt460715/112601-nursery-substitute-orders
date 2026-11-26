"""补植履约后端：把申领、替代确认、分车收苗与保活责任串成一个服务。

资料台（evidence_desk.py）只提供独立回执与到达记录；本服务保存履约状态，
并在接收合作方回执、复核证据时向资料台查询。所有业务写入都要求调用方
显式给出事实，不存在由单据自动回写已签认苗源的路径。
"""

from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from evidence_desk import DATA
from fulfillment import DomainError, Fulfillment


class DeskClient:
    """对资料台的只读查询。"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def get(self, route: str) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=2)
        try:
            connection.request("GET", route)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        except (OSError, HTTPException, ValueError) as exc:
            return 502, {"status": "desk_unreachable", "detail": str(exc)}
        finally:
            connection.close()

    def partner(self, action: str, desk: str) -> dict:
        status, body = self.get("/partner?" + _query({"action": action, "desk": desk}))
        if status == 503:
            return {"status": "temporarily_unavailable", "action": action, "desk": desk}
        if status == 404:
            return {"status": "unknown_request", "action": action, "desk": desk}
        if status != 200:
            return {"status": "desk_unreachable", "action": action, "desk": desk}
        return {"status": "ok", "action": action, "desk": desk, "fact": body.get("fact")}

    def message(self, sequence: int) -> dict | None:
        status, body = self.get("/messages?" + _query({"after": sequence - 1, "size": 1}))
        if status != 200:
            return None
        messages = body.get("messages") or []
        if messages and messages[0].get("sequence") == sequence:
            return messages[0]
        return None


def _query(values: dict[str, object]) -> str:
    from urllib.parse import urlencode

    return urlencode(values)


class FulfillmentServer(ThreadingHTTPServer):
    def __init__(
        self,
        port,
        register,
        desk: DeskClient | None = None,
        clock=None,
        state_path: Path | None = None,
    ):
        self.engine = Fulfillment(register, clock=clock)
        self.desk = desk
        self.state_path = Path(state_path) if state_path else None
        self.lock = Lock()
        if self.state_path and self.state_path.exists():
            self.engine.restore(
                json.loads(self.state_path.read_text(encoding="utf-8"))
            )
        super().__init__(("127.0.0.1", port), FulfillmentHandler)

    def save(self) -> None:
        """状态变更后落盘；先写临时文件再替换，避免半份快照。"""
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.engine.snapshot(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)


class FulfillmentHandler(BaseHTTPRequestHandler):
    server: FulfillmentServer

    # -- 基础设施 -----------------------------------------------------------

    def respond(self, status: int, data) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise DomainError("invalid_json", "请求体应为 JSON 对象") from None
        if not isinstance(data, dict):
            raise DomainError("invalid_json", "请求体应为 JSON 对象")
        return data

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method: str):
        route = urlsplit(self.path)
        path = route.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(route.query, keep_blank_values=True).items()}
        try:
            with self.server.lock:
                handler, args = self.match(method, path)
                data = self.body() if method == "POST" else {}
                result = handler(query, data, *args)
                if method == "POST":
                    # 只读查询不会走到这里；写入成功后才落盘。
                    self.server.save()
        except DomainError as exc:
            self.respond(exc.status, exc.as_dict())
            return
        except BrokenPipeError:
            return
        self.respond(200, result)

    # -- 路由 ---------------------------------------------------------------

    def match(self, method: str, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        routes = self.routes()
        for (verb, pattern), handler in routes:
            if verb != method or len(pattern) != len(parts):
                continue
            args = []
            ok = True
            for expected, actual in zip(pattern, parts):
                if expected.startswith(":"):
                    args.append(actual)
                elif expected != actual:
                    ok = False
                    break
            if ok:
                return handler, args
        raise DomainError("not_found", f"未知路由: {method} {path}", status=404)

    def routes(self):
        return [
            (("GET", ("health",)), self.health),
            (("GET", ()), self.summary),
            (("POST", ("orders",)), self.open_order),
            (("GET", ("orders",)), self.list_orders),
            (("GET", ("orders", ":id")), self.order),
            (("POST", ("orders", ":id", "close")), self.close_order),
            (("POST", ("orders", ":id", "bids")), self.submit_bid),
            (("GET", ("orders", ":id", "bids")), self.list_bids),
            (("GET", ("orders", ":id", "substitution-review")), self.review),
            (("GET", ("orders", ":id", "warranty")), self.order_warranty),
            (("GET", ("orders", ":id", "gaps")), self.gaps),
            (("POST", ("bids", ":id", "decide")), self.decide_bid),
            (("POST", ("loads",)), self.announce_load),
            (("GET", ("loads",)), self.list_loads),
            (("POST", ("loads", ":id", "close")), self.close_load),
            (("POST", ("loads", ":id", "reduce")), self.reduce_load),
            (("POST", ("loads", ":id", "receive")), self.receive_load),
            (("GET", ("receipts",)), self.list_receipts),
            (("POST", ("receipts", ":id", "countersign")), self.countersign),
            (("POST", ("receipts", ":id", "plantings")), self.record_planting),
            (("GET", ("receipts", ":id", "warranty")), self.warranty),
            (("POST", ("receipts", ":id", "claims")), self.submit_claim),
            (("POST", ("receipts", ":id", "mismatch")), self.report_mismatch),
            (("POST", ("receipts", ":id", "resolve")), self.resolve_receipt),
            (("POST", ("proofs",)), self.register_proof),
            (("GET", ("proofs",)), self.list_proofs),
            (("GET", ("proofs", ":id", "recheck")), self.recheck),
            (("GET", ("plantings",)), self.list_plantings),
            (("GET", ("claims",)), self.list_claims),
            (("GET", ("desk", "partner")), self.desk_partner),
            (("GET", ("desk", "messages")), self.desk_messages),
        ]

    # -- 处理 ---------------------------------------------------------------

    def health(self, query, data):
        return {"status": "ok"}

    def summary(self, query, data):
        return self.server.engine.summary()

    def open_order(self, query, data):
        return self.server.engine.open_order(
            site_id=data.get("site"),
            quantity=data.get("quantity"),
            follow_up_of=data.get("follow_up_of"),
        )

    def list_orders(self, query, data):
        return {"orders": self.server.engine.list_orders()}

    def order(self, query, data, order_id):
        return self.server.engine.order_status(order_id)

    def close_order(self, query, data, order_id):
        return self.server.engine.close_order(order_id, note=data.get("note"))

    def submit_bid(self, query, data, order_id):
        return self.server.engine.submit_bid(order_id, data.get("batch"), data.get("quantity"))

    def list_bids(self, query, data, order_id):
        return {"bids": self.server.engine.list_bids(order_id)}

    def review(self, query, data, order_id):
        return self.server.engine.review_substitution(order_id, query.get("batch"))

    def order_warranty(self, query, data, order_id):
        return self.server.engine.order_warranty(order_id)

    def gaps(self, query, data, order_id):
        return {"order": order_id, "gaps": self.server.engine.gaps(order_id)}

    def decide_bid(self, query, data, bid_id):
        return self.server.engine.decide_bid(
            bid_id, data.get("decision"), quantity=data.get("quantity")
        )

    def announce_load(self, query, data):
        return self.server.engine.announce_load(
            order_id=data.get("order"),
            batch_id=data.get("batch"),
            quantity=data.get("quantity"),
            vehicle=data.get("vehicle"),
        )

    def list_loads(self, query, data):
        return {
            "loads": self.server.engine.list_loads(
                order_id=query.get("order"), site=query.get("site")
            )
        }

    def close_load(self, query, data, load_id):
        return self.server.engine.close_load(load_id, note=data.get("note"))

    def reduce_load(self, query, data, load_id):
        return self.server.engine.reduce_load(
            load_id, quantity=data.get("quantity"), note=data.get("note")
        )

    def receive_load(self, query, data, load_id):
        partner = None
        requested = data.get("partner")
        if requested:
            action = requested.get("action")
            desk = requested.get("desk")
            if not (action and desk):
                raise DomainError("bad_partner_request", "合作方查询需要 action 与 desk")
            partner = {"action": action, "desk": desk}
            if self.server.desk is not None:
                partner = self.server.desk.partner(action, desk)
        return self.server.engine.receive_load(
            load_id,
            vehicle=data.get("vehicle"),
            accepted=data.get("accepted"),
            field_signer=data.get("field_signer"),
            seen=data.get("seen"),
            nursery_signer=data.get("nursery_signer"),
            proof_id=data.get("proof_id"),
            partner=partner,
        )

    def list_receipts(self, query, data):
        return {
            "receipts": self.server.engine.list_receipts(
                order_id=query.get("order"), site=query.get("site")
            )
        }

    def countersign(self, query, data, receipt_id):
        return self.server.engine.countersign(receipt_id, data.get("nursery_signer"))

    def record_planting(self, query, data, receipt_id):
        return self.server.engine.record_planting(
            receipt_id,
            row=data.get("row"),
            count=data.get("count"),
            planted_at=data.get("planted_at"),
            site=data.get("site"),
        )

    def warranty(self, query, data, receipt_id):
        as_of = query.get("as_of")
        return self.server.engine.warranty(
            receipt_id, as_of=int(as_of) if as_of is not None else None
        )

    def submit_claim(self, query, data, receipt_id):
        as_of = data.get("as_of")
        return self.server.engine.submit_claim(
            receipt_id,
            as_of=int(as_of) if as_of is not None else None,
            note=data.get("note"),
        )

    def report_mismatch(self, query, data, receipt_id):
        return self.server.engine.report_mismatch(
            receipt_id, data.get("observed_batch"), note=data.get("note")
        )

    def resolve_receipt(self, query, data, receipt_id):
        return self.server.engine.resolve_receipt(
            receipt_id,
            decision=data.get("decision"),
            quantity=data.get("quantity"),
            note=data.get("note"),
        )

    def register_proof(self, query, data):
        return self.server.engine.register_proof(
            data.get("batch"),
            data.get("citation"),
            source=data.get("source"),
            covers=data.get("covers"),
        )

    def list_proofs(self, query, data):
        return {"proofs": self.server.engine.list_proofs(query.get("batch"))}

    def recheck(self, query, data, proof_id):
        return self.server.engine.recheck(proof_id)

    def list_plantings(self, query, data):
        return {
            "plantings": self.server.engine.list_plantings(
                site=query.get("site"), order_id=query.get("order")
            )
        }

    def list_claims(self, query, data):
        return {"claims": self.server.engine.list_claims(query.get("order"))}

    def desk_partner(self, query, data):
        if self.server.desk is None:
            raise DomainError("desk_not_configured", "未配置资料台", status=503)
        return self.server.desk.partner(query.get("action", ""), query.get("desk", ""))

    def desk_messages(self, query, data):
        if self.server.desk is None:
            raise DomainError("desk_not_configured", "未配置资料台", status=503)
        sequence = int(query.get("sequence", "0"))
        message = self.server.desk.message(sequence)
        if message is None:
            raise DomainError("unknown_message", f"未取得序号 {sequence} 的到达记录", status=404)
        return {"message": message}

    def log_message(self, *args):
        return


def build_server(args) -> FulfillmentServer:
    register = json.loads((Path(args.data) / "register.json").read_text(encoding="utf-8"))
    desk = DeskClient(*args.desk) if args.desk else None
    return FulfillmentServer(args.port, register, desk=desk, state_path=args.state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument(
        "--desk",
        nargs=2,
        metavar=("HOST", "PORT"),
        help="资料台地址，例如 --desk 127.0.0.1 8765",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="履约状态文件；省略则只保存在内存，重启即回到登记初始值",
    )
    args = parser.parse_args()
    server = build_server(args)
    print(f"履约后端 http://127.0.0.1:{server.server_port}", flush=True)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
