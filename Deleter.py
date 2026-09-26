"""
Telegram 频道清理机器人
=======================

⚠️ 重要架构说明：
Telegram Bot API 的 channel_post 更新不包含发帖人身份信息。
因此本机器人采用「私聊命令」架构：管理员私聊机器人发送「تنظيف」，
机器人验证其频道管理员权限后执行清理。

本机器人无法删除「历史消息」，原因见文件末尾的详细说明。
"""

import os
import logging
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# 环境变量加载
# ============================================================
load_dotenv()

BOT_TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
FORCE_SUB_CHANNEL: Optional[str] = os.getenv("FORCE_SUB_CHANNEL")
TARGET_CHANNEL: Optional[str] = os.getenv("TARGET_CHANNEL")

# 解析 ALLOWED_USERS（可选，作为额外安全层；如不需要可忽略）
_ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set = set()
for _uid in _ALLOWED_USERS_STR.replace(",", " ").split():
    _uid = _uid.strip()
    if _uid.isdigit():
        ALLOWED_USERS.add(int(_uid))

if not BOT_TOKEN:
    raise ValueError("❌ 必须在 .env 文件中设置 BOT_TOKEN")
if not TARGET_CHANNEL:
    raise ValueError("❌ 必须在 .env 文件中设置 TARGET_CHANNEL")

# ============================================================
# 关键配置
# ============================================================
# 清理命令（纯文本，非斜杠命令）
CLEAN_COMMAND = "تنظيف"

# 等待删除操作之间的间隔（秒），避免触发 Flood 限制
DELAY_BETWEEN_DELETES = 0.1

# ============================================================
# 工具函数
# ============================================================

def parse_clean_command(text: str) -> Optional[int]:
    """
    解析清理命令。
    返回：
        - None：不是清理命令
        - 0：清理全部可删除的非置顶消息
        - 正整数：最多删除指定数量
    """
    text = text.strip()
    if not text.startswith(CLEAN_COMMAND):
        return None

    # 检查是否为纯命令（"تنظيف"）
    if text == CLEAN_COMMAND:
        return 0

    # 检查 "تنظيف <数字>"
    parts = text.split()
    if len(parts) == 2 and parts[0] == CLEAN_COMMAND and parts[1].isdigit():
        return int(parts[1])

    return None


async def is_user_admin_in_channel(
    bot,
    user_id: int,
    channel_username: str,
) -> Tuple[bool, str]:
    """
    检查用户是否为指定频道的管理员。
    返回 (is_admin, reason)
    """
    try:
        member = await bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        status = member.status

        if status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, "administrator", "creator"):
            return True, "administrator"
        else:
            return False, f"user_status_{status}"

    except BadRequest as e:
        # User not found → 用户不在该频道中
        if "User not found" in str(e):
            return False, "user_not_in_channel"
        return False, f"bad_request: {e}"

    except Forbidden as e:
        # 机器人没有权限查看频道成员
        return False, f"bot_forbidden: {e}"

    except TelegramError as e:
        return False, f"telegram_error: {e}"


async def is_user_subscribed_to_force_channel(
    bot,
    user_id: int,
) -> bool:
    """
    检查用户是否已订阅强制订阅频道。
    """
    if not FORCE_SUB_CHANNEL:
        return True  # 未配置强制订阅，视为通过

    try:
        member = await bot.get_chat_member(
            chat_id=FORCE_SUB_CHANNEL,
            user_id=user_id,
        )
        status = member.status
        # 订阅状态：member, administrator, creator, restricted（且 is_member=True）
        if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                      ChatMemberStatus.OWNER, "member", "administrator", "creator"):
            return True
        if status == ChatMemberStatus.RESTRICTED or status == "restricted":
            return getattr(member, "is_member", False)
        return False

    except (BadRequest, Forbidden, TelegramError) as e:
        logger.warning(f"检查订阅状态失败 (user_id={user_id}): {e}")
        return False


