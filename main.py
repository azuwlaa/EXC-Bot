import io, sqlite3, re, calendar
from datetime import datetime, time
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import pandas as pd

# ---------------- CONFIG ----------------
BOT_TOKEN = ""
GROUP_ID = -1003463796946
LOG_CHANNEL_ID = -1003395196772
BOT_ADMINS = [2119444261, 624102836]
DB_FILE = "exc_bot.db"
SHIFT_START = time(hour=19, minute=45)
SHIFT_END = time(hour=23, minute=0)
AUTO_BACKUP_TIME = time(hour=0, minute=5)  # daily backup at 00:05 server time

# ---------------- DATABASE ----------------
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

# ---------------- HELPERS ----------------
def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def escape_md(text: str):
    if not text: return ""
    return re.sub(r'([_\*\[\]\(\)\~\>\#\+\-\=\|\{\}\.\!])', r'\\\1', text)

async def send_log(bot, msg: str):
    try:
        await bot.send_message(LOG_CHANNEL_ID, msg, parse_mode=ParseMode.MARKDOWN)
    except: pass

async def is_admin(context, user_id):
    try:
        admins = await context.bot.get_chat_administrators(GROUP_ID)
        return user_id in [a.user.id for a in admins] or user_id in BOT_ADMINS
    except: return user_id in BOT_ADMINS

async def admin_only(update, context):
    if not await is_admin(context, update.effective_user.id):
        await update.message.reply_text("❌ You are not allowed to use this command.")
        return False
    return True

def compute_late(clock_in_str):
    now_dt = datetime.strptime(clock_in_str, "%H:%M").time()
    late = max(0,int((datetime.combine(datetime.today(), now_dt)-datetime.combine(datetime.today(), SHIFT_START)).total_seconds()/60))
    return late

def compute_overtime(clock_out_str):
    now_dt = datetime.strptime(clock_out_str, "%H:%M").time()
    overtime = max(0,int((datetime.combine(datetime.today(), now_dt)-datetime.combine(datetime.today(), SHIFT_END)).total_seconds()/60))
    return overtime

def compute_worked(clock_in_str, clock_out_str):
    if not clock_in_str or not clock_out_str: return 0
    t1 = datetime.strptime(clock_in_str, "%H:%M")
    t2 = datetime.strptime(clock_out_str, "%H:%M")
    return round(max(0,(t2-t1).total_seconds()/3600),2)

def auto_absent():
    today = today_str()
    cur.execute("SELECT user_id, full_name FROM staff")
    for uid,name in cur.fetchall():
        cur.execute("SELECT id FROM attendance WHERE user_id=? AND date=?",(uid,today))
        if not cur.fetchone():
            cur.execute("INSERT OR IGNORE INTO attendance(user_id,full_name,date,status) VALUES(?,?,?,'Absent')", (uid,name,today))
    conn.commit()

