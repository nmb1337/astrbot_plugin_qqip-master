import hashlib
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

        ip_list = self._extract_ips(message_obj)
        if not ip_list:
            return

        sender_id = str(self._get_sender_id(message_obj) or "unknown")
        sender_name = str(event.get_sender_name() or sender_id)

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
    @filter.command("谁在窥屏", alias={"窥屏", "qqip", "谁在看屏"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def who_is_peeping(self, event: AstrMessageEvent):
        """显示当前群已记录到的 IP 和归属地（仅展示本插件可见数据）。"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        records_map = self._group_records.get(group_id, {})
        if not records_map:
            yield event.plain_result("暂无可用数据。先让群里有人发言后再试。")
            return

        records = sorted(
            records_map.values(),
            key=lambda item: item.get("timestamp", datetime.min),
            reverse=True,
        )[: self._show_limit]

        lines: list[str] = ["谁在窥屏", "===================="]
        digest_source: list[str] = []

        for rec in records:
            ip = str(rec.get("ip", ""))
            ts = rec.get("timestamp")
            if not isinstance(ts, datetime):
                ts = datetime.now()
            addr = await self._resolve_ip_location(ip)
            digest_source.append(f"{ip}|{addr}|{ts.isoformat()}")

            lines.extend(
                [
                    "IP:",
                    ip,
                    f"Address:  {addr}",
                    f"Time:  {ts.strftime('%Y.%m.%d-%H:%M:%S')}",
                    "====================",
                ]
            )

        digest = hashlib.md5("\n".join(digest_source).encode("utf-8")).hexdigest()
        lines.append(f"共{len(records)}个")
        lines.append("摘要:")
        lines.append(digest)

        yield event.plain_result("\n".join(lines))

    @filter.command("窥屏清空", alias={"qqip清空", "清空窥屏"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def clear_records(self, event: AstrMessageEvent):
        """清空当前群的记录。"""
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        if not group_id:
            yield event.plain_result("该指令仅支持群聊。")
            return

        self._group_records[group_id] = {}
        yield event.plain_result("已清空当前群的窥屏记录。")

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

        candidates: list[Any] = []
        for key in ("ip", "client_ip", "remote_ip", "sender_ip", "source_ip", "peer_ip"):
            val = self._deep_get(raw_message, key)
            if val:
                candidates.append(val)

        for c in candidates:
            for ip in self._find_ips_from_value(c):
                result.add(ip)

        if not result:
            # 兜底：遍历原始对象，但跳过文本消息字段，减少误提取概率
            for ip in self._scan_ips_fallback(raw_message):
                result.add(ip)

        return sorted(result)

    def _scan_ips_fallback(self, obj: Any, depth: int = 0) -> set[str]:
        if depth > 6:
            return set()

        bad_keys = {"message", "message_str", "text", "content", "raw_message"}
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

        if isinstance(obj, dict):
            if target_key in obj:
                return obj[target_key]
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
