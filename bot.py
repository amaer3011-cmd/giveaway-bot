"""
بوت مسابقات تليجرام - النسخة الاحترافية
------------------------------------------------------------
pip install "python-telegram-bot[job-queue]>=21,<22"

export BOT_TOKEN="xxxx"
export ADMIN_IDS="123456789"          # ايديهات الادمنز مفصولة بفاصلة
export DB_PATH="giveaway.db"          # اختياري
python bot.py

لازم البوت يكون ادمن في قناة المسابقة وفي أي قناة اشتراك اجباري.
------------------------------------------------------------
"""
import csv
import html
import io
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update,
)
from telegram.constants import ChatMemberStatus as CMS, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Africa/Cairo")
except Exception:  # لو tzdata مش موجودة
    TZ = timezone(timedelta(hours=2))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("giveaway")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN مش متحدد في المتغيرات (Variables).")
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "giveaway.db")
rng = random.SystemRandom()
HTML = ParseMode.HTML

TITLE, DESC, WINNERS, CHANNEL, REQUIRED, ENDTIME, CONFIRM = range(7)
ICON = {"open": "🟢", "closed": "🔒", "drawn": "🏆", "cancelled": "🚫"}


# ============================== قاعدة البيانات ==============================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS giveaways(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, descr TEXT DEFAULT '',
                winners INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, message_id INTEGER,
                required TEXT DEFAULT '[]',      -- JSON: قنوات الاشتراك الاجباري
                end_at INTEGER,                  -- unix time او NULL
                status TEXT DEFAULT 'open',      -- open/closed/drawn/cancelled
                created_by INTEGER, created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS participants(
                giveaway_id INTEGER, user_id INTEGER,
                username TEXT, full_name TEXT, joined_at INTEGER,
                is_winner INTEGER DEFAULT 0,     -- 0 عادي / 1 فايز / -1 اتستبدل
                PRIMARY KEY (giveaway_id, user_id)
            );
            """
        )


def now() -> int:
    return int(time.time())


def get_g(gid):
    with db() as con:
        return con.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()


def get_ps(gid, where="1=1"):
    with db() as con:
        return con.execute(
            f"SELECT * FROM participants WHERE giveaway_id=? AND {where} ORDER BY joined_at",
            (gid,),
        ).fetchall()


def count(gid) -> int:
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (gid,)
        ).fetchone()["c"]


def set_status(gid, status):
    with db() as con:
        con.execute("UPDATE giveaways SET status=? WHERE id=?", (status, gid))


# ============================== أدوات مساعدة ==============================
def is_admin(uid: int) -> bool:
    return uid in ADMINS


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") + " (توقيت القاهرة)"


def user_line(p) -> str:
    name = html.escape(p["full_name"] or "بدون اسم")
    uname = f"@{p['username']}" if p["username"] else "بدون يوزرنيم"
    return f'<a href="tg://user?id={p["user_id"]}">{name}</a> — {uname} — <code>{p["user_id"]}</code>'


def parse_duration(text: str):
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.strip().lower())
    if not m:
        return None
    n, unit = int(m[1]), m[2]
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(
            text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


async def send_long(message, text, kb=None):
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
    for i, c in enumerate(chunks):
        await message.reply_text(
            c, parse_mode=HTML, disable_web_page_preview=True,
            reply_markup=kb if i == len(chunks) - 1 else None,
        )


async def resolve_channel(bot, ref: str):
    """يرجع (chat) بعد التأكد ان البوت ادمن فيها."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        ref = int(ref)
    elif not ref.startswith("@"):
        ref = "@" + ref.replace("https://t.me/", "")
    chat = await bot.get_chat(ref)
    me = await bot.get_chat_member(chat.id, bot.id)
    if me.status not in (CMS.ADMINISTRATOR, CMS.OWNER):
        raise PermissionError(chat.title)
    return chat


async def missing_channels(bot, g, user_id):
    """القنوات اللي المستخدم مش مشترك فيها (الاساسية + الاجبارية)."""
    chans = [{"id": g["chat_id"], "title": "القناة الأساسية"}] + json.loads(g["required"])
    missing = []
    for ch in chans:
        try:
            m = await bot.get_chat_member(ch["id"], user_id)
            if m.status in (CMS.LEFT, CMS.BANNED):
                missing.append(ch["title"])
        except Exception as e:
            log.warning("membership check failed (%s): %s", ch["id"], e)
    return missing


