import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlmodel import select
from astrbot_plugin_Favour_Ultra.storage import FavourDBManager, FavourRecord
from astrbot_plugin_Favour_Ultra.web_api import FavourWebApi, GLOBAL_SESSION_ID


@pytest.mark.asyncio
async def test_db_nickname_and_group_helpers(tmp_path: Path):
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # 1. 验证 update_favour 能保存 nickname
    await db.update_favour(
        user_id="1001",
        session_id="global",
        favour=50,
        nickname="小明",
    )
    rec = await db.get_favour("1001", "global")
    assert rec is not None
    assert rec.nickname == "小明"

    # 2. 插入群发言记录
    await db.record_daily_group_utterance(
        group_id="9999",
        user_id="1001",
        user_name="小明群名片",
        content="发言1",
        date="2026-10-09",
    )
    await db.record_daily_group_utterance(
        group_id="9999",
        user_id="1002",
        user_name="小红",
        content="发言2",
        date="2026-10-09",
    )
    await db.record_daily_group_utterance(
        group_id="8888",
        user_id="1003",
        user_name="小刚",
        content="发言3",
        date="2026-10-09",
    )

    # 3. 验证 get_all_utterance_group_ids
    gids = await db.get_all_utterance_group_ids()
    assert "9999" in gids
    assert "8888" in gids

    # 4. 验证 get_users_in_group
    uids_9999 = await db.get_users_in_group("9999")
    assert set(uids_9999) == {"1001", "1002"}

    # 5. 验证 get_user_nicknames_map
    nicks = await db.get_user_nicknames_map(["1001", "1002", "1003"])
    assert nicks["1001"] == "小明群名片"
    assert nicks["1002"] == "小红"
    assert nicks["1003"] == "小刚"

    # 6. 验证 update_user_nickname
    await db.update_user_nickname("1001", "大明")
    rec_updated = await db.get_favour("1001", "global")
    assert rec_updated.nickname == "大明"

    await db.close()


@pytest.mark.asyncio
async def test_web_api_group_filter_global_mode(tmp_path: Path):
    """测试全局好感度模式下，按群筛选能正确提取该群成员的全局好感度数据"""
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # 建立全局好感度记录
    await db.update_favour("1001", "global", favour=80, nickname="用户一", impression="高手")
    await db.update_favour("1002", "global", favour=30, nickname="用户二", impression="新手")
    await db.update_favour("1003", "global", favour=10, nickname="用户三", impression="潜水")

    # 1001 和 1002 在群 9999 活跃发言
    await db.record_daily_group_utterance("9999", "1001", "用户一", "打卡", "2026-10-09")
    await db.record_daily_group_utterance("9999", "1002", "用户二", "冒泡", "2026-10-09")

    mock_plugin = MagicMock()
    mock_plugin.db_manager = db
    mock_plugin.is_global_favour = True  # 全局好感模式
    mock_plugin._get_daily_favour_change.return_value = 0

    api = FavourWebApi(mock_plugin)
    api._get_platform_bot = lambda: None

    # 构建假 request
    class FakeQuery(dict):
        def get(self, key, default=None, type=None):
            val = super().get(key, default)
            if type and val is not None:
                try:
                    return type(val)
                except Exception:
                    return default
            return val

    from astrbot.api.web import _request_var

    class FakeReq:
        def __init__(self, query_dict):
            self.query = FakeQuery(query_dict)

    # 1. 查询全局：应包含全部 3 人
    token = _request_var.set(FakeReq({"session_id": "global", "page": 1, "page_size": 20}))
    try:
        resp_global = await api.list_records()
        body_global = json.loads(resp_global.body)
        assert len(body_global["rows"]) == 3

        # 2. 查询群 9999：在全局模式下，应只筛选出群 9999 包含的 1001 和 1002
        _request_var.set(FakeReq({"session_id": "9999", "page": 1, "page_size": 20}))
        resp_grp = await api.list_records()
        body_grp = json.loads(resp_grp.body)

        grp_uids = [r["user_id"] for r in body_grp["rows"]]
        assert "1001" in grp_uids
        assert "1002" in grp_uids
        assert "1003" not in grp_uids
        assert body_grp["rows"][0]["nickname"] in ("用户一", "用户二")

        # 3. 测试通过昵称搜索
        _request_var.set(FakeReq({"session_id": "global", "keyword": "用户二", "page": 1, "page_size": 20}))
        resp_search = await api.list_records()
        body_search = json.loads(resp_search.body)

        assert len(body_search["rows"]) == 1
        assert body_search["rows"][0]["user_id"] == "1002"
    finally:
        _request_var.reset(token)

    await db.close()


