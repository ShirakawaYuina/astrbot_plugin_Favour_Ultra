from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot_plugin_Favour_Ultra.main import FavourManagerTool
from astrbot_plugin_Favour_Ultra.storage import FavourDBManager


@pytest.fixture
def temp_db(tmp_path: Path) -> FavourDBManager:
    return FavourDBManager(data_dir=tmp_path)


@pytest.mark.asyncio
async def test_record_daily_group_utterance_and_limit(temp_db: FavourDBManager):
    group_id = "group-1"
    user_id = "user-100"
    date_str = "2026-10-09"

    # 写入两条记录，限制上限为 2
    res1 = await temp_db.record_daily_group_utterance(
        group_id, user_id, "张三", "第一条发言", date_str, max_daily_count=2
    )
    res2 = await temp_db.record_daily_group_utterance(
        group_id, user_id, "张三", "第二条发言", date_str, max_daily_count=2
    )
    res3 = await temp_db.record_daily_group_utterance(
        group_id, user_id, "张三", "第三条超出上限", date_str, max_daily_count=2
    )

    assert res1 is True
    assert res2 is True
    assert res3 is False  # 超过上限

    # 验证活跃群列表
    groups = await temp_db.get_active_groups_by_date(date_str)
    assert groups == ["group-1"]

    await temp_db.close()


@pytest.mark.asyncio
async def test_get_group_active_users_threshold(temp_db: FavourDBManager):
    group_id = "group-1"
    date_str = "2026-10-09"

    # 用户1：发言 10 条，每条 2 字（命中条数 >= 10）
    for i in range(10):
        await temp_db.record_daily_group_utterance(
            group_id, "u1", "用户一", f"发言{i}", date_str
        )

    # 用户2：发言 2 条，每条 30 字（命中字数 >= 50）
    for i in range(2):
        await temp_db.record_daily_group_utterance(
            group_id,
            "u2",
            "用户二",
            "这是一段非常长非常长的测试发言文本用来凑字数测试确保超过二十五个字",
            date_str,
        )

    # 用户3：发言 1 条，短文本（未达到门槛）
    await temp_db.record_daily_group_utterance(
        group_id, "u3", "用户三", "你好", date_str
    )

    active = await temp_db.get_group_active_users_utterances(
        group_id, date_str, min_msg_count=10, min_total_chars=50
    )

    assert "u1" in active
    assert "u2" in active
    assert "u3" not in active
    assert active["u1"]["msg_count"] == 10
    assert active["u2"]["total_chars"] >= 50

    await temp_db.close()


def test_build_group_impression_prompt():
    active_users = {
        "1001": {
            "user_name": "张三",
            "messages": ["今天写代码好累", "晚上去吃火锅"],
            "msg_count": 2,
            "total_chars": 15,
        },
        "1002": {
            "user_name": "李四",
            "messages": ["哈哈哈哈哈笑死"],
            "msg_count": 1,
            "total_chars": 7,
        },
    }
    existing = {"1001": "苦逼程序员"}

    prompt = FavourManagerTool._build_group_impression_prompt(
        group_id="group-888",
        active_users=active_users,
        user_existing_impressions=existing,
    )

    assert "group-888" in prompt
    assert "UID: 1001" in prompt
    assert "既有印象: 苦逼程序员" in prompt
    assert "UID: 1002" in prompt
    assert "标准 JSON" in prompt


def test_parse_group_impression_json():
    # 测试常规带 Markdown 包裹和前缀的内容
    raw_response = """```json
    {
        "1001": "该用户的印象是：热爱摄影的技术宅",
        "1002": "“高频造梗气氛组”",
        "1003": "这是一个超出十个汉字超级超级超级长的印象短句"
    }
    ```"""

    res = FavourManagerTool._parse_group_impression_json(raw_response)
    assert res["1001"] == "热爱摄影的技术宅"
    assert res["1002"] == "高频造梗气氛组"
    assert len(res["1003"]) <= 10
    assert res["1003"] == "这是一个超出十个汉字"