# ---------------- STAFF COMMANDS ----------------
async def cmd_clockin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    today = today_str()
    now_time = datetime.now().strftime("%H:%M")
    cur.execute("SELECT user_id FROM staff WHERE user_id=?",(uid,))
    if not cur.fetchone():
        await update.message.reply_text("❌ You are not registered as staff.")
        return
    cur.execute("SELECT clock_in FROM attendance WHERE user_id=? AND date=?",(uid,today))
    row = cur.fetchone()
    if row and row[0]:
        await update.message.reply_text("❌ You already clocked in.")
        return
    late = compute_late(now_time)
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,clock_in,status,late_minutes)
    VALUES(?,?,?,?,?,?)
    ON CONFLICT(user_id,date)
    DO UPDATE SET clock_in=excluded.clock_in,status='Clocked In',late_minutes=excluded.late_minutes
    """,(uid,name,today,now_time,"Clocked In",late))
    conn.commit()
    text=f"🟢 [{escape_md(name)}](tg://user?id={uid}) clocked in at `{now_time}` (Late: {late}m)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str();now_time=datetime.now().strftime("%H:%M")
    cur.execute("SELECT clock_in, clock_out FROM attendance WHERE user_id=? AND date=?",(uid,today))
    row=cur.fetchone()
    if not row or not row[0]:
        await update.message.reply_text("❌ You haven't clocked in yet.")
        return
    if row[1]:
        await update.message.reply_text("❌ You already clocked out.")
        return
    ot=compute_overtime(now_time)
    worked=compute_worked(row[0],now_time)
    cur.execute("""UPDATE attendance SET clock_out=?,status='Clocked Out',overtime_minutes=?,worked_hours=? WHERE user_id=? AND date=?""",(now_time,ot,worked,uid,today))
    conn.commit()
    text=f"🔴 [{escape_md(name)}](tg://user?id={uid}) clocked out at `{now_time}` (OT: {ot}m, Worked: {worked}h)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_sick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str()
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,status) VALUES(?,?,?,?)
    ON CONFLICT(user_id,date)
    DO UPDATE SET status='Sick',clock_in=NULL,clock_out=NULL,late_minutes=0,overtime_minutes=0,worked_hours=0
    """,(uid,name,today,"Sick"))
    conn.commit()
    text=f"🤒 Marked Sick for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str()
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,status) VALUES(?,?,?,?)
    ON CONFLICT(user_id,date)
    DO UPDATE SET status='Off',clock_in=NULL,clock_out=NULL,late_minutes=0,overtime_minutes=0,worked_hours=0
    """,(uid,name,today,"Off"))
    conn.commit()
    text=f"📘 Marked Off for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

# ---------------- ADMIN COMMANDS ----------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    if not context.args or len(context.args)<2:
        await update.message.reply_text("Usage: /add <id> <Full Name>")
        return
    try: uid=int(context.args[0]); name=" ".join(context.args[1:])
    except: await update.message.reply_text("Invalid ID."); return
    cur.execute("INSERT OR REPLACE INTO staff(user_id,full_name) VALUES(?,?)",(uid,name))
    conn.commit()
    await update.message.reply_text(f"✅ Staff added: *{escape_md(name)}*",parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,f"➕ Staff added: {name} (ID:{uid})")

async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    if not context.args: await update.message.reply_text("Usage: /rm <id>"); return
    try: uid=int(context.args[0])
    except: await update.message.reply_text("Invalid ID."); return
    cur.execute("DELETE FROM staff WHERE user_id=?",(uid,))
    conn.commit()
    await update.message.reply_text("✅ Staff removed.")
    await send_log(context.bot,f"➖ Staff removed ID:{uid}")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    cur.execute("SELECT user_id,full_name FROM staff ORDER BY full_name")
    rows=cur.fetchall()
    text="*Staff list:*\n"+("\n".join([f"• [{escape_md(r[1])}](tg://user?id={r[0]})" for r in rows]) if rows else "No staff.")
    await update.message.reply_text(text,parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    today=today_str()
    cur.execute("SELECT full_name,user_id,clock_in,clock_out,status FROM attendance WHERE date=? ORDER BY clock_in",(today,))
    rows=cur.fetchall()
    text="*Today's Attendance:*\n"+("\n".join([f"• [{escape_md(r[0])}](tg://user?id={r[1]}) - `{r[2]}`" if r[2] else f"• [{escape_md(r[0])}](tg://user?id={r[1]}) - {r[4]}" for r in rows]) if rows else "No records.")
    await update.message.reply_text(text,parse_mode=ParseMode.MARKDOWN)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    if not context.args: await update.message.reply_text("Usage: /check <id>"); return
    try: uid=int(context.args[0])
    except: await update.message.reply_text("Invalid ID"); return
    cur.execute("SELECT full_name FROM staff WHERE user_id=?",(uid,))
    row=cur.fetchone()
    if not row: await update.message.reply_text("Staff not found"); return
    full_name=row[0]; month=datetime.now().strftime("%Y-%m")
    cur.execute("SELECT * FROM attendance WHERE user_id=? AND date LIKE ?",(uid,f"{month}%"))
    df=pd.DataFrame(cur.fetchall(),columns=[d[0] for d in cur.description])
    if df.empty: await update.message.reply_text("No data"); return
    bio=io.BytesIO(); bio.name=f"{full_name}_{month}.xlsx"; df.to_excel(bio,index=False); bio.seek(0)
    await update.message.reply_document(document=InputFile(bio),filename=bio.name)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    cur.execute("DELETE FROM attendance"); cur.execute("DELETE FROM staff"); conn.commit()
    await update.message.reply_text("✅ All data reset."); await send_log(context.bot,"⚠️ All data reset.")

async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    cur.execute("UPDATE attendance SET clock_in=NULL,clock_out=NULL,late_minutes=0,overtime_minutes=0,worked_hours=0,status='present'")
    conn.commit()
    await update.message.reply_text("✅ Clock data reset."); await send_log(context.bot,"⚠️ Clock data reset.")

async def cmd_undone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    if not context.args: await update.message.reply_text("Usage: /undone <id>"); return
    try: uid=int(context.args[0])
    except: await update.message.reply_text("Invalid ID"); return
    today=today_str()
    cur.execute("UPDATE attendance SET clock_out=NULL,status='Clocked In',overtime_minutes=0,worked_hours=0 WHERE user_id=? AND date=?",(uid,today))
    conn.commit()
    await update.message.reply_text("✅ Undo clock-out done."); await send_log(context.bot,f"↩️ Undo clock-out for ID:{uid}")

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    df=pd.read_sql_query("SELECT * FROM attendance",conn)
    if df.empty: await update.message.reply_text("No data to backup."); return
    bio=io.BytesIO(); bio.name=f"EXC_Backup_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    with pd.ExcelWriter(bio, engine='xlsxwriter') as w: df.to_excel(w,sheet_name='Attendance',index=False)
    bio.seek(0); await context.bot.send_document(LOG_CHANNEL_ID, document=InputFile(bio,filename=bio.name))
    await update.message.reply_text("✅ Backup sent."); await send_log(context.bot,"💾 Backup sent.")

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update,context): return
    if not context.args: await update.message.reply_text("Usage: /report <month_name>"); return
    month_name=context.args[0].capitalize()
    try: month_num=list(calendar.month_name).index(month_name)
    except: await update.message.reply_text("Invalid month name"); return
    year=datetime.now().year; month_str=f"{year}-{month_num:02d}"
    df=pd.read_sql_query("SELECT * FROM attendance WHERE date LIKE ?",conn,(f"{month_str}%",))
    if df.empty: await update.message.reply_text("No data for this month"); return
    bio=io.BytesIO(); bio.name=f"Attendance_{month_name}_{year}.xlsx"; df.to_excel(bio,index=False); bio.seek(0)
    await update.message.reply_document(document=InputFile(bio,filename=bio.name))

# ---------------- AUTO BACKUP ----------------
async def daily_backup(context: ContextTypes.DEFAULT_TYPE):
    df=pd.read_sql_query("SELECT * FROM attendance ORDER BY date",conn)
    if df.empty: return
    bio=io.BytesIO(); bio.name=f"EXC_Backup_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    with pd.ExcelWriter(bio, engine='xlsxwriter') as w: df.to_excel(w,sheet_name='Attendance',index=False)
    bio.seek(0); await context.bot.send_document(LOG_CHANNEL_ID, document=InputFile(bio,filename=bio.name))
    await send_log(context.bot,"💾 Daily backup sent.")

# ---------------- MAIN ----------------
def main():
    auto_absent()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Staff handlers
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("sick", cmd_sick))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin handlers
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    app.add_handler(CommandHandler("undone", cmd_undone))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("report", cmd_report))

    # Job queue for daily backup
    job_queue = app.job_queue
    job_queue.run_daily(daily_backup, time=AUTO_BACKUP_TIME)

    print("✅ EXC-Bot running...")
    app.run_polling()

if __name__=="__main__":
    main()
