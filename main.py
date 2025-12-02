import io, sqlite3, re, calendar
from datetime import datetime, time as dt_time
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, JobQueue
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import pandas as pd

# ---------------- CONFIG ----------------
BOT_TOKEN = ""
GROUP_ID = -1003463796946
LOG_CHANNEL_ID = -1003395196772
BOT_ADMINS = [2119444261, 624102836]
DB_FILE = "exc_bot.db"
SHIFT_START = dt_time(hour=19, minute=45)
SHIFT_END = dt_time(hour=23, minute=0)
AUTO_BACKUP_TIME = dt_time(hour=0, minute=5)

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

def compute_late(clock_in_str):
    now_dt = datetime.strptime(clock_in_str, "%H:%M").time()
    late = max(0,int((datetime.combine(datetime.today(), now_dt)-datetime.combine(datetime.today(), SHIFT_START)).total_seconds()/60))
    return late

def compute_overtime(clock_out_str):
    now_dt = datetime.strptime(clock_out_str, "%H:%M").time()
    overtime = max(0,int((datetime.combine(datetime.today(), now_dt)-datetime.combine(datetime.today(), SHIFT_END)).total_seconds()/60))
    return max(0,overtime)

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
    user = update.effective_user;uid=user.id;name=user.full_name;today=today_str();now_time=datetime.now().strftime("%H:%M")
    cur.execute("SELECT user_id FROM staff WHERE user_id=?",(uid,))
    if not cur.fetchone():
        await update.message.reply_text("❌ You are not registered as staff."); return
    cur.execute("SELECT clock_in FROM attendance WHERE user_id=? AND date=?",(uid,today))
    row = cur.fetchone()
    if row and row[0]:
        await update.message.reply_text("❌ Already clocked in."); return
    late = compute_late(now_time)
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,clock_in,status,late_minutes)
    VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET clock_in=excluded.clock_in,status='Clocked In',late_minutes=excluded.late_minutes""",
    (uid,name,today,now_time,"Clocked In",late))
    conn.commit()
    text=f"🟢 [{escape_md(name)}](tg://user?id={uid}) clocked in at `{now_time}` (Late: {late}m)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_clockout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str();now_time=datetime.now().strftime("%H:%M")
    cur.execute("SELECT clock_in, clock_out FROM attendance WHERE user_id=? AND date=?",(uid,today))
    row=cur.fetchone()
    if not row or not row[0]:
        await update.message.reply_text("❌ Haven't clocked in."); return
    if row[1]:
        await update.message.reply_text("❌ Already clocked out."); return
    ot=compute_overtime(now_time)
    worked=compute_worked(row[0],now_time)
    cur.execute("""UPDATE attendance SET clock_out=?,status='Clocked Out',overtime_minutes=?,worked_hours=? WHERE user_id=? AND date=?""",
                (now_time,ot,worked,uid,today))
    conn.commit()
    text=f"🔴 [{escape_md(name)}](tg://user?id={uid}) clocked out at `{now_time}` (OT: {ot}m, Worked: {worked}h)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_sick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str()
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,status) VALUES(?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET status='Sick',clock_in=NULL,clock_out=NULL,late_minutes=0,overtime_minutes=0,worked_hours=0""",(uid,name,today,"Sick"))
    conn.commit()
    text=f"🤒 Marked Sick for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user;uid=user.id;name=user.full_name;today=today_str()
    cur.execute("""INSERT INTO attendance(user_id,full_name,date,status) VALUES(?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET status='Off',clock_in=NULL,clock_out=NULL,late_minutes=0,overtime_minutes=0,worked_hours=0""",(uid,name,today,"Off"))
    conn.commit()
    text=f"📘 Marked Off for [{escape_md(name)}](tg://user?id={uid})"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_log(context.bot,text)

# ---------------- ADMIN COMMANDS ----------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    args=context.args
    if not args or len(args)<2: 
        await update.message.reply_text("Usage: /add <id> <Full Name>"); return
    try: uid=int(args[0]); name=" ".join(args[1:])
    except: await update.message.reply_text("Invalid ID"); return
    cur.execute("INSERT OR REPLACE INTO staff(user_id,full_name) VALUES(?,?)",(uid,name)); conn.commit()
    await update.message.reply_text(f"✅ Staff added: *{escape_md(name)}*",parse_mode=ParseMode.MARKDOWN)

