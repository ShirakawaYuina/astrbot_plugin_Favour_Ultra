# main.py
import asyncio
import base64
import json
import mimetypes
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .image_sender import FavourImageSender
from .permissions import PermissionManager, PermLevel
from .storage import FavourDBManager, FavourRecord
from .utils import is_valid_userid
from .web_api import FavourWebApi


class ImpressionResult(str):
    """印象分析结果封装，兼容直接作为纯文本印象使用以及获取别名列表。"""

    def __new__(cls, impression: str = "", aliases: list[str] | None = None):
        instance = super().__new__(cls, impression)
        instance.impression = impression
        instance.aliases = aliases or []
        return instance

    def get(self, key, default=None):
        if key == "impression":
            return self.impression
        if key == "aliases":
            return self.aliases
        return default

    def __getitem__(self, key):
        if key == "impression":
            return self.impression
        if key == "aliases":
            return self.aliases
        return super().__getitem__(key)

class FavourManagerTool(Star):
    enable_daily_impression: bool = False
    daily_impression_time: str = "03:30"
    daily_imp_min_messages: int = 10
    daily_imp_min_chars: int = 50
    daily_imp_max_user_messages: int = 50
    impression_provider: str = ""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context  # 存储context引用，用于访问LLM provider
        self.config = config

        # 基础配置
        self.favour_mode = self.config.get("favour_mode", "galgame")
        self.is_global_favour = self.config.get("is_global_favour", False)
        self.group_sort_by = self.config.get("group_sort_by", "default")
        self.enable_relationship_table = self.config.get(
            "enable_relationship_table", True
        )
        self.min_favour_value = self.config.get("min_favour_value", -100)
        self.max_favour_value = self.config.get("max_favour_value", 100)
        self.default_favour = self.config.get("default_favour", 0)
        self.favour_rule_prompt = self.config.get("favour_rule_prompt", "")
        self.template = self.config.get("template", "default")

        # 高级配置
        adv_conf = self.config.get("advanced_config", {})
        self.admin_default_favour = adv_conf.get("admin_default_favour", 50)
        self.favour_envoys = adv_conf.get("favour_envoys", [])
        self.favour_increase_min = adv_conf.get("favour_increase_min", 1)
        self.favour_increase_max = adv_conf.get("favour_increase_max", 3)
        self.favour_decrease_min = adv_conf.get("favour_decrease_min", 1)
        self.favour_decrease_max = adv_conf.get("favour_decrease_max", 5)
        self.perm_level_threshold = adv_conf.get("level_threshold", 50)
        self.blocked_sessions = [
            str(s).strip()
            for s in adv_conf.get("blocked_sessions", [])
            if str(s).strip()
        ]
        self.allowed_sessions = [
            str(s).strip()
            for s in adv_conf.get("allowed_sessions", [])
            if str(s).strip()
        ]
        self.webui_filter_empty_groups = adv_conf.get(
            "webui_filter_empty_groups", True
        )
        self.impression_natural_rounds = max(
            1, adv_conf.get("natural_dialogue_rounds", 8)
        )
        daily_imp_conf = adv_conf.get("daily_impression_config", {})
        self.impression_provider = str(
            daily_imp_conf.get("impression_provider")
            or adv_conf.get("impression_provider", "")
            or ""
        ).strip()
        self.enable_daily_impression = daily_imp_conf.get(
            "enable_daily_impression", True
        )
        self.daily_impression_time = daily_imp_conf.get(
            "schedule_time", "03:30"
        )
        self.daily_imp_min_messages = daily_imp_conf.get(
            "min_daily_messages", 10
        )
        self.daily_imp_min_chars = daily_imp_conf.get("min_daily_chars", 50)
        self.daily_imp_max_user_messages = daily_imp_conf.get(
            "max_daily_messages_per_user", 50
        )
        self.daily_imp_timeout = max(
            10, int(daily_imp_conf.get("timeout", 180) or 180)
        )

        # 惩罚机制配置
        penalty_conf = adv_conf.get("penalty_config", {})
        self.enable_penalty = penalty_conf.get("enable_penalty", True)
        self.penalty_whitelist = penalty_conf.get("penalty_whitelist", [])
        self.use_llm_for_spam_detection = penalty_conf.get(
            "use_llm_for_spam_detection", True
        )
        self.spam_detection_message_count = penalty_conf.get(
            "spam_detection_message_count", 10
        )
        self.spam_detection_time_window = penalty_conf.get(
            "spam_detection_time_window", 60
        )

        # 惩罚持续时间（统一为5分钟）
        self.penalty_duration = penalty_conf.get("penalty_duration", 5)

        self._validate_config()

        # 权限管理初始化
        self.admins_id = context.get_config().get("admins_id", [])
        PermissionManager.get_instance(
            superusers=self.admins_id, level_threshold=self.perm_level_threshold
        )

        # 数据库初始化
        self.data_dir = (
            Path(context.get_config().get("plugin.data_dir", "./data"))
            / "plugin_data"
            / "astrbot_plugin_favour_ultra"
        )
        self.db_manager = FavourDBManager(
            self.data_dir, self.min_favour_value, self.max_favour_value
        )

        # 异步初始化数据库和迁移数据
        asyncio.create_task(self._init_storage())

        # 正则表达式
        self.favour_pattern = re.compile(
            r"[\[［][^\[\]［］]*?(?:好.*?感|好.*?度|感.*?度)[^\[\]［］]*?[\]］]",
            re.DOTALL | re.IGNORECASE,
        )
        self.spam_pattern = re.compile(
            r"[\[［][^\[\]［］]*?(?:刷分|防刷)[^\[\]［］]*?[\]］]",
            re.DOTALL | re.IGNORECASE,
        )
        self.relationship_pattern = re.compile(
            r"[\[［]\s*用户申请确认关系\s*[:：]\s*(.*?)\s*[:：]\s*(true|false)(?:\s*[:：]\s*(true|false))?\s*[\]］]",
            re.IGNORECASE,
        )

        self.pending_updates = {}

        # 防刷检测相关
        self.user_messages: dict[
            str, list[tuple[datetime, str]]
        ] = {}  # 记录用户消息：{user_id: [(timestamp, message), ...]}
        self.penalty_records: dict[
            str, dict
        ] = {}  # 惩罚记录：{user_id: {level: str, expires: datetime}}

        # 每日好感度限制
        self.daily_favour_changes: dict[
            str, dict[str, int]
        ] = {}  # 记录每日好感度变化：{user_id: {date: change}}
        self.max_daily_increase = self.config.get(
            "max_daily_favour_increase", 20
        )  # 每日最高可提升好感度，从配置读取

        # 印象样本按“用户 + 会话”保存完整的一问一答，避免不同群聊串线，
        # 也避免相邻消息滑窗把同一句话重复写入多个样本。
        self.user_dialogue_rounds: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.pending_dialogue_round_updates: dict[tuple[str, str], int] = {}
        self.impression_refresh_locks: set[tuple[str, str]] = set()

        # 好感度回归配置
        reset_conf = self.config.get("favour_reset_config", {})
        self.enable_favour_reset = reset_conf.get("enable_favour_reset", True)
        self.favour_reset_day = reset_conf.get("favour_reset_day", 0)
        self.favour_reset_time = reset_conf.get("favour_reset_time", "23:59:59")

        self.image_sender = FavourImageSender(
            send_mode=self.config.get("image_send_mode", "base64"),
            url_base=self.config.get("image_send_url_base", ""),
        )

        # WebUI 好感度管理台，页面位于 pages/records/
        self.web_api = FavourWebApi(self)
        self.web_api.register()

        self._validate_config()

    async def _init_storage(self):
        """初始化存储并迁移数据"""
        try:
            await self.db_manager.init_db()

            # 检查旧文件并迁移
            old_global = self.data_dir / "global_favour.json"
            old_local = self.data_dir / "haogan.json"

            if old_global.exists():
                logger.info("检测到旧版全局好感度文件，开始迁移...")
                await self.db_manager.migrate_from_json(old_global, is_global=True)

            if old_local.exists():
                logger.info("检测到旧版会话好感度文件，开始迁移...")
                await self.db_manager.migrate_from_json(old_local, is_global=False)

            # 从数据库加载每日好感度变化数据
            await self._load_daily_favour_changes()

            # 注册好感度回归定时任务
            if self.enable_favour_reset:
                await self._register_favour_reset_task()

            # 注册每日群活跃用户印象分析任务
            if self.enable_daily_impression:
                await self._register_daily_impression_task()

        except Exception as e:
            logger.error(f"数据库初始化或迁移失败: {str(e)}\n{traceback.format_exc()}")

    async def _load_daily_favour_changes(self):
        """从数据库加载每日好感度变化数据"""
        try:
            await self.db_manager.init_db()
            data = await self.db_manager.get_all_daily_favour_changes()
            self.daily_favour_changes = data
            logger.info(f"已从数据库加载 {len(data)} 个用户的每日好感度变化数据")
        except Exception as e:
            logger.error(f"加载每日好感度变化数据失败: {str(e)}")

    async def _register_favour_reset_task(self):
        """注册好感度回归定时任务"""
        try:
            task_name = "favour_reset_weekly"
            time_parts = self.favour_reset_time.split(":")
            hour = int(time_parts[0]) if len(time_parts) > 0 else 23
            minute = int(time_parts[1]) if len(time_parts) > 1 else 59

            cron_expr = f"{minute} {hour} * * {self.favour_reset_day}"

            existing_jobs = await self.context.cron_manager.list_jobs(job_type="basic")
            for job in existing_jobs:
                if job.name == task_name:
                    await self.context.cron_manager.delete_job(job.job_id)

            await self.context.cron_manager.add_basic_job(
                name=task_name,
                cron_expression=cron_expr,
                handler=self._favour_reset_task,
                description="每周定时重置负好感度为0",
                enabled=True,
                persistent=False,
            )
            logger.info(f"好感度回归定时任务已注册: cron={cron_expr}")
        except Exception as e:
            logger.error(f"注册好感度回归定时任务失败: {e}")

    async def _register_daily_impression_task(self):
        """注册每日群活跃用户印象分析定时任务"""
        try:
            task_name = "daily_group_impression_analysis"
            time_parts = self.daily_impression_time.split(":")
            hour = int(time_parts[0]) if len(time_parts) > 0 else 3
            minute = int(time_parts[1]) if len(time_parts) > 1 else 30

            cron_expr = f"{minute} {hour} * * *"

            existing_jobs = await self.context.cron_manager.list_jobs(job_type="basic")
            for job in existing_jobs:
                if job.name == task_name:
                    await self.context.cron_manager.delete_job(job.job_id)

            await self.context.cron_manager.add_basic_job(
                name=task_name,
                cron_expression=cron_expr,
                handler=self._run_daily_group_impression_batch,
                description="每日定时拉取各群聊天记录分析活跃用户印象",
                enabled=True,
                persistent=False,
            )
            logger.info(f"每日群活跃用户印象分析任务已注册: cron={cron_expr}")
        except Exception as e:
            logger.error(f"注册每日群活跃用户印象分析任务失败: {e}")

    async def _favour_reset_task(self):
        """好感度回归任务处理函数"""
        try:
            count = await self.db_manager.reset_negative_favour_to_zero()
            if count > 0:
                logger.info(f"好感度回归完成: 已将 {count} 个负好感度用户重置为0")
            else:
                logger.info("好感度回归完成: 无需重置的用户")
        except Exception as e:
            logger.error(f"好感度回归任务执行失败: {e}")

    def _validate_config(self) -> None:
        if self.min_favour_value >= self.max_favour_value:
            self.min_favour_value = -100
            self.max_favour_value = 100

        self.default_favour = max(
            self.min_favour_value, min(self.max_favour_value, self.default_favour)
        )
        self.admin_default_favour = max(
            self.min_favour_value, min(self.max_favour_value, self.admin_default_favour)
        )

    def _is_natural_dialogue_message(self, message: str) -> bool:
        text = (message or "").strip()
        if not text:
            return False
        if text.startswith("/"):
            return False
        return True

    def _is_favour_session_enabled(self, session_id: str | None) -> bool:
        """判断当前好感度会话是否允许处理和采集对话。"""

        if not session_id:
            return False
        session_id_str = str(session_id).strip()
        if session_id_str == "global":
            return True
        raw_id = (
            session_id_str.split(":")[-1]
            if ":" in session_id_str
            else session_id_str
        )
        if self.allowed_sessions:
            if (
                session_id_str not in self.allowed_sessions
                and raw_id not in self.allowed_sessions
            ):
                return False
        if (
            session_id_str in self.blocked_sessions
            or raw_id in self.blocked_sessions
        ):
            return False
        return True

    def _append_user_dialogue_round(
        self,
        user_id: str,
        session_id: str,
        user_message: str,
        bot_message: str,
    ) -> None:
        """追加一个已完成的用户—机器人对话轮次。

        缓存键包含会话 ID；全局好感度模式下 session_id 本身为 ``global``，
        非全局模式则天然隔离各群聊与私聊。
        """

        cache_key = (user_id, session_id)
        dialogue_rounds = self.user_dialogue_rounds.setdefault(cache_key, [])
        dialogue_rounds.append((user_message.strip(), bot_message.strip()))
        max_rounds = max(1, self.impression_natural_rounds)
        if len(dialogue_rounds) > max_rounds:
            del dialogue_rounds[:-max_rounds]

    def _get_recent_natural_dialogue_text(self, user_id: str, session_id: str) -> str:
        """将完整轮次格式化为角色明确的印象生成证据。"""

        dialogue_rounds = self.user_dialogue_rounds.get((user_id, session_id), [])
        if not dialogue_rounds:
            return "None"
        formatted_rounds = []
        for round_index, (user_message, bot_message) in enumerate(
            dialogue_rounds, start=1
        ):
            formatted_rounds.append(
                f"第{round_index}轮\n"
                f"- 目标用户: {user_message}\n"
                f"- 机器人: {bot_message}"
            )
        return "\n".join(formatted_rounds)

    @staticmethod
    def _build_impression_prompt(user_id: str, dialogue_text: str) -> str:
        """构造证据约束明确的用户印象生成提示词。"""

        return (
            "请根据以下完整的用户—机器人自然对话轮次，为目标用户生成一句不超过10个汉字的印象。"
            "只分析“目标用户”的发言和行为，机器人发言仅用于理解语境，不能算作用户行为证据。"
            "选择在多轮对话中稳定出现、最有区分度且能被原文直接支持的互动特点。"
            "可以幽默、有梗，但不要为了抽象效果臆造特点，也不要根据单次行为下结论。"
            "只有目标用户在至少3轮中独立发送实质相同或高度近似的内容时，才允许使用复读、重复、回声等印象；"
            "机器人复述、引用或延续话题，以及同一轮中的相似措辞，都不属于用户复读证据。"
            "不要输出解释、引号、前缀或其他内容，只输出印象文本本身。\n\n"
            f"目标用户ID: {user_id}\n"
            f"完整自然对话轮次:\n{dialogue_text}"
        )

    def _extract_plain_text_from_result(self, event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
            if not result:
                return ""

            parts = []
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    text = comp.text
                    text = self.favour_pattern.sub("", text)
                    spam_re = getattr(self, "spam_pattern", None)
                    if spam_re:
                        text = spam_re.sub("", text)
                    else:
                        text = re.sub(r"[\[［][^\[\]［］]*?(?:刷分|防刷)[^\[\]［］]*?[\]］]", "", text)
                    text = re.sub(r"[\[［][^\[\]［］]*?印象[^\[\]［］]*?[\]］]", "", text)
                    text = self.relationship_pattern.sub("", text)
                    text = text.strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        except Exception:
            return ""

    def _get_impression_provider(self):
        llm_provider = None
        try:
            if self.impression_provider:
                llm_provider = self.context.get_provider_by_id(
                    provider_id=self.impression_provider
                )
        except Exception:
            llm_provider = None

        if not llm_provider:
            llm_provider = self.context.get_using_provider()
        return llm_provider

    async def _mark_dialogue_round_for_user(
        self,
        user_id: str,
        session_id: str,
        user_message: str,
        bot_message: str,
    ) -> bool:
        """记录完整轮次并返回本轮是否达到印象刷新阈值。"""

        record = await self.db_manager.get_favour(user_id, session_id)
        round_key = (user_id, session_id)
        stored_round_count = record.dialogue_round_count if record else 0
        pending_round_count = self.pending_dialogue_round_updates.get(
            round_key, stored_round_count
        )
        # 好感度控制标签偶尔解析失败时，数据库写入可能晚于印象采样；
        # 必须从尚未落库的计数继续累加，避免真实完整轮次被下一次更新覆盖。
        next_round_count = max(stored_round_count, pending_round_count) + 1
        self.pending_dialogue_round_updates[round_key] = next_round_count

        self._append_user_dialogue_round(
            user_id,
            session_id,
            user_message,
            bot_message,
        )

        if next_round_count % self.impression_natural_rounds != 0:
            return False

        # 数据库计数会跨重启保留，但完整对话样本只存在于当前进程内存中。
        # 若重启后恰好命中计数阈值，不能仅凭一两轮新样本仓促覆盖既有印象。
        cached_round_count = len(
            self.user_dialogue_rounds.get((user_id, session_id), [])
        )
        return cached_round_count >= self.impression_natural_rounds

    def _schedule_impression_refresh(self, user_id: str, session_id: str) -> None:
        """后台刷新用户印象，避免额外 LLM 调用阻塞用户消息回复链路。"""
        lock_key = (user_id, session_id)
        if lock_key in self.impression_refresh_locks:
            return

        task = asyncio.create_task(
            self._refresh_user_impression_from_dialogue(user_id, session_id)
        )
        task.add_done_callback(self._log_impression_refresh_task_result)

    def _log_impression_refresh_task_result(self, task: asyncio.Task) -> None:
        """显式记录后台任务异常，避免异步任务失败后只留下难以定位的事件循环告警。"""
        try:
            task.result()
        except Exception as e:
            logger.error(f"Impression refresh task failed: {e}")

    async def _refresh_user_impression_from_dialogue(
        self, user_id: str, session_id: str
    ) -> None:
        lock_key = (user_id, session_id)
        if lock_key in self.impression_refresh_locks:
            return

        self.impression_refresh_locks.add(lock_key)
        try:
            dialogue_text = self._get_recent_natural_dialogue_text(user_id, session_id)
            provider = self._get_impression_provider()
            if not provider:
                return

            prompt = self._build_impression_prompt(
                user_id=user_id,
                dialogue_text=dialogue_text,
            )
            response = await provider.text_chat(
                contexts=[{"role": "user", "content": prompt}]
            )
            impression = (response.completion_text or "").strip()
            if not impression:
                return

            if len(impression) > 10:
                impression = impression[:10]

            latest_record = await self.db_manager.get_favour(user_id, session_id)
            dialogue_round_count = self.pending_dialogue_round_updates.get(
                (user_id, session_id),
                latest_record.dialogue_round_count if latest_record else 0,
            )
            await self.db_manager.update_favour(
                user_id,
                session_id,
                dialogue_round_count=dialogue_round_count,
                impression=impression,
            )
        except Exception as e:
            logger.error(f"Refresh impression from dialogue failed: {e}")
        finally:
            self.impression_refresh_locks.discard(lock_key)

    @staticmethod
    def _build_group_impression_prompt(
        group_id: str,
        active_users: dict[str, dict],
        user_existing_impressions: dict[str, str] | None = None,
        user_existing_aliases: dict[str, list[str]] | None = None,
    ) -> str:
        """构建针对群聊所有活跃成员的单次印象分析提示词。

        Args:
            group_id: 群会话ID。
            active_users: 活跃用户数据字典 {uid: {"user_name": str, "messages": list[str]}}。
            user_existing_impressions: 用户的历史既有印象映射 {uid: str}。

        Returns:
            发送给大模型的完整提示词文本。
        """
        existing_map = user_existing_impressions or {}
        existing_aliases_map = user_existing_aliases or {}
        user_sections = []
        for idx, (uid, data) in enumerate(active_users.items(), start=1):
            name = data.get("user_name", uid)
            msgs = data.get("messages", [])
            old_imp = existing_map.get(uid, "").strip()
            old_tag = f" (既有印象: {old_imp})" if old_imp else ""
            old_aliases = existing_aliases_map.get(uid, [])
            alias_tag = f" (已知称呼: {', '.join(old_aliases)})" if old_aliases else ""
            msg_list = "\n".join(f"- {m}" for m in msgs)
            user_sections.append(
                f"【成员 {idx}】UID: {uid}, 昵称: {name}{old_tag}{alias_tag}\n发言列表:\n{msg_list}"
            )

        users_text = "\n\n".join(user_sections)
        prompt = (
            f"你是一个敏锐细腻的群聊人设观察员。\n"
            f"以下是群聊【{group_id}】在过去一天中各活跃成员的真实发言记录。\n"
            f"请阅读各成员的言行与互动氛围，为每一位活跃成员提炼一句生动的“今日人设印象”，并识别群友在聊天中对该成员的“常用外号/称呼”。\n\n"
            f"【群成员发言记录】\n"
            f"{users_text}\n\n"
            f"【生成要求】\n"
            f"1. 观察每位成员的发言风格、情绪倾向和常聊话题（例如：热心技术宅、造梗气氛组、吐槽狂人、佛系摸鱼人等）；\n"
            f"2. 为每位成员生成一句不超过 10 个汉字的“今日人设印象”；\n"
            f"3. 观察群友在聊天中如何呼叫或指代该成员（提取专属称呼、简称、外号或梗名，例如“牢尤”、“猫猫”、“阿强”）；\n"
            f"   - 严格排除无针对性的通用社交代词（如：哥们、大哥、兄弟、老板、佬、群主、管理、机器人、大家、群友等）；\n"
            f"   - 若当天没有出现特定指代外号，aliases 必须为空列表 []；\n"
            f"4. 若成员存在既有印象或已知称呼，请在原有基调上平滑微调或保留，避免剧烈颠覆；\n"
            f"5. 必须以严格的标准 JSON 键值对格式输出，键为成员的 UID 字符串，值为包含 impression（不超过10字印象字符串）和 aliases（外号字符串列表）的对象；\n"
            f"6. 严禁输出 Markdown 代码块标记（如 ```json ），严禁附带解释或多余字符，直接输出标准 JSON 对象。\n\n"
            f"【输出示例】\n"
            f'{{"10001": {{"impression": "爱吐槽的加班打工人", "aliases": ["牢尤", "猫猫"]}}, "10002": {{"impression": "高频造梗气氛组", "aliases": ["咕师傅"]}}}}'
        )
        return prompt

    @staticmethod
    def _parse_group_impression_json(response_text: str) -> dict[str, dict]:
        """解析并清洗大模型返回的群印象与外号 JSON 结果。

        Args:
            response_text: 大模型返回的原始回复文本。

        Returns:
            {user_id: {"impression": str, "aliases": list[str]}} 字典。
        """
        if not response_text:
            return {}
        text = response_text.strip()
        text = re.sub(
            r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE
        )
        text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE).strip()

        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            text = json_match.group(0)

        try:
            data = json.loads(text)
        except Exception as e:
            logger.error(f"解析群印象 JSON 失败: {e}, 原文: {response_text[:300]}")
            return {}

        if not isinstance(data, dict):
            return {}

        result = {}
        blacklist = {
            "哥们", "大哥", "兄弟", "老板", "佬", "群主", "管理", "管理员",
            "机器人", "大家", "群友", "有人", "楼主", "群友们", "某人", "本人", "自己",
            "self", "me", "admin", "bot",
        }
        for uid, raw_val in data.items():
            uid_str = str(uid).strip()
            if not uid_str:
                continue

            raw_imp = ""
            raw_aliases = []
            if isinstance(raw_val, dict):
                raw_imp = str(raw_val.get("impression") or "")
                raw_aliases = raw_val.get("aliases") or []
            elif isinstance(raw_val, str):
                raw_imp = str(raw_val)

            cleaned_imp = re.sub(
                r"^(?:目标用户的?|该用户的?|用户)?(?:印象(?:是)?|特质|评价|画像)[:：\s]*",
                "",
                raw_imp.strip(),
            )
            cleaned_imp = cleaned_imp.strip("“”\"'‘’。，！! ")
            if len(cleaned_imp) > 10:
                cleaned_imp = cleaned_imp[:10]

            cleaned_aliases = []
            if isinstance(raw_aliases, list):
                for a in raw_aliases:
                    if not isinstance(a, str):
                        continue
                    clean_a = re.sub(r"[\[\]［］()（）\"'“”‘’\s]", "", a).strip()
                    if not clean_a or len(clean_a) > 10 or clean_a.isdigit():
                        continue
                    if clean_a.lower() in blacklist:
                        continue
                    if clean_a.lower() not in [x.lower() for x in cleaned_aliases]:
                        cleaned_aliases.append(clean_a)

            if cleaned_imp or cleaned_aliases:
                result[uid_str] = ImpressionResult(cleaned_imp, cleaned_aliases[:5])
        return result

    async def _run_daily_group_impression_batch(
        self, target_date: str | None = None
    ) -> dict[str, dict[str, str]]:
        """每日自动拉取各群聊天记录并批量分析活跃用户印象。

        Args:
            target_date: 目标分析日期 (YYYY-MM-DD)，若为 None 则默认取前一天。

        Returns:
            按群归类的印象生成结果：{group_id: {user_id: impression}}。
        """
        if not target_date:
            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        logger.info(f"开始执行每日群活跃用户印象分析批处理: 日期={target_date}")
        groups = await self.db_manager.get_active_groups_by_date(target_date)
        if not groups:
            logger.info(f"日期 {target_date} 没有群发言记录，跳过每日印象分析")
            return {}

        # 方案 2 (主活跃群决定论)：当开启全局好感度模式时，预先计算每个用户在哪一个群是主活跃群
        user_primary_group: dict[str, str] = {}
        if self.is_global_favour:
            user_activity = await self.db_manager.get_user_activity_across_groups(
                target_date
            )
            for uid, group_chars in user_activity.items():
                if group_chars:
                    primary_grp = max(group_chars.items(), key=lambda x: x[1])[0]
                    user_primary_group[uid] = primary_grp

        provider = self._get_impression_provider()
        if not provider:
            logger.error("未找到可用的 LLM Provider，每日印象分析终止")
            return {}

        total_results: dict[str, dict[str, str]] = {}

        for group_id in groups:
            if not self._is_favour_session_enabled(group_id):
                continue

            active_users = await self.db_manager.get_group_active_users_utterances(
                group_id=group_id,
                date=target_date,
                min_msg_count=self.daily_imp_min_messages,
                min_total_chars=self.daily_imp_min_chars,
            )

            # 在全局好感度模式下（方案2），若某用户当天在多个群活跃，只在主活跃群中进行分析与更新
            if self.is_global_favour:
                active_users = {
                    uid: data
                    for uid, data in active_users.items()
                    if user_primary_group.get(uid) == group_id
                }

            if not active_users:
                continue

            existing_impressions = {}
            for uid in active_users:
                session_key = "global" if self.is_global_favour else group_id
                record = await self.db_manager.get_favour(uid, session_key)
                if record and record.impression:
                    existing_impressions[uid] = record.impression

            existing_aliases = {}
            for uid in active_users:
                session_key = "global" if self.is_global_favour else group_id
                rec = await self.db_manager.get_favour(uid, session_key)
                if rec and rec.get_aliases():
                    existing_aliases[uid] = rec.get_aliases()

            prompt = self._build_group_impression_prompt(
                group_id=group_id,
                active_users=active_users,
                user_existing_impressions=existing_impressions,
                user_existing_aliases=existing_aliases,
            )

            timeout_seconds = float(
                getattr(self, "daily_imp_timeout", 180) or 180
            )
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(
                        contexts=[{"role": "user", "content": prompt}]
                    ),
                    timeout=timeout_seconds,
                )
                parsed_impressions = self._parse_group_impression_json(
                    response.completion_text or ""
                )

                group_res = {}
                for uid, item in parsed_impressions.items():
                    if uid in active_users:
                        impression = item.get("impression", "") if hasattr(item, "get") else str(item)
                        new_aliases = item.get("aliases", []) if hasattr(item, "get") else []
                        session_key = "global" if self.is_global_favour else group_id
                        record = await self.db_manager.get_favour(uid, session_key)

                        # 处理别名合并与锁定
                        merged_aliases = None
                        if record and record.aliases_locked:
                            merged_aliases = None
                        elif new_aliases:
                            old_aliases = record.get_aliases() if record else []
                            merged = list(old_aliases)
                            for a in new_aliases:
                                if a.lower() not in [x.lower() for x in merged]:
                                    merged.append(a)
                            merged_aliases = merged[-5:]

                        await self.db_manager.update_favour(
                            user_id=uid,
                            session_id=session_key,
                            impression=impression if impression else None,
                            aliases=merged_aliases,
                        )
                        if impression:
                            group_res[uid] = impression
                if group_res:
                    total_results[group_id] = group_res
                    logger.info(
                        f"群 {group_id} 印象分析完成，已更新 {len(group_res)} 位成员印象: {group_res}"
                    )
            except (asyncio.TimeoutError, TimeoutError):
                logger.error(
                    f"群 {group_id} 每日印象分析失败: 大模型调用超时 (超过 {int(timeout_seconds)} 秒)，建议在配置中调大 daily_impression_config.timeout 或更换响应更快的模型"
                )
            except Exception as e:
                logger.error(
                    f"群 {group_id} 每日印象分析失败: {type(e).__name__}: {e or '未知错误'}"
                )

        # 批处理完成后清理 3 天前的过期发言缓冲
        try:
            cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            cleaned_count = await self.db_manager.clean_expired_group_utterances(cutoff)
            if cleaned_count > 0:
                logger.info(f"已清理 {cleaned_count} 条过期发言缓冲记录 (< {cutoff})")
        except Exception as e:
            logger.error(f"清理过期发言记录失败: {e}")

        return total_results

    async def _record_completed_dialogue_round(
        self, event: AstrMessageEvent, bot_message: str
    ) -> bool:
        """消费当前事件暂存的用户输入，形成唯一的一问一答样本。"""

        if not event.get_extra("_favour_llm_response_received", False):
            return False
        if event.get_extra("_favour_dialogue_round_recorded", False):
            return False
        user_id = str(event.get_extra("_favour_dialogue_user_id", "") or "")
        session_id = str(event.get_extra("_favour_dialogue_session_id", "") or "")
        user_message = str(
            event.get_extra("_favour_dialogue_user_message", "") or ""
        ).strip()
        clean_bot_message = (bot_message or "").strip()
        if not user_id or not session_id or not user_message or not clean_bot_message:
            return False

        impression_refresh_required = await self._mark_dialogue_round_for_user(
            user_id,
            session_id,
            user_message,
            clean_bot_message,
        )
        # 只有轮次成功进入缓存和计数后才标记已消费，避免数据库读取异常留下伪成功状态。
        event.set_extra("_favour_dialogue_round_recorded", True)
        return impression_refresh_required

    def _get_target_uid(self, event: AstrMessageEvent, text_arg: str) -> str | None:
        """获取目标用户ID，支持At和纯文本"""
        # 1. 检查 At
        bot_self_id = None
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "self_id"):
            bot_self_id = str(event.message_obj.self_id)

        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for component in event.message_obj.message:
                if isinstance(component, At):
                    uid = str(component.qq)
                    if bot_self_id and uid == bot_self_id:
                        continue
                    return uid

        # 2. 检查文本参数
        if text_arg:
            cleaned_arg = text_arg.strip()
            if is_valid_userid(cleaned_arg):
                return cleaned_arg

        return None

    def _get_session_id(self, event: AstrMessageEvent) -> str | None:
        if self.is_global_favour:
            return "global"
        return event.unified_msg_origin

    def _escape_markdown(self, text: str) -> str:
        """转义 Markdown 特殊字符以防止表格错位或渲染错误"""
        if not text:
            return ""
        mapping = {
            "|": "&#124;",
            "`": "&#96;",
            "*": "&#42;",
            "~": "&#126;",
            "_": "&#95;",
            "[": "&#91;",
            "]": "&#93;",
            "\n": " ",  # 表格内不能有换行
        }
        for char, entity in mapping.items():
            text = text.replace(char, entity)
        return text

    async def _get_user_display_name(
        self, event: AstrMessageEvent, user_id: str
    ) -> str:
        try:
            group_id = event.get_group_id()
            if group_id:
                info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(user_id), no_cache=True
                )
                return info.get("card") or info.get("nickname") or user_id
            else:
                info = await event.bot.get_stranger_info(user_id=int(user_id))
                return info.get("nickname") or user_id
        except Exception:
            return user_id

    async def _check_permission(
        self, event: AstrMessageEvent, required_level: int
    ) -> bool:
        if str(event.get_sender_id()) in self.admins_id:
            return True
        if not isinstance(event, AiocqhttpMessageEvent):
            return False
        perm_mgr = PermissionManager.get_instance()
        level = await perm_mgr.get_perm_level(event, event.get_sender_id())
        return level >= required_level

    async def _get_initial_favour(self, event: AstrMessageEvent) -> int:
        user_id = str(event.get_sender_id())

        if not self.is_global_favour:
            global_rec = await self.db_manager.get_favour(user_id, "global")
            if global_rec:
                return max(
                    self.min_favour_value, min(self.max_favour_value, global_rec.favour)
                )

        is_envoy = str(user_id) in [str(e) for e in getattr(self, "favour_envoys", [])]
        is_admin = await self._check_permission(event, PermLevel.OWNER)

        admin_default = getattr(self, "admin_default_favour", 50)
        user_default = getattr(self, "default_favour", 0)
        base = admin_default if (is_envoy or is_admin) else user_default
        min_val = getattr(self, "min_favour_value", -100)
        max_val = getattr(self, "max_favour_value", 100)
        return max(min_val, min(max_val, base))

    async def _is_spam_behavior(
        self, user_id: str, message: str, event: AstrMessageEvent
    ) -> tuple[bool, float | None]:
        """已停用刷分检测机制"""
        return False, None

    async def _apply_penalty(self, user_id: str, session_id: str):
        """已停用惩罚机制"""
        return

    def _get_daily_favour_change(self, user_id: str) -> int:
        """获取用户今日好感度变化"""
        today = datetime.now().strftime("%Y-%m-%d")
        if user_id not in self.daily_favour_changes:
            self.daily_favour_changes[user_id] = {}
        return self.daily_favour_changes[user_id].get(today, 0)

    async def _update_daily_favour_change(self, user_id: str, change: int):
        """更新用户今日好感度变化，只保留当天的数据"""
        today = datetime.now().strftime("%Y-%m-%d")
        # 获取当前累计的变化值
        current = 0
        if user_id in self.daily_favour_changes:
            # 检查是否是今天的数据
            if today in self.daily_favour_changes[user_id]:
                current = self.daily_favour_changes[user_id][today]
        # 计算新的变化值
        new_change = current + change
        # 只保留当天的数据，覆盖前一天的数据
        self.daily_favour_changes[user_id] = {today: new_change}
        # 持久化到数据库
        await self.db_manager.update_daily_favour_change(user_id, today, new_change)

    def _is_within_daily_limit(self, user_id: str, change: int) -> bool:
        """检查是否在每日好感度限制内"""
        if change <= 0:
            return True  # 只限制增加
        current = self._get_daily_favour_change(user_id)
        return current + change <= self.max_daily_increase

    async def _sort_records(
        self, event: AstrMessageEvent, records: list[FavourRecord]
    ) -> list[FavourRecord]:
        """根据配置对记录进行排序"""
        if not records:
            return []

        if self.group_sort_by == "favour":
            return sorted(records, key=lambda x: x.favour, reverse=True)
        elif self.group_sort_by == "userid":
            return sorted(records, key=lambda x: x.user_id)
        elif self.group_sort_by == "nickname":
            enriched = []
            for r in records:
                name = await self._get_user_display_name(event, r.user_id)
                enriched.append((name, r))
            enriched.sort(key=lambda x: x[0].lower())
            return [x[1] for x in enriched]
        else:
            # default: 按添加时间 (created_at) 排序，如果没有则按 id
            return sorted(
                records, key=lambda x: x.created_at if x.created_at else datetime.min
            )

    async def _send_chunked_t2i(
        self,
        event: AstrMessageEvent,
        title: str,
        headers: list[str],
        rows: list[str],
        chunk_size: int = 200,
    ):
        """分块发送 T2I 图片"""
        total = len(rows)
        if total == 0:
            await event.send(event.plain_result(f"{title}\n暂无数据"))
            return

        for i in range(0, total, chunk_size):
            chunk = rows[i : i + chunk_size]
            page_user_ids: set[str] = set()
            for row in chunk:
                row_text = str(row).strip()
                if not row_text.startswith("|"):
                    continue
                parts = [part.strip() for part in row_text.split("|")[1:-1]]
                if len(parts) < 2:
                    continue
                user_id = parts[1]
                if user_id.isdigit():
                    page_user_ids.add(user_id)

            md_lines = [f"# {title}", ""]
            md_lines.extend(headers)
            md_lines.extend(chunk)

            md_text = "\n".join(md_lines)
            try:
                from pathlib import Path

                if self.template == "style2":
                    template_content = self._compose_style2_template()
                    avatar_data_map = self._load_style2_avatar_data_map(page_user_ids)

                    structured_rows = []
                    for row in chunk:
                        row_text = str(row).strip()
                        if not row_text.startswith("|"):
                            continue

                        parts = [part.strip() for part in row_text.split("|")[1:-1]]
                        if len(parts) < 6:
                            continue

                        fav_text = parts[2]
                        try:
                            fav_value = int(fav_text)
                        except ValueError:
                            fav_value = fav_text

                        minimal_record = SimpleNamespace(
                            user_id=parts[1],
                            favour=fav_value,
                            relationship=parts[3],
                            impression=parts[5],
                        )
                        structured_rows.append(
                            await self._build_style2_row_view_model(
                                event, minimal_record, avatar_data_map
                            )
                        )

                    render_data = {
                        "eyebrow": "Fia Affection Ledger",
                        "title": title,
                        "rows": structured_rows,
                    }
                else:
                    template_path = (
                        Path(__file__).parent
                        / "template"
                        / self.template
                        / "default.html"
                    )
                    template_content = template_path.read_text(encoding="utf-8")
                    render_data = {
                        "text": md_text,
                    }

                image_data = await self.html_render(
                    template_content,
                    render_data,
                    False,
                    {
                        "type": "jpeg",
                        "quality": 100,
                        "full_page": True,
                        "scale": "device",
                        "device_scale_factor_level": "high",
                    },
                )
                await self.image_sender.send_image(event, image_data)
            except Exception as e:
                logger.error(f"生成图片失败 (Page {i + 1}): {e}")
                await event.send(event.plain_result("生成图片失败，请检查日志。"))

    # ================= 事件处理 =================

    def _compose_style2_template(self) -> str:
        base_template = (
            Path(__file__).parent / "template" / "style2" / "base.html"
        ).read_text(encoding="utf-8")
        replacements = {
            "{{{ styles }}}": (
                Path(__file__).parent
                / "template"
                / "style2"
                / "partials"
                / "styles.html"
            ).read_text(encoding="utf-8"),
            "{{{ hero }}}": (
                Path(__file__).parent / "template" / "style2" / "partials" / "hero.html"
            ).read_text(encoding="utf-8"),
            "{{{ favour_table }}}": (
                Path(__file__).parent
                / "template"
                / "style2"
                / "partials"
                / "favour_table.html"
            ).read_text(encoding="utf-8"),
        }
        for placeholder, content in replacements.items():
            base_template = base_template.replace(placeholder, content)
        return base_template

    def _get_style2_score_palette(self, score: int) -> dict[str, str]:
        if score >= 100:
            return {
                "fill": "linear-gradient(90deg,#8fc9f3 0%,#d3d4f5 18%,#f2ddb0 42%,#f2c2d2 70%,#c9e7d9 100%)",
                "heart": "#d9bb86",
                "score_bg": "linear-gradient(135deg,#f8efd0,#edd8ec)",
                "score_color": "#6a5168",
                "badge_bg": "linear-gradient(135deg,rgba(201,231,217,.72),rgba(242,218,180,.72),rgba(225,211,248,.72))",
                "impression_bg": "linear-gradient(90deg,rgba(207,229,244,.78),rgba(242,213,224,.72))",
            }
        if score >= 60:
            return {
                "fill": "linear-gradient(90deg,#f2d29c,#efbfa7)",
                "heart": "#e9b87a",
                "score_bg": "rgba(252,236,211,.92)",
                "score_color": "#8a5937",
                "badge_bg": "linear-gradient(135deg,rgba(248,226,182,.78),rgba(248,215,205,.76))",
                "impression_bg": "linear-gradient(90deg,rgba(248,237,211,.86),rgba(247,220,197,.72))",
            }
        if score >= 20:
            return {
                "fill": "linear-gradient(90deg,#85c7ef,#98d9dc)",
                "heart": "#8cbde8",
                "score_bg": "rgba(224,241,251,.94)",
                "score_color": "#46729f",
                "badge_bg": "linear-gradient(135deg,rgba(203,228,248,.8),rgba(216,242,240,.74))",
                "impression_bg": "linear-gradient(90deg,rgba(216,235,246,.84),rgba(220,241,235,.72))",
            }
        if score < 0:
            return {
                "fill": "linear-gradient(90deg,#e49ab0,#efb4c4)",
                "heart": "#e7a0b4",
                "score_bg": "rgba(252,233,239,.94)",
                "score_color": "#9b4f67",
                "badge_bg": "linear-gradient(135deg,rgba(247,214,224,.78),rgba(242,230,235,.72))",
                "impression_bg": "linear-gradient(90deg,rgba(248,225,233,.82),rgba(243,232,238,.74))",
            }
        return {
            "fill": "linear-gradient(90deg,#d6cde2,#c7d3e4)",
            "heart": "#cbbfd8",
            "score_bg": "rgba(242,239,248,.92)",
            "score_color": "#6e6582",
            "badge_bg": "linear-gradient(135deg,rgba(231,228,241,.78),rgba(221,230,241,.72))",
            "impression_bg": "linear-gradient(90deg,rgba(238,236,246,.84),rgba(228,234,245,.74))",
        }

    def _load_style2_avatar_data_map(self, user_ids: set[str]) -> dict[str, str]:
        avatar_data_map: dict[str, str] = {}
        if not user_ids:
            return avatar_data_map

        avatar_cache_dir = (
            self.data_dir.parent
            / "astrbot_plugin_qq_group_daily_analysis"
            / "cache"
            / "avatars"
        )
        if not avatar_cache_dir.exists():
            return avatar_data_map

        for user_id in user_ids:
            avatar_candidates = sorted(avatar_cache_dir.glob(f"{user_id}_40.*"))
            if not avatar_candidates:
                continue
            avatar_file = avatar_candidates[0]
            try:
                mime_type = mimetypes.guess_type(avatar_file.name)[0] or "image/jpeg"
                encoded = base64.b64encode(avatar_file.read_bytes()).decode("ascii")
                avatar_data_map[user_id] = f"data:{mime_type};base64,{encoded}"
            except Exception as exc:
                logger.warning(f"璇诲彇澶村儚缂撳瓨澶辫触 {avatar_file}: {exc}")
        return avatar_data_map

    def _get_style2_avatar_background(self, name: str) -> str:
        palettes = [
            ("#e7c8df", "#f5e8c6"),
            ("#c9dcf8", "#d9f0f3"),
            ("#e0d6fb", "#f6d4df"),
            ("#c7ead8", "#f5ecd1"),
            ("#f3d6da", "#ddd7fb"),
            ("#d7e6ff", "#f7d2ca"),
        ]
        seed = sum(ord(char) for char in name or "")
        left, right = palettes[seed % len(palettes)]
        return f"linear-gradient(135deg,{left},{right})"

    async def _build_style2_row_view_model(self, event, record, avatar_data_map):
        user_id = str(record.user_id)
        name = await self._get_user_display_name(event, user_id)
        today_change = self._get_daily_favour_change(user_id)
        if today_change > 0:
            today_change_text = f"+{today_change}"
            today_change_state = "positive"
        elif today_change < 0:
            today_change_text = str(today_change)
            today_change_state = "negative"
        else:
            today_change_text = "0"
            today_change_state = "neutral"

        relationship_text = (record.relationship or "").strip() or "None"
        impression_text = (record.impression or "").strip() or "None"
        score_value = (
            int(record.favour)
            if isinstance(record.favour, int)
            else int(str(record.favour))
        )
        palette = self._get_style2_score_palette(score_value)
        avatar_src = avatar_data_map.get(user_id, "")
        avatar_fallback = (name or "?").replace(" ", "")[:2] or "?"

        return {
            "name": name,
            "user_id": user_id,
            "favour": score_value,
            "score_width": f"{min(100, max(0, abs(score_value)))}%",
            "relationship_text": relationship_text,
            "today_change_text": today_change_text,
            "today_change_state": today_change_state,
            "impression_text": impression_text,
            "avatar_src": avatar_src,
            "avatar_fallback": avatar_fallback,
            "avatar_background": self._get_style2_avatar_background(name),
            **palette,
        }

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def track_natural_dialogue_message(self, event: AstrMessageEvent):
        """暂存当前事件的自然用户输入，等待同一事件的最终机器人回复配对。"""

        try:
            message = event.message_str or ""
            if not self._is_natural_dialogue_message(message):
                return
            session_id = self._get_session_id(event)
            if not session_id or not self._is_favour_session_enabled(session_id):
                return

            event.set_extra("_favour_dialogue_user_id", str(event.get_sender_id()))
            event.set_extra("_favour_dialogue_session_id", session_id)
            event.set_extra("_favour_dialogue_user_message", message.strip())

            # 记录用户群内有效独立发言（用于每日群印象批量分析）
            if self.enable_daily_impression:
                try:
                    group_id = ""
                    if hasattr(event, "get_group_id"):
                        group_id = str(event.get_group_id() or "")
                    if not group_id and getattr(event, "unified_msg_origin", None):
                        origin = str(event.unified_msg_origin)
                        if "group" in origin.lower() or ":" in origin:
                            group_id = origin

                    sender_id = str(event.get_sender_id() or "")
                    bot_self_id = None
                    if hasattr(event, "message_obj") and hasattr(event.message_obj, "self_id"):
                        bot_self_id = str(event.message_obj.self_id)

                    if group_id and sender_id and sender_id != bot_self_id:
                        if self._is_favour_session_enabled(group_id):
                            clean_text = (message or "").strip()
                            if (
                                len(clean_text) >= 2
                                and not clean_text.startswith("/")
                                and not re.match(r"^https?://\S+$", clean_text)
                                and re.search(r"[\u4e00-\u9fa5a-zA-Z0-9]", clean_text)
                            ):
                                if len(clean_text) > 100:
                                    clean_text = clean_text[:100]
                                user_name = sender_id
                                if hasattr(event, "get_sender_name"):
                                    user_name = event.get_sender_name() or sender_id
                                date_str = datetime.now().strftime("%Y-%m-%d")
                                await self.db_manager.record_daily_group_utterance(
                                    group_id=group_id,
                                    user_id=sender_id,
                                    user_name=user_name,
                                    content=clean_text,
                                    date=date_str,
                                    max_daily_count=self.daily_imp_max_user_messages,
                                )
                except Exception as ex:
                    logger.error(f"记录群用户每日发言异常: {ex}")
        except Exception as e:
            logger.error(f"Track natural dialogue message failed: {e}")

    @filter.on_llm_request()
    async def inject_favour_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            session_id = self._get_session_id(event)
            user_id = str(event.get_sender_id())

            if not self._is_favour_session_enabled(session_id):
                return

            daily_change = self._get_daily_favour_change(user_id)
            daily_remaining_increase = max(0, self.max_daily_increase - daily_change)

            record = await self.db_manager.get_favour(user_id, session_id)
            if record:
                current_favour = record.favour
                current_relationship = record.relationship or "none"
            else:
                current_favour = await self._get_initial_favour(event)
                current_relationship = "none"

            increase_block_reasons = []
            if current_favour >= self.max_favour_value:
                increase_block_reasons.append("current favour already reached max")
            if daily_remaining_increase <= 0:
                increase_block_reasons.append(
                    "daily favour increase quota is exhausted"
                )
            can_increase_favour = not increase_block_reasons

            if str(user_id) in [str(admin_id) for admin_id in self.admins_id]:
                admin_status = "Bot管理员"
            elif await self._check_permission(event, PermLevel.OWNER):
                admin_status = "群主"
            elif await self._check_permission(event, PermLevel.ADMIN):
                admin_status = "管理员"
            else:
                admin_status = "普通用户"

            relationship_table_str = "无"
            if session_id != "global" and self.enable_relationship_table:
                records = await self.db_manager.get_all_in_session(session_id)
                rel_rows = []
                for r in records:
                    if r.relationship and r.user_id != user_id:
                        rel_rows.append(
                            f"- user_id: {r.user_id} | relationship: {r.relationship} | favour: {r.favour}"
                        )
                if rel_rows:
                    relationship_table_str = "\n".join(rel_rows)


            # 获取用户别名信息与当前会话群成员常用称呼映射
            user_aliases = record.get_aliases() if record else []
            user_aliases_str = ", ".join(user_aliases) if user_aliases else "暂无特定外号"

            alias_session_key = "global" if self.is_global_favour else session_id
            session_aliases_map = await self.db_manager.get_session_aliases_map(alias_session_key)
            if session_aliases_map:
                alias_rows = []
                for m_uid, m_data in list(session_aliases_map.items())[:15]:
                    m_name = m_data.get("user_name") or m_uid
                    m_aliases = ", ".join(m_data.get("aliases") or [])
                    alias_rows.append(f"- {m_name} (UID: {m_uid}): 常用称呼: [{m_aliases}]")
                session_aliases_table_str = "\n".join(alias_rows)
            else:
                session_aliases_table_str = "无"
            if self.favour_mode == "galgame":
                mode_instruction = (
                    "模式：Galgame。\n"
                    "行为影响规则：\n"
                    "1. 明确的夸奖、关心、安慰、支持、送礼、偏爱表达：默认视为正向互动。"
                    "Threshold: Low. Impact Direction: Increase. Impact Magnitude: Medium to High.\n"
                    "2. 轻松玩笑、撒娇、贴贴、熟人式打趣：如果语气友善且不冒犯，"
                    "默认视为亲密互动。Threshold: Low to Medium. Impact Direction: Flat or Slight Increase. "
                    "Impact Magnitude: Low to Medium.\n"
                    "3. 持续稳定的陪伴、认真接话、记住设定、顺着人设互动："
                    "视为高质量陪伴。Threshold: Medium. Impact Direction: Increase. Impact Magnitude: Medium.\n"
                    "4. 普通闲聊、简短回应、无明显情绪价值的日常互动："
                    "视为常规互动。Threshold: None. Impact Direction: Usually Flat. Impact Magnitude: Very Low.\n"
                    "5. 轻微失礼、冒失、笨拙示好：若整体仍带善意，可弱化负面判断。"
                    "Threshold: Medium. Impact Direction: Flat or Slight Decrease. Impact Magnitude: Low.\n"
                    "6. 明显冒犯、羞辱、命令口吻、恶意挑衅、越界要求："
                    "视为负向互动。Threshold: Low. Impact Direction: Decrease. Impact Magnitude: Medium to High.\n"
                    "7. 如果用户已经处于较高好感阶段，对亲密表达和偏爱互动的接受度可以提高，"
                    "同样行为更容易触发上升；但仍不能无条件大幅上涨。\n"
                )
            else:
                mode_instruction = (
                    "模式：现实向。\n"
                    "行为影响规则：\n"
                    "1. 基本礼貌、正常问候、普通聊天：视为常规互动。"
                    "Threshold: None. Impact Direction: Usually Flat. Impact Magnitude: Very Low.\n"
                    "2. 真诚关心、尊重边界、认真倾听、长期稳定陪伴："
                    "视为正向互动。Threshold: Medium. Impact Direction: Increase. Impact Magnitude: Low to Medium.\n"
                    "3. 明显帮助、坚定支持、关键时刻安慰、持续提供情绪价值："
                    "视为高质量正向互动。Threshold: Medium to High. Impact Direction: Increase. Impact Magnitude: Medium.\n"
                    "4. 轻浮调情、过早亲密、强行拉近关系、越过当前关系边界的表达："
                    "视为越界互动。Threshold: Low. Impact Direction: Flat or Decrease. Impact Magnitude: Low to Medium.\n"
                    "5. 粗鲁、阴阳怪气、冒犯、讽刺、逼迫、否定感受："
                    "视为负向互动。Threshold: Low. Impact Direction: Decrease. Impact Magnitude: Medium to High.\n"
                    "6. 恶意攻击、羞辱、持续施压、触碰底线："
                    "视为严重负向互动。Threshold: Very Low. Impact Direction: Strong Decrease. Impact Magnitude: High.\n"
                    "7. 即使是正向行为，也需要一定连续性和质量才能稳定提升好感，"
                    "不能因为一次普通示好就大幅上涨。\n"
                )

            increase_block_reason_text = (
                "; ".join(increase_block_reasons) if increase_block_reasons else "无"
            )

            prompt_final = f"""# 好感度主链路

你在这次回复中只负责两件事：
1. 自然回复用户最新消息
2. 判断好感度变化

禁止事项：
- 不要生成任何印象、画像、总结
- 不要输出 `[印象：...]`
- 不要输出除好感度标签以外的额外系统标签
- 不要暴露你的推理过程
- 如果当前不允许好感度上升，则绝对不能输出好感度上升

用户信息：
- 用户ID：{user_id}
- 管理员状态：{admin_status}
- 当前好感度：{current_favour}
- 好感度上限：{self.max_favour_value}
- 当前关系：{current_relationship}
- 用户的常用外号/称呼：{user_aliases_str}

当前会话关系表：
{relationship_table_str}

当前会话成员常用称呼/外号表：
{session_aliases_table_str}

称呼与指代指引：
1. 当其他成员在聊天中提及上述外号或称呼时，理解其指代对应 UID 成员及其好感背景；
2. 你在回复中若提及这些成员，可自然、亲切地选用这些常用称呼指代他们。

互动模式说明：
{mode_instruction}

附加好感规则：
{self.favour_rule_prompt or "无"}

限制条件：
- 好感度上升范围：{self.favour_increase_min}-{self.favour_increase_max}
- 好感度下降范围：{self.favour_decrease_min}-{self.favour_decrease_max}
- 今日已增加好感度：{daily_change}
- 今日剩余可增加好感度：{daily_remaining_increase}
- 当前是否允许好感度上升：{"是" if can_increase_favour else "否"}
- 禁止上升原因：{increase_block_reason_text}

【输出格式强制要求】：
1. 先正常回复用户的最新消息（保持人设口吻）；
2. 回复正文完成后，必须单独换一行输出好感度变动标签（必须三选一，若无明显变动必须输出 [好感度持平]，严禁省略该标签）：
[好感度上升：X]
or [好感度降低：Y]
or [好感度持平]
3. 严禁输出 `[印象：...]`，严禁输出任何刷分或额外系统标记，不要暴露任何推理说明，严格确保末尾标签完整。
"""
            existing_sp = (getattr(req, "system_prompt", None) or "").strip()
            if existing_sp:
                req.system_prompt = f"{existing_sp}\n\n{prompt_final}".strip()
            else:
                req.system_prompt = prompt_final
        except Exception as e:
            logger.error(
                f"Inject favour prompt failed: {str(e)}\n{traceback.format_exc()}"
            )

    @filter.on_llm_response()
    async def handle_llm_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        if not hasattr(event, "message_obj"):
            return
        msg_id = str(event.message_obj.message_id)
        text = resp.completion_text
        if text and text.strip():
            # 仅由实际经过 AstrBot 回复链路的 LLM 响应解锁印象轮次记录。
            event.set_extra("_favour_llm_response_received", True)

        update_data = {
            "change": 0,
            "found": False,
            "is_spam": False,
        }

        # 提取好感度变化
        matches = self.favour_pattern.findall(text)
        for m in matches:
            val = 0
            num = re.search(r"(\d+)", m)
            if num:
                val = int(num.group(1))

            if "降低" in m:
                update_data["change"] = -val
                update_data["found"] = True
            elif "上升" in m:
                update_data["change"] = val
                update_data["found"] = True
            elif "持平" in m:
                update_data["change"] = 0
                update_data["found"] = True

        # 提取刷分检测结果
        spam_match = re.search(
            r"[\[［]\s*(?:刷分|防刷)(?:检测)?\s*[:：]?\s*(是|否|true|false|yes|no|有|无|正常)?\s*[\]］]",
            text,
            re.IGNORECASE,
        )
        if spam_match:
            val = (spam_match.group(1) or "").lower()
            update_data["is_spam"] = val in ("是", "true", "yes", "有")

        if update_data["found"]:
            self.pending_updates[msg_id] = update_data
        elif text and len(text.strip()) > 0:
            # 优雅兜底：若 LLM 偶尔漏附标签，自动按“好感度持平”处理，由 update_data 保持原好感不变并持久化对话轮次
            logger.debug(
                f"LLM回复未显式包含好感度标签，自动按好感度持平处理 (MsgID: {msg_id})"
            )

    @filter.on_decorating_result(priority=10)
    async def update_data(self, event: AstrMessageEvent):
        if not hasattr(event, "message_obj"):
            return
        msg_id = str(event.message_obj.message_id)
        data = self.pending_updates.pop(msg_id, None)

        res = event.get_result()
        new_chain = []
        for comp in res.chain:
            if isinstance(comp, Plain) and comp.text:
                # 清理所有特殊标签
                t = self.favour_pattern.sub("", comp.text)
                spam_re = getattr(self, "spam_pattern", None)
                if spam_re:
                    t = spam_re.sub("", t)
                else:
                    t = re.sub(r"[\[［][^\[\]［］]*?(?:刷分|防刷)[^\[\]［］]*?[\]］]", "", t)
                t = re.sub(r"[\[［][^\[\]［］]*?印象[^\[\]［］]*?[\]］]", "", t)
                t = self.relationship_pattern.sub("", t)
                # 清理空行
                t = "\n".join([line for line in t.split("\n") if line.strip()])
                if t.strip():
                    new_chain.append(Plain(t))
            else:
                new_chain.append(comp)
        res.chain = new_chain

        bot_reply_text = self._extract_plain_text_from_result(event)
        impression_refresh_required = False
        if bot_reply_text:
            # 先计入本轮，确保后续主写库同步保存最新自然对话轮数；
            # 后台刷新则必须等写库成功后再调度，避免新印象被旧记录覆盖。
            impression_refresh_required = await self._record_completed_dialogue_round(
                event, bot_reply_text
            )

        if not data:
            # 即使好感度控制标签解析失败，也保留已经真实完成的一问一答。
            user_id = str(event.get_extra("_favour_dialogue_user_id", "") or "")
            session_id = str(event.get_extra("_favour_dialogue_session_id", "") or "")
            dialogue_round_count = self.pending_dialogue_round_updates.get(
                (user_id, session_id)
            )
            if dialogue_round_count is not None:
                update_success = await self.db_manager.update_favour(
                    user_id,
                    session_id,
                    dialogue_round_count=dialogue_round_count,
                )
                if not update_success:
                    logger.error(
                        "保存自然对话轮数失败: user_id=%s session_id=%s count=%s",
                        user_id,
                        session_id,
                        dialogue_round_count,
                    )
                    return
            if impression_refresh_required and not self.enable_daily_impression:
                self._schedule_impression_refresh(user_id, session_id)
            return

        try:
            user_id = str(event.get_sender_id())
            session_id = self._get_session_id(event)
            llm_suggested_change = data["change"]
            adjusted_reason = ""

            record = await self.db_manager.get_favour(user_id, session_id)
            old_fav = record.favour if record else await self._get_initial_favour(event)

            # 检查每日好感度限制
            if not self._is_within_daily_limit(user_id, data["change"]):
                # 超过每日限制，只取剩余可增加的好感度
                remaining = self.max_daily_increase - self._get_daily_favour_change(
                    user_id
                )
                if remaining > 0:
                    data["change"] = remaining
                    adjusted_reason = f"每日上限限制为剩余 {remaining}"
                else:
                    data["change"] = 0
                    adjusted_reason = "今日好感度上涨额度已耗尽"

            new_fav = old_fav + data["change"]
            new_fav = max(self.min_favour_value, min(self.max_favour_value, new_fav))
            data["change"] = new_fav - old_fav
            if llm_suggested_change > 0 and data["change"] == 0 and not adjusted_reason:
                if old_fav >= self.max_favour_value:
                    adjusted_reason = "当前好感度已达上限"
                else:
                    adjusted_reason = "系统规则将上涨修正为持平"

            # 保持现有关系
            rel = self.db_manager._get_relationship(new_fav)

            # 更新互动次数
            current_interact_count = record.interact_count + 1 if record else 1

            # 确定最终的互动次数
            interact_count = current_interact_count
            dialogue_round_count = self.pending_dialogue_round_updates.get(
                (user_id, session_id),
                record.dialogue_round_count if record else 0,
            )

            sender_name = ""
            if hasattr(event, "get_sender_name"):
                sender_name = event.get_sender_name() or ""

            # 更新数据库
            update_success = await self.db_manager.update_favour(
                user_id,
                session_id,
                favour=new_fav,
                relationship=rel,
                is_unique=False,
                interact_count=interact_count,
                dialogue_round_count=dialogue_round_count,
                nickname=sender_name,
            )
            if not update_success:
                raise RuntimeError(
                    f"主好感度数据写入失败: user_id={user_id} session_id={session_id}"
                )
            if impression_refresh_required and not self.enable_daily_impression:
                self._schedule_impression_refresh(user_id, session_id)

            # 更新每日好感度变化记录
            await self._update_daily_favour_change(user_id, data["change"])

            log_msg = f"用户 {user_id} (会话 {session_id}) 数据更新: 好感度 {old_fav}->{new_fav} (Δ{data['change']})"
            if llm_suggested_change != data["change"]:
                log_msg += (
                    f", LLM原始建议变化 {llm_suggested_change}"
                    f" 被调整为 {data['change']}"
                )
                if adjusted_reason:
                    log_msg += f"（原因：{adjusted_reason}）"
            logger.info(log_msg)

        except Exception as e:
            logger.error(f"更新好感度数据失败: {str(e)}\n{traceback.format_exc()}")

    # ================= 0. LLM 实时称呼工具 =================

    @filter.llm_tool(name="record_member_nickname")
    async def record_member_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str,
        target_user: str = "self",
        user_id: str = "",
    ) -> str:
        """当用户在对话中明确告知机器人某位用户的称呼、外号、昵称（例如：'以后叫我阿强'、'尤约拉猫叫猫猫'、'他叫老李'）时调用此工具，将其记录至数据库。

        Args:
            nickname(string): 要记录的称呼/外号/昵称（如：'阿强'、'猫猫'、'老李'）。
            target_user(string): 目标用户的名称或代指。若用户指代自己（如'叫我xx'），传'self'；若指代他人，传对方的群名片/昵称或描述。
            user_id(string, optional): 若消息中能明确推断出目标用户的数字UID，则传入；否则留空由系统自动解析。
        """
        try:
            clean_nick = re.sub(r"[\[\]［］()（）\"'“”‘’\s]", "", str(nickname or "")).strip()
            if not clean_nick or len(clean_nick) > 10:
                return "称呼长度需在1到10个字符之间"
            if clean_nick.isdigit():
                return "称呼不能全为纯数字"
            if clean_nick.lower() in {
                "哥们", "大哥", "兄弟", "老板", "佬", "群主", "管理", "管理员",
                "机器人", "大家", "群友", "有人", "楼主", "群友们", "某人", "本人", "自己",
                "self", "me", "admin", "bot",
            }:
                return f"'{clean_nick}' 属于通用社交代词，不能作为特定用户的专属外号"

            target_uid = ""
            target_name = ""

            target_desc = str(target_user or "").strip().lower()
            sender_id = str(event.get_sender_id() or "")
            sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else sender_id

            if target_desc in ("self", "me", "我", "自己", "本人") or (not target_desc and not user_id):
                target_uid = sender_id
                target_name = sender_name or target_uid
            else:
                # 1. 检查消息链中是否有 At 组件
                if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
                    for comp in event.message_obj.message:
                        if isinstance(comp, At) and getattr(comp, "qq", None):
                            at_uid = str(comp.qq)
                            if at_uid != str(getattr(event.message_obj, "self_id", "")):
                                target_uid = at_uid
                                break

                # 2. 检查是否传入显式 user_id
                if not target_uid and user_id and str(user_id).strip().isdigit():
                    target_uid = str(user_id).strip()

                # 3. 在当前会话记录中模糊匹配名称
                session_id = self._get_session_id(event)
                if not target_uid and target_user:
                    records = await self.db_manager.get_all_in_session(session_id)
                    for r in records:
                        if r.nickname and (target_user in r.nickname or r.nickname in target_user):
                            target_uid = str(r.user_id)
                            target_name = r.nickname
                            break

                if not target_uid:
                    return f"未能定位目标用户 '{target_user}'，请@对方或提供对方的群名片/UID"

            if not target_name:
                target_name = await self._get_user_display_name(event, target_uid)

            # 确定持久化会话键（遵循全局模式设定）
            session_key = "global" if self.is_global_favour else self._get_session_id(event)
            record = await self.db_manager.get_favour(target_uid, session_key)

            if record and record.aliases_locked:
                return f"用户 {target_name} ({target_uid}) 的称呼已被管理员锁定，无法通过聊天修改，请在管理面板中调整。"

            existing_aliases = record.get_aliases() if record else []
            merged = [a for a in existing_aliases if a.lower() != clean_nick.lower()]
            merged.append(clean_nick)
            merged = merged[-5:]

            await self.db_manager.update_favour(
                user_id=target_uid,
                session_id=session_key,
                nickname=target_name,
                aliases=merged,
            )
            return f"成功为用户 {target_name} ({target_uid}) 记录称呼: {clean_nick}，当前全部称呼: [{', '.join(merged)}]"
        except Exception as e:
            logger.error(f"LLM工具 record_member_nickname 执行失败: {e}\n{traceback.format_exc()}")
            return f"记录称呼失败: {e}"


    # ================= 1. 查询类型 =================

    @filter.command(
        "好感度", alias={"查询好感度", "查好感度", "好感度查询", "查看好感度"}
    )
    async def query_favour(self, event: AstrMessageEvent, target: str = ""):
        """查询自己或他人的好感度"""
        target_uid = self._get_target_uid(event, target) or str(event.get_sender_id())
        session_id = self._get_session_id(event)

        record = await self.db_manager.get_favour(target_uid, session_id)
        fav = (
            record.favour
            if record
            else (
                await self._get_initial_favour(event)
                if target_uid == str(event.get_sender_id())
                else 0
            )
        )
        rel = self.db_manager._get_relationship(fav)

        name = await self._get_user_display_name(event, target_uid)

        # 获取今日好感度变化
        today_change = self._get_daily_favour_change(target_uid)
        # 格式化今日好感度变化，正数绿色，负数红色
        if today_change > 0:
            change_str = f"+{today_change}"
        elif today_change < 0:
            change_str = f"{today_change}"
        else:
            change_str = "0"
        msg = f"🔍 用户：{name}\n🆔 ID：{target_uid}\n❤ 好感度：{fav}\n🔗 关系：{rel}\n� 今日好感度变化：{change_str}\n💭 印象：{record.impression if record and record.impression else '无'}"
        yield event.plain_result(msg)

    @filter.command(
        "当前好感度",
        alias={
            "查询当前好感度",
            "查当前好感度",
            "查询本群好感度",
            "查本群好感度",
            "查群好感度",
            "查询群好感度",
            "本群好感度",
            "群好感度",
        },
    )
    async def query_current_session_favour(
        self, event: AstrMessageEvent, page: int = 1
    ):
        """查询当前会话的所有好感度记录 (支持分页)"""
        if self.is_global_favour:
            yield event.plain_result(
                "当前为全局模式，此命令无效。请使用【全局好感度】。"
            )
            return

        session_id = self._get_session_id(event)
        records = await self.db_manager.get_all_in_session(session_id)

        if not records:
            yield event.plain_result("当前会话暂无好感度记录。")
            return

        records = await self._sort_records(event, records)

        page_size = 20
        total_records = len(records)
        total_pages = (total_records + page_size - 1) // page_size
        if page < 1:
            page = 1
        if page > total_pages and total_pages > 0:
            page = total_pages

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_records = records[start_idx:end_idx]

        headers = [
            "| 用户昵称 | 用户ID | 好感度 | 关系 | 今日好感度变化 | 印象 |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ]
        rows = []
        for r in page_records:
            name = self._escape_markdown(
                await self._get_user_display_name(event, r.user_id)
            )
            rel = self._escape_markdown(self.db_manager._get_relationship(r.favour))
            # 获取今日好感度变化
            today_change = self._get_daily_favour_change(r.user_id)
            # 日志：调试今日好感度变化
            logger.info(f"用户 {r.user_id}: today_change={today_change}")
            # 格式化今日好感度变化
            if today_change > 0:
                change_str = f"+{today_change}"
                logger.info(f"用户 {r.user_id}: change_str={change_str}")
            elif today_change < 0:
                change_str = f"{today_change}"
                logger.info(f"用户 {r.user_id}: change_str={change_str}")
            else:
                change_str = "0"
                logger.info(f"用户 {r.user_id}: change_str={change_str}")
            # 印象字数限制为10字
            impression = self._escape_markdown(r.impression or "无")
            if len(impression) > 10:
                impression = impression[:10] + "..."
            rows.append(
                f"| {name} | {r.user_id} | {r.favour} | {rel} | {change_str} | {impression} |"
            )

        title = f"好感度记录 - 第 {page}/{total_pages} 页"
        await self._send_chunked_t2i(event, title, headers, rows)

    @filter.command(
        "全部好感度", alias={"查询全部好感度", "查全部好感度", "查看全部好感度"}
    )
    async def query_all_sessions_favour(self, event: AstrMessageEvent):
        """查询所有非全局会话的好感度 (仅Bot管理员)"""
        if not await self._check_permission(event, PermLevel.SUPERUSER):
            yield event.plain_result("权限不足！仅Bot管理员可用。")
            return

        records = await self.db_manager.get_non_global_records()
        if not records:
            yield event.plain_result("暂无非全局好感度记录。")
            return

        is_current_private = not event.get_group_id()

        session_groups = {}
        for r in records:
            if r.session_id not in session_groups:
                session_groups[r.session_id] = []
            session_groups[r.session_id].append(r)

        headers = [
            "| 用户昵称 | 用户ID | 好感度 | 关系 | 今日好感度变化 | 印象 |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ]
        rows = []
        hidden_private_sessions = 0

        for sid, group_records in session_groups.items():
            is_private_session = "private" in str(sid)
            if is_private_session and not is_current_private:
                hidden_private_sessions += 1
                continue

            group_records = await self._sort_records(event, group_records)

            rows.append(
                f"\n## 会话: {self._escape_markdown(str(sid))} (共 {len(group_records)} 人)"
            )
            rows.append(headers[0])
            rows.append(headers[1])

            count = len(group_records)
            if count <= 10:
                display_list = group_records
            else:
                display_list = group_records[:5] + [None] + group_records[-5:]

            for r in display_list:
                if r is None:
                    rows.append("| ... | ... | ... | ... | ... | ... |")
                else:
                    name = self._escape_markdown(
                        await self._get_user_display_name(event, r.user_id)
                    )
                    rel = self._escape_markdown(
                        self.db_manager._get_relationship(r.favour)
                    )
                    # 获取今日好感度变化
                    today_change = self._get_daily_favour_change(r.user_id)
                    # 格式化今日好感度变化，正数绿色，负数红色
                    if today_change > 0:
                        change_str = f"<font color='green'>+{today_change}</font>"
                    elif today_change < 0:
                        change_str = f"<font color='red'>{today_change}</font>"
                    else:
                        change_str = "0"
                    # 印象字数限制为10字
                    impression = self._escape_markdown(r.impression or "无")
                    if len(impression) > 10:
                        impression = impression[:10] + "..."
                    rows.append(
                        f"| {name} | {r.user_id} | {r.favour} | {rel} | {change_str} | {impression} |"
                    )

        if hidden_private_sessions > 0:
            rows.append(
                f"\n> 另有 {hidden_private_sessions} 个私聊会话的数据已隐藏（仅在私聊查询时显示）。"
            )

        await self._send_chunked_t2i(event, "好感度记录", [], rows)

    @filter.command(
        "全局好感度",
        alias={"查询全局好感度", "查全局好感度", "查看全局好感度", "全局好感度查询"},
    )
    async def query_global_favour(self, event: AstrMessageEvent, page: int = 1):
        """查询全局模式下的好感度 (支持分页)"""
        # 移除权限检查，允许普通成员使用

        records = await self.db_manager.get_global_records()
        if not records:
            yield event.plain_result("暂无全局好感度记录。")
            return

        # 检查是否是群聊事件，只显示当前群聊的好感度记录
        group_id = event.get_group_id()
        if group_id:
            try:
                # 获取群成员列表
                members = await event.bot.get_group_member_list(group_id=int(group_id))
                # 提取用户ID列表（转换为字符串）
                group_user_ids = [str(member["user_id"]) for member in members]
                # 过滤记录，只保留群成员
                records = [r for r in records if r.user_id in group_user_ids]
                if not records:
                    yield event.plain_result("当前群聊暂无好感度记录。")
                    return
            except Exception as e:
                logger.error(f"获取群成员列表失败: {e}", exc_info=True)
                # 如果获取失败，继续显示所有记录，避免功能中断
                pass

        records = await self._sort_records(event, records)

        page_size = 20
        total_records = len(records)
        total_pages = (total_records + page_size - 1) // page_size
        if page < 1:
            page = 1
        if page > total_pages and total_pages > 0:
            page = total_pages

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_records = records[start_idx:end_idx]

        headers = [
            "| 用户昵称 | 用户ID | 好感度 | 关系 | 今日好感度变化 | 印象 |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ]
        rows = []
        for r in page_records:
            name = self._escape_markdown(
                await self._get_user_display_name(event, r.user_id)
            )
            rel = self._escape_markdown(self.db_manager._get_relationship(r.favour))
            # 获取今日好感度变化
            today_change = self._get_daily_favour_change(r.user_id)
            # 格式化今日好感度变化
            if today_change > 0:
                change_str = f"+{today_change}"
            elif today_change < 0:
                change_str = f"{today_change}"
            else:
                change_str = "0"
            # 印象字数限制为10字
            impression = self._escape_markdown(r.impression or "无")
            if len(impression) > 10:
                impression = impression[:10] + "..."
            rows.append(
                f"| {name} | {r.user_id} | {r.favour} | {rel} | {change_str} | {impression} |"
            )

        title = f"好感度记录 - 第 {page}/{total_pages} 页"
        await self._send_chunked_t2i(event, title, headers, rows)

    # ================= 2. 修改类型 =================

    @filter.command("修改好感度")
    async def modify_favour(self, event: AstrMessageEvent, target: str, value: int):
        """修改好感度: /修改好感度 @用户 50 (群管理员)"""
        if not await self._check_permission(event, PermLevel.ADMIN):
            yield event.plain_result("权限不足！需要群管理员及以上权限。")
            return

        uid = self._get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        session_id = self._get_session_id(event)
        try:
            # 检查用户是否存在
            record = await self.db_manager.get_favour(uid, session_id)
            if not record:
                yield event.plain_result(f"用户 {uid} 不存在于好感度列表中，无法修改。")
                return
            # 获取当前好感度值
            old_fav = record.favour
            # 计算变化值
            change = value - old_fav
            # 更新好感度
            await self.db_manager.update_favour(uid, session_id, favour=value)
            # 更新每日好感度变化记录
            await self._update_daily_favour_change(uid, change)
            yield event.plain_result(f"已将用户 {uid} 的好感度修改为 {value}。")
            logger.info(
                f"管理员 {event.get_sender_id()} 修改用户 {uid} 好感度为 {value} (Δ{change})"
            )
        except Exception as e:
            logger.error(f"修改好感度失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    @filter.command("全局修改好感度")
    async def global_modify_favour(
        self, event: AstrMessageEvent, target: str, value: int
    ):
        """全局修改好感度 (Bot管理员)"""
        if not await self._check_permission(event, PermLevel.SUPERUSER):
            yield event.plain_result("权限不足！仅Bot管理员可用。")
            return

        uid = self._get_target_uid(event, target)
        if not uid:
            return

        try:
            # 检查用户是否存在
            records = await self.db_manager.get_user_records(uid)
            if not records:
                yield event.plain_result(f"用户 {uid} 不存在于好感度列表中，无法修改。")
                return
            # 获取任意一个记录的当前好感度值（用于计算变化）
            old_fav = records[0].favour
            # 计算变化值
            change = value - old_fav
            # 更新所有记录
            count = await self.db_manager.update_user_all_records(uid, favour=value)
            # 更新每日好感度变化记录
            await self._update_daily_favour_change(uid, change)
            yield event.plain_result(
                f"已更新用户 {uid} 在所有会话中的好感度为 {value} (共 {count} 条记录)。"
            )
            logger.info(
                f"Bot管理员 {event.get_sender_id()} 全局修改用户 {uid} 好感度为 {value} (Δ{change})"
            )
        except Exception as e:
            logger.error(f"全局修改好感度失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    @filter.command("跨会话修改")
    async def cross_session_modify(
        self,
        event: AstrMessageEvent,
        target_sid: str,
        operation: str,
        target_uid: str,
        arg1: str = "",
        arg2: str = "",
    ):
        """跨会话修改数据 (Bot管理员)"""
        if not await self._check_permission(event, PermLevel.SUPERUSER):
            yield event.plain_result("权限不足！仅Bot管理员可用。")
            return

        if not target_sid or not operation or not target_uid:
            yield event.plain_result("参数错误。请查看帮助。")
            return

        if not is_valid_userid(target_uid):
            yield event.plain_result(f"用户ID {target_uid} 格式无效。")
            return

        try:
            if operation == "修改好感度":
                val = int(arg1)
                # 检查用户是否存在
                record = await self.db_manager.get_favour(target_uid, target_sid)
                if not record:
                    yield event.plain_result(
                        f"用户 {target_uid} 在会话 {target_sid} 中不存在，无法修改。"
                    )
                    return
                # 获取当前好感度值
                old_fav = record.favour
                # 计算变化值
                change = val - old_fav
                # 更新好感度
                await self.db_manager.update_favour(target_uid, target_sid, favour=val)
                # 更新每日好感度变化记录
                await self._update_daily_favour_change(target_uid, change)
                yield event.plain_result(
                    f"已将会话 {target_sid} 中用户 {target_uid} 的好感度修改为 {val}。"
                )
                logger.info(
                    f"Bot管理员 {event.get_sender_id()} 跨会话修改 {target_sid} 用户 {target_uid} 好感度为 {val} (Δ{change})"
                )

            else:
                yield event.plain_result(
                    f"未知操作: {operation}。支持的操作: 修改好感度"
                )
        except Exception as e:
            logger.error(f"跨会话修改失败: {e}")
            yield event.plain_result("操作失败，请检查日志。")

    # ================= 3. 清空类型 =================

    @filter.command("清空好感度")
    async def clear_user_favour(self, event: AstrMessageEvent, target: str):
        """清空指定用户好感度 (群主)"""
        if not await self._check_permission(event, PermLevel.OWNER):
            yield event.plain_result("权限不足！需要群主及以上权限。")
            return

        uid = self._get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        yield event.plain_result(
            f"⚠️ 警告：即将清空用户 {uid} 在当前会话的好感度数据。\n请在 30 秒内回复「确认清空」以继续，回复其他内容取消。"
        )

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空":
                sid = self._get_session_id(evt)
                record = await self.db_manager.get_favour(uid, sid)
                if record:
                    backup_file = await self.db_manager.backup_data(
                        [record], f"backup_user_{uid}_{sid}"
                    )
                    await self.db_manager.delete_favour(uid, sid)
                    await evt.send(
                        evt.plain_result(f"✅ 已清空用户 {uid} 的好感度数据。")
                    )
                    logger.info(
                        f"管理员 {evt.get_sender_id()} 清空了用户 {uid} 在会话 {sid} 的好感度\n备份文件已保存至: {backup_file}"
                    )
                else:
                    await evt.send(evt.plain_result("该用户在当前会话无好感度记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    @filter.command("清空当前好感度")
    async def clear_current_favour(self, event: AstrMessageEvent):
        """清空当前会话好感度 (群主)"""
        if not await self._check_permission(event, PermLevel.OWNER):
            yield event.plain_result("权限不足！需要群主及以上权限。")
            return

        sid = self._get_session_id(event)
        yield event.plain_result(
            f"⚠️ 警告：即将清空当前会话 ({sid}) 的所有好感度数据。\n请在 30 秒内回复「确认清空」以继续，回复其他内容取消。"
        )

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空":
                records = await self.db_manager.get_all_in_session(sid)
                if records:
                    backup_file = await self.db_manager.backup_data(
                        records, f"backup_session_{sid}"
                    )
                    await self.db_manager.clear_session(sid)
                    await evt.send(
                        evt.plain_result("✅ 已清空当前会话的所有好感度数据。")
                    )
                    logger.info(
                        f"管理员 {evt.get_sender_id()} 清空了会话 {sid} 的所有好感度\n备份文件已保存至: {backup_file}"
                    )
                else:
                    await evt.send(evt.plain_result("当前会话无好感度记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    @filter.command("清空全局好感度")
    async def clear_all_favour(self, event: AstrMessageEvent):
        """清空所有好感度 (Bot管理员)"""
        if not await self._check_permission(event, PermLevel.SUPERUSER):
            yield event.plain_result("权限不足！仅Bot管理员可用。")
            return

        yield event.plain_result(
            "🚨 极度危险：即将清空数据库中【所有】好感度数据！\n请在 30 秒内回复「确认清空所有数据」以继续，回复其他内容取消。"
        )

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空所有数据":
                records = (
                    await self.db_manager.get_global_records()
                    + await self.db_manager.get_non_global_records()
                )
                if records:
                    backup_file = await self.db_manager.backup_data(
                        records, "backup_all_database"
                    )
                    await self.db_manager.clear_all()
                    await evt.send(evt.plain_result("✅ 已清空所有好感度数据。"))
                    logger.warning(
                        f"Bot管理员 {evt.get_sender_id()} 清空了所有好感度数据！\n备份文件已保存至: {backup_file}"
                    )
                else:
                    await evt.send(evt.plain_result("数据库中无好感度记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    # ================= 4. 帮助类型 =================

    @filter.command("好感度帮助", alias={"查看好感度帮助"})
    async def help_menu(self, event: AstrMessageEvent):
        """显示可用命令菜单"""
        is_superuser = await self._check_permission(event, PermLevel.SUPERUSER)
        is_owner = await self._check_permission(event, PermLevel.OWNER)
        is_admin = await self._check_permission(event, PermLevel.ADMIN)

        msg = ["⭐ 好感度插件命令菜单 ⭐"]

        msg.append("\n[通用命令]")
        msg.append("- 好感度 [@用户]")
        msg.append("- 当前好感度 [页码]")
        msg.append("- 全局好感度 [页码]")
        msg.append("- 查询惩罚列表")
        msg.append("- 好感度指令帮助")

        if is_admin or is_superuser:
            msg.append("\n[管理员命令]")
            msg.append("- 修改好感度 @用户 <数值>")

        if is_owner or is_superuser:
            msg.append("\n[群主命令]")

            msg.append("- 清空好感度 @用户")
            msg.append("- 清空当前好感度")

        if is_superuser:
            msg.append("\n[Bot管理员命令]")
            msg.append("- 全部好感度")
            msg.append("- 全局修改好感度 @用户 <数值>")

            msg.append("- 跨会话修改 <sid> 修改好感度 <用户ID> <数值>")
            msg.append("- 清空全局好感度")

        yield event.plain_result("\n".join(msg))

    @filter.command("查询惩罚列表")
    async def query_penalty_list(self, event: AstrMessageEvent):
        """查询当前在惩罚期的用户列表"""
        yield event.plain_result("刷分检测与惩罚机制已被移除。")

    @filter.command("好感度指令帮助")
    async def help_usage(self, event: AstrMessageEvent):
        """显示详细指令用法"""
        msg = """⭐ 好感度指令用法示例 ⭐

1. 查询个人好感度
   用法: /好感度 [@用户或用户ID]
   示例: /好感度
   示例: /好感度 @糯米茨
   说明: 不填写目标时查询自己；旧指令 /查询好感度 仍可使用。

2. 查询当前会话排行
   用法: /当前好感度 [页码]
   示例: /当前好感度
   示例: /当前好感度 2
   说明: 全局模式下请改用 /全局好感度。

3. 查询全局排行
   用法: /全局好感度 [页码]
   示例: /全局好感度 1
   说明: 群聊中只显示当前群成员的全局记录。

4. 管理员修改好感度
   用法: /修改好感度 @用户 <数值>
   示例: /修改好感度 @糯米茨 60
   权限: 群管理员及以上
   说明: 目标用户必须已有当前会话的好感度记录。

5. 群主清空数据
   用法: /清空好感度 @用户
   用法: /清空当前好感度
   权限: 群主及以上
   说明: 清空前需要在 30 秒内回复确认文本，插件会自动备份数据。

6. Bot管理员全局维护
   用法: /全部好感度
   用法: /全局修改好感度 @用户 <数值>
   用法: /跨会话修改 <sid> 修改好感度 <用户ID> <数值>
   用法: /清空全局好感度
   示例: /跨会话修改 group:123456 修改好感度 10001 50
   说明: /全部好感度 查询非全局会话记录；/清空全局好感度 会清空数据库全部记录。

7. 惩罚状态
   用法: /查询惩罚列表
   说明: 查看当前因刷好感检测进入惩罚期的用户。

8. 每日群印象分析
   用法: /分析群印象 [日期YYYY-MM-DD]
   别名: /刷新群印象
   权限: 管理员及以上
   说明: 针对当前群拉取聊天记录并批量分析活跃成员印象，默认分析今天。
"""
        yield event.plain_result(msg)

    @filter.command("分析群印象", alias={"刷新群印象"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def analyze_group_impression_cmd(
        self, event: AstrMessageEvent, date_arg: str = ""
    ):
        """手动触发分析群活跃成员印象"""
        group_id = str(event.get_group_id() or event.unified_msg_origin or "")
        if not group_id:
            yield event.plain_result("请在群聊中使用此指令，或确保当前会话具有群ID。")
            return

        target_date = (
            date_arg.strip()
            if date_arg.strip()
            else datetime.now().strftime("%Y-%m-%d")
        )
        yield event.plain_result(
            f"正在分析群 {group_id} 在 {target_date} 的活跃成员印象..."
        )

        res = await self._run_daily_group_impression_batch(
            target_date=target_date
        )
        group_res = res.get(group_id, {})
        if not group_res:
            yield event.plain_result(
                f"群 {group_id} 在 {target_date} 未发现达到门槛的活跃用户或分析无产出。"
            )
        else:
            lines = [f"📊 群 {group_id} 印象分析完成："]
            for uid, imp in group_res.items():
                name = await self._get_user_display_name(event, uid)
                lines.append(f"- {name} ({uid}): {imp}")
            yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载或重载时优雅注销定时任务并释放数据库资源。"""
        try:
            if hasattr(self.context, "cron_manager"):
                jobs = await self.context.cron_manager.list_jobs(
                    job_type="basic"
                )
                for job in jobs:
                    if job.name in [
                        "daily_group_impression_analysis",
                        "favour_reset_weekly",
                    ]:
                        await self.context.cron_manager.delete_job(job.job_id)
            if hasattr(self, "db_manager"):
                await self.db_manager.close()
            logger.info("astrbot_plugin_Favour_Ultra 资源已成功释放")
        except Exception as e:
            logger.error(f"插件 terminate 异常: {e}")