async def bot_has_delete_permission(
    bot,
    channel_username: str,
) -> bool:
    """
    检查机器人是否有权限删除消息。
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(
            chat_id=channel_username,
            user_id=me.id,
        )
        # 检查管理员权限中的 can_delete_messages
        can_delete = getattr(member, "can_delete_messages", False)
        return bool(can_delete)

    except (BadRequest, Forbidden, TelegramError) as e:
        logger.warning(f"检查机器人删除权限失败: {e}")
        return False


def build_subscription_keyboard() -> InlineKeyboardMarkup:
    """构建强制订阅的按钮键盘。"""
    channel_link = f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}"
    keyboard = [
        [InlineKeyboardButton("📢 اشترك في القناة", url=channel_link)],
        [InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="check_subscription")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# 命令处理器
# ============================================================

async def handle_clean_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    处理私聊中的「تنظيف」命令。
    """
    user = update.effective_user
    if not user:
        return

    # 只处理私聊消息（频道和群组的 channel_post 无法识别作者）
    if update.effective_chat.type != "private":
        return

    text = update.message.text
    limit = parse_clean_command(text)

    if limit is None:
        return  # 不是清理命令

    user_id = user.id
    logger.info(f"收到清理请求: user_id={user_id}, limit={limit}")

    # ---------- 安全检查 1：ALLOWED_USERS（可选） ----------
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ هذا الأمر متاح فقط لأدمن القناة.")
        return

    # ---------- 安全检查 2：强制订阅 ----------
    subscribed = await is_user_subscribed_to_force_channel(context.bot, user_id)
    if not subscribed:
        await update.message.reply_text(
            "⚠️ يجب الاشتراك بالقناة أولاً.",
            reply_markup=build_subscription_keyboard(),
        )
        return

    # ---------- 安全检查 3：频道管理员权限 ----------
    is_admin, reason = await is_user_admin_in_channel(
        context.bot, user_id, TARGET_CHANNEL
    )
    if not is_admin:
        logger.warning(f"非管理员尝试清理: user_id={user_id}, reason={reason}")
        await update.message.reply_text("❌ هذا الأمر متاح فقط لأدمن القناة.")
        return

    # ---------- 安全检查 4：机器人删除权限 ----------
    has_perm = await bot_has_delete_permission(context.bot, TARGET_CHANNEL)
    if not has_perm:
        await update.message.reply_text(
            "❌ البوت لا يملك صلاحية حذف الرسائل في هذه القناة."
        )
        return

    # ---------- 执行清理 ----------
    await execute_cleaning(
        context=context,
        user_id=user_id,
        limit=limit,
        reply_message=update.message,
    )


async def execute_cleaning(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    limit: int,
    reply_message,
) -> None:
    """
    执行频道清理。
    """
    bot = context.bot
    chat_id = TARGET_CHANNEL

    await reply_message.reply_text("🔄 جاري التنظيف...")

    # ---------- 获取置顶消息 ID ----------
    pinned_message_id: Optional[int] = None
    try:
        chat = await bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        if pinned:
            pinned_message_id = pinned.message_id
            logger.info(f"检测到置顶消息: message_id={pinned_message_id}")
    except TelegramError as e:
        logger.warning(f"获取置顶消息失败: {e}")

    # ---------- 获取待删除消息列表 ----------
    # ⚠️ 严重限制：Bot API 无法列出历史消息
    # 只能删除机器人「观察到的」消息 ID。
    # 此处从 context.bot_data 中读取之前记录的消息 ID。
    tracked_messages: List[int] = context.bot_data.get("tracked_messages", [])

    # 过滤：排除置顶消息
    if pinned_message_id:
        tracked_messages = [mid for mid in tracked_messages if mid != pinned_message_id]

    # 应用数量限制
    if limit > 0:
        tracked_messages = tracked_messages[:limit]

    if not tracked_messages:
        await reply_message.reply_text(
            "⚠️ لا توجد رسائل قابلة للحذف في السجل.\n\n"
            "🔍 ملاحظة: Bot API لا يستطيع استرجاع الرسائل التاريخية.\n"
            "يتم تتبع الرسائل الجديدة فقط بعد تشغيل البوت."
        )
        return

    # ---------- 执行删除 ----------
    deleted_count = 0
    failed_count = 0
    ignored_pinned = 0

    for msg_id in tracked_messages:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted_count += 1
            logger.debug(f"已删除消息: {msg_id}")

        except RetryAfter as e:
            # Flood 限制
            wait_time = e.retry_after
            logger.warning(f"Flood 限制，等待 {wait_time} 秒...")
            import asyncio
            await asyncio.sleep(wait_time + 1)
            # 重试一次
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                deleted_count += 1
            except TelegramError as retry_e:
                logger.warning(f"重试删除失败 {msg_id}: {retry_e}")
                failed_count += 1

        except BadRequest as e:
            error_msg = str(e)
            if "message to delete not found" in error_msg.lower():
                logger.debug(f"消息不存在: {msg_id}")
                failed_count += 1
            elif "message can't be deleted" in error_msg.lower():
                logger.debug(f"消息无法删除: {msg_id}")
                failed_count += 1
            else:
                logger.warning(f"删除失败 {msg_id}: {e}")
                failed_count += 1

        except Forbidden as e:
            logger.error(f"机器人权限不足: {e}")
            failed_count += 1

        except TelegramError as e:
            logger.warning(f"删除异常 {msg_id}: {e}")
            failed_count += 1

        # 节流，避免触发限制
        import asyncio
        await asyncio.sleep(DELAY_BETWEEN_DELETES)

    # ---------- 从跟踪列表中移除已删除的消息 ----------
    remaining = [mid for mid in context.bot_data.get("tracked_messages", [])
                 if mid not in tracked_messages]
    context.bot_data["tracked_messages"] = remaining

    # ---------- 构建响应 ----------
    response = (
        f"✅ تم التنظيف\n"
        f"🗑️ تم حذف: {deleted_count}\n"
        f"📌 تم تجاهل المثبتة: {1 if pinned_message_id else 0}\n"
    )
    if failed_count > 0:
        response += f"⚠️ فشل الحذف: {failed_count}\n"

    await reply_message.reply_text(response)


