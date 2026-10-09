# storage.py
import asyncio
import json
from datetime import datetime
from pathlib import Path

from aiofiles import open as aio_open
from aiofiles.os import path as aio_path
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, SQLModel, delete, select, update

from astrbot.api import logger

from .utils import is_valid_userid


# 定义数据库模型
class FavourRecord(SQLModel, table=True):
    __tablename__ = "favour_records"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    nickname: str = Field(default="")
    session_id: str = Field(
        default="global", index=True
    )  # "global" 表示全局，或者具体的 session_id
    favour: int = Field(default=0)
    relationship: str = Field(default="")
    interact_count: int = Field(default=0)
    dialogue_round_count: int = Field(default=0)
    impression: str = Field(default="")
    is_unique: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


class DailyFavourChange(SQLModel, table=True):
    __tablename__ = "daily_favour_changes"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    date: str = Field(index=True)  # 格式: YYYY-MM-DD
    change: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


class DailyGroupUtterance(SQLModel, table=True):
    __tablename__ = "daily_group_utterances"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    user_id: str = Field(index=True)
    user_name: str = Field(default="")
    date: str = Field(index=True)  # 格式: YYYY-MM-DD
    content: str = Field(default="", max_length=300)
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


class FavourDBManager:
    """基于SQLite的好感度数据库管理器"""

    def __init__(self, data_dir: Path, min_val: int = -100, max_val: int = 100):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "favour.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.min_val = min_val
        self.max_val = max_val

        # 创建异步引擎
        self.engine = create_async_engine(self.db_url, echo=False)
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _get_relationship(self, favour: int) -> str:
        """根据好感度值自动计算关系标签"""
        if favour == 100:
            return "宿命回响"
        elif favour >= 60:
            return "亲密"
        elif favour >= 40:
            return "在意"
        elif favour >= 20:
            return "熟悉"
        elif favour >= 0:
            return "普通"
        elif favour >= -20:
            return "疏远"
        elif favour >= -40:
            return "冷淡"
        elif favour >= -60:
            return "反感"
        elif favour >= -80:
            return "厌恶"
        else:
            return "极度厌恶"

    async def init_db(self):
        """初始化数据库表并执行必要的迁移"""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            try:
                async with self.engine.begin() as conn:
                    # 检查表是否存在
                    result = await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='favour_records'"
                        )
                    )
                    table_exists = result.scalar() is not None

                    if not table_exists:
                        await conn.run_sync(SQLModel.metadata.create_all)
                    else:
                        # 检查是否缺少字段 (数据库升级逻辑)
                        result = await conn.execute(
                            text("PRAGMA table_info(favour_records)")
                        )
                        columns = [row[1] for row in result.fetchall()]

                        # 添加 created_at 字段（如果不存在）
                        if "created_at" not in columns:
                            logger.info("正在升级数据库：添加 created_at 字段...")
                            await conn.execute(
                                text(
                                    "ALTER TABLE favour_records ADD COLUMN created_at DATETIME"
                                )
                            )
                            await conn.execute(
                                text(
                                    "UPDATE favour_records SET created_at = updated_at WHERE created_at IS NULL"
                                )
                            )
                            logger.info("数据库升级完成：添加了 created_at 字段")

                        # 添加 interact_count 字段（如果不存在）
                        if "interact_count" not in columns:
                            logger.info("正在升级数据库：添加 interact_count 字段...")
                            await conn.execute(
                                text(
                                    "ALTER TABLE favour_records ADD COLUMN interact_count INTEGER DEFAULT 0"
                                )
                            )
                            logger.info("数据库升级完成：添加了 interact_count 字段")

                        # 添加 impression 字段（如果不存在）
                        if "dialogue_round_count" not in columns:
                            logger.info("Adding dialogue_round_count column...")
                            await conn.execute(
                                text(
                                    "ALTER TABLE favour_records ADD COLUMN dialogue_round_count INTEGER DEFAULT 0"
                                )
                            )
                            logger.info("Added dialogue_round_count column")

                        if "impression" not in columns:
                            logger.info("正在升级数据库：添加 impression 字段...")
                            await conn.execute(
                                text(
                                    "ALTER TABLE favour_records ADD COLUMN impression TEXT DEFAULT ''"
                                )
                            )
                            logger.info("数据库升级完成：添加了 impression 字段")

                    # 确保创建 daily_favour_changes 表
                    result = await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_favour_changes'"
                        )
                    )
                    if not result.scalar():
                        logger.info("创建 daily_favour_changes 表...")
                        await conn.run_sync(SQLModel.metadata.create_all)
                        logger.info("数据库初始化：创建了 daily_favour_changes 表")

                    # 确保创建 daily_group_utterances 表
                    result_utt = await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_group_utterances'"
                        )
                    )
                    if not result_utt.scalar():
                        logger.info("创建 daily_group_utterances 表...")
                        await conn.run_sync(SQLModel.metadata.create_all)
                        logger.info("数据库初始化：创建了 daily_group_utterances 表")

                    # 检查 favour_records 表是否存在 nickname 列
                    result_fav = await conn.execute(
                        text("PRAGMA table_info(favour_records)")
                    )
                    fav_cols = [row[1] for row in result_fav.fetchall()]
                    if fav_cols and "nickname" not in fav_cols:
                        logger.info("favour_records 缺少 nickname 列，执行自动迁移添加...")
                        await conn.execute(
                            text("ALTER TABLE favour_records ADD COLUMN nickname TEXT DEFAULT ''")
                        )
                        logger.info("favour_records 已添加 nickname 列")

                self._initialized = True
                logger.info(f"好感度数据库已初始化: {self.db_path}")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

    async def migrate_from_json(self, json_path: Path, is_global: bool = False):
        """从旧版JSON文件迁移数据"""
        await self.init_db()
        if not await aio_path.exists(json_path):
            return

        try:
            async with aio_open(json_path, encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)

            count = 0
            async with self.async_session() as session:
                if is_global:
                    if isinstance(data, dict):
                        for uid, fav in data.items():
                            stmt = select(FavourRecord).where(
                                FavourRecord.user_id == str(uid),
                                FavourRecord.session_id == "global",
                            )
                            result = await session.execute(stmt)
                            if not result.scalars().first():
                                record = FavourRecord(
                                    user_id=str(uid),
                                    session_id="global",
                                    favour=int(fav),
                                )
                                session.add(record)
                                count += 1
                else:
                    if isinstance(data, list):
                        for item in data:
                            uid = str(item.get("userid", ""))
                            sid = str(item.get("session_id", "")) or "global"
                            if not uid:
                                continue

                            stmt = select(FavourRecord).where(
                                FavourRecord.user_id == uid,
                                FavourRecord.session_id == sid,
                            )
                            result = await session.execute(stmt)
                            if not result.scalars().first():
                                record = FavourRecord(
                                    user_id=uid,
                                    session_id=sid,
                                    favour=int(item.get("favour", 0)),
                                    relationship=str(item.get("relationship", "")),
                                    is_unique=bool(item.get("is_unique", False)),
                                )
                                session.add(record)
                                count += 1

                await session.commit()

            if count > 0:
                logger.info(f"成功从 {json_path.name} 迁移了 {count} 条数据到数据库")
                backup_path = json_path.with_suffix(".json.bak")
                import shutil

                shutil.move(json_path, backup_path)
                logger.info(f"旧文件已备份为: {backup_path.name}")

        except Exception as e:
            logger.error(f"迁移数据失败 {json_path}: {str(e)}")

    async def backup_data(self, records: list[FavourRecord], prefix: str) -> str | None:
        """备份指定记录到JSON文件"""
        if not records:
            return None
        try:
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = backup_dir / f"{prefix}_{timestamp}.json"

            data_to_save = []
            for r in records:
                d = r.dict()
                d["created_at"] = (
                    d["created_at"].isoformat() if d.get("created_at") else None
                )
                d["updated_at"] = (
                    d["updated_at"].isoformat() if d.get("updated_at") else None
                )
                data_to_save.append(d)

            async with aio_open(filename, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data_to_save, ensure_ascii=False, indent=2))
            return str(filename)
        except Exception as e:
            logger.error(f"备份数据失败: {e}")
            return None

    async def get_favour(
        self, user_id: str, session_id: str | None = None
    ) -> FavourRecord | None:
        """获取好感度记录"""
        await self.init_db()
        sid = session_id if session_id else "global"
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(
                FavourRecord.user_id == user_id, FavourRecord.session_id == sid
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def update_favour(
        self,
        user_id: str,
        session_id: str | None,
        favour: int | None = None,
        relationship: str | None = None,
        is_unique: bool | None = None,
        interact_count: int | None = None,
        dialogue_round_count: int | None = None,
        impression: str | None = None,
        nickname: str | None = None,
    ) -> bool:
        """更新好感度记录"""
        await self.init_db()
        if not is_valid_userid(user_id):
            return False

        sid = session_id if session_id else "global"

        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(
                    FavourRecord.user_id == user_id, FavourRecord.session_id == sid
                )
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    # 只有uid为1158662154的用户才会达到100好感度
                    max_favour = 100 if user_id == "1158662154" else 99
                    init_favour = (
                        max(self.min_val, min(max_favour, favour))
                        if favour is not None
                        else 0
                    )
                    calculated_relationship = self._get_relationship(init_favour)
                    record = FavourRecord(
                        user_id=user_id,
                        nickname=nickname if nickname is not None else "",
                        session_id=sid,
                        favour=init_favour,
                        relationship=calculated_relationship,
                        interact_count=interact_count
                        if interact_count is not None
                        else 0,
                        dialogue_round_count=dialogue_round_count
                        if dialogue_round_count is not None
                        else 0,
                        impression=impression if impression is not None else "",
                        is_unique=is_unique if is_unique is not None else False,
                    )
                    session.add(record)
                else:
                    if favour is not None:
                        # 只有uid为1158662154的用户才会达到100好感度
                        max_favour = 100 if user_id == "1158662154" else 99
                        record.favour = max(self.min_val, min(max_favour, favour))
                        # 自动更新关系
                        record.relationship = self._get_relationship(record.favour)
                    if relationship is not None:
                        record.relationship = relationship
                    if is_unique is not None:
                        record.is_unique = is_unique
                    if interact_count is not None:
                        record.interact_count = interact_count
                    if dialogue_round_count is not None:
                        record.dialogue_round_count = dialogue_round_count
                    if impression is not None:
                        record.impression = impression
                    if nickname is not None and nickname:
                        record.nickname = nickname
                    record.updated_at = datetime.now()
                    session.add(record)

                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新数据库失败: {str(e)}")
            return False

    async def update_user_all_records(
        self,
        user_id: str,
        favour: int | None = None,
        relationship: str | None = None,
        is_unique: bool | None = None,
    ) -> int:
        """更新某用户在所有会话中的记录（全局修改）"""
        await self.init_db()
        if not is_valid_userid(user_id):
            return 0

        try:
            async with self.async_session() as session:
                if favour is not None:
                    # 当更新好感度时，需要逐个更新记录以计算关系
                    stmt = select(FavourRecord).where(FavourRecord.user_id == user_id)
                    result = await session.execute(stmt)
                    records = result.scalars().all()

                    for record in records:
                        # 只有uid为1158662154的用户才会达到100好感度
                        max_favour = 100 if user_id == "1158662154" else 99
                        record.favour = max(self.min_val, min(max_favour, favour))
                        record.relationship = self._get_relationship(record.favour)
                        record.updated_at = datetime.now()
                        session.add(record)

                    await session.commit()
                    return len(records)
                else:
                    # 构建更新字典
                    values = {"updated_at": datetime.now()}
                    if relationship is not None:
                        values["relationship"] = relationship
                    if is_unique is not None:
                        values["is_unique"] = is_unique

                    stmt = (
                        update(FavourRecord)
                        .where(FavourRecord.user_id == user_id)
                        .values(**values)
                    )
                    result = await session.execute(stmt)
                    await session.commit()
                    return result.rowcount
        except Exception as e:
            logger.error(f"全局更新失败: {str(e)}")
            return 0

    async def delete_favour(
        self, user_id: str, session_id: str | None = None
    ) -> tuple[bool, str]:
        """删除单条记录"""
        await self.init_db()
        sid = session_id if session_id else "global"
        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(
                    FavourRecord.user_id == user_id, FavourRecord.session_id == sid
                )
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    return False, "未找到记录"

                await session.delete(record)
                await session.commit()
                return True, "删除成功"
        except Exception as e:
            logger.error(f"删除记录失败: {str(e)}")
            return False, f"数据库错误: {str(e)}"

    async def get_all_in_session(
        self, session_id: str | None = None
    ) -> list[FavourRecord]:
        """获取某会话下的所有记录"""
        await self.init_db()
        sid = session_id if session_id else "global"
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(FavourRecord.session_id == sid)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_global_records(self) -> list[FavourRecord]:
        """仅获取全局记录"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(FavourRecord.session_id == "global")
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_non_global_records(self) -> list[FavourRecord]:
        """获取所有非全局记录"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(FavourRecord.session_id != "global")
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_all_session_ids(self) -> list[str]:
        """获取所有存在记录的会话 ID，供 WebUI 会话筛选使用"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord.session_id).distinct()
            result = await session.execute(stmt)
            return [
                str(session_id) for session_id in result.scalars().all() if session_id
            ]

    async def get_all_utterance_group_ids(self) -> list[str]:
        """获取所有存在日常发言记录的群号"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(DailyGroupUtterance.group_id).distinct()
            result = await session.execute(stmt)
            return [str(gid) for gid in result.scalars().all() if gid]

    async def get_users_in_group(self, group_id: str) -> list[str]:
        """获取某群在群发言表中存在记录的所有用户ID"""
        await self.init_db()
        raw_gid = group_id.split(":")[-1] if ":" in group_id else group_id
        async with self.async_session() as session:
            stmt = (
                select(DailyGroupUtterance.user_id)
                .where(
                    (DailyGroupUtterance.group_id == group_id)
                    | (DailyGroupUtterance.group_id == raw_gid)
                    | (DailyGroupUtterance.group_id.like(f"%{raw_gid}%"))
                )
                .distinct()
            )
            result = await session.execute(stmt)
            return [str(uid) for uid in result.scalars().all() if uid]

    async def get_records_by_users(
        self, user_ids: list[str], session_id: str = "global"
    ) -> list[FavourRecord]:
        """获取指定用户列表在某个会话中的好感度记录"""
        await self.init_db()
        if not user_ids:
            return []
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(
                FavourRecord.session_id == session_id,
                FavourRecord.user_id.in_(user_ids),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_records_for_group(
        self, group_id: str, is_global: bool = False
    ) -> list[FavourRecord]:
        """获取某个群的好感度记录：全局模式下取群成员的全局记录，分群模式下取该群专属记录"""
        await self.init_db()
        raw_gid = group_id.split(":")[-1] if ":" in group_id else group_id
        if is_global:
            user_ids = await self.get_users_in_group(group_id)
            if not user_ids:
                return []
            return await self.get_records_by_users(user_ids, session_id="global")
        else:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(
                    (FavourRecord.session_id == group_id)
                    | (FavourRecord.session_id == raw_gid)
                    | (FavourRecord.session_id.like(f"%{raw_gid}%"))
                )
                result = await session.execute(stmt)
                return list(result.scalars().all())

    async def get_user_nicknames_map(self, user_ids: list[str]) -> dict[str, str]:
        """从 daily_group_utterances 批量提取用户最新非空发言昵称"""
        await self.init_db()
        if not user_ids:
            return {}
        async with self.async_session() as session:
            stmt = (
                select(DailyGroupUtterance.user_id, DailyGroupUtterance.user_name)
                .where(
                    DailyGroupUtterance.user_id.in_(user_ids),
                    DailyGroupUtterance.user_name != "",
                    DailyGroupUtterance.user_name != DailyGroupUtterance.user_id,
                )
                .order_by(DailyGroupUtterance.id.desc())
            )
            result = await session.execute(stmt)
            res_map: dict[str, str] = {}
            for uid, name in result.all():
                if uid not in res_map and name:
                    res_map[str(uid)] = str(name)
            return res_map

    async def update_user_nickname(self, user_id: str, nickname: str) -> None:
        """更新指定用户在 favour_records 中的昵称"""
        if not user_id or not nickname:
            return
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = (
                    update(FavourRecord)
                    .where(FavourRecord.user_id == user_id)
                    .values(nickname=nickname)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.debug(f"更新用户昵称失败: {e}")

    async def clear_session(self, session_id: str | None = None) -> bool:
        """清空某会话记录"""
        await self.init_db()
        sid = session_id if session_id else "global"
        try:
            async with self.async_session() as session:
                stmt = delete(FavourRecord).where(FavourRecord.session_id == sid)
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"清空会话记录失败: {str(e)}")
            return False

    async def clear_all(self) -> bool:
        """清空所有记录"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = delete(FavourRecord)
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"清空所有记录失败: {e}")
            return False

    async def get_daily_favour_change(self, user_id: str, date: str) -> int:
        """获取用户指定日期的好感度变化"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(DailyFavourChange).where(
                DailyFavourChange.user_id == user_id, DailyFavourChange.date == date
            )
            result = await session.execute(stmt)
            record = result.scalars().first()
            return record.change if record else 0

    async def update_daily_favour_change(
        self, user_id: str, date: str, change: int
    ) -> bool:
        """更新用户指定日期的好感度变化"""
        await self.init_db()
        if not is_valid_userid(user_id):
            return False

        try:
            async with self.async_session() as session:
                stmt = select(DailyFavourChange).where(
                    DailyFavourChange.user_id == user_id, DailyFavourChange.date == date
                )
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    record = DailyFavourChange(
                        user_id=user_id, date=date, change=change
                    )
                else:
                    record.change = change
                    record.updated_at = datetime.now()

                session.add(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新每日好感度变化失败: {e}")
            return False

    async def get_all_daily_favour_changes(self) -> dict[str, dict[str, int]]:
        """获取所有用户的每日好感度变化"""
        await self.init_db()
        result = {}
        try:
            async with self.async_session() as session:
                stmt = select(DailyFavourChange)
                records = await session.execute(stmt)
                for record in records.scalars():
                    if record.user_id not in result:
                        result[record.user_id] = {}
                    result[record.user_id][record.date] = record.change
        except Exception as e:
            logger.error(f"获取每日好感度变化失败: {e}")
        return result

    async def reset_negative_favour_to_zero(self) -> int:
        """将所有好感度小于0的记录重置为0，返回影响的记录数"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = (
                    update(FavourRecord)
                    .where(FavourRecord.favour < 0)
                    .values(favour=0, updated_at=datetime.now())
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount
        except Exception as e:
            logger.error(f"重置负好感度失败: {e}")
            return 0

    async def record_daily_group_utterance(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        content: str,
        date: str,
        max_daily_count: int = 50,
    ) -> bool:
        """记录用户在群内的发言，若单日达到条数上限则不再记录。

        Args:
            group_id: 群会话ID。
            user_id: 用户ID。
            user_name: 用户群昵称。
            content: 发言纯文本。
            date: 日期字符串 (YYYY-MM-DD)。
            max_daily_count: 单人单群每日最大暂存条数。

        Returns:
            成功写入返回 True，超出条数上限或发生异常返回 False。
        """
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt_count = select(DailyGroupUtterance).where(
                    DailyGroupUtterance.group_id == group_id,
                    DailyGroupUtterance.user_id == user_id,
                    DailyGroupUtterance.date == date,
                )
                existing = (await session.execute(stmt_count)).scalars().all()
                if len(existing) >= max_daily_count:
                    return False

                record = DailyGroupUtterance(
                    group_id=group_id,
                    user_id=user_id,
                    user_name=user_name,
                    content=content,
                    date=date,
                )
                session.add(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"记录群用户发言失败: {e}")
            return False

    async def get_active_groups_by_date(self, date: str) -> list[str]:
        """获取指定日期有发言记录的所有群会话ID列表。

        Args:
            date: 目标日期字符串 (YYYY-MM-DD)。

        Returns:
            不重复的群会话ID列表。
        """
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = (
                    select(DailyGroupUtterance.group_id)
                    .where(DailyGroupUtterance.date == date)
                    .distinct()
                )
                result = await session.execute(stmt)
                return [r for r in result.scalars().all() if r]
        except Exception as e:
            logger.error(f"获取活跃群列表失败: {e}")
            return []

    async def get_group_active_users_utterances(
        self,
        group_id: str,
        date: str,
        min_msg_count: int = 10,
        min_total_chars: int = 50,
    ) -> dict[str, dict]:
        """查询指定群在目标日期达到活跃门槛的用户发言记录。

        Args:
            group_id: 群会话ID。
            date: 目标日期字符串 (YYYY-MM-DD)。
            min_msg_count: 活跃判定最少发言条数。
            min_total_chars: 活跃判定最少总字数。

        Returns:
            用户活跃数据字典：{user_id: {"user_name": str, "messages": list[str], "msg_count": int, "total_chars": int}}。
        """
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = (
                    select(DailyGroupUtterance)
                    .where(
                        DailyGroupUtterance.group_id == group_id,
                        DailyGroupUtterance.date == date,
                    )
                    .order_by(DailyGroupUtterance.id.asc())
                )
                result = await session.execute(stmt)
                records = result.scalars().all()

                user_data: dict[str, dict] = {}
                for r in records:
                    uid = str(r.user_id)
                    if uid not in user_data:
                        user_data[uid] = {
                            "user_name": r.user_name or uid,
                            "messages": [],
                            "msg_count": 0,
                            "total_chars": 0,
                        }
                    user_data[uid]["messages"].append(r.content)
                    user_data[uid]["msg_count"] += 1
                    user_data[uid]["total_chars"] += len(r.content)
                    if r.user_name:
                        user_data[uid]["user_name"] = r.user_name

                # 筛选满足活跃门槛的用户 (发言 >= min_msg_count 或 总字数 >= min_total_chars)
                filtered = {
                    uid: data
                    for uid, data in user_data.items()
                    if data["msg_count"] >= min_msg_count
                    or data["total_chars"] >= min_total_chars
                }
                return filtered
        except Exception as e:
            logger.error(f"查询群活跃用户发言失败: {e}")
            return {}

    async def get_user_activity_across_groups(
        self, date: str
    ) -> dict[str, dict[str, int]]:
        """获取目标日期各用户在所有群的发言总字数，用于全局模式下判定主活跃群。

        Args:
            date: 目标日期字符串 (YYYY-MM-DD)。

        Returns:
            各用户在各群发言字数字典：{user_id: {group_id: total_chars}}。
        """
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = select(DailyGroupUtterance).where(
                    DailyGroupUtterance.date == date
                )
                records = (await session.execute(stmt)).scalars().all()
                activity: dict[str, dict[str, int]] = {}
                for r in records:
                    uid = str(r.user_id)
                    gid = str(r.group_id)
                    if uid not in activity:
                        activity[uid] = {}
                    activity[uid][gid] = activity[uid].get(gid, 0) + len(r.content)
                return activity
        except Exception as e:
            logger.error(f"获取跨群活跃统计失败: {e}")
            return {}

    async def clean_expired_group_utterances(self, cutoff_date: str) -> int:
        """清理早于截止日期的过期群发言记录。

        Args:
            cutoff_date: 截止日期字符串 (YYYY-MM-DD)，早于此日期的记录将被删除。

        Returns:
            被删除的记录行数。
        """
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = delete(DailyGroupUtterance).where(
                    DailyGroupUtterance.date < cutoff_date
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount
        except Exception as e:
            logger.error(f"清理过期发言记录失败: {e}")
            return 0

    async def close(self) -> None:
        """显式释放数据库引擎资源，避免文件句柄锁定。"""
        try:
            await self.engine.dispose()
        except Exception as e:
            logger.error(f"关闭数据库引擎失败: {e}")