@pytest.mark.asyncio
async def test_build_session_options_aggregation(tmp_path: Path):
    """测试会话选项聚合：私聊彻底排除、死群过滤、黑名单联动、活跃群保留"""
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # 1. 活跃群 7777 有发言记录
    await db.record_daily_group_utterance("7777", "u1", "用户", "嗨", "2026-10-09")
    # 活跃群 1072408967 也有发言记录
    await db.record_daily_group_utterance("1072408967", "u2", "用户2", "你好", "2026-10-09")
    # 插入私聊记录（应当被彻底排除）
    await db.update_favour("u3", "astrbot:FriendMessage:1158662154", favour=20, nickname="私聊好友")
    await db.update_favour("u4", "webchat:FriendMessage:webchat!abc", favour=10, nickname="网页私聊")

    # 在线列表包含：活跃群 1072408967、死群 1085132297、黑名单群 6666
    mock_bot = MagicMock()
    mock_bot.call_action = AsyncMock(return_value=[
        {"group_id": 1072408967, "group_name": "新-绒布球避难所"},
        {"group_id": 1085132297, "group_name": "死群-已被封禁"},
        {"group_id": 6666, "group_name": "黑名单群"},
    ])

    mock_plugin = MagicMock()
    mock_plugin.db_manager = db
    mock_plugin.webui_filter_empty_groups = True
    mock_plugin.context.get_db().get_umo_aliases = AsyncMock(return_value=[])
    # 模拟黑名单：屏蔽 6666
    mock_plugin._is_favour_session_enabled = lambda sid: str(sid).split(":")[-1] != "6666"

    api = FavourWebApi(mock_plugin)
    api._get_platform_bot = lambda: mock_bot

    sessions = await api._build_session_options()
    s_ids = [s["session_id"] for s in sessions]

    # 全局固定排在最前面
    assert sessions[0]["session_id"] == GLOBAL_SESSION_ID
    # 活跃群保留
    assert "1072408967" in s_ids
    opt_1072 = next(s for s in sessions if s["session_id"] == "1072408967")
    assert "新-绒布球避难所 (1072408967)" == opt_1072["name"]
    assert "7777" in s_ids

    # 死群 1085132297 无任何记录且无发言，已被自动过滤排除
    assert "1085132297" not in s_ids

    # 黑名单群 6666 已被黑名单机制排除
    assert "6666" not in s_ids

    # 私聊会话必须彻底排除，绝不出现在下拉选项中
    assert "astrbot:FriendMessage:1158662154" not in s_ids
    assert "1158662154" not in s_ids
    assert "webchat:FriendMessage:webchat!abc" not in s_ids

    # 若关闭死群过滤开关，则未被黑名单封禁的无记录群也会显示
    mock_plugin.webui_filter_empty_groups = False
    sessions_all = await api._build_session_options()
    s_ids_all = [s["session_id"] for s in sessions_all]
    assert "1085132297" in s_ids_all
    assert "6666" not in s_ids_all  # 黑名单依然生效
    assert "1158662154" not in s_ids_all  # 私聊依然彻底排除

    await db.close()


@pytest.mark.asyncio
async def test_record_updated_at_local_timezone_and_sorting(tmp_path: Path):
    """测试更新时间使用本地时间存储并在 WebUI 列表中正常格式化与降序排序。"""
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # 1. 模拟写入两条记录，更新时间均为本地时间
    await db.update_favour("u_old", "global", favour=10, impression="旧印象")
    # 手动让旧记录早 1 小时
    async with db.async_session() as session:
        stmt = select(FavourRecord).where(FavourRecord.user_id == "u_old")
        rec = (await session.execute(stmt)).scalars().first()
        rec.updated_at = datetime.now() - timedelta(hours=1)
        session.add(rec)
        await session.commit()

    # 2. 模拟刚刚更新的新印象
    await db.update_favour("u_new", "global", favour=20, impression="最新印象")

    mock_plugin = MagicMock()
    mock_plugin.db_manager = db
    mock_plugin.is_global_favour = True
    mock_plugin._get_daily_favour_change.return_value = 0

    api = FavourWebApi(mock_plugin)
    api._get_platform_bot = lambda: None

    from astrbot.api.web import _request_var

    class FakeQuery(dict):
        def get(self, key, default=None, type=None):
            val = super().get(key, default)
            if type and val is not None:
                try:
                    return type(val)
                except Exception:
                    return default
            return val

    class FakeReq:
        def __init__(self, query_dict):
            self.query = FakeQuery(query_dict)

    # 按更新时间降序查询
    _request_var.set(
        FakeReq({"session_id": "global", "sort_by": "updated_at", "sort_desc": True, "page": 1, "page_size": 10})
    )
    resp = await api.list_records()
    body = json.loads(resp.body)

    rows = body["rows"]
    assert len(rows) == 2
    # 最新更新的记录应排在第 1 位
    assert rows[0]["user_id"] == "u_new"
    assert rows[0]["impression"] == "最新印象"
    assert rows[1]["user_id"] == "u_old"

    # 时间格式为 YYYY-MM-DD HH:MM
    now_prefix = datetime.now().strftime("%Y-%m-%d")
    assert rows[0]["updated_at"].startswith(now_prefix)

    await db.close()



