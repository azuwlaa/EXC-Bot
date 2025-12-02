import os
import re
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from telegram import Update, InputFile
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------- CONFIG --------------------
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
GROUP_ID = -1003463796946
LOG_CHANNEL_ID = -1003395196772
BOT_ADMINS = [2119444261, 624102836]
DB_FILE = "exc_bot.db"
SHIFT_START = "19:45"
SHIFT_END = "23:00"

# -------------------- TIME HELPERS --------------------
def gmt5_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5)

def today_str() -> str:
    return gmt5_now().strftime("%Y-%m-%d")

def hhmm_to_dt(hhmm: str, ref: Optional[datetime] = None) -> datetime:
    ref_dt = ref or gmt5_now()
    hh, mm = map(int, hhmm.split(":"))
    return ref_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)

def escape_md(t: str) -> str:
    if not t:
        return ""
    return re.sub(r'([_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!])', r'\\\1', t)

# -------------------- DATABASE --------------------
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_cur = _conn.cursor()

def init_db():
    _cur.execute("""
        CREATE TABLE IF NOT EXISTS staff(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT
        )
    """)
    _cur.execute("""
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
    """)
    _cur.execute("CREATE INDEX IF NOT EXISTS idx_att_user_date ON attendance(user_id,date)")
    _conn.commit()

# -------------------- LOGGING --------------------
async def bot_log(bot, text: str):
    try:
        await bot.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print("LOG ERROR:", e)

