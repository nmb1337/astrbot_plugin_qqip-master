import hashlib
import ipaddress
import os
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")


@register("astrbot_plugin_qqip", "qqip", "群聊 IP 归属地记录与查询", "1.0.0")
class QQIPPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._group_records: dict[str, dict[str, dict[str, Any]]] = {}
        self._group_activity: dict[str, dict[str, datetime]] = {}
        self._group_sender_meta: dict[str, dict[str, dict[str, str]]] = {}
        self._group_network_hints: dict[str, dict[str, list[str]]] = {}
        self._consent_sessions: dict[str, dict[str, Any]] = {}
        self._consent_records: dict[str, list[dict[str, str]]] = {}
        self._consent_lock = threading.Lock()
        self._hash_salt = secrets.token_hex(16)
        self._listen_host = str(
            self._cfg_get("tracker_host", os.getenv("QQIP_LISTEN_HOST", "0.0.0.0"))
        ).strip() or "0.0.0.0"
        self._listen_port = self._safe_int(
            str(self._cfg_get("tracker_port", os.getenv("QQIP_LISTEN_PORT", "8787"))),
            8787,
        )
        self._public_base_url = str(
            self._cfg_get("public_base_url", os.getenv("QQIP_PUBLIC_BASE_URL", ""))
        ).strip()
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._ip_location_cache: dict[str, str] = {}
        self._max_records_per_group = max(
            10,
            self._safe_int(str(self._cfg_get("max_records_per_group", 100)), 100),
        )
        self._show_limit = max(
            1,
            self._safe_int(str(self._cfg_get("show_limit", 10)), 10),
        )

    async def initialize(self):
        self._start_consent_http_server()
        logger.info("QQIP 插件已初始化")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return

        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            return

        sender_id = str(self._get_sender_id(message_obj) or "unknown")
        sender_name = str(event.get_sender_name() or sender_id)
        sender_meta = self._extract_sender_meta(message_obj)

        # 即便没有 IP，也记录发言痕迹，便于查询时给出准确提示。
        self._group_activity.setdefault(group_id, {})[sender_id] = datetime.now()
        self._group_sender_meta.setdefault(group_id, {})[sender_id] = sender_meta

        hints = self._collect_network_hints(
            [
                getattr(message_obj, "raw_message", None),
                message_obj,
                getattr(message_obj, "sender", None),
            ]
        )
        self._group_network_hints.setdefault(group_id, {})[sender_id] = hints

        ip_list = self._extract_ips(message_obj)
        if not ip_list:
            return

        group_map = self._group_records.setdefault(group_id, {})
        for ip in ip_list:
            key = f"{sender_id}::{ip}"
            group_map[key] = {
                "ip": ip,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "timestamp": datetime.now(),
            }

        # 控制内存体积，保留最近 N 条
        if len(group_map) > self._max_records_per_group:
            sorted_items = sorted(
                group_map.items(),
                key=lambda kv: kv[1].get("timestamp", datetime.min),
                reverse=True,
            )
            self._group_records[group_id] = dict(sorted_items[: self._max_records_per_group])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qqip")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def query_qqip(self, event: AstrMessageEvent, target_qq: str = ""):
        """管理员查询指定 QQ 的 IP 归属地。用法: /qqip 123456"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        target_qq = re.sub(r"\D", "", target_qq or "")
        if not target_qq:
            yield event.plain_result("用法: /qqip QQ号")
            return

        records_map = self._group_records.get(group_id, {})
        records = sorted(
            [rec for rec in records_map.values() if str(rec.get("sender_id", "")) == target_qq],
            key=lambda item: item.get("timestamp", datetime.min),
            reverse=True,
        )[: self._show_limit]

        if not records:
            last_seen = self._group_activity.get(group_id, {}).get(target_qq)
            if isinstance(last_seen, datetime):
                sender_meta = self._group_sender_meta.get(group_id, {}).get(target_qq, {})
                area = sender_meta.get("area", "")
                lines = [
                    f"QQ {target_qq} 有发言记录。",
                    f"最近发言时间: {last_seen.strftime('%Y.%m.%d-%H:%M:%S')}",
                    "但当前平台事件未提供可用 IP 字段，无法定位到市。",
                ]
                if area:
                    lines.append(f"资料地区(非IP): {area}")
                yield event.plain_result(
                    "\n".join(lines)
                )
            else:
                yield event.plain_result(f"未找到 QQ {target_qq} 的发言记录。")
            return

        lines: list[str] = [f"QQ {target_qq} 的 IP 记录", "===================="]

        for rec in records:
            ip = str(rec.get("ip", ""))
            ts = rec.get("timestamp")
            if not isinstance(ts, datetime):
                ts = datetime.now()
            addr = await self._resolve_ip_location(ip)

            lines.extend(
                [
                    "IP:",
                    ip,
                    f"Address:  {addr}",
                    f"Time:  {ts.strftime('%Y.%m.%d-%H:%M:%S')}",
                    "====================",
                ]
            )

        lines.append(f"共{len(records)}个")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qqip调试")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def debug_qqip(self, event: AstrMessageEvent, target_qq: str = ""):
        """调试指定 QQ 最近消息里是否存在可提取的网络字段。用法: /qqip调试 123456"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        target_qq = re.sub(r"\D", "", target_qq or "")
        if not target_qq:
            yield event.plain_result("用法: /qqip调试 QQ号")
            return

        last_seen = self._group_activity.get(group_id, {}).get(target_qq)
        hints = self._group_network_hints.get(group_id, {}).get(target_qq, [])
        sender_meta = self._group_sender_meta.get(group_id, {}).get(target_qq, {})

        if not isinstance(last_seen, datetime):
            yield event.plain_result(f"未找到 QQ {target_qq} 的发言记录。")
            return

        lines = [
            f"QQ {target_qq} 调试信息",
            f"最近发言: {last_seen.strftime('%Y.%m.%d-%H:%M:%S')}",
        ]

        if sender_meta:
            simple_meta = ", ".join([f"{k}={v}" for k, v in sender_meta.items() if v])
            if simple_meta:
                lines.append(f"sender字段: {simple_meta}")

        if hints:
            lines.append("网络相关字段样本:")
            for item in hints[:8]:
                lines.append(f"- {item}")
        else:
            lines.append("网络相关字段样本: 未发现")

        lines.append("说明: 若这里没有任何 IP 字段，则平台侧未上报，插件无法推算市级位置。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qqip链接")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def create_consent_link(self, event: AstrMessageEvent, title: str = "群聊的聊天记录"):
        """生成同意后记录的访问链接。用法: /qqip链接 标题(可选)"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        session_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")
        with self._consent_lock:
            self._consent_sessions[session_id] = {
                "group_id": group_id,
                "title": title or "群聊的聊天记录",
                "created_at": datetime.now(),
                "creator": str(self._get_sender_id(message_obj) or ""),
            }
            self._consent_records[session_id] = []

        link = f"{self._get_public_base_url()}/qqip/consent/{session_id}"
        lines = [
            "已生成同意记录链接。",
            f"链接ID: {session_id}",
            f"访问链接: {link}",
            f"查询命令: /qqip记录 {session_id}",
        ]
        if not self._public_base_url:
            lines.append("提示: 未设置 QQIP_PUBLIC_BASE_URL，当前链接可能仅本机/内网可访问。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qqip记录")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def query_consent_records(self, event: AstrMessageEvent, session_id: str = ""):
        """查询某个同意链接的访问记录。用法: /qqip记录 链接ID"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        session_id = (session_id or "").strip()
        if not session_id:
            yield event.plain_result("用法: /qqip记录 链接ID")
            return

        with self._consent_lock:
            session = self._consent_sessions.get(session_id)
            records = list(self._consent_records.get(session_id, []))

        if not session or str(session.get("group_id", "")) != group_id:
            yield event.plain_result("未找到该链接ID，或该链接不属于当前群。")
            return

        if not records:
            yield event.plain_result("该链接暂无同意访问记录。")
            return

        title = str(session.get("title", "群聊的聊天记录"))
        lines: list[str] = [title, "===================="]
        digest_src: list[str] = []

        for rec in records[-self._show_limit :]:
            visitor_id = str(rec.get("visitor_id", "unknown"))
            address = str(rec.get("address", "未知"))
            time_str = str(rec.get("time", ""))
            digest_src.append(f"{visitor_id}|{address}|{time_str}")

            lines.extend(
                [
                    "ID:",
                    visitor_id,
                    f"Address:  {address}",
                    f"Time:  {time_str}",
                    "====================",
                ]
            )

        lines.append(f"共{len(records)}个")
        lines.append("摘要:")
        lines.append(hashlib.md5("\n".join(digest_src).encode("utf-8")).hexdigest())
        yield event.plain_result("\n".join(lines))

    @filter.command("qqip清空", alias={"清空qqip"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def clear_records(self, event: AstrMessageEvent):
        """清空当前群的记录。"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        self._group_records[group_id] = {}
        self._group_activity[group_id] = {}
        self._group_sender_meta[group_id] = {}
        self._group_network_hints[group_id] = {}

        with self._consent_lock:
            to_delete = [sid for sid, s in self._consent_sessions.items() if str(s.get("group_id", "")) == group_id]
            for sid in to_delete:
                self._consent_sessions.pop(sid, None)
                self._consent_records.pop(sid, None)

        yield event.plain_result("已清空当前群的 qqip 记录与链接记录。")

    async def _resolve_ip_location(self, ip: str) -> str:
        if not ip:
            return "未知"

        if ip in self._ip_location_cache:
            return self._ip_location_cache[ip]

        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved:
                self._ip_location_cache[ip] = "局域网/保留地址"
                return self._ip_location_cache[ip]
        except ValueError:
            self._ip_location_cache[ip] = "非法 IP"
            return self._ip_location_cache[ip]

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"https://ipwho.is/{ip}?lang=zh")
                data = resp.json()

            if data.get("success") is False:
                location = "未知"
            else:
                country = str(data.get("country") or "")
                region = str(data.get("region") or "")
                city = str(data.get("city") or "")
                location = "".join([country, region, city]).strip() or "未知"
        except Exception as exc:
            logger.warning(f"查询 IP 归属地失败: {ip} {exc}")
            location = "未知"

        self._ip_location_cache[ip] = location
        return location

    def _extract_ips(self, message_obj: Any) -> list[str]:
        result: set[str] = set()
        raw_message = getattr(message_obj, "raw_message", None)
        roots = [raw_message, message_obj, getattr(message_obj, "sender", None)]

        candidates: list[Any] = []
        for root in roots:
            for key in (
                "ip",
                "client_ip",
                "remote_ip",
                "sender_ip",
                "source_ip",
                "peer_ip",
                "clientIp",
                "remoteIp",
                "senderIp",
                "sourceIp",
                "peerIp",
            ):
                val = self._deep_get(root, key)
                if val:
                    candidates.append(val)

        for c in candidates:
            for ip in self._find_ips_from_value(c):
                result.add(ip)

        if not result:
            # 兜底：遍历原始对象和消息对象，但跳过文本消息字段，减少误提取概率
            for root in roots:
                for ip in self._scan_ips_fallback(root):
                    result.add(ip)

        return sorted(result)

    def _scan_ips_fallback(self, obj: Any, depth: int = 0) -> set[str]:
        if depth > 6:
            return set()

        bad_keys = {"message", "message_str", "text", "content"}
        found: set[str] = set()

        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in bad_keys:
                    continue
                found.update(self._scan_ips_fallback(v, depth + 1))
            return found

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                found.update(self._scan_ips_fallback(item, depth + 1))
            return found

        if isinstance(obj, str):
            for ip in self._find_ips_from_value(obj):
                found.add(ip)
            return found

        if hasattr(obj, "__dict__"):
            return self._scan_ips_fallback(vars(obj), depth + 1)

        return found

    def _find_ips_from_value(self, value: Any) -> list[str]:
        text = str(value)
        possible = set(IPV4_RE.findall(text)) | set(IPV6_RE.findall(text))
        valid: list[str] = []
        for ip in possible:
            try:
                ipaddress.ip_address(ip)
                valid.append(ip)
            except ValueError:
                continue
        return valid

    def _deep_get(self, obj: Any, target_key: str, depth: int = 0) -> Any:
        if obj is None or depth > 6:
            return None

        target_key_norm = self._normalize_key(target_key)

        if isinstance(obj, dict):
            for k, v in obj.items():
                if self._normalize_key(k) == target_key_norm:
                    return v

            for v in obj.values():
                found = self._deep_get(v, target_key, depth + 1)
                if found is not None:
                    return found
            return None

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                found = self._deep_get(item, target_key, depth + 1)
                if found is not None:
                    return found
            return None

        if hasattr(obj, "__dict__"):
            return self._deep_get(vars(obj), target_key, depth + 1)

        return None

    def _normalize_key(self, key: Any) -> str:
        return str(key).replace("_", "").lower()

    def _extract_sender_meta(self, message_obj: Any) -> dict[str, str]:
        sender = getattr(message_obj, "sender", None)
        if sender is None:
            return {}

        keys = ("nickname", "card", "area", "sex", "age", "role", "title")
        meta: dict[str, str] = {}
        for key in keys:
            val = getattr(sender, key, None)
            if val is not None:
                text = str(val).strip()
                if text:
                    meta[key] = text

        if isinstance(sender, dict):
            for key in keys:
                val = sender.get(key)
                if val is not None:
                    text = str(val).strip()
                    if text:
                        meta[key] = text

        return meta

    def _collect_network_hints(self, roots: list[Any], limit: int = 20) -> list[str]:
        patterns = ("ip", "addr", "address", "host", "hostname")
        hints: list[str] = []

        def walk(obj: Any, path: str = "", depth: int = 0):
            nonlocal hints
            if len(hints) >= limit or depth > 5 or obj is None:
                return

            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = str(k)
                    next_path = f"{path}.{key}" if path else key
                    key_norm = self._normalize_key(key)
                    if any(p in key_norm for p in patterns):
                        value_text = str(v)
                        if len(value_text) > 120:
                            value_text = value_text[:120] + "..."
                        hints.append(f"{next_path}={value_text}")
                        if len(hints) >= limit:
                            return
                    walk(v, next_path, depth + 1)
                return

            if isinstance(obj, (list, tuple, set)):
                idx = 0
                for item in obj:
                    walk(item, f"{path}[{idx}]", depth + 1)
                    idx += 1
                    if len(hints) >= limit:
                        return
                return

            if hasattr(obj, "__dict__"):
                walk(vars(obj), path, depth + 1)

        for root in roots:
            walk(root)
            if len(hints) >= limit:
                break

        # 去重并保持顺序
        uniq: list[str] = []
        seen: set[str] = set()
        for h in hints:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        return uniq

    def _safe_int(self, value: str, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _cfg_get(self, key: str, default: Any) -> Any:
        try:
            if hasattr(self.config, "get"):
                value = self.config.get(key, default)
                return default if value is None else value
        except Exception:
            return default
        return default

    def _get_public_base_url(self) -> str:
        if self._public_base_url:
            return self._public_base_url.rstrip("/")
        host = self._listen_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self._listen_port}"

    def _start_consent_http_server(self):
        if self._http_server is not None:
            return

        plugin = self

        class ConsentHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                plugin._handle_consent_http_get(self)

            def log_message(self, format: str, *args: Any):
                return

        try:
            self._http_server = ThreadingHTTPServer((self._listen_host, self._listen_port), ConsentHandler)
            self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
            self._http_thread.start()
            logger.info(f"QQIP 同意记录 HTTP 服务已启动: {self._listen_host}:{self._listen_port}")
        except Exception as exc:
            self._http_server = None
            self._http_thread = None
            logger.warning(f"QQIP 同意记录 HTTP 服务启动失败: {exc}")

    def _stop_consent_http_server(self):
        if self._http_server is None:
            return
        try:
            self._http_server.shutdown()
            self._http_server.server_close()
        except Exception as exc:
            logger.warning(f"QQIP 同意记录 HTTP 服务关闭异常: {exc}")
        finally:
            self._http_server = None
            self._http_thread = None

    def _handle_consent_http_get(self, handler: BaseHTTPRequestHandler):
        path = urlparse(handler.path).path
        parts = [p for p in path.split("/") if p]

        if len(parts) == 3 and parts[0] == "qqip" and parts[1] == "consent":
            session_id = parts[2]
            self._render_consent_page(handler, session_id)
            return

        if len(parts) == 4 and parts[0] == "qqip" and parts[1] == "consent" and parts[3] == "ok":
            session_id = parts[2]
            self._record_consent_visit(handler, session_id)
            return

        self._send_html(handler, 404, "<h3>404 Not Found</h3>")

    def _render_consent_page(self, handler: BaseHTTPRequestHandler, session_id: str):
        with self._consent_lock:
            session = self._consent_sessions.get(session_id)

        if not session:
            self._send_html(handler, 404, "<h3>链接不存在或已失效。</h3>")
            return

        title = str(session.get("title", "群聊的聊天记录"))
        html = f"""
<html>
<head><meta charset=\"utf-8\"><title>{title}</title></head>
<body style=\"font-family: Arial, sans-serif; max-width: 680px; margin: 40px auto; line-height: 1.6;\">
  <h2>{title}</h2>
  <p>本页面用于群聊访问记录统计。</p>
  <p>继续表示你同意记录：访问时间、城市级归属地、匿名访客ID（不保存原始IP）。</p>
  <p><a href=\"/qqip/consent/{session_id}/ok\">我同意并继续访问</a></p>
</body>
</html>
"""
        self._send_html(handler, 200, html)

    def _record_consent_visit(self, handler: BaseHTTPRequestHandler, session_id: str):
        with self._consent_lock:
            session = self._consent_sessions.get(session_id)

        if not session:
            self._send_html(handler, 404, "<h3>链接不存在或已失效。</h3>")
            return

        ip = self._extract_request_ip(handler)
        ua = str(handler.headers.get("User-Agent", ""))
        address = self._resolve_ip_location_sync(ip) if ip else "未知"
        visitor_id = self._hash_visitor(ip, ua)
        now_str = datetime.now().strftime("%Y.%m.%d-%H:%M:%S")

        with self._consent_lock:
            records = self._consent_records.setdefault(session_id, [])
            records.append(
                {
                    "visitor_id": visitor_id,
                    "address": address,
                    "time": now_str,
                }
            )
            # 控制内存体积
            if len(records) > 1000:
                self._consent_records[session_id] = records[-1000:]

        html = """
<html>
<head><meta charset=\"utf-8\"><title>记录成功</title></head>
<body style=\"font-family: Arial, sans-serif; max-width: 680px; margin: 40px auto; line-height: 1.6;\">
  <h2>记录成功</h2>
  <p>已记录你的访问时间和归属地（匿名化处理）。</p>
  <p>你可以关闭本页面。</p>
</body>
</html>
"""
        self._send_html(handler, 200, html)

    def _send_html(self, handler: BaseHTTPRequestHandler, status: int, html: str):
        body = html.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _extract_request_ip(self, handler: BaseHTTPRequestHandler) -> str:
        candidates: list[str] = []

        xff = str(handler.headers.get("X-Forwarded-For", "")).strip()
        if xff:
            candidates.append(xff.split(",")[0].strip())

        for key in ("CF-Connecting-IP", "X-Real-IP", "X-Client-IP"):
            val = str(handler.headers.get(key, "")).strip()
            if val:
                candidates.append(val)

        if handler.client_address and handler.client_address[0]:
            candidates.append(str(handler.client_address[0]).strip())

        for cand in candidates:
            try:
                ipaddress.ip_address(cand)
                return cand
            except ValueError:
                continue

        return ""

    def _hash_visitor(self, ip: str, ua: str) -> str:
        source = f"{self._hash_salt}|{ip}|{ua[:80]}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]

    def _resolve_ip_location_sync(self, ip: str) -> str:
        if not ip:
            return "未知"

        if ip in self._ip_location_cache:
            return self._ip_location_cache[ip]

        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved:
                self._ip_location_cache[ip] = "局域网/保留地址"
                return self._ip_location_cache[ip]
        except ValueError:
            self._ip_location_cache[ip] = "非法 IP"
            return self._ip_location_cache[ip]

        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"https://ipwho.is/{ip}?lang=zh")
                data = resp.json()

            if data.get("success") is False:
                location = "未知"
            else:
                country = str(data.get("country") or "")
                region = str(data.get("region") or "")
                city = str(data.get("city") or "")
                location = "".join([country, region, city]).strip() or "未知"
        except Exception as exc:
            logger.warning(f"查询 IP 归属地失败(同步): {ip} {exc}")
            location = "未知"

        self._ip_location_cache[ip] = location
        return location

    async def terminate(self):
        self._stop_consent_http_server()
        logger.info("QQIP 插件已终止")

    def _get_sender_id(self, message_obj: Any) -> str:
        sender = getattr(message_obj, "sender", None)
        if sender is None:
            return ""

        for key in ("user_id", "id", "uin", "qq"):
            val = getattr(sender, key, None)
            if val:
                return str(val)

        if isinstance(sender, dict):
            for key in ("user_id", "id", "uin", "qq"):
                val = sender.get(key)
                if val:
                    return str(val)

        return ""