@pytest.mark.asyncio
async def test_webui_modify_aliases_and_lock_in_group_view(tmp_path: Path):
    from astrbot.api.web import _request_var
    """测试在群组筛选视图下，修改外号与锁定外号能直接且正确生效于全局记录中。"""
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # 1. 初始建立用户的全局记录，未锁定且外号为空
    await db.update_favour("2088126650", "global", favour=10, nickname="黑尘酱", aliases=[], aliases_locked=False)

    # 2. 该用户在群 1072408967 发言
    await db.record_daily_group_utterance("1072408967", "2088126650", "黑尘酱", "在群里说话", "2026-10-25")

    mock_plugin = MagicMock()
    mock_plugin.db_manager = db
    mock_plugin.is_global_favour = True  # 全局好感模式
    mock_plugin._get_daily_favour_change.return_value = 0

    api = FavourWebApi(mock_plugin)
    api._get_platform_bot = lambda: None

    class FakeQuery(dict):
        def get(self, key, default=None, type=None):
            val = super().get(key, default)
            if type and val is not None:
                try:
                    return type(val)
                except Exception:
                    return default
            return val

    class FakeReq:
        def __init__(self, query_dict=None, json_data=None):
            self.query = FakeQuery(query_dict or {})
            self._json_data = json_data or {}

        async def json(self, default=None):
            return self._json_data

    # 3. 模拟在群组视图查询列表：必须返回正确的 session_id (global)
    token = _request_var.set(FakeReq(query_dict={"session_id": "1072408967", "page": 1, "page_size": 20}))
    try:
        resp = await api.list_records()
        body = json.loads(resp.body)
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["user_id"] == "2088126650"
        assert row["session_id"] == "global"
        assert row["aliases_locked"] is False

        # 4. 模拟前端在群筛选下提交修改外号与锁定外号（传的可能是群号 1072408967）
        _request_var.set(FakeReq(json_data={
            "user_id": "2088126650",
            "session_id": "1072408967",
            "aliases": ["笨鱼", "鱼", "鱼鱼"],
            "aliases_locked": True,
        }))
        update_resp = await api.update_record()
        up_body = json.loads(update_resp.body)
        assert up_body["ok"] is True
        assert up_body["affected"] == 1

        # 5. 校验数据库：必须直接更新 global 记录，且不生成孤儿记录 1072408967
        rec_global = await db.get_favour("2088126650", "global")
        assert rec_global is not None
        assert rec_global.get_aliases() == ["笨鱼", "鱼", "鱼鱼"]
        assert rec_global.aliases_locked is True

        orphan = await db.get_favour("2088126650", "1072408967")
        # 兼容模糊匹配：如果查询 1072408967，应当查不到孤儿非全局记录（或者被重定向到全局）
        # 检查表中 session_id != 'global' 的记录数量
        non_global_recs = await db.get_all_in_session("1072408967")
        assert len(non_global_recs) == 0

        # 6. 再次在群视图查询：必须显示锁已生效且外号列表完整
        _request_var.set(FakeReq(query_dict={"session_id": "1072408967", "page": 1, "page_size": 20}))
        resp2 = await api.list_records()
        body2 = json.loads(resp2.body)
        row2 = body2["rows"][0]
        assert row2["aliases"] == ["笨鱼", "鱼", "鱼鱼"]
        assert row2["aliases_locked"] is True
    finally:
        _request_var.reset(token)

    await db.close()


@pytest.mark.asyncio
async def test_cleanup_orphan_group_records_in_global_mode(tmp_path: Path):
    """测试自动清理历史误创建的孤儿群好感度记录并回填外号和锁定状态。"""
    db = FavourDBManager(data_dir=tmp_path)
    await db.init_db()

    # global 记录无外号未锁定
    await db.update_favour("u_orphan", "global", favour=5, aliases=[], aliases_locked=False)
    # 模拟历史产生的孤儿记录 335688112，有外号且已锁定
    await db.update_favour("u_orphan", "335688112", favour=0, aliases=["猫猫", "阿强"], aliases_locked=True)

    # 执行清理
    await db.cleanup_orphan_group_records_in_global_mode()

    # 验证孤儿记录已被清理
    async with db.async_session() as session:
        from sqlmodel import select
        stmt = select(FavourRecord).where(FavourRecord.session_id == "335688112")
        orphan_records = (await session.execute(stmt)).scalars().all()
        assert len(orphan_records) == 0

    # 验证全局记录已继承外号和锁定状态
    rec = await db.get_favour("u_orphan", "global")
    assert rec.get_aliases() == ["猫猫", "阿强"]
    assert rec.aliases_locked is True

    await db.close()
