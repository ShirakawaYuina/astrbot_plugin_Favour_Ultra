from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Plain

from astrbot_plugin_Favour_Ultra.main import FavourManagerTool


class _DialogueEvent:
    """提供印象采样链路所需的最小事件接口。"""

    def __init__(self, *, user_id: str, session_id: str, message: str) -> None:
        self.user_id = user_id
        self.message_str = message
        self.unified_msg_origin = session_id
        self.message_obj = SimpleNamespace(self_id="bot-999", message_id="msg-1")
        self._extras: dict[str, object] = {}
        self._result = SimpleNamespace(chain=[])

    def get_sender_id(self) -> str:
        return self.user_id

    def set_extra(self, key: str, value: object) -> None:
        self._extras[key] = value

    def get_extra(self, key: str, default=None):  # noqa: ANN001
        return self._extras.get(key, default)

    def get_result(self):  # noqa: ANN201
        return self._result


def _build_plugin() -> FavourManagerTool:
    plugin = FavourManagerTool.__new__(FavourManagerTool)
    plugin.impression_natural_rounds = 3
    plugin.user_dialogue_rounds = {}
    plugin.pending_dialogue_round_updates = {}
    plugin.impression_refresh_locks = set()
    plugin.pending_updates = {}
    plugin.allowed_sessions = []
    plugin.blocked_sessions = []
    plugin.favour_pattern = re.compile(r"\[好感度(?:上升：\d+|降低：\d+|持平)\]")
    plugin.relationship_pattern = re.compile(r"$^")
    plugin.is_global_favour = False
    plugin.db_manager = SimpleNamespace(
        get_favour=AsyncMock(return_value=None),
        update_favour=AsyncMock(return_value=True),
    )
    plugin._schedule_impression_refresh = lambda _user_id, _session_id: None
    return plugin


@pytest.mark.asyncio
async def test_two_group_users_are_not_paired_as_dialogue() -> None:
    plugin = _build_plugin()
    plugin._mark_dialogue_round_for_user = AsyncMock(return_value=False)
    event_a = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="A 的观点",
    )
    event_b = _DialogueEvent(
        user_id="200",
        session_id="group-1",
        message="B 的回复",
    )

    await plugin.track_natural_dialogue_message(event_a)
    await plugin.track_natural_dialogue_message(event_b)

    plugin._mark_dialogue_round_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_session_does_not_start_impression_tracking() -> None:
    plugin = _build_plugin()
    plugin.blocked_sessions = ["group-1"]
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="不应采样",
    )

    await plugin.track_natural_dialogue_message(event)

    assert event.get_extra("_favour_dialogue_user_message") is None


@pytest.mark.asyncio
async def test_completed_llm_reply_records_one_exact_user_bot_round() -> None:
    plugin = _build_plugin()
    plugin._mark_dialogue_round_for_user = AsyncMock(return_value=False)
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="今天天气如何",
    )

    await plugin.track_natural_dialogue_message(event)
    event.set_extra("_favour_llm_response_received", True)
    await plugin._record_completed_dialogue_round(event, "天气不错")

    plugin._mark_dialogue_round_for_user.assert_awaited_once_with(
        "100",
        "group-1",
        "今天天气如何",
        "天气不错",
    )


@pytest.mark.asyncio
async def test_completed_round_is_consumed_only_once() -> None:
    plugin = _build_plugin()
    plugin._mark_dialogue_round_for_user = AsyncMock(return_value=False)
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="你好",
    )

    await plugin.track_natural_dialogue_message(event)
    event.set_extra("_favour_llm_response_received", True)
    await plugin._record_completed_dialogue_round(event, "你好呀")
    await plugin._record_completed_dialogue_round(event, "你好呀")

    plugin._mark_dialogue_round_for_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_hooks_record_clean_visible_reply_once() -> None:
    plugin = _build_plugin()
    plugin._mark_dialogue_round_for_user = AsyncMock(return_value=False)
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="你好",
    )
    event._result.chain = [
        Plain("你好呀\n[刷分检测：否]")
    ]

    await plugin.track_natural_dialogue_message(event)
    await plugin.handle_llm_response(
        event,
        SimpleNamespace(completion_text="你好呀\n[刷分检测：否]"),
    )
    await plugin.update_data(event)

    plugin._mark_dialogue_round_for_user.assert_awaited_once_with(
        "100", "group-1", "你好", "你好呀"
    )
    assert event._result.chain[0].text == "你好呀"


def test_dialogue_round_cache_has_no_sliding_window_duplicates() -> None:
    plugin = _build_plugin()

    plugin._append_user_dialogue_round(
        "100", "group-1", "你好", "你好呀"
    )
    plugin._append_user_dialogue_round(
        "100", "group-1", "今天天气如何", "天气不错"
    )

    assert plugin.user_dialogue_rounds[("100", "group-1")] == [
        ("你好", "你好呀"),
        ("今天天气如何", "天气不错"),
    ]