# ============================== نصوص ولوحات ==============================
def post_text(g, winners=None) -> str:
    st = g["status"]
    head = {"open": "🎉", "closed": "🔒", "drawn": "🏆", "cancelled": "🚫"}[st]
    lines = [f"{head} <b>{html.escape(g['title'])}</b>"]
    if g["descr"]:
        lines += ["", html.escape(g["descr"])]
    lines += ["", f"🏆 عدد الفايزين: <b>{g['winners']}</b>"]
    req = json.loads(g["required"])
    if req and st in ("open", "closed"):
        lines.append("📌 اشتراك إجباري في: " + "، ".join(html.escape(c["title"]) for c in req))
    if g["end_at"] and st == "open":
        lines.append(f"⏰ ينتهي: {fmt_time(g['end_at'])}")
    if st == "open":
        lines += ["", "اضغط على الزرار تحت للاشتراك 👇"]
    elif st == "closed":
        lines += ["", "🔒 انتهى الاشتراك، انتظروا إعلان الفايزين."]
    elif st == "cancelled":
        lines += ["", "تم إلغاء المسابقة."]
    elif st == "drawn" and winners:
        lines += ["", "🎊 <b>الفايزين:</b>"]
        for i, w in enumerate(winners, 1):
            tag = f" (@{w['username']})" if w["username"] else ""
            lines.append(f"{i}. {html.escape(w['full_name'] or '')}{tag}")
        lines += ["", "مبروك للفايزين وحظ أوفر للباقي ❤️"]
    return "\n".join(lines)


def join_markup(g) -> Markup:
    rows = [[Btn(f"🎁 اشترك ({count(g['id'])})", callback_data=f"join:{g['id']}")]]
    for ch in json.loads(g["required"]):
        if ch.get("username"):
            rows.append([Btn(f"📢 {ch['title']}", url=f"https://t.me/{ch['username']}")])
    return Markup(rows)


def closed_markup(g) -> Markup:
    return Markup([[Btn(f"🔒 انتهى الاشتراك ({count(g['id'])} مشترك)", callback_data="noop")]])


def main_menu() -> Markup:
    return Markup([
        [Btn("➕ مسابقة جديدة", callback_data="new")],
        [Btn("📋 مسابقاتي", callback_data="my:0"), Btn("📊 إحصائيات", callback_data="stats:0")],
    ])


def detail_view(g):
    n = count(g["id"])
    st = g["status"]
    lines = [
        f"{ICON[st]} <b>#{g['id']} — {html.escape(g['title'])}</b>",
        f"الحالة: {st} | فايزين: {g['winners']} | مشتركين: {n}",
    ]
    if g["end_at"]:
        lines.append(f"⏰ النهاية: {fmt_time(g['end_at'])}")
    gid = g["id"]
    rows = []
    if st in ("open", "closed"):
        rows.append([Btn("🎲 اسحب الفايزين", callback_data=f"draw:{gid}")])
        if st == "open":
            rows.append([Btn("🔒 قفل الاشتراك", callback_data=f"close:{gid}")])
    if st == "drawn":
        rows.append([Btn("🏆 النتائج", callback_data=f"results:{gid}")])
    rows.append([Btn("👥 المشتركين", callback_data=f"plist:{gid}"),
                 Btn("📄 CSV", callback_data=f"csv:{gid}")])
    if st in ("open", "closed"):
        rows.append([Btn("🗑 إلغاء المسابقة", callback_data=f"cancel:{gid}")])
    rows.append([Btn("⬅️ رجوع", callback_data="my:0")])
    return "\n".join(lines), Markup(rows)


def results_view(g):
    winners = get_ps(g["id"], "is_winner=1")
    lines = [f"🏆 <b>فايزين #{g['id']} — {html.escape(g['title'])}</b>", ""]
    rows = []
    for i, w in enumerate(winners, 1):
        lines.append(f"{i}. {user_line(w)}")
        rows.append([Btn(f"🔄 استبدال #{i}", callback_data=f"reroll:{g['id']}:{w['user_id']}")])
    if len(winners) < g["winners"]:
        lines += ["", f"⚠️ المشتركين المؤهلين كانوا أقل من {g['winners']}."]
    rows.append([Btn("✉️ ابلاغ الفايزين", callback_data=f"notify:{g['id']}"),
                 Btn("📄 CSV", callback_data=f"csv:{g['id']}")])
    rows.append([Btn("⬅️ رجوع", callback_data=f"open:{g['id']}")])
    return "\n".join(lines), Markup(rows)


