import os
import re
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pandas as pd
from telegram import Update, InputFile
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------- CONFIG --------------------
BOT_TOKEN = ""  # <-- Replace with your bot token
GROUP_ID = -1003463796946          # <-- main group id where staff operate
LOG_CHANNEL_ID = -1003395196772    # <-- channel/group id where logs are posted
BOT_ADMINS = [2119444261, 624102836]  # <-- list of user ids treated as bot admins
DB_FILE = "exc_bot.db"
SHIFT_START = "19:45"  # shift start time (HH:MM)
SHIFT_END = "23:00"    # shift end time (HH:MM)

# -------------------- TIME HELPERS --------------------
def gmt5_now() -> datetime:
    """Return now adjusted to GMT+5 as timezone-aware datetime."""
    return datetime.now(timezone.utc) + timedelta(hours=5)


def today_str() -> str:
    """Return current date in YYYY-MM-DD in GMT+5."""
    return gmt5_now().strftime("%Y-%m-%d")


def hhmm_to_dt(hhmm: str, ref: Optional[datetime] = None) -> datetime:
    """Convert HH:MM to a datetime on the same day as ref (or now)."""
    ref_dt = ref or gmt5_now()
    hh, mm = map(int, hhmm.split(":"))
    return ref_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def escape_md(t: str) -> str:
    """Escape Markdown special chars for Telegram messages (v2 style)."""
    if not t:
        return ""
    return re.sub(r'([_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!])', r'\\\1', t)


# -------------------- DB SETUP --------------------
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_cur = _conn.cursor()


