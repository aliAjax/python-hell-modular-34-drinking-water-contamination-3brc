import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from .domain import DomainError


def _unquote(value):
    return unquote(value or "")


def build_handler(service, static_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularPythonHell/1.0"

        def log_message(self, fmt, *args):
            return

        def _identity(self):
            actor = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            region = self.headers.get("X-Region", "").strip() or None
            return actor, role, region

        def _json_body(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise DomainError("invalid_json", "请求体不是有效 JSON", 400)

        def _send(self, status, value, content_type="application/json; charset=utf-8"):
            if not isinstance(value, (bytes, bytearray)):
                value = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

        def _error(self, exc):
            status = getattr(exc, "status", 500)
            code = getattr(exc, "code", "internal_error")
            self._send(status, {"error": code, "message": str(exc)})

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/health":
                    return self._send(200, {"status": "ok"})
                if path == "/api/state":
                    return self._send(200, service.state())
                if path == "/api/items":
                    return self._send(200, {"items": service.list_items()})
                # 应急水源余量与占用去向
                if path == "/api/water-sources":
                    return self._send(200, {"sources": service.list_water_sources()})
                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "water-sources"]:
                    return self._send(200, service.get_water_source(parts[2]))
                if len(parts) == 3 and parts[:2] == ["api", "items"]:
                    return self._send(200, service.get_item(int(parts[2])))
                if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "audit":
                    item = service.get_item(int(parts[2]))
                    return self._send(200, {"events": item["audit"]})
                # 事件下的调度单
                if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "dispatch-orders":
                    return self._send(200, {"orders": service.list_dispatch_orders(int(parts[2]))})
                # 片区送水进度：/api/zones/progress?item_id=&zone_id=
                if parts == ["api", "zones", "progress"]:
                    item_id = int(query["item_id"][0]) if query.get("item_id") else None
                    zone_id = query.get("zone_id", [None])[0]
                    return self._send(200, service.zone_progress(item_id, zone_id))
                # 分配台账：/api/allocation-ledger?source_code=&item_id=
                if parts == ["api", "allocation-ledger"]:
                    source_code = query.get("source_code", [None])[0]
                    item_id = int(query["item_id"][0]) if query.get("item_id") else None
                    return self._send(200, service.allocation_ledger(source_code, item_id))
                # 单张调度单查询
                if len(parts) == 3 and parts[:2] == ["api", "dispatch-orders"]:
                    return self._send(200, service.get_dispatch_order(parts[2]))
                if path == "/":
                    file_path = os.path.join(static_dir, "index.html")
                    with open(file_path, "rb") as handle:
                        content = handle.read()
                    return self._send(200, content, "text/html; charset=utf-8")
                return self._send(404, {"error": "not_found", "message": "接口不存在"})
            except DomainError as exc:
                return self._error(exc)
            except (ValueError, OSError) as exc:
                return self._error(DomainError("invalid_request", str(exc), 400))

        def do_POST(self):
            actor = role = region = None
            try:
                actor, role, region = self._identity()
                path = urlparse(self.path).path
                payload = self._json_body()
                parts = [part for part in path.split("/") if part]
                if parts == ["api", "items"]:
                    return self._send(201, service.create_item(payload, actor, role, region))
                if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "sources":
                    return self._send(201, service.add_source(int(parts[2]), payload, actor, role, region))
                if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "actions":
                    action = payload.pop("action", "")
                    if not action:
                        raise DomainError("action_required", "缺少 action", 400)
                    expected = payload.pop("expected_version", None)
                    return self._send(200, service.act(int(parts[2]), action, payload, actor, role, expected, region))
                # 登记应急水源
                if parts == ["api", "water-sources"]:
                    return self._send(201, service.register_water_source(payload, actor, role))
                # 调度申请：POST /api/items/<id>/dispatch
                if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "dispatch":
                    expected = payload.pop("expected_version", None)
                    return self._send(201, service.request_dispatch(int(parts[2]), payload, actor, role, expected))
                # 片区送水回报：POST /api/items/<id>/dispatch/<order_no>/delivery
                if (len(parts) == 6 and parts[:2] == ["api", "items"]
                        and parts[3] == "dispatch" and parts[5] == "delivery"):
                    order_no = _unquote(parts[4])
                    return self._send(200, service.report_delivery(int(parts[2]), order_no, payload, actor, role))
                return self._send(404, {"error": "not_found", "message": "接口不存在"})
            except DomainError as exc:
                return self._error(exc)
            except Exception as exc:
                return self._error(DomainError("internal_error", str(exc), 500))

    return Handler