# ============================== /start والقوائم ==============================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👋 لوحة تحكم المسابقات:", reply_markup=main_menu())
    else:
        await update.message.reply_text(
            "أهلاً! 🎁 للاشتراك في أي مسابقة اضغط على زرار الاشتراك تحت المنشور في القناة.\n"
            "ولو عملت Start للبوت هقدر أبلغك لو فزت."
        )


async def menu_cmd(update: Update, ctx):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("لوحة التحكم:", reply_markup=main_menu())


# ============================== معالج الأزرار للادمن ==============================
async def admin_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("للادمن فقط.", show_alert=True)
        return
    await q.answer()
    action, *args = q.data.split(":")

    if action == "menu":
        await edit(q, "👋 لوحة تحكم المسابقات:", main_menu())

    elif action == "stats":
        with db() as con:
            tg = con.execute("SELECT COUNT(*) c FROM giveaways").fetchone()["c"]
            op = con.execute("SELECT COUNT(*) c FROM giveaways WHERE status='open'").fetchone()["c"]
            tu = con.execute("SELECT COUNT(DISTINCT user_id) c FROM participants").fetchone()["c"]
            te = con.execute("SELECT COUNT(*) c FROM participants").fetchone()["c"]
        await edit(q, f"📊 <b>إحصائيات</b>\n\nمسابقات: {tg} (مفتوحة: {op})\n"
                      f"مستخدمين مختلفين: {tu}\nإجمالي المشاركات: {te}",
                   Markup([[Btn("⬅️ رجوع", callback_data="menu:0")]]))

    elif action == "my":
        with db() as con:
            rows = con.execute("SELECT * FROM giveaways ORDER BY id DESC LIMIT 15").fetchall()
        if not rows:
            await edit(q, "مفيش مسابقات لسه.", main_menu())
            return
        kb = [[Btn(f"{ICON[g['status']]} #{g['id']} {g['title'][:30]} ({count(g['id'])})",
                   callback_data=f"open:{g['id']}")] for g in rows]
        kb.append([Btn("⬅️ رجوع", callback_data="menu:0")])
        await edit(q, "📋 <b>مسابقاتك:</b>", Markup(kb))

    elif action == "open":
        g = get_g(int(args[0]))
        text, kb = detail_view(g)
        await edit(q, text, kb)

    elif action == "plist":
        gid = int(args[0])
        ps = get_ps(gid)
        if not ps:
            await q.message.reply_text("مفيش مشتركين لسه.")
            return
        lines = [f"{i}. {user_line(p)}" for i, p in enumerate(ps[:150], 1)]
        if len(ps) > 150:
            lines.append(f"\n... والباقي ({len(ps) - 150}) في ملف CSV")
        await send_long(q.message, f"👥 <b>مشتركين #{gid}</b> ({len(ps)}):\n\n" + "\n".join(lines))

    elif action == "csv":
        gid = int(args[0])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "username", "full_name", "joined_at", "status"])
        for p in get_ps(gid):
            w.writerow([p["user_id"], p["username"] or "", p["full_name"] or "",
                        datetime.fromtimestamp(p["joined_at"], TZ).isoformat(timespec="seconds"),
                        {1: "winner", -1: "replaced", 0: ""}[p["is_winner"]]])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        data.name = f"giveaway_{gid}.csv"
        await q.message.reply_document(data, caption=f"📄 مشتركين المسابقة #{gid}")

    elif action == "close":
        gid = int(args[0])
        ok = await close_giveaway(ctx, gid, auto=False)
        g = get_g(gid)
        text, kb = detail_view(g)
        await edit(q, text + ("" if ok else "\n\n⚠️ مش مفتوحة."), kb)

    elif action == "cancel":
        gid = int(args[0])
        await edit(q, f"⚠️ متأكد من إلغاء المسابقة #{gid}؟",
                   Markup([[Btn("✅ أيوه الغي", callback_data=f"cancelyes:{gid}"),
                            Btn("↩️ لأ", callback_data=f"open:{gid}")]]))

    elif action == "cancelyes":
        gid = int(args[0])
        g = get_g(gid)
        if g["status"] in ("open", "closed"):
            set_status(gid, "cancelled")
            remove_end_job(ctx.application, gid)
            await safe_edit_channel(ctx, get_g(gid), None)
        text, kb = detail_view(get_g(gid))
        await edit(q, text, kb)

    elif action == "draw":
        gid = int(args[0])
        g = get_g(gid)
        if g["status"] not in ("open", "closed"):
            await q.message.reply_text("المسابقة دي مش متاحة للسحب.")
            return
        if not get_ps(gid, "is_winner=0"):
            await q.message.reply_text("مفيش مشتركين، مينفعش أسحب.")
            return
        await edit(q, "🎲 جاري السحب والتحقق من اشتراك الفايزين...")
        winners = await pick(ctx.bot, g, g["winners"])
        with db() as con:
            con.executemany(
                "UPDATE participants SET is_winner=1 WHERE giveaway_id=? AND user_id=?",
                [(gid, w["user_id"]) for w in winners],
            )
        set_status(gid, "drawn")
        remove_end_job(ctx.application, gid)
        g = get_g(gid)
        await safe_edit_channel(ctx, g, winners)
        text, kb = results_view(g)
        await edit(q, text, kb)

    elif action == "results":
        text, kb = results_view(get_g(int(args[0])))
        await edit(q, text, kb)

    elif action == "reroll":
        gid, uid = int(args[0]), int(args[1])
        g = get_g(gid)
        repl = await pick(ctx.bot, g, 1)
        if not repl:
            await q.message.reply_text("⚠️ مفيش مشتركين مؤهلين للاستبدال.")
            return
        with db() as con:
            con.execute("UPDATE participants SET is_winner=-1 WHERE giveaway_id=? AND user_id=?", (gid, uid))
            con.execute("UPDATE participants SET is_winner=1 WHERE giveaway_id=? AND user_id=?",
                        (gid, repl[0]["user_id"]))
        await safe_edit_channel(ctx, g, get_ps(gid, "is_winner=1"))
        text, kb = results_view(g)
        await edit(q, text, kb)

    elif action == "notify":
        gid = int(args[0])
        g = get_g(gid)
        ok, fail = 0, []
        for w in get_ps(gid, "is_winner=1"):
            try:
                await ctx.bot.send_message(
                    w["user_id"],
                    f"🎉 مبروك! فزت في مسابقة «{g['title']}».\nهنتواصل معاك قريب.",
                )
                ok += 1
            except Exception:
                fail.append(w)
        msg = f"✉️ اتبعت لـ {ok} فايز."
        if fail:
            msg += "\n\nمقدرتش أبعت لـ (لازم يعملوا Start للبوت، تواصل معاهم يدوي):\n"
            msg += "\n".join(user_line(w) for w in fail)
        await send_long(q.message, msg)