def init_db() -> None:
    """Initialize database tables and indexes."""
    _cur.execute(
        """
        CREATE TABLE IF NOT EXISTS staff(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT
        )
    """
    )
    _cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            full_name TEXT,
            date TEXT,
            clock_in TEXT,
            clock_out TEXT,
            status TEXT,
            late_minutes INTEGER DEFAULT 0,
            overtime_minutes INTEGER DEFAULT 0,
            UNIQUE(user_id,date)
        )
    """
    )
    _cur.execute("CREATE INDEX IF NOT EXISTS idx_att_user_date ON attendance(user_id,date)")
    _conn.commit()


# -------------------- HELPER: MESSAGE LINKS --------------------
def make_tme_link(chat_id: int, message_id: int) -> str:
    """
    Construct a Telegram t.me/c/ link for groups/channels (where possible).
    For groups/channels the chat_id has form -100{channel_id}, so we strip -100.
    Returns a URL string or 'N/A' when not possible.
    """
    try:
        s = str(chat_id)
        if s.startswith("-100"):
            channel_part = s[4:]  # remove leading -100
            return f"https://t.me/c/{channel_part}/{message_id}"
        # For supergroups/channels this should work; otherwise return tg://
        return f"tg://user?id={chat_id}"
    except Exception:
        return "N/A"


# -------------------- LOGGING --------------------
async def bot_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """
    Send a free-form log message to LOG_CHANNEL_ID.
    Use parse_mode=MARKDOWN for formatting.
    """
    try:
        await context.bot.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        # Fail gracefully — print to stdout
        print("LOG ERROR:", e)
        print(text)


async def log_action_detailed(
    context: ContextTypes.DEFAULT_TYPE,
    tag: str,
    user_id: int,
    user_name: str,
    shift_start: str,
    late_minutes: int,
    overtime_minutes: int,
    status: str,
    conf_chat_id: int,
    conf_message_id: int,
) -> None:
    """
    Log the action in Option B detailed format:
    - includes staff name, user id, date, time, shift start, late minutes, overtime, status, and message link
    """
    now = gmt5_now()
    link = make_tme_link(conf_chat_id, conf_message_id)
    lines = [
        f"#{tag}",
        f"• Staff Name: {escape_md(user_name)} 🍷🍥",
        f"• User ID: {user_id}",
        f"• Date: {now.strftime('%Y-%m-%d')}",
        f"• Time: {now.strftime('%H:%M')}",
        f"• Shift Start: {shift_start}",
        f"• Late Minutes: {late_minutes}",
        f"• Overtime Minutes: {overtime_minutes}",
        f"• Status: {escape_md(status)}",
        f"• Message link: Go to message ({link})",
    ]
    await bot_log(context, "\n".join(lines))


# -------------------- ADMIN CHECK --------------------
async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Return True if user is admin in GROUP_ID (or bot admin list)."""
    try:
        m = await context.bot.get_chat_member(GROUP_ID, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if caller is admin (BOT_ADMINS or group admin).
    Replies with denial when not allowed. Returns True if allowed.
    """
    uid = update.effective_user.id
    if uid in BOT_ADMINS:
        return True
    if await is_group_admin(context, uid):
        return True
    try:
        await update.message.reply_text("❌ You are not allowed to use this command.")
    except Exception:
        # message may be None/unsupported — ignore
        pass
    return False


# -------------------- STAFF COMMANDS --------------------
async def cmd_clockin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Staff command: /clockin
    - Auto-delete the user command (if possible)
    - Record clock in time and late minutes
    - Send confirmation message
    - Log detailed information to LOG_CHANNEL_ID (Option B)
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    uid = user.id
    name = user.full_name or user.username or str(uid)
    today = today_str()
    now = gmt5_now()
    now_s = now.strftime("%H:%M")

    # Attempt to delete the user's command message (requires bot admin)
    try:
        await msg.delete()
    except Exception:
        # ignore if bot can't delete
        pass

    # Ensure staff exists
    _cur.execute("SELECT user_id FROM staff WHERE user_id=?", (uid,))
    if not _cur.fetchone():
        # If we cannot reply to the deleted message, send a new ephemeral reply
        await context.bot.send_message(msg.chat_id, "❌ You are not registered as staff.")
        return

    # Check if already clocked in
    _cur.execute("SELECT clock_in FROM attendance WHERE user_id=? AND date=?", (uid, today))
    rec = _cur.fetchone()
    if rec and rec[0]:
        await context.bot.send_message(msg.chat_id, "❌ You already clocked in.")
        return

    # Compute late minutes relative to SHIFT_START
    shift_start_dt = hhmm_to_dt(SHIFT_START, now)
    late_m = max(0, int((now - shift_start_dt).total_seconds() // 60))

    # Insert or update attendance
    _cur.execute(
        """
        INSERT INTO attendance (user_id, full_name, date, clock_in, status, late_minutes)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET clock_in=excluded.clock_in, status='Clocked In', late_minutes=excluded.late_minutes
        """,
        (uid, name, today, now_s, "Clocked In", late_m),
    )
    _conn.commit()

    # Send confirmation message
    conf_msg = await context.bot.send_message(
        chat_id=msg.chat_id,
        text=f"✅ [{escape_md(name)}](tg://user?id={uid}) clocked in at `{now_s}` (Late: {late_m}m)",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Log detailed action
    await log_action_detailed(
        context=context,
        tag="clockin",
        user_id=uid,
        user_name=name,
        shift_start=SHIFT_START,
        late_minutes=late_m,
        overtime_minutes=0,
        status="Clocked In",
        conf_chat_id=conf_msg.chat.id,
        conf_message_id=conf_msg.message_id,
    )


async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Staff command: /clockout
    - Auto-delete command message
    - Update clock_out, compute overtime minutes
    - Send confirmation and detailed log
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    uid = user.id
    name = user.full_name or user.username or str(uid)
    today = today_str()
    now = gmt5_now()
    now_s = now.strftime("%H:%M")

    try:
        await msg.delete()
    except Exception:
        pass

    _cur.execute("SELECT clock_in, clock_out, late_minutes FROM attendance WHERE user_id=? AND date=?", (uid, today))
    rec = _cur.fetchone()
    if not rec or not rec[0]:
        await context.bot.send_message(msg.chat_id, "❌ You haven't clocked in.")
        return
    if rec[1]:
        await context.bot.send_message(msg.chat_id, "❌ You already clocked out.")
        return

    shift_end_dt = hhmm_to_dt(SHIFT_END, now)
    overtime = max(0, int((now - shift_end_dt).total_seconds() // 60)) if now > shift_end_dt else 0

    _cur.execute(
        """
        UPDATE attendance
        SET clock_out=?, status='Clocked Out', overtime_minutes=?
        WHERE user_id=? AND date=?
        """,
        (now_s, overtime, uid, today),
    )
    _conn.commit()

    conf_msg = await context.bot.send_message(
        chat_id=msg.chat_id,
        text=f"🔴 [{escape_md(name)}](tg://user?id={uid}) clocked out at `{now_s}` (OT: {overtime}m)",
        parse_mode=ParseMode.MARKDOWN,
    )

    # read late minutes (if present)
    late_minutes = rec[2] or 0

    await log_action_detailed(
        context=context,
        tag="clockout",
        user_id=uid,
        user_name=name,
        shift_start=SHIFT_START,
        late_minutes=late_minutes,
        overtime_minutes=overtime,
        status="Clocked Out",
        conf_chat_id=conf_msg.chat.id,
        conf_message_id=conf_msg.message_id,
    )


async def cmd_sick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Staff command: /sick
    - Mark the day's attendance as Sick (clears clock_in/clock_out)
    - Auto-delete user command, confirm, and log
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    uid = user.id
    name = user.full_name or user.username or str(uid)
    today = today_str()

    try:
        await msg.delete()
    except Exception:
        pass

    _cur.execute(
        """
        INSERT INTO attendance (user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Sick', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0
        """,
        (uid, name, today, "Sick"),
    )
    _conn.commit()

    conf_msg = await context.bot.send_message(
        chat_id=msg.chat_id,
        text=f"🤒 Marked Sick for [{escape_md(name)}](tg://user?id={uid})",
        parse_mode=ParseMode.MARKDOWN,
    )

    await log_action_detailed(
        context=context,
        tag="sick",
        user_id=uid,
        user_name=name,
        shift_start=SHIFT_START,
        late_minutes=0,
        overtime_minutes=0,
        status="Sick",
        conf_chat_id=conf_msg.chat.id,
        conf_message_id=conf_msg.message_id,
    )


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Staff command: /off
    - Mark the day as Off (clears clock times)
    - Auto-delete user command, confirm, and log
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    uid = user.id
    name = user.full_name or user.username or str(uid)
    today = today_str()

    try:
        await msg.delete()
    except Exception:
        pass

    _cur.execute(
        """
        INSERT INTO attendance (user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Off', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0
        """,
        (uid, name, today, "Off"),
    )
    _conn.commit()

    conf_msg = await context.bot.send_message(
        chat_id=msg.chat_id,
        text=f"📘 Marked Off for [{escape_md(name)}](tg://user?id={uid})",
        parse_mode=ParseMode.MARKDOWN,
    )

    await log_action_detailed(
        context=context,
        tag="off",
        user_id=uid,
        user_name=name,
        shift_start=SHIFT_START,
        late_minutes=0,
        overtime_minutes=0,
        status="Off",
        conf_chat_id=conf_msg.chat.id,
        conf_message_id=conf_msg.message_id,
    )


# -------------------- ADMIN HELPERS --------------------
def auto_absent() -> None:
    """
    For every staff member, insert an 'Absent' record for today if nothing exists.
    Run before monthly checks to ensure missing days are marked.
    """
    today = today_str()
    _cur.execute("SELECT user_id, full_name FROM staff")
    staff_rows = _cur.fetchall()
    for uid, name in staff_rows:
        _cur.execute("SELECT id FROM attendance WHERE user_id=? AND date=?", (uid, today))
        if not _cur.fetchone():
            _cur.execute(
                """
                INSERT OR IGNORE INTO attendance(user_id, full_name, date, status)
                VALUES (?, ?, ?, 'Absent')
                """,
                (uid, name, today),
            )
    _conn.commit()


# -------------------- ADMIN COMMANDS --------------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /add <id> <Full Name>  OR reply to user
    Adds staff to staff table.
    """
    if not await admin_only(update, context):
        return

    msg = update.message
    if not msg:
        return

    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
        name = " ".join(context.args) if context.args else msg.reply_to_message.from_user.full_name
    else:
        if len(context.args) < 2:
            await msg.reply_text("Usage: /add <id> <Full Name> OR reply")
            return
        uid = int(context.args[0])
        name = " ".join(context.args[1:])

    _cur.execute("INSERT OR REPLACE INTO staff(user_id, full_name) VALUES (?,?)", (uid, name))
    _conn.commit()
    await msg.reply_text(f"✅ Added staff: {name}")
    # Log admin action (not as detailed as staff logs)
    await bot_log(context, f"#add\n• Admin: {escape_md(update.effective_user.full_name)}\n• Added staff: {escape_md(name)} ({uid})")


async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /rm <id> OR reply
    Remove staff from staff table.
    """
    if not await admin_only(update, context):
        return

    msg = update.message
    if not msg:
        return

    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
    else:
        if not context.args:
            await msg.reply_text("Usage: /rm <id> OR reply")
            return
        uid = int(context.args[0])

    _cur.execute("DELETE FROM staff WHERE user_id=?", (uid,))
    _conn.commit()
    await msg.reply_text("✅ Removed staff.")
    await bot_log(context, f"#rm\n• Admin: {escape_md(update.effective_user.full_name)}\n• Removed staff: {uid}")


async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /staff
    List all staff members.
    """
    if not await admin_only(update, context):
        return

    _cur.execute("SELECT user_id, full_name FROM staff ORDER BY full_name")
    rows = _cur.fetchall()
    if not rows:
        await update.message.reply_text("No staff.")
        return

    lines = [f"• [{escape_md(n)}](tg://user?id={uid}) — `{uid}`" for uid, n in rows]
    await update.message.reply_text("*Staff List:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /check <id> or reply
    Show monthly attendance summary for the user (current month).
    """
    if not await admin_only(update, context):
        return

    auto_absent()
    msg = update.message
    if not msg:
        return

    now = gmt5_now()
    month = now.strftime("%Y-%m")

    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
    else:
        if not context.args:
            await msg.reply_text("Usage: reply or /check <id>")
            return
        uid = int(context.args[0])

    _cur.execute("SELECT full_name FROM staff WHERE user_id=?", (uid,))
    row = _cur.fetchone()
    if not row:
        await msg.reply_text("Staff not found.")
        return
    name = row[0]

    _cur.execute(
        """
        SELECT date, clock_in, clock_out, status, late_minutes, overtime_minutes
        FROM attendance
        WHERE user_id=? AND date LIKE ?
        ORDER BY date
        """,
        (uid, f"{month}%"),
    )
    rows = _cur.fetchall()
    if not rows:
        await msg.reply_text("No records this month.")
        return

    total_late = sum((r[4] or 0) for r in rows)
    total_ot = sum((r[5] or 0) for r in rows)
    total_hours = 0.0
    details = []

    for d, cin, cout, st, late, ot in rows:
        worked = 0.0
        if cin and cout:
            try:
                t1 = datetime.strptime(f"{d} {cin}", "%Y-%m-%d %H:%M")
                t2 = datetime.strptime(f"{d} {cout}", "%Y-%m-%d %H:%M")
                worked = round((t2 - t1).total_seconds() / 3600, 2)
                total_hours += worked
            except Exception:
                worked = 0.0
        details.append(f"• {d} — In:{cin or '-'} Out:{cout or '-'} {st} Late:{late or 0}m OT:{ot or 0}m Worked:{worked}h")

    text = (
        f"*Summary for {escape_md(name)} — {now.strftime('%B %Y')}*\n"
        f"• Total Late: {total_late} minutes\n"
        f"• Total OT: {total_ot} minutes\n"
        f"• Total Hours: {round(total_hours,2)}\n\n"
        "*Daily:* \n" + "\n".join(details)
    )
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /report
    Generate an Excel monthly report for all attendance and send as file.
    """
    if not await admin_only(update, context):
        return

    # Optionally fill absent for today before generating aggregate
    auto_absent()
    df = pd.read_sql_query(
        """
        SELECT user_id, full_name, date, clock_in, clock_out, status, late_minutes, overtime_minutes
        FROM attendance
        ORDER BY date
        """,
        _conn,
    )

    if df.empty:
        await update.message.reply_text("No data.")
        return

    bio = io.BytesIO()
    fname = f"exc_report_{gmt5_now().strftime('%Y-%m')}.xlsx"
    bio.name = fname

    # Write excel to memory
    with pd.ExcelWriter(bio, engine="xlsxwriter") as w:
        df.to_excel(w, index=False, sheet_name="Attendance")

    bio.seek(0)
    await update.message.reply_document(InputFile(bio, filename=fname))
    await bot_log(context, f"#report\n• Admin: {escape_md(update.effective_user.full_name)}\n• Report: {fname} sent.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /status
    Show today's attendance (summarized).
    """
    if not await admin_only(update, context):
        return

    today = today_str()
    _cur.execute(
        """
        SELECT full_name, user_id, clock_in, clock_out, status
        FROM attendance
        WHERE date=?
        ORDER BY full_name
        """,
        (today,),
    )
    rows = _cur.fetchall()
    if not rows:
        await update.message.reply_text("No attendance today.")
        return

    lines = []
    for name, uid, cin, cout, st in rows:
        if cin and cout:
            lines.append(f"• [{escape_md(name)}](tg://user?id={uid}) In:`{cin}` Out:`{cout}`")
        elif cin:
            lines.append(f"• [{escape_md(name)}](tg://user?id={uid}) In:`{cin}`")
        else:
            lines.append(f"• [{escape_md(name)}](tg://user?id={uid}) — {st}")

    await update.message.reply_text("*Today's attendance:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /backup
    Send the sqlite DB file to admin.
    """
    if not await admin_only(update, context):
        return

    if os.path.exists(DB_FILE):
        await update.message.reply_document(InputFile(DB_FILE))
        await bot_log(context, f"#backup\n• Admin: {escape_md(update.effective_user.full_name)}\n• Backup sent.")
    else:
        await update.message.reply_text("DB file not found.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /reset
    Delete all attendance records (use with caution).
    """
    if not await admin_only(update, context):
        return

    _cur.execute("DELETE FROM attendance")
    _conn.commit()
    await update.message.reply_text("✅ All attendance records cleared.")
    await bot_log(context, f"#reset\n• Admin: {escape_md(update.effective_user.full_name)}\n• Cleared all attendance.")


async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /reset_clock
    Delete attendance records for today only.
    """
    if not await admin_only(update, context):
        return

    t = today_str()
    _cur.execute("DELETE FROM attendance WHERE date=?", (t,))
    _conn.commit()
    await update.message.reply_text("✅ Today's attendance cleared.")
    await bot_log(context, f"#reset_clock\n• Admin: {escape_md(update.effective_user.full_name)}\n• Reset today's attendance ({t}).")


async def cmd_undone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /undone <user_id> <date?>  OR reply
    Undo clock_out (clear clock_out, reset overtime, set status to Clocked In).
    """
    if not await admin_only(update, context):
        return

    msg = update.message
    if not msg:
        return

    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
        date_s = context.args[0] if context.args else today_str()
    else:
        if len(context.args) < 1:
            await msg.reply_text("Usage: /undone <user_id> <date (optional)>")
            return
        uid = int(context.args[0])
        date_s = context.args[1] if len(context.args) > 1 else today_str()

    # validate date
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except Exception:
        await msg.reply_text("❌ Invalid date format. Use YYYY-MM-DD.")
        return

    _cur.execute(
        """
        UPDATE attendance
        SET clock_out=NULL, overtime_minutes=0, status='Clocked In'
        WHERE user_id=? AND date=?
        """,
        (uid, date_s),
    )
    _conn.commit()
    await msg.reply_text(f"↩️ Clock-out undone for user {uid} on {date_s}.")
    await bot_log(context, f"#undone\n• Admin: {escape_md(update.effective_user.full_name)}\n• Undone clock-out for {uid} on {date_s}")


# -------------------- STARTUP / MAIN --------------------
def main() -> None:
    """
    Initialize DB and start the Telegram Application with all handlers wired up.
    This function is the main entry point for the script.
    """
    print("EXC-bot starting...")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Staff commands (auto-deleting commands + confirmation + detailed logging)
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("sick", cmd_sick))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin commands
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    app.add_handler(CommandHandler("undone", cmd_undone))

    # Run the bot
    print("EXC-bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
