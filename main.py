#!/usr/bin/env python3
"""
EXC-Bot: Attendance tracking bot
Shift: 19:45 -> 23:00
Overtime: after 23:00
Staff: /clockin, /clockout, /sick, /off
Admins: all other commands
Logs to LOG_CHANNEL_ID
"""

import io
import sqlite3
from datetime import datetime, time, timedelta
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import pandas as pd

# -------------------- CONFIG --------------------
BOT_TOKEN = ""
GROUP_ID = -1003463796946
LOG_CHANNEL_ID = -1003395196772
BOT_ADMINS = [2119444261, 624102836]
DB_FILE = "exc_bot.db"
SHIFT_START = time(hour=19, minute=45)
SHIFT_END = time(hour=23, minute=0)

# -------------------- DATABASE --------------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS staff(
    user_id INTEGER PRIMARY KEY,
    full_name TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS attendance(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    full_name TEXT,
    date TEXT,
    clock_in TEXT,
    clock_out TEXT,
    late_minutes INTEGER DEFAULT 0,
    overtime_minutes INTEGER DEFAULT 0,
    worked_hours REAL DEFAULT 0,
    status TEXT DEFAULT 'present',
    UNIQUE(user_id,date)
)
""")
conn.commit()

# -------------------- HELPERS --------------------
def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def escape_md(text: str):
    import re
    if not text:
        return ""
    return re.sub(r'([_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!])', r'\\\1', text)

async def send_log(bot, msg: str):
    try:
        await bot.send_message(LOG_CHANNEL_ID, msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print("Log error:", e)

async def is_admin(context, user_id):
    try:
        admins = await context.bot.get_chat_administrators(GROUP_ID)
        admin_ids = [a.user.id for a in admins]
        return user_id in admin_ids or user_id in BOT_ADMINS
    except:
        return user_id in BOT_ADMINS

async def admin_only(update, context):
    uid = update.effective_user.id
    if not await is_admin(context, uid):
        await update.message.reply_text("❌ You are not allowed to use this command.")
        return False
    return True

def compute_late_minutes(clock_in_str):
    now_dt = datetime.strptime(clock_in_str, "%H:%M").time()
    late = max(0, int((datetime.combine(datetime.today(), now_dt) -
                        datetime.combine(datetime.today(), SHIFT_START)).total_seconds() // 60))
    return late

def compute_overtime(clock_out_str):
    now_dt = datetime.strptime(clock_out_str, "%H:%M").time()
    overtime = max(0, int((datetime.combine(datetime.today(), now_dt) -
                           datetime.combine(datetime.today(), SHIFT_END)).total_seconds() // 60))
    return overtime

def compute_worked_hours(clock_in_str, clock_out_str):
    if not clock_in_str or not clock_out_str:
        return 0
    t1 = datetime.strptime(clock_in_str, "%H:%M")
    t2 = datetime.strptime(clock_out_str, "%H:%M")
    worked = (t2 - t1).total_seconds() / 3600
    return round(max(0, worked), 2)

def auto_absent():
    """Mark absent staff automatically if not clocked in today"""
    today = today_str()
    cur.execute("SELECT user_id, full_name FROM staff")
    staff_rows = cur.fetchall()
    for uid, name in staff_rows:
        cur.execute("SELECT id FROM attendance WHERE user_id=? AND date=?", (uid, today))
        if not cur.fetchone():
            cur.execute("""
                INSERT OR IGNORE INTO attendance(user_id, full_name, date, status)
                VALUES (?, ?, ?, 'Absent')
            """, (uid, name, today))
    conn.commit()

# -------------------- STAFF COMMANDS --------------------
async def cmd_clockin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    today = today_str()
    now_time = datetime.now().strftime("%H:%M")

    # Check if staff
    cur.execute("SELECT user_id FROM staff WHERE user_id=?", (uid,))
    if not cur.fetchone():
        await update.message.reply_text("❌ You are not registered as staff.")
        return

    # Already clocked in
    cur.execute("SELECT clock_in FROM attendance WHERE user_id=? AND date=?", (uid, today))
    row = cur.fetchone()
    if row and row[0]:
        await update.message.reply_text("❌ You already clocked in.")
        return

    late_m = compute_late_minutes(now_time)
    cur.execute("""
        INSERT INTO attendance(user_id, full_name, date, clock_in, status, late_minutes)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET clock_in=excluded.clock_in, status='Clocked In', late_minutes=excluded.late_minutes
    """, (uid, name, today, now_time, "Clocked In", late_m))
    conn.commit()

    text = f"🟢 [{escape_md(name)}](tg://user?id={uid}) clocked in at `{now_time}` (Late: {late_m}m)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot, text)

async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    today = today_str()
    now_time = datetime.now().strftime("%H:%M")

    cur.execute("SELECT clock_in, clock_out FROM attendance WHERE user_id=? AND date=?", (uid, today))
    row = cur.fetchone()
    if not row or not row[0]:
        await update.message.reply_text("❌ You haven't clocked in yet.")
        return
    if row[1]:
        await update.message.reply_text("❌ You already clocked out.")
        return

    ot = compute_overtime(now_time)
    worked = compute_worked_hours(row[0], now_time)
    cur.execute("""
        UPDATE attendance
        SET clock_out=?, status='Clocked Out', overtime_minutes=?, worked_hours=?
        WHERE user_id=? AND date=?
    """, (now_time, ot, worked, uid, today))
    conn.commit()

    text = f"🔴 [{escape_md(name)}](tg://user?id={uid}) clocked out at `{now_time}` (OT: {ot}m, Worked: {worked}h)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot, text)

async def cmd_sick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    today = today_str()
    cur.execute("""
        INSERT INTO attendance(user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Sick', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0, worked_hours=0
    """, (uid, name, today, "Sick"))
    conn.commit()
    text = f"🤒 Marked Sick for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot, text)

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    today = today_str()
    cur.execute("""
        INSERT INTO attendance(user_id, full_name, date, status)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id,date)
        DO UPDATE SET status='Off', clock_in=NULL, clock_out=NULL, late_minutes=0, overtime_minutes=0, worked_hours=0
    """, (uid, name, today, "Off"))
    conn.commit()
    text = f"📘 Marked Off for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot, text)

# -------------------- ADMIN COMMANDS --------------------
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    auto_absent()
    msg = update.message
    now = datetime.now()
    month = now.strftime("%Y-%m")
    if msg.reply_to_message:
        uid = msg.reply_to_message.from_user.id
    elif context.args:
        uid = int(context.args[0])
    else:
        await msg.reply_text("Usage: reply or /check <user_id>")
        return

    cur.execute("SELECT full_name FROM staff WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row:
        await msg.reply_text("Staff not found.")
        return
    name = row[0]

    cur.execute("""
        SELECT date, clock_in, clock_out, status, late_minutes, overtime_minutes, worked_hours
        FROM attendance
        WHERE user_id=? AND date LIKE ?
        ORDER BY date
    """, (uid, f"{month}%"))
    rows = cur.fetchall()
    if not rows:
        await msg.reply_text("No records this month.")
        return

    total_late = sum(r[4] for r in rows)
    total_ot = sum(r[5] for r in rows)
    total_hours = sum(r[6] for r in rows)
    details = []
    for d, cin, cout, st, late, ot, wh in rows:
        details.append(f"• {d} — In:{cin or '-'} Out:{cout or '-'} {st} Late:{late}m OT:{ot}m Worked:{wh}h")

    text = f"*Summary for {escape_md(name)} — {now.strftime('%B %Y')}*\n" \
           f"• Total Late: {total_late} minutes\n" \
           f"• Total OT: {total_ot} minutes\n" \
           f"• Total Hours Worked: {round(total_hours,2)}\n\n" \
           "*Daily Records:* \n" + "\n".join(details)
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    auto_absent()
    df = pd.read_sql_query("SELECT * FROM attendance ORDER BY date", conn)
    if df.empty:
        await update.message.reply_text("No data.")
        return
    bio = io.BytesIO()
    bio.name = f"EXC_Attendance_{datetime.now().strftime('%Y-%m')}.xlsx"
    with pd.ExcelWriter(bio, engine='xlsxwriter') as w:
        df.to_excel(w, index=False)
    bio.seek(0)
    await update.message.reply_document(InputFile(bio, filename=bio.name))
    await send_log(context.bot, "📁 Attendance report sent.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    auto_absent()
    today = today_str()
    cur.execute("SELECT full_name, user_id, clock_in, clock_out, status FROM attendance WHERE date=? ORDER BY full_name", (today,))
    rows = cur.fetchall()
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

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    await update.message.reply_document(InputFile(DB_FILE))
    await send_log(context.bot, "💾 Database backup sent.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    cur.execute("DELETE FROM attendance")
    conn.commit()
    await update.message.reply_text("✅ All attendance cleared.")
    await send_log(context.bot, "⚠️ Admin cleared all attendance.")

async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    cur.execute("DELETE FROM attendance WHERE date=?", (today_str(),))
    conn.commit()
    await update.message.reply_text("✅ Today's attendance cleared.")
    await send_log(context.bot, "♻️ Admin reset today's attendance.")

async def cmd_undone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /undone <user_id>")
        return
    uid = int(context.args[0])
    cur.execute("UPDATE attendance SET clock_out=NULL, overtime_minutes=0, worked_hours=0, status='Clocked In' WHERE user_id=? AND date=?", (uid, today_str()))
    conn.commit()
    await update.message.reply_text("✅ Clock-out undone.")
    await send_log(context.bot, f"↩️ Clock-out undone for {uid} today.")

# -------------------- STAFF MANAGEMENT --------------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add <id> <Full Name>")
        return
    uid = int(context.args[0])
    name = " ".join(context.args[1:])
    cur.execute("INSERT OR REPLACE INTO staff(user_id, full_name) VALUES (?,?)", (uid, name))
    conn.commit()
    await update.message.reply_text(f"✅ Added staff: {name}")
    await send_log(context.bot, f"➕ Added staff {uid} ({escape_md(name)})")

async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /rm <id>")
        return
    uid = int(context.args[0])
    cur.execute("DELETE FROM staff WHERE user_id=?", (uid,))
    conn.commit()
    await update.message.reply_text(f"✅ Removed staff {uid}")
    await send_log(context.bot, f"➖ Removed staff {uid}")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context): return
    cur.execute("SELECT user_id, full_name FROM staff ORDER BY full_name")
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("No staff found.")
        return
    text = "\n".join([f"• [{escape_md(n)}](tg://user?id={uid})" for uid, n in rows])
    await update.message.reply_text("*Staff List:*\n" + text, parse_mode=ParseMode.MARKDOWN)

# -------------------- MAIN --------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Staff commands
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("sick", cmd_sick))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin commands
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    app.add_handler(CommandHandler("undone", cmd_undone))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))

    print("✅ EXC-Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