async def pick(bot, g, n):
    """يختار n مشتركين عشوائي، ويتأكد انهم لسه في القنوات المطلوبة."""
    pool = get_ps(g["id"], "is_winner=0")
    rng.shuffle(pool)
    chosen = []
    for p in pool:
        if len(chosen) >= n:
            break
        if not await missing_channels(bot, g, p["user_id"]):
            chosen.append(p)
    return chosen


async def safe_edit_channel(ctx, g, winners):
    try:
        kb = None
        await ctx.bot.edit_message_text(
            post_text(g, winners), chat_id=g["chat_id"], message_id=g["message_id"],
            parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("channel edit failed: %s", e)


# ============================== الاشتراك في المسابقة ==============================
async def on_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    gid = int(q.data.split(":")[1])
    u = q.from_user
    g = get_g(gid)

    if not g or g["status"] != "open" or (g["end_at"] and now() >= g["end_at"]):
        await q.answer("المسابقة دي خلصت.", show_alert=True)
        return

    miss = await missing_channels(ctx.bot, g, u.id)
    if miss:
        await q.answer(("لازم تشترك في: " + "، ".join(miss))[:190], show_alert=True)
        return

    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO participants(giveaway_id,user_id,username,full_name,joined_at)"
            " VALUES(?,?,?,?,?)", (gid, u.id, u.username, u.full_name, now()),
        )
        new = cur.rowcount == 1
    await q.answer("✅ تم اشتراكك، بالتوفيق!" if new else "إنت مشترك قبل كده 👍", show_alert=True)
    if new:
        ctx.bot_data.setdefault("dirty", set()).add(gid)


async def noop(update: Update, ctx):
    await update.callback_query.answer("انتهى الاشتراك في المسابقة دي.", show_alert=True)


# ============================== مؤقتات ==============================
def remove_end_job(app, gid):
    for j in app.job_queue.get_jobs_by_name(f"end:{gid}"):
        j.schedule_removal()


