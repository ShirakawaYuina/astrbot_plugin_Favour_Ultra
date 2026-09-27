# web_api.py
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.umo_alias import build_umo_alias_map, normalize_umo_name, parse_umo

from .utils import is_valid_userid

PLUGIN_NAME = "astrbot_plugin_Favour_Ultra"
GLOBAL_SESSION_ID = "global"
SORT_FIELDS = ("favour", "user_id", "updated_at")
MAX_PAGE_SIZE = 100


class FavourWebApi:
    """好感度插件的 WebUI 后端接口。

    WebUI 运行时拿不到 AstrMessageEvent，因此无法调用 OneBot 拉取群名片，
    列表统一以 user_id 为主键展示，昵称由前端头像服务补齐。
    """

    def __init__(self, plugin) -> None:
        self.plugin = plugin

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

        会话展示名优先取 AstrCore 维护的 umo_aliases（/name 手动别名 > 平台自动群名），
        都没有时退化为 UMO 里的会话号，因此 WebUI 无需持有 event 或 bot 也能显示群名。

        Args:
            无。参数全部来自 query：session_id、keyword、sort_by、desc、page、page_size。

        Returns:
            包含 sessions、overview、rows、total、page、page_size 的 JSON 响应。
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

        records = await self.plugin.db_manager.get_all_in_session(session_id)
        rows = [
            {
                "user_id": str(record.user_id),
                "favour": int(record.favour),
                "relationship": str(record.relationship or ""),
                "impression": str(record.impression or ""),
                "interact_count": int(record.interact_count or 0),
                "dialogue_round_count": int(record.dialogue_round_count or 0),
                "today_change": self.plugin._get_daily_favour_change(
                    str(record.user_id)
                ),
                "created_at": record.created_at.strftime("%Y-%m-%d %H:%M")
                if record.created_at
                else "",
                "updated_at": record.updated_at.strftime("%Y-%m-%d %H:%M")
                if record.updated_at
                else "",
            }
            for record in records
        ]

        # 概览基于整个会话计算，不受搜索、排序和分页影响。
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

        if keyword:
            rows = [
                row
                for row in rows
                if keyword in row["user_id"].lower()
                or keyword in row["impression"].lower()
            ]

        # favour 是整数，user_id 和 updated_at 是字符串，排序键类型需要分别处理。
        sort_key = int if sort_by == "favour" else str
        rows.sort(key=lambda row: sort_key(row[sort_by]), reverse=descending)

        total = len(rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start_index = (page - 1) * page_size

        return json_response(
            {
                "session_id": session_id,
                "sessions": await self._build_session_options(),
                "overview": overview,
                "rows": rows[start_index : start_index + page_size],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }
        )

    async def _build_session_options(self) -> list[dict[str, str]]:
        """为会话下拉构建 [{session_id, name}]，全局会话固定排在最前。"""
        session_ids = await self.plugin.db_manager.get_all_session_ids()
        alias_map = build_umo_alias_map(
            await self.plugin.context.get_db().get_umo_aliases(session_ids)
        )
        sessions = [
            {
                "session_id": session_id,
                "name": (
                    normalize_umo_name(
                        getattr(alias_map.get(session_id), "user_alias", "")
                    )
                    or normalize_umo_name(
                        getattr(alias_map.get(session_id), "auto_name", "")
                    )
                    or parse_umo(session_id)["session_id"]
                ),
            }
            for session_id in session_ids
        ]
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

        if favour is None and relationship is None and impression is None:
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
