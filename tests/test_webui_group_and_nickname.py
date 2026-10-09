import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
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