def schedule_end(app, g):
    remove_end_job(app, g["id"])
    if g["end_at"] and g["status"] == "open":
        app.job_queue.run_once(end_job, max(1, g["end_at"] - now()),
                               name=f"end:{g['id']}", data=g["id"])


async def end_job(ctx: ContextTypes.DEFAULT_TYPE):
    await close_giveaway(ctx, ctx.job.data, auto=True)


async def close_giveaway(ctx, gid, auto: bool) -> bool:
    g = get_g(gid)
    if not g or g["status"] != "open":
        return False
    set_status(gid, "closed")
    remove_end_job(ctx.application, gid)
    g = get_g(gid)
    try:
        await ctx.bot.edit_message_text(
            post_text(g), chat_id=g["chat_id"], message_id=g["message_id"], parse_mode=HTML,
            reply_markup=closed_markup(g), disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("close edit failed: %s", e)
    if auto:
        try:
            await ctx.bot.send_message(
                g["created_by"],
                f"⏰ المسابقة #{gid} «{html.escape(g['title'])}» خلص وقتها.\nمشتركين: {count(gid)}",
                parse_mode=HTML,
                reply_markup=Markup([[Btn("🎲 اسحب الفايزين", callback_data=f"draw:{gid}"),
                                      Btn("📂 فتح", callback_data=f"open:{gid}")]]),
            )
        except Exception as e:
            log.warning("notify admin failed: %s", e)
    return True


async def refresh_counters(ctx: ContextTypes.DEFAULT_TYPE):
    dirty = ctx.bot_data.get("dirty", set())
    for gid in list(dirty):
        dirty.discard(gid)
        g = get_g(gid)
        if g and g["status"] == "open":
            try:
                await ctx.bot.edit_message_reply_markup(
                    g["chat_id"], g["message_id"], reply_markup=join_markup(g))
            except Exception as e:
                if "not modified" not in str(e).lower():
                    log.warning("counter refresh failed: %s", e)


async def post_init(app: Application):
    with db() as con:
        rows = con.execute("SELECT * FROM giveaways WHERE status='open' AND end_at IS NOT NULL").fetchall()
    for g in rows:
        schedule_end(app, g)
    app.job_queue.run_repeating(refresh_counters, interval=15, first=15)
    log.info("bot ready, %d timers restored", len(rows))


# ============================== معالج انشاء مسابقة ==============================
async def new_entry(update: Update, ctx):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
        uid = update.callback_query.from_user.id
    else:
        msg, uid = update.message, update.effective_user.id
    if not is_admin(uid):
        return ConversationHandler.END
    ctx.user_data.clear()
    await msg.reply_text("1️⃣ اكتب <b>عنوان المسابقة / الجايزة</b>:\n(للإلغاء: /cancel)", parse_mode=HTML)
    return TITLE


async def got_title(update: Update, ctx):
    ctx.user_data["title"] = update.message.text.strip()[:120]
    await update.message.reply_text("2️⃣ اكتب <b>تفاصيل/شروط</b> المسابقة، أو /skip للتخطي:", parse_mode=HTML)
    return DESC


async def got_desc(update: Update, ctx):
    ctx.user_data["descr"] = update.message.text.strip()[:600]
    return await ask_winners(update)


async def skip_desc(update: Update, ctx):
    ctx.user_data["descr"] = ""
    return await ask_winners(update)


async def ask_winners(update):
    await update.message.reply_text("3️⃣ <b>عدد الفايزين</b> كام؟ (رقم)", parse_mode=HTML)
    return WINNERS


async def got_winners(update: Update, ctx):
    t = update.message.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 100:
        await update.message.reply_text("ابعت رقم من 1 لـ 100.")
        return WINNERS
    ctx.user_data["winners"] = int(t)
    await update.message.reply_text(
        "4️⃣ ابعت <b>يوزر القناة</b> اللي هتنزل فيها المسابقة (مثال: @mychannel) أو الـ ID.\n"
        "لازم البوت يكون ادمن فيها.", parse_mode=HTML)
    return CHANNEL


async def got_channel(update: Update, ctx):
    try:
        chat = await resolve_channel(ctx.bot, update.message.text)
    except Exception:
        await update.message.reply_text("❌ مش قادر أوصل للقناة أو البوت مش ادمن فيها. جرب تاني أو /cancel")
        return CHANNEL
    ctx.user_data["chat_id"] = chat.id
    ctx.user_data["chat_title"] = chat.title
    await update.message.reply_text(
        "5️⃣ <b>قنوات اشتراك إجباري</b> إضافية؟ ابعت يوزراتها مفصولة بمسافة\n"
        "(مثال: @ch1 @ch2) — البوت لازم يكون ادمن فيها. أو /skip للتخطي.", parse_mode=HTML)
    return REQUIRED


async def got_required(update: Update, ctx):
    chans = []
    for ref in update.message.text.split():
        try:
            c = await resolve_channel(ctx.bot, ref)
            chans.append({"id": c.id, "title": c.title, "username": c.username})
        except Exception:
            await update.message.reply_text(f"❌ مش قادر أتحقق من {ref} (البوت لازم يكون ادمن فيها). صحح وابعت تاني أو /skip")
            return REQUIRED
    ctx.user_data["required"] = chans
    return await ask_end(update)


async def skip_required(update: Update, ctx):
    ctx.user_data["required"] = []
    return await ask_end(update)


async def ask_end(update):
    await update.message.reply_text(
        "6️⃣ <b>مدة المسابقة</b>؟ اكتب مثلاً: <code>30m</code> (دقايق) أو <code>2h</code> (ساعات) أو <code>1d</code> (أيام)\n"
        "أو <code>0</code> لو هتقفلها بنفسك.", parse_mode=HTML)
    return ENDTIME


async def got_end(update: Update, ctx):
    t = update.message.text.strip()
    if t == "0":
        ctx.user_data["end_at"] = None
    else:
        secs = parse_duration(t)
        if not secs:
            await update.message.reply_text("صيغة غلط. مثال: 30m أو 2h أو 1d أو 0")
            return ENDTIME
        ctx.user_data["end_at"] = now() + secs
    d = ctx.user_data
    preview = {
        "title": d["title"], "descr": d["descr"], "winners": d["winners"],
        "required": json.dumps(d["required"]), "end_at": d["end_at"], "status": "open",
    }
    await update.message.reply_text(
        "👀 <b>معاينة المنشور:</b>\n\n" + post_text(preview) +
        f"\n\n📍 هتنزل في: {html.escape(d['chat_title'])}",
        parse_mode=HTML,
        reply_markup=Markup([[Btn("✅ انشر", callback_data="pub"), Btn("❌ إلغاء", callback_data="abort")]]),
    )
    return CONFIRM


async def confirm(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "abort":
        await q.edit_message_text("اتلغى.")
        return ConversationHandler.END
    d = ctx.user_data
    with db() as con:
        cur = con.execute(
            "INSERT INTO giveaways(title,descr,winners,chat_id,required,end_at,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (d["title"], d["descr"], d["winners"], d["chat_id"], json.dumps(d["required"]),
             d["end_at"], q.from_user.id, now()),
        )
        gid = cur.lastrowid
    g = get_g(gid)
    try:
        msg = await ctx.bot.send_message(
            g["chat_id"], post_text(g), parse_mode=HTML, reply_markup=join_markup(g),
            disable_web_page_preview=True)
    except Exception as e:
        with db() as con:
            con.execute("DELETE FROM giveaways WHERE id=?", (gid,))
        await q.edit_message_text(f"❌ فشل النشر في القناة: {e}")
        return ConversationHandler.END
    with db() as con:
        con.execute("UPDATE giveaways SET message_id=? WHERE id=?", (msg.message_id, gid))
    schedule_end(ctx.application, get_g(gid))
    await q.edit_message_text(
        f"✅ المسابقة #{gid} اتنشرت.",
        reply_markup=Markup([[Btn("📂 فتح المسابقة", callback_data=f"open:{gid}")]]))
    return ConversationHandler.END


async def cancel(update: Update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("اتلغى.", reply_markup=main_menu())
    return ConversationHandler.END


# ============================== الأخطاء والتشغيل ==============================
async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled error", exc_info=ctx.error)


def main():
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    text = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_entry),
                      CallbackQueryHandler(new_entry, pattern=r"^new$")],
        states={
            TITLE: [MessageHandler(text, got_title)],
            DESC: [MessageHandler(text, got_desc), CommandHandler("skip", skip_desc)],
            WINNERS: [MessageHandler(text, got_winners)],
            CHANNEL: [MessageHandler(text, got_channel)],
            REQUIRED: [MessageHandler(text, got_required), CommandHandler("skip", skip_required)],
            ENDTIME: [MessageHandler(text, got_end)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(pub|abort)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=900,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_join, pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(
        admin_router,
        pattern=r"^(menu|stats|my|open|plist|csv|close|cancel|cancelyes|draw|results|reroll|notify):"))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