# -------------------- ADMIN CHECK --------------------
async def is_group_admin(context, user_id):
    try:
        m = await context.bot.get_chat_member(GROUP_ID, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False

async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in BOT_ADMINS:
        return True
    if await is_group_admin(context, uid):
        return True
    await update.message.reply_text("❌ You are not allowed to use this command.")
    return False

# -------------------- STAFF COMMANDS --------------------
async def cmd_clockin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    uid = user.id
    name = user.full_name
    today = today_str()
    now = gmt5_now()
    now_s = now.strftime("%H:%M")

    _cur.execute("SELECT user_id FROM staff WHERE user_id=?", (uid,))
    if not _cur.fetchone():
        await msg.reply_text("❌ You are not registered as staff.")
        return

    _cur.execute("SELECT clock_in FROM attendance WHERE user_id=? AND date=?", (uid, today))
    rec = _cur.fetchone()
    if rec and rec[0]:
        await msg.reply_text("❌ You already clocked in.")
        return

    shift_start_dt = hhmm_to_dt(SHIFT_START, now)
    late_m = max(0, int((now - shift_start_dt).total_seconds() // 60))

    _cur.execute("""
        INSERT INTO attendance (user_id, full_name, date, clock_in, status, late_minutes)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET clock_in=excluded.clock_in, status='Clocked In', late_minutes=excluded.late_minutes
    """, (uid, name, today, now_s, "Clocked In", late_m))
    _conn.commit()

    text = f"✅ [{escape_md(name)}](tg://user?id={uid}) clocked in at `{now_s}` (Late: {late_m}m)"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await bot_log(context.bot, "🟢 " + text)

async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    uid = user.id
    name = user.full_name
    today = today_str()
    now = gmt5_now()
    now_s = now.strftime("%H:%M")

    _cur.execute("SELECT clock_in, clock_out FROM attendance WHERE user_id=? AND date=?", (uid, today))
    rec = _cur.fetchone()
    if not rec or not rec[0]:
        await msg.reply_text("❌ You haven't clocked in.")
        return
    if rec[1]:
        await msg.reply_text("❌ You already clocked out.")
        return

    shift_end_dt = hhmm_to_dt(SHIFT_END, now)
    overtime = max(0, int((now - shift_end_dt).total_seconds() // 60)) if now > shift_end_dt else 0

    _cur.execute("""
        UPDATE attendance
        SET clock_out=?, status='Clocked Out', overtime_minutes=?
        WHERE user_id=? AND date=?
    """, (now_s, overtime, uid, today))
    _conn.commit()

    text = f"🔴 [{escape_md(name)}](tg://user?id={uid}) clocked out at `{now_s}` (OT: {overtime}m)"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await bot_log(context.bot, "🔴 " + text)

async def cmd_sick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    uid = user.id
    name = user.full_name
    today = today_str()

    _cur.execute("""
        INSERT INTO attendance (user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Sick', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0
    """, (uid, name, today, "Sick"))
    _conn.commit()

    text = f"🤒 Marked Sick for [{escape_md(name)}](tg://user?id={uid})"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await bot_log(context.bot, text)

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    uid = user.id
    name = user.full_name
    today = today_str()

    _cur.execute("""
        INSERT INTO attendance (user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Off', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0
    """, (uid, name, today, "Off"))
    _conn.commit()

    text = f"📘 Marked Off for [{escape_md(name)}](tg://user?id={uid})"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await bot_log(context.bot, text)

# -------------------- ADMIN HELPERS --------------------
def auto_absent():
    today = today_str()
    _cur.execute("SELECT user_id, full_name FROM staff")
    staff_rows = _cur.fetchall()
    for uid, name in staff_rows:
        _cur.execute("SELECT id FROM attendance WHERE user_id=? AND date=?", (uid, today))
        if not _cur.fetchone():
            _cur.execute("""
                INSERT OR IGNORE INTO attendance(user_id, full_name, date, status)
                VALUES (?, ?, ?, 'Absent')
            """, (uid, name, today))
    _conn.commit()

# -------------------- ADMIN COMMANDS --------------------
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    auto_absent()
    msg = update.message
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

    _cur.execute("""
        SELECT date, clock_in, clock_out, status, late_minutes, overtime_minutes
        FROM attendance
        WHERE user_id=? AND date LIKE ?
        ORDER BY date
    """, (uid, f"{month}%"))
    rows = _cur.fetchall()
    if not rows:
        await msg.reply_text("No records this month.")
        return

    total_late = sum((r[4] or 0) for r in rows)
    total_ot = sum((r[5] or 0) for r in rows)
    total_hours = 0.0
    details = []

    for d, cin, cout, st, late, ot in rows:
        worked = 0
        if cin and cout:
            t1 = datetime.strptime(f"{d} {cin}", "%Y-%m-%d %H:%M")
            t2 = datetime.strptime(f"{d} {cout}", "%Y-%m-%d %H:%M")
            worked = round((t2 - t1).total_seconds() / 3600, 2)
            total_hours += worked
        details.append(f"• {d} — In:{cin or '-'} Out:{cout or '-'} {st} Late:{late}m OT:{ot}m Worked:{worked}h")

    text = (
        f"*Summary for {escape_md(name)} — {now.strftime('%B %Y')}*\n"
        f"• Total Late: {total_late} minutes\n"
        f"• Total OT: {total_ot} minutes\n"
        f"• Total Hours: {round(total_hours,2)}\n\n"
        "*Daily:* \n" + "\n".join(details)
    )
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    df = pd.read_sql_query("""
        SELECT user_id, full_name, date, clock_in, clock_out, status, late_minutes, overtime_minutes
        FROM attendance
        ORDER BY date
    """, _conn)
    if df.empty:
        await update.message.reply_text("No data.")
        return
    bio = io.BytesIO()
    fname = f"exc_report_{gmt5_now().strftime('%Y-%m')}.xlsx"
    bio.name = fname
    with pd.ExcelWriter(bio, engine="xlsxwriter") as w:
        df.to_excel(w, index=False, sheet_name="Attendance")
    bio.seek(0)
    await update.message.reply_document(InputFile(bio, filename=fname))
    await bot_log(context.bot, "📁 Report sent.")

async def cmd_undone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    msg = update.message
    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
        date_s = context.args[0] if context.args else today_str()
    else:
        if len(context.args) < 1:
            await msg.reply_text("Usage: /undone <user_id> <date>")
            return
        uid = int(context.args[0])
        date_s = context.args[1] if len(context.args) > 1 else today_str()
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except ValueError:
        await msg.reply_text("❌ Invalid date format. Use YYYY-MM-DD.")
        return
    _cur.execute("""
        UPDATE attendance
        SET clock_out=NULL, overtime_minutes=0, status='Clocked In'
        WHERE user_id=? AND date=?
    """, (uid, date_s))
    _conn.commit()
    await msg.reply_text(f"↩️ Clock-out undone for user {uid} on {date_s}.")
    await bot_log(context.bot, f"↩️ Admin undone clock-out for user {uid} on {date_s}")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    _cur.execute("DELETE FROM attendance")
    _conn.commit()
    await update.message.reply_text("Attendance cleared.")
    await bot_log(context.bot, "⚠️ Admin cleared all attendance.")

async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    t = today_str()
    _cur.execute("DELETE FROM attendance WHERE date=?", (t,))
    _conn.commit()
    await update.message.reply_text("Today's attendance cleared.")
    await bot_log(context.bot, "♻️ Admin reset today's attendance.")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    msg = update.message
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
    await msg.reply_text(f"Added staff: {name}")
    await bot_log(context.bot, f"➕ Added staff {uid} ({escape_md(name)})")

async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    msg = update.message
    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
    else:
        if not context.args:
            await msg.reply_text("Usage: /rm <id> OR reply")
            return
        uid = int(context.args[0])
    _cur.execute("DELETE FROM staff WHERE user_id=?", (uid,))
    _conn.commit()
    await msg.reply_text("Removed staff.")
    await bot_log(context.bot, f"➖ Removed staff {uid}")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    _cur.execute("SELECT user_id, full_name FROM staff ORDER BY full_name")
    rows = _cur.fetchall()
    if not rows:
        await update.message.reply_text("No staff.")
        return
    lines = [f"• [{escape_md(n)}](tg://user?id={uid})" for uid, n in rows]
    await update.message.reply_text("*Staff List:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# -------------------- MAIN --------------------
def main():
    print("EXC-bot running…")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Staff commands
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("sick", cmd_sick))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin staff management
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))

    # Admin attendance management
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    app.add_handler(CommandHandler("undone", cmd_undone))

    app.run_polling()

if __name__ == "__main__":
    main()