def test_dialogue_round_cache_is_isolated_by_session() -> None:
    plugin = _build_plugin()

    plugin._append_user_dialogue_round("100", "group-1", "群一问题", "群一回答")
    plugin._append_user_dialogue_round("100", "group-2", "群二问题", "群二回答")

    assert "群一问题" in plugin._get_recent_natural_dialogue_text("100", "group-1")
    assert "群二问题" not in plugin._get_recent_natural_dialogue_text(
        "100", "group-1"
    )
    assert "群二问题" in plugin._get_recent_natural_dialogue_text("100", "group-2")


@pytest.mark.asyncio
async def test_pending_round_count_keeps_incrementing_before_database_write() -> None:
    plugin = _build_plugin()

    await plugin._mark_dialogue_round_for_user(
        "100", "group-1", "第一问", "第一答"
    )
    await plugin._mark_dialogue_round_for_user(
        "100", "group-1", "第二问", "第二答"
    )

    assert plugin.pending_dialogue_round_updates[("100", "group-1")] == 2


@pytest.mark.asyncio
async def test_refresh_waits_for_complete_runtime_evidence_window() -> None:
    plugin = _build_plugin()
    plugin.impression_natural_rounds = 3
    plugin.db_manager.get_favour = AsyncMock(
        return_value=SimpleNamespace(dialogue_round_count=2)
    )

    refresh_required = await plugin._mark_dialogue_round_for_user(
        "100", "group-1", "重启后的第一问", "重启后的第一答"
    )

    assert refresh_required is False
    assert len(plugin.user_dialogue_rounds[("100", "group-1")]) == 1


@pytest.mark.asyncio
async def test_refresh_is_scheduled_after_round_count_persistence() -> None:
    plugin = _build_plugin()
    plugin.impression_natural_rounds = 1
    call_order: list[str] = []

    async def update_favour(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        call_order.append("persist")
        return True

    plugin.db_manager.update_favour = AsyncMock(side_effect=update_favour)
    plugin._schedule_impression_refresh = (
        lambda _user_id, _session_id: call_order.append("schedule")
    )
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="测试问题",
    )
    event._result.chain = [Plain("测试回答\n[刷分检测：否]")]

    await plugin.track_natural_dialogue_message(event)
    await plugin.handle_llm_response(
        event,
        SimpleNamespace(completion_text="测试回答\n[刷分检测：否]"),
    )
    await plugin.update_data(event)

    assert call_order == ["persist", "schedule"]


@pytest.mark.asyncio
async def test_normal_favour_update_does_not_overwrite_impression() -> None:
    plugin = _build_plugin()
    existing_record = SimpleNamespace(
        favour=10,
        relationship="普通",
        interact_count=2,
        dialogue_round_count=0,
        impression="后台刚生成的新印象",
    )
    plugin.db_manager.get_favour = AsyncMock(return_value=existing_record)
    plugin.db_manager._get_relationship = lambda _favour: "普通"
    plugin.min_favour_value = -100
    plugin.max_favour_value = 100
    plugin.max_daily_increase = 20
    plugin.penalty_records = {}
    plugin._is_within_daily_limit = lambda _user_id, _change: True
    plugin._get_daily_favour_change = lambda _user_id: 0
    plugin._update_daily_favour_change = AsyncMock()
    event = _DialogueEvent(
        user_id="100",
        session_id="group-1",
        message="正常问题",
    )
    event._result.chain = [Plain("正常回答\n[好感度持平]\n[刷分检测：否]")]

    await plugin.track_natural_dialogue_message(event)
    await plugin.handle_llm_response(
        event,
        SimpleNamespace(completion_text="正常回答\n[好感度持平]\n[刷分检测：否]"),
    )
    await plugin.update_data(event)

    plugin.db_manager.update_favour.assert_awaited_once()
    update_kwargs = plugin.db_manager.update_favour.await_args.kwargs
    assert update_kwargs["dialogue_round_count"] == 1
    assert "impression" not in update_kwargs


def test_impression_prompt_requires_user_only_repetition_evidence() -> None:
    plugin = _build_plugin()

    prompt = plugin._build_impression_prompt(
        user_id="100",
        dialogue_text=(
            "第1轮\n- 目标用户: 你好\n- 机器人: 你好呀\n"
            "第2轮\n- 目标用户: 今天天气如何\n- 机器人: 天气不错"
        ),
    )

    assert "只分析“目标用户”" in prompt
    assert "机器人复述、引用或延续话题" in prompt
    assert "至少3轮" in prompt
    assert "当前好感度" not in prompt
    assert "当前关系" not in prompt