async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    args=context.args
    if not args: await update.message.reply_text("Usage: /rm <id>"); return
    try: uid=int(args[0])
    except: await update.message.reply_text("Invalid ID"); return
    cur.execute("DELETE FROM staff WHERE user_id=?",(uid,)); conn.commit()
    await update.message.reply_text("✅ Staff removed.")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    cur.execute("SELECT user_id,full_name FROM staff ORDER BY full_name"); rows=cur.fetchall()
    lines=[f"• **[{escape_md(n)}](tg://user?id={uid})**" for uid,n in rows]
    text=f"*Staff list ({len(rows)} total):*\n"+"\n".join(lines)
    await update.message.reply_text(text,parse_mode=ParseMode.MARKDOWN)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    args=context.args
    if not args: await update.message.reply_text("Usage: /check <user_id>"); return
    try: uid=int(args[0])
    except: await update.message.reply_text("Invalid ID"); return
    month_prefix=datetime.now().strftime("%Y-%m")
    cur.execute("SELECT full_name FROM staff WHERE user_id=?",(uid,))
    row=cur.fetchone(); 
    if not row: await update.message.reply_text("Staff not found."); return
    name=row[0]
    cur.execute("SELECT date,clock_in,clock_out,late_minutes,overtime_minutes,worked_hours,status FROM attendance WHERE user_id=? AND date LIKE ?",(uid,f"{month_prefix}%"))
    data=cur.fetchall()
    if not data: await update.message.reply_text("No attendance data this month."); return
    total_hours=sum(d[5] for d in data if d[5]); total_ot=sum(d[4] for d in data); days=len(data)
    text=f"*Attendance for {escape_md(name)} - {datetime.now().strftime('%B %Y')}*\n• Days: {days}\n• Total Hours Worked: {total_hours}\n• Total Overtime: {total_ot} minutes"
    await update.message.reply_text(text,parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    today=today_str()
    cur.execute("SELECT full_name,user_id,clock_in,clock_out,status FROM attendance WHERE date=?",(today,))
    rows=cur.fetchall()
    lines=[]
    for n,uid,cin,cout,st in rows:
        if cin: lines.append(f"• **[{escape_md(n)}](tg://user?id={uid})** - `{cin}` / `{cout or '--'}`")
        else: lines.append(f"• **[{escape_md(n)}](tg://user?id={uid})** - {st}")
    text=f"*Today's Staff:* \n"+"\n".join(lines) if lines else "No staff today."
    await update.message.reply_text(text,parse_mode=ParseMode.MARKDOWN)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    cur.execute("DELETE FROM attendance"); conn.commit()
    await update.message.reply_text("✅ All attendance reset.")

async def cmd_reset_clock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    cur.execute("DELETE FROM attendance WHERE status='Clocked In'"); conn.commit()
    await update.message.reply_text("✅ Clock-in data reset.")

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    if not context.args: await update.message.reply_text("Usage: /report <MonthName>"); return
    month_name = context.args[0].capitalize()
    month_num = list(calendar.month_name).index(month_name) if month_name in calendar.month_name else 0
    if month_num==0: await update.message.reply_text("Invalid month"); return
    year=datetime.now().year
    month_prefix=f"{year}-{month_num:02d}"
    df=pd.read_sql_query(f"SELECT * FROM attendance WHERE date LIKE '{month_prefix}%'",conn)
    if df.empty: await update.message.reply_text("No data for this month."); return
    bio=io.BytesIO(); bio.name=f"Attendance_{month_name}_{year}.xlsx"
    df.to_excel(bio,index=False); bio.seek(0)
    await context.bot.send_document(update.effective_chat.id, document=InputFile(bio,bio.name))
    await update.message.reply_text(f"📊 Report for {month_name} sent.")

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context,update.effective_user.id): return
    df=pd.read_sql_query("SELECT * FROM attendance ORDER BY date",conn)
    if df.empty: await update.message.reply_text("No data to backup."); return
    bio=io.BytesIO(); bio.name=f"EXC_Backup_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    df.to_excel(bio,index=False); bio.seek(0)
    await context.bot.send_document(LOG_CHANNEL_ID, document=InputFile(bio,bio.name))
    await update.message.reply_text("✅ Backup sent to log channel.")

# ---------------- DAILY BACKUP ----------------
async def daily_backup(context: ContextTypes.DEFAULT_TYPE):
    df = pd.read_sql_query("SELECT * FROM attendance ORDER BY date", conn)
    if df.empty: return
    bio = io.BytesIO()
    bio.name = f"EXC_Backup_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    df.to_excel(bio, index=False)
    bio.seek(0)
    await context.bot.send_document(LOG_CHANNEL_ID, document=InputFile(bio, bio.name))
    print("✅ Daily backup sent to log channel")

# ---------------- MAIN ----------------
def main():
    auto_absent()
    
    # Build app without automatically binding JobQueue
    app = Application.builder().token(BOT_TOKEN).build()

    # Explicitly get job_queue
    job_queue = JobQueue()
    job_queue.set_application(app)

    # Staff commands
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("sick", cmd_sick))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin commands
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("staff", cmd_staff))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("reset_clock", cmd_reset_clock))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("backup", cmd_backup))

    # Schedule daily backup at AUTO_BACKUP_TIME
    job_queue.run_daily(daily_backup, time=AUTO_BACKUP_TIME)
    job_queue.start()

    print("✅ EXC-Bot is running...")
    app.run_polling()
    
if __name__=="__main__":
    main()
