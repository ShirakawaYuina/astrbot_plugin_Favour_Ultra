# web_api.py
import asyncio
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.umo_alias import build_umo_alias_map, normalize_umo_name, parse_umo

from .utils import is_valid_userid

PLUGIN_NAME = "astrbot_plugin_Favour_Ultra"
GLOBAL_SESSION_ID = "global"
SORT_FIELDS = ("favour", "user_id", "updated_at", "nickname")
MAX_PAGE_SIZE = 100


class FavourWebApi:
    """好感度插件的 WebUI 后端接口。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._nickname_cache: dict[str, str] = {}

    def _get_platform_bot(self):
        """尝试获取在线的 OneBot/aiocqhttp 平台实例。"""
        try:
            platform_mgr = getattr(self.plugin.context, "platform_manager", None)
            if not platform_mgr:
                return None
            insts = getattr(platform_mgr, "get_insts", lambda: [])()
            for inst in insts:
                bot = getattr(inst, "bot", None)
                if bot and hasattr(bot, "call_action"):
                    return bot
        except Exception as e:
            logger.debug(f"获取平台 bot 失败: {e}")
        return None

    def register(self) -> None:
        """向 Dashboard 注册插件页面使用的后端接口。"""
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/records",
            self.list_records,
            ["GET"],
            "获取好感度记录列表与概览统计",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/records/update",
            self.update_record,
            ["POST"],
            "修改好感度记录",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/records/delete",
            self.delete_record,
            ["POST"],
            "删除好感度记录",
        )

    async def list_records(self):
        """返回指定会话的记录列表、概览统计和可选会话列表。

        支持按群精准筛选（全局模式下筛选群成员、分群模式下筛选专属记录），并自动补全展示用户昵称。
        """
        session_id = str(request.query.get("session_id", GLOBAL_SESSION_ID) or "")
        session_id = session_id.strip() or GLOBAL_SESSION_ID
        keyword = str(request.query.get("keyword", "") or "").strip().lower()
        sort_by = str(request.query.get("sort_by", "favour") or "favour")
        if sort_by not in SORT_FIELDS:
            sort_by = "favour"
        descending = str(request.query.get("desc", "true") or "true").lower() != "false"
        page = max(1, int(request.query.get("page", 1, type=int) or 1))
        page_size = min(
            MAX_PAGE_SIZE,
            max(1, int(request.query.get("page_size", 20, type=int) or 20)),
        )

        bot = self._get_platform_bot()

        # 根据所选会话加载记录
        if session_id == GLOBAL_SESSION_ID:
            records = await self.plugin.db_manager.get_all_in_session(GLOBAL_SESSION_ID)
        else:
            raw_gid = session_id.split(":")[-1] if ":" in session_id else session_id
            if self.plugin.is_global_favour:
                # 全局好感度模式下，按群筛选：聚合属于该群的所有成员并展示其全局记录
                group_user_ids = set(
                    await self.plugin.db_manager.get_users_in_group(session_id)
                )
                if bot and raw_gid.isdigit():
                    try:
                        members = await bot.call_action(
                            "get_group_member_list", group_id=int(raw_gid)
                        )
                        if isinstance(members, list):
                            for m in members:
                                uid = str(m.get("user_id") or "")
                                if uid:
                                    group_user_ids.add(uid)
                                    nick = m.get("card") or m.get("nickname")
                                    if nick:
                                        self._nickname_cache[uid] = str(nick)
                    except Exception as e:
                        logger.debug(f"从 OneBot 获取群成员列表提示: {e}")

                records = await self.plugin.db_manager.get_records_by_users(
                    list(group_user_ids), session_id=GLOBAL_SESSION_ID
                )
            else:
                # 分群好感度模式下：直接获取该群专属记录
                records = await self.plugin.db_manager.get_records_for_group(
                    session_id, is_global=False
                )

        # 批量补充昵称解析
        missing_uids = [
            str(r.user_id)
            for r in records
            if not getattr(r, "nickname", None)
            and str(r.user_id) not in self._nickname_cache
        ]
        if missing_uids:
            try:
                db_names = await self.plugin.db_manager.get_user_nicknames_map(
                    missing_uids
                )
                self._nickname_cache.update(db_names)
            except Exception as e:
                logger.debug(f"从发言记录查询用户昵称提示: {e}")

        rows = []
        for record in records:
            uid = str(record.user_id)
            nick = (
                getattr(record, "nickname", "")
                or self._nickname_cache.get(uid, "")
            )
            if getattr(record, "nickname", ""):
                self._nickname_cache[uid] = record.nickname
            elif nick:
                # 异步写回数据库持久化缓存
                asyncio.create_task(
                    self.plugin.db_manager.update_user_nickname(uid, nick)
                )

            rows.append(
                {
                    "user_id": uid,
                    "nickname": str(nick or ""),
                    "favour": int(record.favour),
                    "relationship": str(record.relationship or ""),
                    "impression": str(record.impression or ""),
                    "aliases": record.get_aliases() if hasattr(record, "get_aliases") else [],
                    "aliases_locked": bool(getattr(record, "aliases_locked", False)),
                    "interact_count": int(record.interact_count or 0),
                    "dialogue_round_count": int(record.dialogue_round_count or 0),
                    "today_change": self.plugin._get_daily_favour_change(uid),
                    "created_at": (
                        record.created_at.strftime("%Y-%m-%d %H:%M")
                        if hasattr(record.created_at, "strftime")
                        else str(record.created_at or "")[:16]
                    )
                    if record.created_at
                    else "",
                    "updated_at": (
                        record.updated_at.strftime("%Y-%m-%d %H:%M")
                        if hasattr(record.updated_at, "strftime")
                        else str(record.updated_at or "")[:16]
                    )
                    if record.updated_at
                    else "",
                }
            )

        # 概览统计基于当前筛选视图的全部匹配项
        favour_values = [row["favour"] for row in rows]
        today_changes = [row["today_change"] for row in rows]
        overview = {
            "total": len(rows),
            "average_favour": round(sum(favour_values) / len(favour_values), 1)
            if favour_values
            else 0,
            "max_favour": max(favour_values) if favour_values else 0,
            "min_favour": min(favour_values) if favour_values else 0,
            "today_net_change": sum(today_changes),
        }

        # 关键词搜索匹配（支持 ID、昵称、印象）
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in row["user_id"].lower()
                or keyword in row.get("nickname", "").lower()
                or keyword in row["impression"].lower()
                or any(keyword in a.lower() for a in row.get("aliases", []))
            ]

        # 排序
        sort_key = int if sort_by == "favour" else str
        rows.sort(key=lambda row: sort_key(row.get(sort_by, "")), reverse=descending)

        total = len(rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start_index = (page - 1) * page_size
        page_rows = rows[start_index : start_index + page_size]

        # 对当前展示分页仍缺少昵称的用户，尝试调用陌生人资料接口做最终解析
        if bot:
            for item in page_rows:
                uid = item["user_id"]
                if not item["nickname"] and uid.isdigit():
                    try:
                        stranger = await bot.call_action(
                            "get_stranger_info", user_id=int(uid)
                        )
                        if isinstance(stranger, dict):
                            s_name = stranger.get("nickname") or stranger.get("nick")
                            if s_name:
                                item["nickname"] = str(s_name)
                                self._nickname_cache[uid] = str(s_name)
                                asyncio.create_task(
                                    self.plugin.db_manager.update_user_nickname(
                                        uid, str(s_name)
                                    )
                                )
                    except Exception:
                        pass

        return json_response(
            {
                "session_id": session_id,
                "sessions": await self._build_session_options(),
                "overview": overview,
                "rows": page_rows,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }
        )

    async def _build_session_options(self) -> list[dict[str, str]]:
        """为会话下拉构建 [{session_id, name}]，汇聚平台在线群、已知别名和发言记录，全局会话固定排在最前。

        排除所有私聊与个人会话；对于无好感记录且无近期发言的死群/封禁群，根据配置进行过滤。
        """
        sessions: list[dict[str, str]] = [
            {"session_id": GLOBAL_SESSION_ID, "name": "全局"}
        ]
        seen_gids: set[str] = set()

        filter_empty = getattr(self.plugin, "webui_filter_empty_groups", True)

        # 1. 收集系统中确有发言或好感记录的群 ID
        active_gids: set[str] = set()
        try:
            utterance_gids = (
                await self.plugin.db_manager.get_all_utterance_group_ids()
            )
            for gid in utterance_gids:
                raw_gid = gid.split(":")[-1] if ":" in gid else gid
                if raw_gid:
                    active_gids.add(raw_gid)
                    active_gids.add(gid)
        except Exception as e:
            logger.debug(f"从日常发言获取群集合提示: {e}")

        try:
            db_sessions = await self.plugin.db_manager.get_all_session_ids()
            for sid in db_sessions:
                if sid == GLOBAL_SESSION_ID:
                    continue
                sid_lower = sid.lower()
                # 严格过滤私聊与非群聊会话
                if (
                    "friend" in sid_lower
                    or "private" in sid_lower
                    or "webchat" in sid_lower
                    or "direct" in sid_lower
                ):
                    continue
                if "group" in sid_lower:
                    raw_id = sid.split(":")[-1] if ":" in sid else sid
                    if raw_id:
                        active_gids.add(raw_id)
                        active_gids.add(sid)
        except Exception as e:
            logger.debug(f"从 favour_records 获取群会话提示: {e}")

        # 2. 从 AstrCore umo_aliases 收集已知群聊与别名映射
        alias_groups: dict[str, str] = {}
        try:
            core_aliases = await self.plugin.context.get_db().get_umo_aliases()
            for alias in core_aliases:
                umo = str(alias.umo or "")
                if "group" in umo.lower():
                    raw_gid = umo.split(":")[-1] if ":" in umo else umo
                    if raw_gid:
                        name = (
                            normalize_umo_name(getattr(alias, "user_alias", None))
                            or normalize_umo_name(getattr(alias, "auto_name", None))
                            or raw_gid
                        )
                        alias_groups[raw_gid] = name
        except Exception as e:
            logger.debug(f"从 umo_aliases 收集群聊提示: {e}")

        # 3. 尝试从在线 OneBot 平台获取群列表 (最全面、最新)
        bot = self._get_platform_bot()
        if bot:
            try:
                group_list = await bot.call_action("get_group_list")
                if isinstance(group_list, list):
                    for g in group_list:
                        gid = str(g.get("group_id") or "").strip()
                        if not gid:
                            continue

                        # 会话黑白名单检查
                        if hasattr(self.plugin, "_is_favour_session_enabled"):
                            if not self.plugin._is_favour_session_enabled(gid):
                                continue

                        # 排除死群/无数据群（无好感记录、无发言、无主动别名）
                        if filter_empty:
                            if (
                                gid not in active_gids
                                and gid not in alias_groups
                            ):
                                continue

                        gname = str(g.get("group_name") or "").strip()
                        if not gname and gid in alias_groups:
                            gname = alias_groups[gid]
                        display_name = (
                            f"{gname} ({gid})" if gname else f"群聊 ({gid})"
                        )
                        sessions.append({"session_id": gid, "name": display_name})
                        seen_gids.add(gid)
            except Exception as e:
                logger.debug(f"获取平台群列表提示: {e}")

        # 4. 从 alias_groups 补充已知群聊
        for raw_gid, name in alias_groups.items():
            if raw_gid not in seen_gids:
                if hasattr(self.plugin, "_is_favour_session_enabled"):
                    if not self.plugin._is_favour_session_enabled(raw_gid):
                        continue
                if filter_empty and raw_gid not in active_gids:
                    continue
                display_name = (
                    f"{name} ({raw_gid})" if name != raw_gid else f"群聊 ({raw_gid})"
                )
                sessions.append({"session_id": raw_gid, "name": display_name})
                seen_gids.add(raw_gid)

        # 5. 从 active_gids 补充尚未加入且合法的群聊
        for gid in active_gids:
            raw_gid = gid.split(":")[-1] if ":" in gid else gid
            if raw_gid not in seen_gids and gid not in seen_gids:
                if hasattr(self.plugin, "_is_favour_session_enabled"):
                    if not self.plugin._is_favour_session_enabled(
                        raw_gid
                    ) and not self.plugin._is_favour_session_enabled(gid):
                        continue
                name = alias_groups.get(raw_gid, "")
                display_name = (
                    f"{name} ({raw_gid})"
                    if name and name != raw_gid and not name.startswith("群聊")
                    else f"群聊 ({raw_gid})"
                )
                sessions.append({"session_id": raw_gid, "name": display_name})
                seen_gids.add(raw_gid)
                seen_gids.add(gid)

        # 排序：全局第一，其他群按名称排序
        sessions.sort(
            key=lambda item: (item["session_id"] != GLOBAL_SESSION_ID, item["name"])
        )
        return sessions

    async def update_record(self):
        """修改单条或跨全部会话的好感度记录。

        Returns:
            成功时返回受影响的记录条数，失败时返回 400 错误响应。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        user_id = str(payload.get("user_id", "") or "").strip()
        if not is_valid_userid(user_id):
            return error_response("user_id 格式不合法", status_code=400)

        session_id = str(
            payload.get("session_id", GLOBAL_SESSION_ID) or GLOBAL_SESSION_ID
        ).strip()
        scope = str(payload.get("scope", "session") or "session").strip()
        if scope not in ("session", "all"):
            return error_response("scope 只能是 session 或 all", status_code=400)

        favour = payload.get("favour")
        if favour is not None:
            try:
                favour = int(favour)
            except (TypeError, ValueError):
                return error_response("favour 必须是整数", status_code=400)

        relationship = payload.get("relationship")
        if relationship is not None and not isinstance(relationship, str):
            return error_response("relationship 必须是字符串", status_code=400)

        impression = payload.get("impression")
        if impression is not None and not isinstance(impression, str):
            return error_response("impression 必须是字符串", status_code=400)

        aliases = payload.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list):
                return error_response("aliases 必须是列表", status_code=400)
            cleaned_aliases = []
            for a in aliases:
                s = str(a).strip()
                if s and len(s) <= 10 and s not in cleaned_aliases:
                    cleaned_aliases.append(s)
            aliases = cleaned_aliases[:5]

        aliases_locked = payload.get("aliases_locked")
        if aliases_locked is not None:
            aliases_locked = bool(aliases_locked)

        if (
            favour is None
            and relationship is None
            and impression is None
            and aliases is None
            and aliases_locked is None
        ):
            return error_response("没有需要修改的字段", status_code=400)

        if scope == "all":
            # 全局模式只支持好感度，关系和印象按会话独立维护。
            if favour is None:
                return error_response("跨会话修改目前仅支持好感度字段", status_code=400)
            affected = await self.plugin.db_manager.update_user_all_records(
                user_id, favour=favour
            )
        else:
            updated = await self.plugin.db_manager.update_favour(
                user_id,
                session_id,
                favour=favour,
                relationship=relationship,
                impression=impression,
                aliases=aliases,
                aliases_locked=aliases_locked,
            )
            affected = 1 if updated else 0

        if not affected:
            logger.warning(
                f"WebUI 修改好感度未生效: user_id={user_id} session_id={session_id} scope={scope}"
            )
            return error_response("记录不存在或数据库写入失败", status_code=400)

        return json_response({"ok": True, "affected": affected})

    async def delete_record(self):
        """删除单条好感度记录。

        Returns:
            成功时返回 ok，失败时返回 400 错误响应。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        user_id = str(payload.get("user_id", "") or "").strip()
        if not is_valid_userid(user_id):
            return error_response("user_id 格式不合法", status_code=400)

        session_id = str(
            payload.get("session_id", GLOBAL_SESSION_ID) or GLOBAL_SESSION_ID
        ).strip()
        deleted, message = await self.plugin.db_manager.delete_favour(
            user_id, session_id
        )
        if not deleted:
            return error_response(message, status_code=400)
        return json_response({"ok": True})