@pytest.mark.asyncio
async def test_run_daily_group_impression_batch_global_mode_option2(tmp_path: Path):
    """测试方案2（主活跃群决定论）：用户在多群存在时，全局模式下仅在发言最多的主群生成印象"""
    db = FavourDBManager(data_dir=tmp_path)
    date_str = "2026-10-09"

    # 用户 u1 在 group-A 发言 15 条短发言（15 * 3 = 45字）
    for i in range(15):
        await db.record_daily_group_utterance(
            "group-A", "u1", "用户一", "短发言", date_str
        )

    # 用户 u1 在 group-B 发言 15 条长发言（15 * 20 = 300字）
    for i in range(15):
        await db.record_daily_group_utterance(
            "group-B", "u1", "用户一", "这是一段测试长文本用于占据主活跃群", date_str
        )

    # group-B 是 u1 的主活跃群
    activity = await db.get_user_activity_across_groups(date_str)
    assert activity["u1"]["group-B"] > activity["u1"]["group-A"]

    plugin = FavourManagerTool.__new__(FavourManagerTool)
    plugin.is_global_favour = True  # 全局模式
    plugin.daily_imp_min_messages = 10
    plugin.daily_imp_min_chars = 40
    plugin.db_manager = db
    plugin._is_favour_session_enabled = lambda _sid: True

    # 模拟大模型 Provider
    mock_provider = MagicMock()
    mock_provider.text_chat = AsyncMock(
        return_value=SimpleNamespace(
            completion_text=json.dumps({"u1": "大群长文活跃者"})
        )
    )
    plugin._get_impression_provider = lambda: mock_provider

    res = await plugin._run_daily_group_impression_batch(target_date=date_str)

    # 方案2要求：u1 仅在主活跃群 group-B 中被分析，group-A 应跳过该用户
    assert "group-B" in res
    assert "u1" in res["group-B"]
    assert res["group-B"]["u1"] == "大群长文活跃者"
    assert "group-A" not in res or "u1" not in res["group-A"]

    # 检查数据库中全局记录写入
    rec = await db.get_favour("u1", "global")
    assert rec is not None
    assert rec.impression == "大群长文活跃者"

    await db.close()


@pytest.mark.asyncio
async def test_run_daily_group_impression_batch_isolated_mode(tmp_path: Path):
    """测试分群模式：用户在不同群各有一套专属印象"""
    db = FavourDBManager(data_dir=tmp_path)
    date_str = "2026-10-09"

    for i in range(10):
        await db.record_daily_group_utterance(
            "group-A", "u1", "用户一", "代码讨论与技术提问", date_str
        )
    for i in range(10):
        await db.record_daily_group_utterance(
            "group-B", "u1", "用户一", "表情包与日常闲聊水群", date_str
        )

    plugin = FavourManagerTool.__new__(FavourManagerTool)
    plugin.is_global_favour = False  # 分群独立模式
    plugin.daily_imp_min_messages = 10
    plugin.daily_imp_min_chars = 30
    plugin.db_manager = db
    plugin._is_favour_session_enabled = lambda _sid: True

    call_count = 0

    async def mock_chat(contexts):
        nonlocal call_count
        call_count += 1
        prompt = contexts[0]["content"]
        if "group-A" in prompt:
            return SimpleNamespace(completion_text=json.dumps({"u1": "技术极客"}))
        else:
            return SimpleNamespace(completion_text=json.dumps({"u1": "水群摸鱼王"}))

    mock_provider = MagicMock()
    mock_provider.text_chat = AsyncMock(side_effect=mock_chat)
    plugin._get_impression_provider = lambda: mock_provider

    res = await plugin._run_daily_group_impression_batch(target_date=date_str)

    assert res["group-A"]["u1"] == "技术极客"
    assert res["group-B"]["u1"] == "水群摸鱼王"

    rec_a = await db.get_favour("u1", "group-A")
    rec_b = await db.get_favour("u1", "group-B")
    assert rec_a.impression == "技术极客"
    assert rec_b.impression == "水群摸鱼王"

    await db.close()


