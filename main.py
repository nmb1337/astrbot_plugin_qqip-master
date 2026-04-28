import ipaddress
import re
from datetime import datetime
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")


@register("astrbot_plugin_qqip", "qqip", "群聊 IP 归属地记录与查询", "1.0.0")
class QQIPPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._group_records: dict[str, dict[str, dict[str, Any]]] = {}
        self._group_activity: dict[str, dict[str, datetime]] = {}
        self._group_sender_meta: dict[str, dict[str, dict[str, str]]] = {}
        self._group_network_hints: dict[str, dict[str, list[str]]] = {}
        self._ip_location_cache: dict[str, str] = {}
        self._max_records_per_group = 100
        self._show_limit = 10

    async def initialize(self):
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
        yield event.plain_result("已清空当前群的 qqip 记录。")

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

    async def terminate(self):
        logger.info("QQIP 插件已终止")