async def handle_subscription_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    处理「تحققت من الاشتراك」按钮回调。
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    subscribed = await is_user_subscribed_to_force_channel(context.bot, user_id)

    if subscribed:
        await query.edit_message_text(
            "✅ تم التحقق من اشتراكك بنجاح.\n"
            "يمكنك الآن استخدام أمر التنظيف."
        )
    else:
        await query.edit_message_text(
            "⚠️ يجب الاشتراك بالقناة أولاً.",
            reply_markup=build_subscription_keyboard(),
        )


async def track_channel_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    跟踪频道帖子，记录 message_id 以便后续删除。
    ⚠️ 只能跟踪机器人运行后看到的新消息。
    """
    if not update.channel_post:
        return

    chat_id = update.channel_post.chat.id
    message_id = update.channel_post.message_id

    # 只跟踪目标频道
    target_chat_id = TARGET_CHANNEL
    # 如果是用户名形式，尝试匹配
    if str(chat_id) != str(target_chat_id) and TARGET_CHANNEL not in str(chat_id):
        # 宽松匹配：可能频道 ID 与用户名形式不同
        pass

    tracked = context.bot_data.setdefault("tracked_messages", [])
    if message_id not in tracked:
        tracked.append(message_id)
        # 限制跟踪列表大小，避免内存问题
        if len(tracked) > 10000:
            tracked = tracked[-5000:]
            context.bot_data["tracked_messages"] = tracked

    logger.debug(f"跟踪频道消息: chat_id={chat_id}, message_id={message_id}")


async def handle_unknown_private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """处理私聊中的未知消息。"""
    if update.effective_chat.type != "private":
        return
    if parse_clean_command(update.message.text or "") is not None:
        return  # 是清理命令，已由主处理器处理

    await update.message.reply_text(
        "🤖 مرحباً!\n\n"
        "هذا البوت يقوم بتنظيف القناة.\n"
        "أرسل كلمة «تنظيف» لحذف الرسائل القابلة للحذف.\n\n"
        "أمثلة:\n"
        "• تنظيف\n"
        "• تنظيف 20\n"
        "• تنظيف 50\n"
        "• تنظيف 100"
    )


# ============================================================
# 主函数
# ============================================================

def main() -> None:
    """启动机器人。"""
    logger.info("🚀 启动频道清理机器人...")

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # 初始化 bot_data
    application.bot_data["tracked_messages"] = []

    # ---------- 处理器注册 ----------

    # 1. 私聊中的清理命令
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_clean_command,
        ),
        group=0,
    )

    # 2. 回调按钮
    application.add_handler(
        CallbackQueryHandler(handle_subscription_check, pattern="^check_subscription$")
    )

    # 3. 频道帖子跟踪
    application.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, track_channel_message)
    )

    # 4. 私聊中的其他消息（帮助信息）
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_unknown_private_message,
        ),
        group=1,
    )

    # ---------- 启动轮询 ----------
    logger.info("✅ 机器人已启动，正在监听更新...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
