import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from astrbot.core.message.components import At, Plain
from astrbot_plugin_Favour_Ultra.main import FavourManagerTool
from astrbot_plugin_Favour_Ultra.storage import FavourDBManager, FavourRecord


class _FakeMessageObj:
    def __init__(self, message_id: int = 1, self_id: str = "999999", message: list | None = None):
        self.message_id = message_id
        self.self_id = self_id
        self.message = message or []


class _DialogueEvent:
    def __init__(
        self,
        user_id: str,
        session_id: str,
        message: str = "",
        message_obj: _FakeMessageObj | None = None,
        sender_name: str = "",
    ):
        self._sender_id = user_id
        self._session_id = session_id
        self.message_str = message
        self.message_obj = message_obj or _FakeMessageObj()
        self._sender_name = sender_name or f"User_{user_id}"
        self._extras = {}
        self._result = SimpleNamespace(chain=[])

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def set_extra(self, key: str, value):
        self._extras[key] = value

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def get_result(self):
        return self._result


def _build_test_plugin() -> FavourManagerTool:
    plugin = FavourManagerTool.__new__(FavourManagerTool)
    plugin.is_global_favour = False
    plugin.min_favour_value = -100
    plugin.max_favour_value = 100
    plugin.max_daily_increase = 20
    plugin.admins_id = []
    plugin.enable_daily_impression = True
    plugin.favour_increase_min = 1
    plugin.favour_increase_max = 3
    plugin.favour_decrease_min = 1
    plugin.favour_decrease_max = 5
    plugin.favour_rule_prompt = ""
    plugin.enable_relationship_table = False
    plugin.favour_mode = "galgame"
    plugin.default_favour = 0
    plugin.admin_default_favour = 50
    plugin.favour_envoys = []
    plugin.pending_updates = {}
    plugin.pending_dialogue_round_updates = {}
    plugin.user_dialogue_rounds = {}
    plugin.impression_refresh_locks = set()
    plugin.impression_natural_rounds = 3
    plugin.favour_pattern = re.compile(
        r"[\[［][^\[\]［］]*?(?:好.*?感|好.*?度|感.*?度)[^\[\]［］]*?[\]］]",
        re.DOTALL | re.IGNORECASE,
    )
    plugin.spam_pattern = re.compile(
        r"[\[［][^\[\]［］]*?(?:刷分|防刷)[^\[\]［］]*?[\]］]",
        re.DOTALL | re.IGNORECASE,
    )
    plugin.relationship_pattern = re.compile(r"$^")
    plugin._is_favour_session_enabled = lambda _sid: True
    plugin._get_session_id = lambda event: getattr(event, "_session_id", "group-1")
    plugin._get_daily_favour_change = lambda _uid: 0
    plugin._check_permission = AsyncMock(return_value=False)
    plugin._get_user_display_name = AsyncMock(side_effect=lambda evt, uid: f"Display_{uid}")
    return plugin


