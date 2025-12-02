import re
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
import io

from telegram import Update, InputFile
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters

import pandas as pd

# ---------------- CONFIG ----------------
BOT_TOKEN = "YOUR_BOT_TOKEN"
GROUP_ID = -1001437300434
LOG_CHANNEL_ID = -1003449720539
DB_FILE = "frc_bot.db"
BOT_ADMINS = [260161408, 744795573, 624102836]  # replace with actual admin IDs

# ---------------- UTILITIES ----------------
def gmt5_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5)

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!])', r'\\\1', text)

async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        mem = await context.bot.get_chat_member(GROUP_ID, user_id)
        return mem.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False

async def is_bot_admin(user_id: int) -> bool:
    return user_id in BOT_ADMINS

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            full_name TEXT,
            date TEXT,
            clock_in TEXT,
            clock_out TEXT,
            status TEXT,
            late_minutes INTEGER,
            overtime_minutes INTEGER
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attendance_user_date ON attendance(user_id, date)")
    conn.commit()
    conn.close()

# ---------------- STAFF MANAGEMENT ----------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    caller_id = msg.from_user.id
    if not await is_user_admin(context, caller_id):
        await msg.reply_text("❌ Only group admins can add staff.")
        return
    if msg.reply_to_message:
        user_id = msg.reply_to_message.from_user.id
        name = " ".join(context.args) if context.args else (msg.reply_to_message.from_user.full_name or str(user_id))
    else:
        if not context.args:
            await msg.reply_text("Usage: /add <id> <Full Name>  OR reply to user with `/add <Full Name>`")
            return
        try:
            user_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("Invalid user id. Usage: /add <id> <Full Name>")
            return
        name = " ".join(context.args[1:]) if len(context.args) > 1 else str(user_id)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO staff (user_id, full_name) VALUES (?, ?)", (user_id, name))
    conn.commit()
    conn.close()
    await msg.reply_text(f"✅ Staff added: *{escape_markdown(name)}*", parse_mode=ParseMode.MARKDOWN)