@pytest.mark.asyncio
async def test_clean_expired_group_utterances(tmp_path: Path):
    db = FavourDBManager(data_dir=tmp_path)
    await db.record_daily_group_utterance("g1", "u1", "名", "旧发言", "2026-10-01")
    await db.record_daily_group_utterance("g1", "u1", "名", "新发言", "2026-10-09")

    cleaned = await db.clean_expired_group_utterances("2026-10-05")
    assert cleaned == 1

    groups_old = await db.get_active_groups_by_date("2026-10-01")
    assert groups_old == []

    groups_new = await db.get_active_groups_by_date("2026-10-09")
    assert groups_new == ["g1"]

    await db.close()


@pytest.mark.asyncio
async def test_impression_provider_config_resolution():
    """测试印象生成专用模型分别在 daily_impression_config 及旧版 advanced_config 下均可正确解析"""
    # 1. 在 daily_impression_config 内部配置
    conf1 = {
        "advanced_config": {
            "daily_impression_config": {
                "impression_provider": "ark/deepseek-v4-flash-ga-260731"
            }
        }
    }
    mock_ctx = MagicMock()
    mock_ctx.get_config.return_value = {}
    p1 = FavourManagerTool(mock_ctx, conf1)
    assert p1.impression_provider == "ark/deepseek-v4-flash-ga-260731"

    # 2. 在旧版 advanced_config 配置（兼容回退）
    conf2 = {
        "advanced_config": {
            "impression_provider": "legacy-provider-id",
            "daily_impression_config": {}
        }
    }
    p2 = FavourManagerTool(mock_ctx, conf2)
    assert p2.impression_provider == "legacy-provider-id"


@pytest.mark.asyncio
async def test_daily_impression_timeout_config_resolution():
    """测试单群大模型分析超时时间配置解析"""
    mock_ctx = MagicMock()
    mock_ctx.get_config.return_value = {}

    # 1. 默认超时时间 180 秒
    conf_default = {"advanced_config": {"daily_impression_config": {}}}
    p1 = FavourManagerTool(mock_ctx, conf_default)
    assert p1.daily_imp_timeout == 180

    # 2. 自定义超时时间 240 秒
    conf_custom = {
        "advanced_config": {
            "daily_impression_config": {
                "timeout": 240
            }
        }
    }
    p2 = FavourManagerTool(mock_ctx, conf_custom)
    assert p2.daily_imp_timeout == 240


@pytest.mark.asyncio
async def test_daily_impression_timeout_error_handling(tmp_path: Path):
    """测试大模型调用超时时优雅捕获并不中断批处理流程"""
    db = FavourDBManager(data_dir=tmp_path)
    date_str = "2026-10-09"

    for i in range(12):
        await db.record_daily_group_utterance(
            "group-timeout", "u1", "用户", "超时测试发言", date_str
        )

    plugin = FavourManagerTool.__new__(FavourManagerTool)
    plugin.is_global_favour = True
    plugin.daily_imp_min_messages = 10
    plugin.daily_imp_min_chars = 40
    plugin.daily_imp_timeout = 0.05  # 设置极短超时 0.05s
    plugin.db_manager = db
    plugin._is_favour_session_enabled = lambda _sid: True

    # 模拟大模型慢调用（触发超时）
    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(0.5)
        return SimpleNamespace(completion_text="{}")

    mock_provider = MagicMock()
    mock_provider.text_chat = slow_chat
    plugin._get_impression_provider = lambda: mock_provider

    res = await plugin._run_daily_group_impression_batch(target_date=date_str)
    # 超时应被安全捕获，返回空字典而不会向上抛出异常崩溃
    assert res == {}

    await db.close()