@pytest.mark.asyncio
async def test_db_aliases_crud_and_migration(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    # 1. 验证创建并更新 aliases 和 aliases_locked
    assert await db.update_favour(
        user_id="1001",
        session_id="group-1",
        favour=10,
        nickname="尤约拉猫",
        aliases=["牢尤", "猫猫"],
        aliases_locked=True,
    )

    rec = await db.get_favour("1001", "group-1")
    assert rec is not None
    assert rec.aliases_locked is True
    assert rec.get_aliases() == ["牢尤", "猫猫"]

    # 2. 验证 get_session_aliases_map
    await db.update_favour(
        user_id="1002",
        session_id="group-1",
        favour=20,
        nickname="咕咕",
        aliases=["咕师傅"],
        aliases_locked=False,
    )
    aliases_map = await db.get_session_aliases_map("group-1")
    assert "1001" in aliases_map
    assert aliases_map["1001"]["aliases"] == ["牢尤", "猫猫"]
    assert aliases_map["1001"]["locked"] is True
    assert "1002" in aliases_map
    assert aliases_map["1002"]["aliases"] == ["咕师傅"]
    assert aliases_map["1002"]["locked"] is False

    # 3. 验证 update_favour_aliases 辅助方法
    await db.update_favour_aliases("1002", "group-1", ["鸽子", "咕师傅"], locked=True)
    rec2 = await db.get_favour("1002", "group-1")
    assert rec2.get_aliases() == ["鸽子", "咕师傅"]
    assert rec2.aliases_locked is True

    await db.close()


def test_build_group_impression_prompt_with_aliases():
    prompt = FavourManagerTool._build_group_impression_prompt(
        group_id="group-1",
        active_users={
            "1001": {"user_name": "尤约拉猫", "messages": ["今天天气真好", "来打两把"]},
        },
        user_existing_impressions={"1001": "电竞宅男"},
        user_existing_aliases={"1001": ["牢尤", "猫猫"]},
    )
    assert "UID: 1001, 昵称: 尤约拉猫 (既有印象: 电竞宅男) (已知称呼: 牢尤, 猫猫)" in prompt
    assert "提取专属称呼、简称、外号或梗名" in prompt
    assert "严格排除无针对性的通用社交代词" in prompt


def test_parse_group_impression_json_composite_and_backward_compatible():
    # 1. 测试复合 JSON 解析与黑名单过滤
    json_text = """
    {
      "1001": {
        "impression": "爱吐槽的加班人",
        "aliases": ["牢尤", "猫猫", "哥们", "群主", "12345"]
      },
      "1002": {
        "impression": "造梗气氛组",
        "aliases": ["咕师傅"]
      }
    }
    """
    res = FavourManagerTool._parse_group_impression_json(json_text)
    assert "1001" in res
    assert res["1001"]["impression"] == "爱吐槽的加班人"
    # "哥们"、"群主"、"12345" 必须被过滤
    assert res["1001"]["aliases"] == ["牢尤", "猫猫"]
    assert res["1002"]["aliases"] == ["咕师傅"]

    # 2. 测试旧版扁平字符串格式的向后兼容性
    old_json_text = '{"1003": "高冷路过学者"}'
    res_old = FavourManagerTool._parse_group_impression_json(old_json_text)
    assert res_old["1003"]["impression"] == "高冷路过学者"
    assert res_old["1003"]["aliases"] == []


@pytest.mark.asyncio
async def test_llm_tool_record_member_nickname_for_self(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    plugin = _build_test_plugin()
    plugin.db_manager = db

    event = _DialogueEvent(user_id="1001", session_id="group-1", sender_name="尤约")

    # 用户指定叫自己阿强
    msg = await plugin.record_member_nickname(
        event=event,
        nickname="阿强",
        target_user="self",
    )
    assert "成功为用户" in msg
    assert "阿强" in msg

    rec = await db.get_favour("1001", "group-1")
    assert rec is not None
    assert rec.get_aliases() == ["阿强"]

    # 再次添加新外号，验证增量合并
    await plugin.record_member_nickname(
        event=event,
        nickname="牢尤",
        target_user="我",
    )
    rec = await db.get_favour("1001", "group-1")
    assert rec.get_aliases() == ["阿强", "牢尤"]

    await db.close()


@pytest.mark.asyncio
async def test_llm_tool_record_member_nickname_with_at_and_locking(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    plugin = _build_test_plugin()
    plugin.db_manager = db

    # 预先设置 1002 并锁定
    await db.update_favour(
        user_id="1002",
        session_id="group-1",
        nickname="老李",
        aliases=["李师傅"],
        aliases_locked=True,
    )

    # 附带 At 组件
    msg_obj = _FakeMessageObj(message=[At(qq="1002")])
    event = _DialogueEvent(user_id="1001", session_id="group-1", message_obj=msg_obj)

    # 尝试修改被锁定的 1002
    res = await plugin.record_member_nickname(
        event=event,
        nickname="老李头",
        target_user="老李",
    )
    assert "已被管理员锁定" in res

    # 验证数据库中 1002 未被修改
    rec = await db.get_favour("1002", "group-1")
    assert rec.get_aliases() == ["李师傅"]

    await db.close()


@pytest.mark.asyncio
async def test_llm_tool_blacklist_and_length_validation(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    plugin = _build_test_plugin()
    plugin.db_manager = db

    event = _DialogueEvent(user_id="1001", session_id="group-1")

    # 尝试设置通用代词
    res1 = await plugin.record_member_nickname(event, nickname="哥们", target_user="self")
    assert "通用社交代词" in res1

    # 尝试设置过长称呼
    res2 = await plugin.record_member_nickname(event, nickname="这是一个超级无敌巨长的不合格外号", target_user="self")
    assert "长度需在1到10个字符之间" in res2

    # 尝试纯数字
    res3 = await plugin.record_member_nickname(event, nickname="123456", target_user="self")
    assert "纯数字" in res3

    await db.close()


@pytest.mark.asyncio
async def test_on_llm_request_injects_alias_table(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    # 在群内添加带别名的成员
    await db.update_favour(
        user_id="1001",
        session_id="group-1",
        nickname="尤约拉猫",
        aliases=["牢尤", "猫猫"],
    )

    plugin = _build_test_plugin()
    plugin.db_manager = db
    plugin._is_favour_session_enabled = lambda _sid: True

    event = _DialogueEvent(user_id="1001", session_id="group-1", message="你好")
    req = SimpleNamespace(system_prompt="你是一个可爱的助手。")

    await plugin.inject_favour_prompt(event, req)

    # 验证系统提示词中注入了称呼对照表和用户自身别名
    assert "当前会话成员常用称呼/外号表：" in req.system_prompt
    assert "尤约拉猫 (UID: 1001): 常用称呼: [牢尤, 猫猫]" in req.system_prompt
    assert "用户的常用外号/称呼：牢尤, 猫猫" in req.system_prompt
    assert "称呼与指代指引：" in req.system_prompt

    await db.close()


@pytest.mark.asyncio
async def test_webui_aliases_api(tmp_path: Path):
    import json
    from unittest.mock import MagicMock
    from astrbot.api.web import _request_var
    from astrbot_plugin_Favour_Ultra.web_api import FavourWebApi

    db = FavourDBManager(tmp_path)
    await db.init_db()

    await db.update_favour(
        user_id="1001",
        session_id="global",
        favour=50,
        nickname="尤约拉猫",
        impression="爱打游戏",
        aliases=["牢尤"],
        aliases_locked=False,
    )

    mock_plugin = MagicMock()
    mock_plugin.db_manager = db
    mock_plugin.is_global_favour = True
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
            self._json = json_data or {}

        async def json(self, default=None):
            return self._json

    # 1. 验证 list_records 返回 aliases 和 aliases_locked
    token = _request_var.set(FakeReq(query_dict={"session_id": "global", "page": 1, "page_size": 20}))
    try:
        resp = await api.list_records()
        body = json.loads(resp.body)
        assert len(body["rows"]) == 1
        assert body["rows"][0]["aliases"] == ["牢尤"]
        assert body["rows"][0]["aliases_locked"] is False

        # 2. 验证 update_record 更新 aliases 与 aliases_locked
        _request_var.set(
            FakeReq(
                json_data={
                    "user_id": "1001",
                    "session_id": "global",
                    "aliases": ["牢尤", "猫猫"],
                    "aliases_locked": True,
                }
            )
        )
        resp_up = await api.update_record()
        body_up = json.loads(resp_up.body)
        assert body_up.get("ok") is True

        # 验证数据库中已持久化
        rec = await db.get_favour("1001", "global")
        assert rec.get_aliases() == ["牢尤", "猫猫"]
        assert rec.aliases_locked is True

        # 3. 验证通过外号关键词搜索
        _request_var.set(FakeReq(query_dict={"session_id": "global", "keyword": "猫猫"}))
        resp_kw = await api.list_records()
        body_kw = json.loads(resp_kw.body)
        assert len(body_kw["rows"]) == 1
        assert body_kw["rows"][0]["user_id"] == "1001"
    finally:
        _request_var.reset(token)

    await db.close()


@pytest.mark.asyncio
async def test_favour_prompt_postposition_and_fallback(tmp_path: Path):
    db = FavourDBManager(tmp_path)
    await db.init_db()

    plugin = _build_test_plugin()
    plugin.db_manager = db
    plugin._is_favour_session_enabled = lambda _sid: True

    # 1. 验证 prompt 后置：既有人设保持在最前端，好感度输出要求追加在最后
    event = _DialogueEvent(user_id="1001", session_id="group-1", message="你好")
    req = SimpleNamespace(system_prompt="【初始系统人设：猫娘助手】")
    await plugin.inject_favour_prompt(event, req)

    assert req.system_prompt.startswith("【初始系统人设：猫娘助手】")
    assert "【输出格式强制要求】" in req.system_prompt
    assert req.system_prompt.endswith("严格确保末尾标签完整。")

    # 2. 验证缺失好感度标签时的优雅兜底
    fake_event = _DialogueEvent(
        user_id="1001",
        session_id="group-1",
        message="你好呀",
    )
    fake_resp = SimpleNamespace(
        completion_text="免费41万钻，这得攒到猴年马月，你先打通我请你吃牛肉饭"
    )

    await plugin.handle_llm_response(fake_event, fake_resp)

    # 验证未捕获标签时未放入待修改队列，避免错误增减分
    assert str(fake_event.message_obj.message_id) not in plugin.pending_updates

    # 验证 update_data 能够优雅平稳流转，好感度保持原样持平
    await plugin.update_data(fake_event)
    rec = await db.get_favour("1001", "group-1")
    # 原好感应保持不变
    assert rec is None or rec.favour == 0

    await db.close()