async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    caller_id = msg.from_user.id
    if not await is_user_admin(context, caller_id):
        await msg.reply_text("❌ Only group admins can remove staff.")
        return
    if msg.reply_to_message:
        user_id = msg.reply_to_message.from_user.id
    elif context.args:
        try:
            user_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("Invalid user id.")
            return
    else:
        await msg.reply_text("Usage: reply to user with /rm or use /rm <id>")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM staff WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    await msg.reply_text("✅ Staff removed.")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, full_name FROM staff ORDER BY full_name COLLATE NOCASE")
    rows = cur.fetchall()
    conn.close()
    lines = [f"• **[{escape_markdown(n)}](tg://user?id={uid})**" for uid, n in rows]
    text = f"*Staff list ({len(rows)} total):*\n" + ("\n".join(lines) if lines else "No staff added yet.")
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ---------------- CLOCK-IN / CLOCK-OUT ----------------
async def cmd_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    user = msg.from_user
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT full_name FROM staff WHERE user_id=?", (user.id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await msg.reply_text("❌ You are not in staff list.")
        return
    full_name = row[0]
    today = gmt5_now().strftime("%Y-%m-%d")
    now = gmt5_now()
    
    shift_start = now.replace(hour=19, minute=45, second=0, microsecond=0)
    shift_end = now.replace(hour=23, minute=0, second=0, microsecond=0)

    cur.execute("SELECT id, clock_in, clock_out FROM attendance WHERE user_id=? AND date=?", (user.id, today))
    record = cur.fetchone()
    now_str = now.strftime("%H:%M")
    
    if not record:
        late_minutes = max(0, int((now - shift_start).total_seconds() // 60))
        cur.execute("""
            INSERT INTO attendance (user_id, full_name, date, clock_in, status, late_minutes, overtime_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user.id, full_name, today, now_str, "Clocked In", late_minutes, 0))
        conn.commit()
        conn.close()
        await msg.reply_text(
            f"✅ [{escape_markdown(full_name)}](tg://user?id={user.id}) clocked in at `{now_str}` (Late: {late_minutes} min)",
            parse_mode=ParseMode.MARKDOWN
        )
    elif record[1] and not record[2]:
        overtime_minutes = max(0, int((now - shift_end).total_seconds() // 60)) if now > shift_end else 0
        cur.execute("UPDATE attendance SET clock_out=?, status='Clocked Out', overtime_minutes=? WHERE id=?", (now_str, overtime_minutes, record[0]))
        conn.commit()
        conn.close()
        await msg.reply_text(
            f"✅ [{escape_markdown(full_name)}](tg://user?id={user.id}) clocked out at `{now_str}` (Overtime: {overtime_minutes} min)",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        conn.close()
        await msg.reply_text("❌ You have already clocked out today.")

# ---------------- SICK / OFF ----------------
async def cmd_sick_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    user = msg.from_user
    cmd = msg.text.split()[0].lstrip("/").lower()
    status = "Sick" if cmd=="sick" else "Off" if cmd=="off" else None
    if not status:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT full_name FROM staff WHERE user_id=?", (user.id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await msg.reply_text("❌ You are not in staff list.")
        return
    full_name = row[0]
    today = gmt5_now().strftime("%Y-%m-%d")
    cur.execute("""
        INSERT OR REPLACE INTO attendance (user_id, full_name, date, clock_in, clock_out, status, late_minutes, overtime_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user.id, full_name, today, None, None, status, 0, 0))
    conn.commit()
    conn.close()
    await msg.reply_text(f"✅ Marked {status} for [{escape_markdown(full_name)}](tg://user?id={user.id}) on {today}", parse_mode=ParseMode.MARKDOWN)

# ---------------- SHOW / STATUS / CHECK ----------------
async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    caller = msg.from_user
    if not await is_user_admin(context, caller.id):
        await msg.reply_text("❌ Only group admins can use /show.")
        return
    if msg.reply_to_message:
        staff_id = msg.reply_to_message.from_user.id
    elif context.args:
        try:
            staff_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("Provide a valid Telegram ID or reply to a staff message.")
            return
    else:
        await msg.reply_text("Reply to the staff message or use `/show <id>`.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT full_name FROM staff WHERE user_id=?", (staff_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await msg.reply_text("Staff not found.")
        return
    full_name = row[0]
    now = gmt5_now()
    month_prefix = now.strftime("%Y-%m")
    cur.execute("""
        SELECT status, COUNT(*)
        FROM attendance
        WHERE user_id=? AND date LIKE ?
        GROUP BY status
    """, (staff_id, f"{month_prefix}%"))
    data = cur.fetchall()
    conn.close()
    total_clocked = absent = sick = off = 0
    for status, count in data:
        if status=="Clocked In" or status=="Clocked Out": total_clocked=count
        elif status=="Absent": absent=count
        elif status=="Sick": sick=count
        elif status=="Off": off=count
    text = (
        f"*Attendance Summary for {escape_markdown(full_name)}*\n"
        f"• Total Days Clocked: {total_clocked}\n"
        f"• Absent Days: {absent}\n"
        f"• Sick Days: {sick}\n"
        f"• Off Days: {off}"
    )
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    today = gmt5_now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT full_name, user_id, clock_in, clock_out, status
        FROM attendance
        WHERE date=?
        ORDER BY clock_in
    """, (today,))
    rows = cur.fetchall()
    conn.close()
    lines=[]
    for full_name, uid, clock_in, clock_out, status in rows:
        if clock_in:
            line = f"• **[{escape_markdown(full_name)}](tg://user?id={uid})** - In: `{clock_in}`"
            if clock_out:
                line += f", Out: `{clock_out}`"
        else:
            line = f"• **[{escape_markdown(full_name)}](tg://user?id={uid})** - {status}"
        lines.append(line)
    text = f"*Clocked-in Staff for {today}:*\n" + ("\n".join(lines) if lines else "No staff have clocked in yet.")
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # ---------------- Auto mark absences ----------------
    today = gmt5_now().strftime("%Y-%m-%d")
    cur.execute("SELECT user_id FROM staff")
    all_staff = [r[0] for r in cur.fetchall()]
    for uid in all_staff:
        cur.execute("SELECT id FROM attendance WHERE user_id=? AND date=?", (uid, today))
        if not cur.fetchone():
            cur.execute("SELECT full_name FROM staff WHERE user_id=?", (uid,))
            name = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO attendance (user_id, full_name, date, clock_in, clock_out, status, late_minutes, overtime_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (uid, name, today, None, None, "Absent", 0, 0))
    conn.commit()

    # Determine staff to show
    if msg.reply_to_message:
        staff_id = msg.reply_to_message.from_user.id
    elif context.args:
        try:
            staff_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("Provide a valid Telegram ID or reply to a staff message.")
            return
    else:
        staff_id = msg.from_user.id

    cur.execute("SELECT full_name FROM staff WHERE user_id=?", (staff_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await msg.reply_text("Staff not found.")
        return
    full_name = row[0]

    now = gmt5_now()
    month_prefix = now.strftime("%Y-%m")
    cur.execute("""
        SELECT date, clock_in, clock_out, status, late_minutes, overtime_minutes
        FROM attendance
        WHERE user_id=? AND date LIKE ?
        ORDER BY date
    """, (staff_id, f"{month_prefix}%"))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await msg.reply_text("No attendance records found for this month.")
        return

    total_days = len(rows)
    total_late_minutes = sum(r[4] or 0 for r in rows)
    total_overtime_minutes = sum(r[5] or 0 for r in rows)
    total_hours_worked = 0.0
    details = []

    for date_str, clock_in, clock_out, status, late, overtime in rows:
        hours = 0.0
        if clock_in and clock_out:
            t1 = datetime.strptime(f"{date_str} {clock_in}", "%Y-%m-%d %H:%M")
            t2 = datetime.strptime(f"{date_str} {clock_out}", "%Y-%m-%d %H:%M")
            hours = round((t2 - t1).total_seconds() / 3600, 2)
            total_hours_worked += hours
        details.append(f"• {date_str}: In: {clock_in or '-'}, Out: {clock_out or '-'}, Status: {status}, Late: {late} min, Overtime: {overtime} min, Worked: {hours}h")

    summary = (
        f"*Attendance Summary for {escape_markdown(full_name)} - {now.strftime('%B %Y')}*\n"
        f"• Total Days: {total_days}\n"
        f"• Total Late Minutes: {total_late_minutes}\n"
        f"• Total Overtime Minutes: {total_overtime_minutes}\n"
        f"• Total Hours Worked: {round(total_hours_worked,2)}\n\n"
        f"*Daily Details:*\n" + "\n".join(details)
    )

    await msg.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

# ---------------- REPORT / BACKUP / RESET ----------------
async def cmd_report_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM attendance", conn)
    conn.close()
    if df.empty:
        await msg.reply_text("No attendance data found.")
        return
    bio = io.BytesIO()
    bio.name = f"Attendance_{gmt5_now().strftime('%Y-%m')}.xlsx"
    df.to_excel(bio, index=False)
    bio.seek(0)
    await msg.reply_document(document=InputFile(bio), filename=bio.name)

async def cmd_reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_bot_admin(msg.from_user.id):
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM attendance")
    conn.commit()
    conn.close()
    await msg.reply_text("✅ All attendance data reset.")

async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_bot_admin(msg.from_user.id):
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM attendance WHERE status='Clocked In'")
    conn.commit()
    conn.close()
    await msg.reply_text("✅ Clock-in data reset.")

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_bot_admin(msg.from_user.id):
        return
    conn = sqlite3.connect(DB_FILE)
    df_attendance = pd.read_sql_query("SELECT * FROM attendance", conn)
    conn.close()
    if df_attendance.empty:
        await msg.reply_text("No data to backup.")
        return
    bio = io.BytesIO()
    bio.name = f"Attendance_Backup_{gmt5_now().strftime('%Y-%m-%d_%H%M')}.xlsx"
    with pd.ExcelWriter(bio, engine='xlsxwriter') as writer:
        df_attendance.to_excel(writer, sheet_name='Attendance', index=False)
    bio.seek(0)
    await msg.reply_document(document=InputFile(bio), filename=bio.name)
    await msg.reply_text("✅ Backup generated.")

# ---------------- BOOT ----------------
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Staff
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))
    # Clock
    app.add_handler(CommandHandler("clock", cmd_clock))
    # Sick/off
    app.add_handler(CommandHandler("sick", cmd_sick_off))
    app.add_handler(CommandHandler("off", cmd_sick_off))
    # Show/status/check
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    # Reports/backups/resets
    app.add_handler(CommandHandler("report", cmd_report_attendance))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("reset", cmd_reset_all))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    print("✅ FRC Clock Bot running.")
    app.run_polling()

if __name__ == "__main__":
    main()
