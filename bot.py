import os
import logging
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from db import Database
from scheduler import schedule_jobs, schedule_reminder, reschedule_all_reminders

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database(os.environ.get("TURSO_DATABASE_URL", "fitterfriends.db"))

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
PENALTY_AMOUNTS = [1, 2, 5, 10, 15, 20]
GOAL_LABELS = {"cal": "🍎 Calories", "run": "🏃 Running", "walk": "🚶 Walking"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize(text: str, max_len: int = 100) -> str:
    """Strip Markdown special chars and cap length."""
    return text.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")[:max_len]


def _week_start(d: date, reset_day: int) -> date:
    return d - timedelta(days=(d.weekday() - reset_day) % 7)


def _user_today(user_id: int, chat_id: int) -> date:
    member = db.get_member(user_id, chat_id)
    tz_str = (
        (member["timezone"] if member else None)
        or db.get_leader_timezone(chat_id)
        or "UTC"
    )
    return datetime.now(pytz.timezone(tz_str)).date()


def _penalty_kb(prefix: str) -> InlineKeyboardMarkup:
    r1 = [InlineKeyboardButton(f"${a}", callback_data=f"{prefix}{a}") for a in PENALTY_AMOUNTS[:3]]
    r2 = [InlineKeyboardButton(f"${a}", callback_data=f"{prefix}{a}") for a in PENALTY_AMOUNTS[3:]]
    return InlineKeyboardMarkup([r1, r2])


async def _guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                 need_group=True, need_member=False) -> bool:
    """Returns True if the request should be blocked."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type == "private":
        await msg.reply_text("Use me in a group chat.")
        return True

    try:
        cm = await ctx.bot.get_chat_member(chat.id, user.id)
        if cm.status in ("left", "kicked", "banned"):
            await msg.reply_text("You're not a member of this group.")
            return True
    except Exception:
        pass

    if need_group and not db.get_group(chat.id):
        await msg.reply_text("No group set up yet. Leader runs /setup.")
        return True

    if need_member:
        targets = db.get_all_member_targets(user.id, chat.id)
        if not targets:
            await msg.reply_text("Set your personal goals first with /mygoals.")
            return True

    return False


def _get_setup(ctx, chat_id):
    return ctx.bot_data.setdefault(chat_id, {})


# ── /start & /help ────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text(
            "👋 Hi! Add me to a group to get started.\n"
            "I track calories, running, and walking — with accountability payments."
        )
        return
    db.ensure_member(user.id, chat.id, user.username or user.first_name)
    group = db.get_group(chat.id)
    if not group:
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm FitterFriends Bot.\n\n"
            "Leader: run /setup to configure group goals.\n"
            "Members: run /mygoals once setup is done."
        )
    else:
        await update.message.reply_text(
            f"👋 Welcome {user.first_name}!\n\n"
            "Run /mygoals to set your personal targets.\n"
            "Use /help to see all commands."
        )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏋️ *FitterFriends Bot*\n\n"
        "*Leader setup*\n"
        "/setup — pick goals, set penalties & reset day\n"
        "/rules — view group rules\n\n"
        "*Your setup* _(do this once after setup)_\n"
        "/mygoals — view & set your personal targets\n\n"
        "*Logging*\n"
        "`/cal 300 lunch` — log calories\n"
        "`/run 5.2 park` — log a run\n"
        "`/walk 8500` — log steps\n"
        "`/weight 65.5` — log your weight\n\n"
        "Add `-1`, `-2`, or `-3` before the number to backlog:\n"
        "`/cal -1 300 lunch` → logs for yesterday\n\n"
        "`/removelog cal` — clear today's calories\n"
        "`/removelog cal #42` — remove a specific entry\n"
        "`/removelog cal -1` — clear yesterday's calories\n\n"
        "*Stats*\n"
        "/status — your progress today & this week\n"
        "/leaderboard — everyone at a glance\n\n"
        "*Challenges*\n"
        "`/newchallenge July Grind 2025-07-31` — start a new challenge (leader only)\n"
        "/challenge — current challenge standings\n"
        "/endchallenge — end challenge early (leader only)\n\n"
        "*Payments*\n"
        "/debt — who owes what\n"
        "`/paid` — mark full debt as paid\n"
        "`/paid 10` — mark partial payment\n"
        "/history — your charges & payments\n",
        parse_mode="Markdown"
    )


# ── /setup (leader, all buttons) ──────────────────────────────────────────────

async def setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("Run /setup in your group chat.")
        return
    group = db.get_group(chat.id)
    if group and group["leader_id"] != user.id:
        await update.message.reply_text("Only the group leader can change rules.")
        return

    _get_setup(ctx, chat.id).clear()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Mon", callback_data="srd_0"),
         InlineKeyboardButton("Tue", callback_data="srd_1"),
         InlineKeyboardButton("Wed", callback_data="srd_2"),
         InlineKeyboardButton("Thu", callback_data="srd_3")],
        [InlineKeyboardButton("Fri", callback_data="srd_4"),
         InlineKeyboardButton("Sat", callback_data="srd_5"),
         InlineKeyboardButton("Sun", callback_data="srd_6")],
    ])
    await update.message.reply_text(
        "⚙️ *Group Setup — Step 1 of 3*\n\nWhich day does the week reset?\n_(This applies to all weekly goals and penalties.)_",
        reply_markup=kb,
        parse_mode="Markdown"
    )


async def cb_goal_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not _is_leader(query, chat_id):
        await query.answer("Only the group leader can do this.", show_alert=True)
        return
    d = _get_setup(ctx, chat_id)
    selected: set = d.setdefault("selected_goals", set())

    goal = query.data[5:]  # strip "gtog_"
    if goal == "next":
        if not selected:
            await query.answer("Select at least one goal!", show_alert=True)
            return
        await query.answer()
        d["goals_queue"] = sorted(selected, key=lambda g: ["cal","run","walk"].index(g))
        d["goals_done"] = []
        await _ask_daily_penalty(query, ctx, chat_id)
        return

    await query.answer()
    if goal in selected:
        selected.discard(goal)
    else:
        selected.add(goal)

    labels = {"cal": "🍎 Calories", "run": "🏃 Running", "walk": "🚶 Walking"}
    kb = [
        [InlineKeyboardButton(
            ("✅" if g in selected else "⬜") + " " + labels[g],
            callback_data=f"gtog_{g}"
        ) for g in ["cal", "run", "walk"]],
        [InlineKeyboardButton("Next ▶️", callback_data="gtog_next")],
    ]
    await query.edit_message_reply_markup(InlineKeyboardMarkup(kb))


async def _ask_daily_penalty(query, ctx, chat_id):
    d = _get_setup(ctx, chat_id)
    goal = d["goals_queue"][0]
    label = GOAL_LABELS[goal]
    done = len(d.get("goals_done", []))
    total = done + len(d["goals_queue"])
    verb = "going over" if goal == "cal" else "missing"
    await query.edit_message_text(
        f"⚙️ *Step 3 of 3 — Penalties* ({done+1}/{total})\n\n"
        f"*{label} — Daily penalty*\nHow much for {verb} the daily goal?",
        reply_markup=_penalty_kb(f"sdp_{goal}_"),
        parse_mode="Markdown"
    )


async def cb_setup_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not _is_leader(query, chat_id):
        await query.answer("Only the group leader can do this.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")  # ['sdp', 'cal', '5']
    goal, amount = parts[1], float(parts[2])
    if goal not in ("cal", "run", "walk") or amount not in PENALTY_AMOUNTS:
        return
    d = _get_setup(ctx, chat_id)
    d.setdefault("penalties", {})[goal] = {"daily": amount}

    goal_label = GOAL_LABELS[goal]
    await query.edit_message_text(
        f"✅ {goal_label} daily penalty: *${amount:.0f}*\n\n*Weekly penalty* — How much for missing the weekly {goal_label.split()[1].lower()} goal?",
        reply_markup=_penalty_kb(f"swp_{goal}_"),
        parse_mode="Markdown"
    )


async def cb_setup_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not _is_leader(query, chat_id):
        await query.answer("Only the group leader can do this.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")  # ['swp', 'cal', '10']
    goal, amount = parts[1], float(parts[2])
    if goal not in ("cal", "run", "walk") or amount not in PENALTY_AMOUNTS:
        return
    d = _get_setup(ctx, chat_id)
    d["penalties"][goal]["weekly"] = amount
    d["goals_done"].append(goal)
    d["goals_queue"].pop(0)

    if goal == "run":
        # Ask for running unit
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📏 Kilometres (km)", callback_data="run_unit_km"),
            InlineKeyboardButton("⏱ Minutes (min)",    callback_data="run_unit_min"),
        ]])
        await query.edit_message_text(
            "🏃 *Running unit*\nWhat unit will everyone log runs in?",
            reply_markup=kb, parse_mode="Markdown"
        )
        return

    await _next_goal_or_finalize(query, ctx, chat_id)


async def cb_run_unit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not _is_leader(query, chat_id):
        await query.answer("Only the group leader can do this.", show_alert=True)
        return
    await query.answer()
    unit = query.data.split("_")[2]  # km or min
    if unit not in ("km", "min"):
        return
    _get_setup(ctx, chat_id)["run_unit"] = unit
    await _next_goal_or_finalize(query, ctx, chat_id)


async def _next_goal_or_finalize(query, ctx, chat_id):
    d = _get_setup(ctx, chat_id)
    if d["goals_queue"]:
        await _ask_daily_penalty(query, ctx, chat_id)
        return
    await _finalize_setup(query, ctx, chat_id)


def _is_leader(query, chat_id) -> bool:
    group = db.get_group(chat_id)
    return group and group["leader_id"] == query.from_user.id


async def cb_reset_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not _is_leader(query, chat_id):
        await query.answer("Only the group leader can do this.", show_alert=True)
        return
    await query.answer()
    day = int(query.data[4:])
    _get_setup(ctx, chat_id)["reset_day"] = day

    kb = [
        [InlineKeyboardButton("⬜ 🍎 Calories",  callback_data="gtog_cal"),
         InlineKeyboardButton("⬜ 🏃 Running",   callback_data="gtog_run"),
         InlineKeyboardButton("⬜ 🚶 Walking",   callback_data="gtog_walk")],
        [InlineKeyboardButton("Next ▶️", callback_data="gtog_next")],
    ]
    await query.edit_message_text(
        f"✅ Resets on *{DAY_NAMES[day]}*\n\n"
        "⚙️ *Step 2 of 3* — Which goals should this group track?\nTap to toggle, then press Next.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def _finalize_setup(trigger, ctx, chat_id):
    d = _get_setup(ctx, chat_id)
    if hasattr(trigger, "effective_user"):
        user_id = trigger.effective_user.id
    else:
        user_id = trigger.from_user.id

    db.save_group(chat_id, user_id, d.get("reset_day", 6), "log", None)
    db.delete_group_goals(chat_id)
    for goal, p in d.get("penalties", {}).items():
        db.save_group_goal(chat_id, goal, p["daily"], p["weekly"],
                           d.get("run_unit", "km"))

    goal_lines = []
    for goal, p in d.get("penalties", {}).items():
        lbl = GOAL_LABELS[goal]
        unit = f" ({d.get('run_unit','km')})" if goal == "run" else ""
        goal_lines.append(f"  {lbl}{unit}: ${p['daily']:.0f}/day · ${p['weekly']:.0f}/week")

    summary = (
        "✅ *Group setup done!*\n\n"
        f"*Active goals:*\n" + "\n".join(goal_lines) + "\n\n"
        f"🔄 Weekly reset: *{DAY_NAMES[d.get('reset_day',6)]}*\n\n"
        "Everyone: run /mygoals to set your personal targets!"
    )
    if hasattr(trigger, "edit_message_text"):
        await trigger.edit_message_text(summary, parse_mode="Markdown")
    else:
        await trigger.message.reply_text(summary, parse_mode="Markdown")

    leader_tz = db.get_leader_timezone(chat_id) or "UTC"
    schedule_reminder(ctx.application, chat_id, leader_tz)


async def rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    group = db.get_group(update.effective_chat.id)
    goals = db.get_group_goals(update.effective_chat.id)
    lines = ["📋 *Group Rules*\n"]
    for g in goals:
        lbl = GOAL_LABELS.get(g["goal_type"], g["goal_type"])
        unit = f" ({g['run_unit']})" if g["goal_type"] == "run" else ""
        lines.append(f"{lbl}{unit}: ${g['daily_penalty']:.0f}/day · ${g['weekly_penalty']:.0f}/week")
    lines.append(f"\n🔄 Weekly reset: *{DAY_NAMES[group['reset_day']]}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /mygoals (member personal targets, all buttons/commands) ──────────────────

async def mygoals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    chat = update.effective_chat
    user = update.effective_user
    db.ensure_member(user.id, chat.id, user.username or user.first_name)

    goals = db.get_group_goals(chat.id)
    if not goals:
        await update.message.reply_text("No goals configured yet. Leader runs /setup first.")
        return

    lines = ["🎯 *Your personal targets*\n"]
    for g in goals:
        gt = g["goal_type"]
        if gt == "cal":
            lines.append("*🍎 Calories*")
            lines.append("`/mygoal cal 1800` — daily limit (e.g. 1800 cal/day)")
            lines.append("")
            lines.append("*⚖️ Weight goal* _(optional, pairs with calories)_")
            lines.append("`/mygoal weight 70 60` — current kg → target kg")
            lines.append("_Works for both loss and gain._\n")
        elif gt == "run":
            unit = g["run_unit"]
            lines.append(f"*🏃 Running ({unit})* — pick one:")
            lines.append(f"`/mygoal run 5 daily` — run {unit} every day")
            lines.append(f"`/mygoal run 3 3x` — run {unit} on 3 days a week _(frequency)_")
            lines.append(f"`/mygoal run 20 weekly` — {unit} total across the week\n")
        elif gt == "walk":
            lines.append("*🚶 Walking (steps)* — pick one:")
            lines.append("`/mygoal walk 10000 daily` — steps every day")
            lines.append("`/mygoal walk 70000 weekly` — total steps across the week\n")

    current = db.get_all_member_targets(user.id, chat.id)
    if current:
        lines.append("*Your current targets:*")
        for t in current:
            gt = t["goal_type"]
            if gt == "cal":
                lines.append(f"  🍎 Calories: *{int(t['target'])} cal/day*")
                if t["target2"]:
                    goal_weight = t["target2"]
                    oldest = db.get_oldest_weight(user.id, chat.id)
                    if oldest is not None:
                        direction = "gain" if goal_weight > oldest else "lose"
                        lines.append(f"  ⚖️ Weight: {direction} to *{goal_weight} kg*")
                    else:
                        lines.append(f"  ⚖️ Weight goal: *{goal_weight} kg*")
            elif gt == "run":
                goal_row = db.get_group_goal(chat.id, "run")
                unit = goal_row["run_unit"] if goal_row else "km"
                period = t["period"]
                if period == "freq":
                    lines.append(f"  🏃 Running: *{t['target']} {unit}* on *{int(t['target2'])} days/week*")
                elif period == "daily":
                    lines.append(f"  🏃 Running: *{t['target']} {unit}/day*")
                else:
                    lines.append(f"  🏃 Running: *{t['target']} {unit}/week* (total)")
            elif gt == "walk":
                period = t["period"]
                if period == "daily":
                    lines.append(f"  🚶 Walking: *{int(t['target']):,} steps/day*")
                else:
                    lines.append(f"  🚶 Walking: *{int(t['target']):,} steps/week* (total)")
    else:
        lines.append("_No targets set yet — use the commands above to get started._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def mygoal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /mygoal <type> <args...>"""
    if await _guard(update, ctx):
        return
    chat = update.effective_chat
    user = update.effective_user
    db.ensure_member(user.id, chat.id, user.username or user.first_name)

    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Run /mygoals to see all target options.",
            parse_mode="Markdown"
        )
        return

    goal_type = ctx.args[0].lower()
    group_goal = db.get_group_goal(chat.id, goal_type if goal_type != "weight" else "cal")

    if goal_type == "cal":
        try:
            limit = int(ctx.args[1])
            assert 500 <= limit <= 10000
        except Exception:
            await update.message.reply_text("Enter a calorie limit between 500 and 10000.")
            return
        db.set_member_target(user.id, chat.id, "cal", limit)
        await update.message.reply_text(
            f"✅ Daily calorie limit: *{limit} cal* (weekly: *{limit*7} cal*)", parse_mode="Markdown"
        )

    elif goal_type == "weight":
        try:
            current, target = float(ctx.args[1]), float(ctx.args[2])
            assert 20 < current < 300 and 20 < target < 300
        except Exception:
            await update.message.reply_text("Usage: `/mygoal weight <current_kg> <target_kg>`", parse_mode="Markdown")
            return
        # Store weight goal alongside cal target (target2 = weight goal)
        existing = db.get_member_target(user.id, chat.id, "cal")
        cal_limit = existing["target"] if existing else None
        db.set_member_target(user.id, chat.id, "cal", cal_limit, target2=target)
        db.log_weight(user.id, chat.id, _user_today(user.id, chat.id), current)
        diff = round(abs(current - target), 1)
        direction = "gain" if target > current else "lose"
        await update.message.reply_text(
            f"✅ Weight goal set!\n⚖️ *{current} kg* → *{target} kg* ({direction} {diff} kg)",
            parse_mode="Markdown"
        )

    elif goal_type == "run":
        if not group_goal:
            await update.message.reply_text("Running is not an active goal in this group.")
            return
        unit = group_goal["run_unit"]
        try:
            target_val = float(ctx.args[1])
            period_arg = ctx.args[2].lower() if len(ctx.args) > 2 else "daily"
            # Frequency mode: "3x" = 3 qualifying days per week
            if period_arg.endswith("x") and period_arg[:-1].isdigit():
                days_per_week = int(period_arg[:-1])
                assert 1 <= days_per_week <= 7
                db.set_member_target(user.id, chat.id, "run", target_val, target2=days_per_week, period="freq")
                await update.message.reply_text(
                    f"✅ Running target: *{target_val} {unit}* on *{days_per_week} days/week*\n"
                    f"Each day you run ≥ {target_val} {unit} counts as a qualifying day.",
                    parse_mode="Markdown"
                )
            else:
                assert period_arg in ("daily", "weekly")
                db.set_member_target(user.id, chat.id, "run", target_val, period=period_arg)
                period_desc = "every day" if period_arg == "daily" else "total across the week"
                await update.message.reply_text(
                    f"✅ Running target: *{target_val} {unit}* {period_desc}", parse_mode="Markdown"
                )
        except Exception:
            await update.message.reply_text(
                "Usage:\n"
                f"`/mygoal run 5 daily` — run {unit} every day\n"
                f"`/mygoal run 3 3x` — run {unit} on 3 days/week\n"
                f"`/mygoal run 20 weekly` — total {unit} per week",
                parse_mode="Markdown"
            )

    elif goal_type == "walk":
        if not db.get_group_goal(chat.id, "walk"):
            await update.message.reply_text("Walking is not an active goal in this group.")
            return
        try:
            steps = int(ctx.args[1])
            period = ctx.args[2].lower() if len(ctx.args) > 2 else "daily"
            assert period in ("daily", "weekly")
        except Exception:
            await update.message.reply_text(
                "Usage:\n"
                "`/mygoal walk 10000 daily` — steps every day\n"
                "`/mygoal walk 70000 weekly` — total steps across the week",
                parse_mode="Markdown"
            )
            return
        db.set_member_target(user.id, chat.id, "walk", steps, period=period)
        period_desc = "every day" if period == "daily" else "total across the week"
        await update.message.reply_text(
            f"✅ Step target: *{steps:,} steps* {period_desc}", parse_mode="Markdown"
        )

    else:
        await update.message.reply_text(
            "Unknown goal type.\nRun /mygoals to see what's available.", parse_mode="Markdown"
        )




# ── Logging ───────────────────────────────────────────────────────────────────

# Rate limiting
_rate: dict[tuple, list] = defaultdict(list)

def _rate_ok(user_id, chat_id) -> bool:
    key = (user_id, chat_id)
    now = datetime.now(timezone.utc).timestamp()
    _rate[key] = [t for t in _rate[key] if now - t < 60]
    if len(_rate[key]) >= 10:
        return False
    _rate[key].append(now)
    return True


def _parse_day_offset(args: list[str]) -> tuple[int, list[str]]:
    """If first arg is -1/-2/-3, return (offset, remaining_args). Else (0, args)."""
    if args and args[0] in ("-1", "-2", "-3"):
        return int(args[0]), args[1:]
    return 0, args


def _cal_receipt(entries, day_total, daily_limit, log_date=None) -> str:
    today_label = f"{log_date}" if log_date else "Today"
    lines = [f"#{e['id']} +{e['calories']} cal{' ' + e['label'] if e['label'] else ''}" for e in entries]
    divider = "—" * max((len(l) for l in lines), default=10)
    return f"```\n" + "\n".join(lines) + f"\n{divider}\n{today_label}: {day_total} cal\n```"


def _activity_receipt(entries, day_total, unit, target=None, log_date=None) -> str:
    today_label = f"{log_date}" if log_date else "Today"
    lines = [f"#{e['id']} +{e['value']:.1f} {unit}{' ' + e['label'] if e['label'] else ''}" for e in entries]
    divider = "—" * max((len(l) for l in lines), default=10)
    summary = f"{today_label}: {day_total:.1f} {unit}"
    if target:
        summary += f" / {target:.1f} {unit}"
    return f"```\n" + "\n".join(lines) + f"\n{divider}\n{summary}\n```"


def _re_evaluate_cal_penalty(user_id, chat_id, log_date: date, daily_limit: int,
                              week_start: date, weekly_limit: int, goal):
    """Re-check and correct cal penalties after any log change for log_date."""
    day_total = db.get_cal_day_total(user_id, chat_id, log_date)
    pkey = f"daily_{log_date}"
    issued = db.penalty_issued(user_id, chat_id, "cal", pkey)
    msgs = []

    if day_total > daily_limit and not issued:
        over = day_total - daily_limit
        db.add_debt(user_id, chat_id, goal["daily_penalty"],
                    f"Exceeded daily cal limit by {over} cal on {log_date}")
        db.mark_penalty(user_id, chat_id, "cal", pkey)
        msgs.append(f"⚠️ Over daily limit by *{over} cal* → *${goal['daily_penalty']:.0f} added*")
    elif day_total <= daily_limit and issued:
        db.reverse_penalty_debt(user_id, chat_id, f"on {log_date}")
        db.unmark_penalty(user_id, chat_id, "cal", pkey)
        msgs.append(f"✅ Back under limit — daily charge reversed")
    elif day_total > daily_limit:
        over = day_total - daily_limit
        msgs.append(f"⚠️ Over daily limit by *{over} cal*")

    week_total = db.get_cal_week_total(user_id, chat_id, week_start)
    wkey = f"weekly_{week_start}"
    w_issued = db.penalty_issued(user_id, chat_id, "cal_weekly", wkey)
    if week_total > weekly_limit and not w_issued:
        over = week_total - weekly_limit
        db.add_debt(user_id, chat_id, goal["weekly_penalty"],
                    f"Exceeded weekly cal limit by {over} cal (week of {week_start})")
        db.mark_penalty(user_id, chat_id, "cal_weekly", wkey)
        msgs.append(f"⚠️ Over weekly limit → *${goal['weekly_penalty']:.0f} added*")
    elif week_total <= weekly_limit and w_issued:
        db.reverse_penalty_debt(user_id, chat_id, f"week of {week_start}")
        db.unmark_penalty(user_id, chat_id, "cal_weekly", wkey)
        msgs.append(f"✅ Back under weekly limit — charge reversed")

    return msgs, day_total, week_total


def _re_evaluate_activity_penalty(user_id, chat_id, goal_type, log_date: date,
                                   week_start: date, goal, target_row):
    """Re-check and correct run/walk penalties after any log change for log_date."""
    period = target_row["period"]
    tgt = target_row["target"]
    msgs = []

    if period == "daily":
        val = db.get_activity_day_total(user_id, chat_id, goal_type, log_date)
        pkey = f"daily_{log_date}"
        issued = db.penalty_issued(user_id, chat_id, goal_type, pkey)
        # Only auto-fire for past dates (scheduler handles today's midnight check)
        is_past = log_date < date.today()
        if val < tgt and not issued and is_past:
            db.add_debt(user_id, chat_id, goal["daily_penalty"],
                        f"Missed daily {goal_type} target ({val:.1f}/{tgt:.1f}) on {log_date}")
            db.mark_penalty(user_id, chat_id, goal_type, pkey)
            msgs.append(f"⚠️ Still under {goal_type} target for {log_date} → *${goal['daily_penalty']:.0f} added*")
        elif val >= tgt and issued:
            db.reverse_penalty_debt(user_id, chat_id, f"on {log_date}")
            db.unmark_penalty(user_id, chat_id, goal_type, pkey)
            msgs.append(f"✅ {goal_type.title()} target now met for {log_date} — charge reversed")

    elif period == "weekly":
        week_val = db.get_activity_week_total(user_id, chat_id, goal_type, week_start)
        wkey = f"weekly_{week_start}"
        w_issued = db.penalty_issued(user_id, chat_id, goal_type, wkey)
        if week_val >= tgt and w_issued:
            db.reverse_penalty_debt(user_id, chat_id, f"week of {week_start}")
            db.unmark_penalty(user_id, chat_id, goal_type, wkey)
            msgs.append(f"✅ Weekly {goal_type} target met — charge reversed")

    elif period == "freq":
        days_needed = int(target_row["target2"])
        qualifying = db.get_activity_qualifying_days(user_id, chat_id, goal_type, week_start, tgt)
        wkey = f"freq_{week_start}"
        w_issued = db.penalty_issued(user_id, chat_id, goal_type, wkey)
        if qualifying >= days_needed and w_issued:
            db.reverse_penalty_debt(user_id, chat_id, f"week of {week_start}")
            db.unmark_penalty(user_id, chat_id, goal_type, wkey)
            msgs.append(f"✅ Frequency target met ({qualifying}/{days_needed} days) — charge reversed")

    return msgs


async def cal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx, need_group=True):
        return
    if not db.get_group_goal(chat.id, "cal"):
        await update.message.reply_text("Calories is not an active goal in this group.")
        return
    if not db.get_member_target(user.id, chat.id, "cal"):
        await update.message.reply_text("Set your calorie target first: `/mygoal cal 1800`", parse_mode="Markdown")
        return
    if not _rate_ok(user.id, chat.id):
        await update.message.reply_text("Slow down — max 10 logs per minute.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/cal <calories> [label]`\nBacklog: `/cal -1 300 lunch` (yesterday)\nExample: `/cal 300 lunch`",
            parse_mode="Markdown"
        )
        return

    offset, rest = _parse_day_offset(list(ctx.args))
    if abs(offset) > 3:
        await update.message.reply_text("Can only backlog up to 3 days.")
        return
    try:
        calories = int(rest[0])
        assert 0 < calories < 20000
    except Exception:
        await update.message.reply_text("Please provide a valid calorie number.")
        return

    label = _sanitize(" ".join(rest[1:])) or None
    today = _user_today(user.id, chat.id)
    log_date = today + timedelta(days=offset)
    group = db.get_group(chat.id)
    goal = db.get_group_goal(chat.id, "cal")
    target = db.get_member_target(user.id, chat.id, "cal")
    daily_limit = int(target["target"])
    week_start = _week_start(log_date, group["reset_day"])
    weekly_limit = daily_limit * 7

    db.log_calories(user.id, chat.id, log_date, calories, label)
    entries = db.get_cal_day_entries(user.id, chat.id, log_date)

    date_label = None if log_date == today else str(log_date)
    lines = [_cal_receipt(entries, db.get_cal_day_total(user.id, chat.id, log_date), daily_limit, date_label)]

    penalty_msgs, day_total, week_total = _re_evaluate_cal_penalty(
        user.id, chat.id, log_date, daily_limit, week_start, weekly_limit, goal
    )
    lines.extend(penalty_msgs)
    if day_total <= daily_limit:
        lines.append(f"✅ *{daily_limit - day_total} cal* remaining{' for ' + str(log_date) if date_label else ' today'}")
    lines.append(f"📊 Week: *{week_total} / {weekly_limit} cal*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx, need_group=True):
        return
    goal = db.get_group_goal(chat.id, "run")
    if not goal:
        await update.message.reply_text("Running is not an active goal in this group.")
        return
    target = db.get_member_target(user.id, chat.id, "run")
    if not target:
        await update.message.reply_text(f"Set your running target first: `/mygoal run 5 daily`", parse_mode="Markdown")
        return
    if not _rate_ok(user.id, chat.id):
        await update.message.reply_text("Slow down.")
        return
    if not ctx.args:
        unit = goal["run_unit"]
        await update.message.reply_text(
            f"Usage: `/run <{unit}> [label]`\nBacklog: `/run -1 5.2 morning` (yesterday)",
            parse_mode="Markdown"
        )
        return

    offset, rest = _parse_day_offset(list(ctx.args))
    try:
        value = float(rest[0])
        assert 0 < value < 1000
    except Exception:
        await update.message.reply_text("Please provide a valid number.")
        return

    label = _sanitize(" ".join(rest[1:])) or None
    unit = goal["run_unit"]
    today = _user_today(user.id, chat.id)
    log_date = today + timedelta(days=offset)
    group = db.get_group(chat.id)
    week_start = _week_start(log_date, group["reset_day"])
    period = target["period"]
    tgt = target["target"]

    db.log_activity(user.id, chat.id, "run", log_date, value, label)
    entries = db.get_activity_day_entries(user.id, chat.id, "run", log_date)
    day_total = db.get_activity_day_total(user.id, chat.id, "run", log_date)
    week_total = db.get_activity_week_total(user.id, chat.id, "run", week_start)

    date_label = None if log_date == today else str(log_date)
    lines = [_activity_receipt(entries, day_total, unit, tgt if period == "daily" else None, date_label)]

    penalty_msgs = _re_evaluate_activity_penalty(user.id, chat.id, "run", log_date, week_start, goal, target)
    lines.extend(penalty_msgs)

    if period == "daily":
        icon = "✅" if day_total >= tgt else "⏳"
        lines.append(f"{icon} *{day_total:.1f}/{tgt:.1f} {unit}*")
    elif period == "freq":
        days_needed = int(target["target2"])
        qualifying = db.get_activity_qualifying_days(user.id, chat.id, "run", week_start, tgt)
        this_day_qualifies = day_total >= tgt
        icon = "✅" if qualifying >= days_needed else "⏳"
        day_note = f" (today {'✅' if this_day_qualifies else f'⏳ {day_total:.1f}/{tgt:.1f} {unit}'})"
        lines.append(f"{icon} *{qualifying}/{days_needed} days* this week{day_note}")
    else:
        icon = "✅" if week_total >= tgt else "⏳"
        lines.append(f"📊 Week: {icon} *{week_total:.1f}/{tgt:.1f} {unit}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def walk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx, need_group=True):
        return
    goal = db.get_group_goal(chat.id, "walk")
    if not goal:
        await update.message.reply_text("Walking is not an active goal in this group.")
        return
    target = db.get_member_target(user.id, chat.id, "walk")
    if not target:
        await update.message.reply_text("Set your step target first: `/mygoal walk 10000 daily`", parse_mode="Markdown")
        return
    if not _rate_ok(user.id, chat.id):
        await update.message.reply_text("Slow down.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/walk <steps> [label]`\nBacklog: `/walk -1 8500` (yesterday)",
            parse_mode="Markdown"
        )
        return

    offset, rest = _parse_day_offset(list(ctx.args))
    try:
        steps = int(rest[0])
        assert 0 < steps < 200000
    except Exception:
        await update.message.reply_text("Please provide a valid step count.")
        return

    label = _sanitize(" ".join(rest[1:])) or None
    today = _user_today(user.id, chat.id)
    log_date = today + timedelta(days=offset)
    group = db.get_group(chat.id)
    week_start = _week_start(log_date, group["reset_day"])
    period = target["period"]
    tgt = int(target["target"])

    db.log_activity(user.id, chat.id, "walk", log_date, steps, label)
    entries = db.get_activity_day_entries(user.id, chat.id, "walk", log_date)
    day_total = int(db.get_activity_day_total(user.id, chat.id, "walk", log_date))
    week_total = int(db.get_activity_week_total(user.id, chat.id, "walk", week_start))

    date_label = None if log_date == today else str(log_date)
    lines = [_activity_receipt(entries, day_total, "steps", tgt if period == "daily" else None, date_label)]

    penalty_msgs = _re_evaluate_activity_penalty(user.id, chat.id, "walk", log_date, week_start, goal, target)
    lines.extend(penalty_msgs)

    if period == "daily":
        icon = "✅" if day_total >= tgt else "⏳"
        lines.append(f"{icon} *{day_total:,}/{tgt:,} steps*" + (f" — {tgt - day_total:,} to go" if day_total < tgt else ""))
    else:
        icon = "✅" if week_total >= tgt else "⏳"
        lines.append(f"📊 Week: {icon} *{week_total:,}/{tgt:,} steps*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx, need_group=True):
        return
    if not ctx.args:
        history = db.get_weight_history(user.id, chat.id, limit=7)
        if not history:
            await update.message.reply_text("Usage: `/weight <kg>`\nExample: `/weight 65.5`", parse_mode="Markdown")
        else:
            lines = ["⚖️ *Recent weight:*"]
            for r in reversed(history):
                lines.append(f"  {r['log_date']} — {r['weight_kg']} kg")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return
    try:
        w = float(ctx.args[0])
        assert 20 < w < 300
    except Exception:
        await update.message.reply_text("Provide a valid weight in kg.")
        return

    today = _user_today(user.id, chat.id)
    db.log_weight(user.id, chat.id, today, w)
    history = db.get_weight_history(user.id, chat.id, limit=2)
    lines = [f"⚖️ Logged *{w} kg*"]
    if len(history) >= 2:
        delta = w - history[1]["weight_kg"]
        arrow = "▼" if delta < 0 else "▲" if delta > 0 else "→"
        lines.append(f"{arrow} {abs(delta):.1f} kg vs last log")

    target = db.get_member_target(user.id, chat.id, "cal")
    if target and target["target2"]:
        goal_weight = target["target2"]
        oldest = db.get_oldest_weight(user.id, chat.id)
        if oldest is not None:
            is_gain = goal_weight > oldest
            if is_gain:
                if w >= goal_weight:
                    lines.append(f"🎉 Weight gain goal of *{goal_weight} kg* reached!")
                else:
                    lines.append(f"🎯 *{round(goal_weight - w, 1)} kg* to gain to reach {goal_weight} kg")
            else:
                if w <= goal_weight:
                    lines.append(f"🎉 Weight loss goal of *{goal_weight} kg* reached!")
                else:
                    lines.append(f"🎯 *{round(w - goal_weight, 1)} kg* to lose to reach {goal_weight} kg")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def removelog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /removelog cal            — clear ALL today's cal logs
    /removelog cal #42        — remove specific entry by ID (shown in receipt)
    /removelog cal -1         — clear ALL yesterday's cal logs
    /removelog run / walk     — same patterns for activity logs
    """
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx, need_group=True):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n`/removelog cal` — clear today's calories\n"
            "`/removelog cal #42` — remove entry #42\n"
            "`/removelog cal -1` — clear yesterday's calories\n"
            "`/removelog run` / `/removelog walk` — same",
            parse_mode="Markdown"
        )
        return

    goal_type = ctx.args[0].lower()
    if goal_type not in ("cal", "run", "walk"):
        await update.message.reply_text("Type must be `cal`, `run`, or `walk`.", parse_mode="Markdown")
        return

    today = _user_today(user.id, chat.id)
    group = db.get_group(chat.id)
    rest = ctx.args[1:]

    # Specific entry by ID?
    if rest and rest[0].startswith("#"):
        try:
            entry_id = int(rest[0][1:])
        except ValueError:
            await update.message.reply_text("Invalid ID. Use the `#42` shown in your log receipt.", parse_mode="Markdown")
            return

        if goal_type == "cal":
            log_date_str = db.delete_cal_entry(entry_id, user.id, chat.id)
            if not log_date_str:
                await update.message.reply_text("Entry not found.")
                return
            log_date = date.fromisoformat(log_date_str)
            goal = db.get_group_goal(chat.id, "cal")
            target = db.get_member_target(user.id, chat.id, "cal")
            if goal and target and target["target"]:
                daily_limit = int(target["target"])
                week_start = _week_start(log_date, group["reset_day"])
                penalty_msgs, _, _ = _re_evaluate_cal_penalty(
                    user.id, chat.id, log_date, daily_limit, week_start, daily_limit * 7, goal
                )
                msg = f"🗑 Entry #{entry_id} removed."
                if penalty_msgs:
                    msg += "\n" + "\n".join(penalty_msgs)
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"🗑 Entry #{entry_id} removed.")
        else:
            row = db.delete_activity_entry(entry_id, user.id, chat.id)
            if not row:
                await update.message.reply_text("Entry not found.")
                return
            log_date = date.fromisoformat(row["log_date"])
            goal = db.get_group_goal(chat.id, goal_type)
            target = db.get_member_target(user.id, chat.id, goal_type)
            if goal and target:
                week_start = _week_start(log_date, group["reset_day"])
                penalty_msgs = _re_evaluate_activity_penalty(
                    user.id, chat.id, goal_type, log_date, week_start, goal, target
                )
                msg = f"🗑 Entry #{entry_id} removed."
                if penalty_msgs:
                    msg += "\n" + "\n".join(penalty_msgs)
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"🗑 Entry #{entry_id} removed.")
        return

    # Day offset or today
    offset = 0
    if rest and rest[0] in ("-1", "-2", "-3"):
        offset = int(rest[0])
    log_date = today + timedelta(days=offset)
    date_str = str(log_date) if log_date != today else "today"

    if goal_type == "cal":
        total = db.get_cal_day_total(user.id, chat.id, log_date)
        if total == 0:
            await update.message.reply_text(f"No calorie logs for {date_str}.")
            return
        db.remove_cal_day(user.id, chat.id, log_date)
        goal = db.get_group_goal(chat.id, "cal")
        target = db.get_member_target(user.id, chat.id, "cal")
        extra = ""
        if goal and target and target["target"]:
            daily_limit = int(target["target"])
            week_start = _week_start(log_date, group["reset_day"])
            penalty_msgs, _, _ = _re_evaluate_cal_penalty(
                user.id, chat.id, log_date, daily_limit, week_start, daily_limit * 7, goal
            )
            extra = "\n" + "\n".join(penalty_msgs) if penalty_msgs else ""
        await update.message.reply_text(f"🗑 Cleared *{total} cal* for {date_str}.{extra}", parse_mode="Markdown")
    else:
        total = db.get_activity_day_total(user.id, chat.id, goal_type, log_date)
        if total == 0:
            await update.message.reply_text(f"No {goal_type} logs for {date_str}.")
            return
        goal = db.get_group_goal(chat.id, goal_type)
        unit = goal["run_unit"] if goal_type == "run" else "steps"
        db.remove_activity_day(user.id, chat.id, goal_type, log_date)
        target = db.get_member_target(user.id, chat.id, goal_type)
        extra = ""
        if goal and target:
            week_start = _week_start(log_date, group["reset_day"])
            penalty_msgs = _re_evaluate_activity_penalty(
                user.id, chat.id, goal_type, log_date, week_start, goal, target
            )
            extra = "\n" + "\n".join(penalty_msgs) if penalty_msgs else ""
        await update.message.reply_text(f"🗑 Cleared *{total:.1f} {unit}* for {date_str}.{extra}", parse_mode="Markdown")


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    chat = update.effective_chat
    user = update.effective_user
    group = db.get_group(chat.id)
    today = _user_today(user.id, chat.id)
    week_start = _week_start(today, group["reset_day"])
    goals = {g["goal_type"]: g for g in db.get_group_goals(chat.id)}
    lines = [f"📊 *{user.first_name}'s Status*\n"]

    # Calories
    if "cal" in goals:
        target = db.get_member_target(user.id, chat.id, "cal")
        if target and target["target"]:
            lim = int(target["target"])
            day_total = db.get_cal_day_total(user.id, chat.id, today)
            week_total = db.get_cal_week_total(user.id, chat.id, week_start)
            if day_total == 0:
                lines.append(f"🍎 Calories today: *not logged yet* (limit {lim})")
            elif day_total <= lim:
                lines.append(f"🍎 Calories today: ✅ *{day_total}/{lim}* ({lim-day_total} left)")
            else:
                lines.append(f"🍎 Calories today: ⚠️ *{day_total}/{lim}* (+{day_total-lim} over)")
            lines.append(f"   Week: *{week_total}/{lim*7}*")
            w = db.get_latest_weight(user.id, chat.id)
            if w:
                goal_weight = target["target2"]
                if goal_weight:
                    oldest = db.get_oldest_weight(user.id, chat.id)
                    is_gain = oldest is not None and goal_weight > oldest
                    if is_gain:
                        icon = "🎉" if w >= goal_weight else "⚖️"
                        diff = f"+{round(goal_weight - w, 1)} kg to go" if w < goal_weight else "goal reached!"
                    else:
                        icon = "🎉" if w <= goal_weight else "⚖️"
                        diff = f"-{round(w - goal_weight, 1)} kg to go" if w > goal_weight else "goal reached!"
                    lines.append(f"   {icon} Weight: *{w} kg* → {goal_weight} kg ({diff})")
                else:
                    lines.append(f"   ⚖️ Latest weight: *{w} kg*")

    # Running
    if "run" in goals:
        goal = goals["run"]
        target = db.get_member_target(user.id, chat.id, "run")
        if target and target["target"]:
            unit = goal["run_unit"]
            day_val = db.get_activity_day_total(user.id, chat.id, "run", today)
            week_val = db.get_activity_week_total(user.id, chat.id, "run", week_start)
            tgt = target["target"]
            period = target["period"]
            if period == "daily":
                icon = "✅" if day_val >= tgt else "⏳"
                lines.append(f"\n🏃 Run today: {icon} *{day_val:.1f}/{tgt:.1f} {unit}*")
            elif period == "freq":
                days_needed = int(target["target2"])
                qualifying = db.get_activity_qualifying_days(user.id, chat.id, "run", week_start, tgt)
                icon = "✅" if qualifying >= days_needed else "⏳"
                lines.append(f"\n🏃 Run frequency: {icon} *{qualifying}/{days_needed} qualifying days* (≥{tgt:.1f} {unit} each)")
                lines.append(f"   Today: *{day_val:.1f} {unit}*" + (" ✅" if day_val >= tgt else ""))
            else:
                icon = "✅" if week_val >= tgt else "⏳"
                lines.append(f"\n🏃 Run this week: {icon} *{week_val:.1f}/{tgt:.1f} {unit}*")

    # Walking
    if "walk" in goals:
        goal = goals["walk"]
        target = db.get_member_target(user.id, chat.id, "walk")
        if target and target["target"]:
            day_val = int(db.get_activity_day_total(user.id, chat.id, "walk", today))
            week_val = int(db.get_activity_week_total(user.id, chat.id, "walk", week_start))
            tgt = int(target["target"])
            period = target["period"]
            if period == "daily":
                icon = "✅" if day_val >= tgt else "⏳"
                lines.append(f"\n🚶 Steps today: {icon} *{day_val:,}/{tgt:,}*")
            else:
                icon = "✅" if week_val >= tgt else "⏳"
                lines.append(f"\n🚶 Steps this week: {icon} *{week_val:,}/{tgt:,}*")

    debt = db.get_total_debt(user.id, chat.id)
    lines.append(f"\n💸 Outstanding debt: *${debt:.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /leaderboard ──────────────────────────────────────────────────────────────

async def leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    chat = update.effective_chat
    group = db.get_group(chat.id)
    goals = {g["goal_type"]: g for g in db.get_group_goals(chat.id)}
    members = db.get_all_members(chat.id)
    leader_tz = db.get_leader_timezone(chat.id) or "UTC"
    today_leader = datetime.now(pytz.timezone(leader_tz)).date()
    week_start = _week_start(today_leader, group["reset_day"])

    active = db.get_active_challenge(chat.id)
    header = f"📊 *Leaderboard*"
    if active:
        header += f" — _{active['name']}_"
    lines = [header, ""]

    rows = []
    for m in members:
        uid = m["user_id"]
        debt = db.get_total_debt(uid, chat.id)
        goal_lines = []

        if "cal" in goals:
            t = db.get_member_target(uid, chat.id, "cal")
            if t and t["target"]:
                lim = int(t["target"])
                day_cal = db.get_cal_day_total(uid, chat.id, today_leader)
                if day_cal == 0:
                    icon = "⬜"
                elif day_cal <= lim:
                    icon = "✅"
                else:
                    icon = "❌"
                goal_lines.append(f"  {icon} Cals: {day_cal:,}/{lim:,}")
            else:
                goal_lines.append("  ➖ Cals: no target set")

        if "run" in goals:
            t = db.get_member_target(uid, chat.id, "run")
            unit = goals["run"]["run_unit"]
            if t and t["target"]:
                if t["period"] == "freq":
                    qualifying = db.get_activity_qualifying_days(uid, chat.id, "run", week_start, t["target"])
                    days_needed = int(t["target2"])
                    icon = "✅" if qualifying >= days_needed else "⏳"
                    goal_lines.append(f"  {icon} Run: {qualifying}/{days_needed} days ≥{t['target']:.1f}{unit} (week)")
                elif t["period"] == "weekly":
                    val = db.get_activity_week_total(uid, chat.id, "run", week_start)
                    icon = "✅" if val >= t["target"] else "⏳"
                    goal_lines.append(f"  {icon} Run: {val:.1f}/{t['target']:.1f}{unit} (week)")
                else:
                    val = db.get_activity_day_total(uid, chat.id, "run", today_leader)
                    icon = "✅" if val >= t["target"] else "⏳"
                    goal_lines.append(f"  {icon} Run: {val:.1f}/{t['target']:.1f}{unit} (today)")
            else:
                goal_lines.append("  ➖ Run: no target set")

        if "walk" in goals:
            t = db.get_member_target(uid, chat.id, "walk")
            if t and t["target"]:
                if t["period"] == "weekly":
                    val = db.get_activity_week_total(uid, chat.id, "walk", week_start)
                    icon = "✅" if val >= t["target"] else "⏳"
                    goal_lines.append(f"  {icon} Steps: {int(val):,}/{int(t['target']):,} (week)")
                else:
                    val = db.get_activity_day_total(uid, chat.id, "walk", today_leader)
                    icon = "✅" if val >= t["target"] else "⏳"
                    goal_lines.append(f"  {icon} Steps: {int(val):,}/{int(t['target']):,} (today)")
            else:
                goal_lines.append("  ➖ Steps: no target set")

        if "weight" in goals or db.get_member_target(uid, chat.id, "weight"):
            latest = db.get_latest_weight(uid, chat.id)
            t = db.get_member_target(uid, chat.id, "weight")
            if latest and t and t["target"]:
                goal_wt = t["target"]
                oldest = db.get_oldest_weight(uid, chat.id)
                gaining = oldest is not None and goal_wt > oldest
                if gaining:
                    icon = "✅" if latest >= goal_wt else "⏳"
                else:
                    icon = "✅" if latest <= goal_wt else "⏳"
                goal_lines.append(f"  {icon} Weight: {latest:.1f} kg → goal {goal_wt:.1f} kg")
            elif latest:
                goal_lines.append(f"  ⚖️ Weight: {latest:.1f} kg")

        debt_str = f"💸 ${debt:.2f} owed" if debt > 0 else "✓ no debt"
        rows.append((debt, m["username"], goal_lines, debt_str))

    for _, name, goal_lines, debt_str in rows:
        lines.append(f"*{name}*  {debt_str}")
        lines.extend(goal_lines)
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /newchallenge · /challenge · /endchallenge ───────────────────────────────

def _parse_challenge_date(s: str):
    """Try to parse a date string in YYYY-MM-DD or DD Mon [YYYY] formats."""
    from datetime import datetime as dt
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
        try:
            d = dt.strptime(s, fmt)
            if d.year == 1900:  # no year given — use current year
                d = d.replace(year=date.today().year)
            return d.date()
        except ValueError:
            pass
    return None


async def newchallenge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx):
        return
    group = db.get_group(chat.id)
    if group["leader_id"] != user.id:
        await update.message.reply_text("Only the group leader can start a new challenge.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/newchallenge <name> [end date]`\n"
            "Examples:\n"
            "`/newchallenge July Grind 2025-07-31`\n"
            "`/newchallenge Summer Cut 31 Jul 2025`\n"
            "`/newchallenge Ongoing` (no end date)",
            parse_mode="Markdown"
        )
        return

    args = list(ctx.args)
    end_date = None
    # Try to parse the last 1–3 args as a date
    for n in (3, 2, 1):
        if len(args) >= n + 1:
            candidate = " ".join(args[-n:])
            parsed = _parse_challenge_date(candidate)
            if parsed:
                if parsed <= date.today():
                    await update.message.reply_text("End date must be in the future.")
                    return
                end_date = parsed
                args = args[:-n]
                break

    name = _sanitize(" ".join(args).strip())
    if not name:
        await update.message.reply_text("Please give the challenge a name.")
        return

    # Post summary of ending challenge if one exists
    existing = db.get_active_challenge(chat.id)
    if existing:
        stats = db.get_challenge_member_stats(existing["id"], chat.id)
        lines = [f"🏁 *{existing['name']}* has ended!\n"]
        sorted_stats = sorted(stats, key=lambda x: x["debt"])
        medals = ["🥇", "🥈", "🥉"]
        for idx, s in enumerate(sorted_stats):
            medal = medals[idx] if idx < 3 else "  "
            debt_str = f"${s['debt']:.2f} owed" if s["debt"] > 0 else "all clear ✓"
            lines.append(f"{medal} *{s['username']}* — {s['days_logged']} days logged — {debt_str}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        db.end_challenge(existing["id"])

    today = _user_today(user.id, chat.id)
    cid = db.create_challenge(chat.id, name, today, end_date)

    end_str = f" · ends {end_date.strftime('%d %b %Y')}" if end_date else " · no end date"
    await update.message.reply_text(
        f"🚀 *{name}* has started! (Challenge #{cid}{end_str})\n\n"
        f"All logs from now will be tagged to this challenge. Good luck! 💪",
        parse_mode="Markdown"
    )


async def challenge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if await _guard(update, ctx):
        return
    active = db.get_active_challenge(chat.id)
    if not active:
        history = db.get_challenge_history(chat.id)
        if not history:
            await update.message.reply_text(
                "No challenge running yet. Leader can start one with `/newchallenge`.",
                parse_mode="Markdown"
            )
        else:
            lines = [f"No active challenge. Past challenges:\n"]
            for c in history[:5]:
                end = c["end_date"] or "open-ended"
                lines.append(f"• *{c['name']}* (#{c['id']}) — {c['start_date']} → {end}")
            lines.append("\nStart a new one with `/newchallenge`.")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    start = date.fromisoformat(active["start_date"])
    days_in = (date.today() - start).days + 1
    end_str = ""
    if active["end_date"]:
        end = date.fromisoformat(active["end_date"])
        days_left = (end - date.today()).days
        end_str = f"Ends: {end.strftime('%d %b %Y')} ({days_left} days left)\n"

    stats = db.get_challenge_member_stats(active["id"], chat.id)
    sorted_stats = sorted(stats, key=lambda x: x["debt"])
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"🏆 *{active['name']}* (Challenge #{active['id']})",
        f"Started: {start.strftime('%d %b %Y')} · Day {days_in}",
    ]
    if end_str:
        lines.append(end_str.strip())
    lines.append("")
    for idx, s in enumerate(sorted_stats):
        medal = medals[idx] if idx < 3 else "  "
        debt_str = f"${s['debt']:.2f} owed" if s["debt"] > 0 else "all clear ✓"
        lines.append(f"{medal} *{s['username']}* — {s['days_logged']} days logged — {debt_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def endchallenge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if await _guard(update, ctx):
        return
    group = db.get_group(chat.id)
    if group["leader_id"] != user.id:
        await update.message.reply_text("Only the group leader can end the challenge.")
        return
    active = db.get_active_challenge(chat.id)
    if not active:
        await update.message.reply_text("No active challenge to end.")
        return

    stats = db.get_challenge_member_stats(active["id"], chat.id)
    sorted_stats = sorted(stats, key=lambda x: x["debt"])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏁 *{active['name']}* — Final Results\n"]
    for idx, s in enumerate(sorted_stats):
        medal = medals[idx] if idx < 3 else "  "
        debt_str = f"${s['debt']:.2f} owed" if s["debt"] > 0 else "all clear ✓"
        lines.append(f"{medal} *{s['username']}* — {s['days_logged']} days logged — {debt_str}")
    lines.append("\nStart the next one with `/newchallenge`.")

    db.end_challenge(active["id"])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /debt · /paid · /history ──────────────────────────────────────────────────

async def debt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    chat = update.effective_chat
    rows = db.get_all_debts_for_group(chat.id)
    group = db.get_group(chat.id)
    lines = ["💸 *Debt Summary*\n"]
    any_debt = False
    for r in rows:
        owing = max(r["owing"], 0)
        if owing > 0:
            any_debt = True
            lines.append(f"• *{r['username']}* owes ${owing:.2f} (paid ${r['paid_total']:.2f} to date)")
    if not any_debt:
        lines.append("Everyone's clear! 🎉")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    user = update.effective_user
    chat = update.effective_chat
    if not _rate_ok(user.id, chat.id):
        await update.message.reply_text("Slow down.")
        return
    total_debt = db.get_total_debt(user.id, chat.id)

    amount = total_debt if not ctx.args else None
    if ctx.args:
        try:
            amount = float(ctx.args[0].replace("$", ""))
            assert amount > 0
        except Exception:
            await update.message.reply_text("Usage: `/paid` (all) or `/paid <amount>`", parse_mode="Markdown")
            return

    if total_debt == 0:
        await update.message.reply_text("You have no outstanding debt. 🎉")
        return

    amount = min(amount, total_debt)
    db.record_payment(user.id, chat.id, amount)
    remaining = db.get_total_debt(user.id, chat.id)
    text = f"✅ *${amount:.2f} paid*" + (" — all clear! 🎉" if remaining == 0 else f" — *${remaining:.2f} still owing*")
    await update.message.reply_text(text, parse_mode="Markdown")


async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _guard(update, ctx):
        return
    user = update.effective_user
    chat = update.effective_chat
    debts = db.get_debt_history(user.id, chat.id)
    payments = db.get_payment_history(user.id, chat.id)
    lines = [f"📋 *{user.first_name}'s History*\n"]
    if debts:
        lines.append("*Charges:*")
        for d in debts[-10:]:
            lines.append(f"  • {d['date']} — ${d['amount']:.2f} ({d['reason']})")
    else:
        lines.append("*Charges:* none yet")
    if payments:
        lines.append("\n*Payments:*")
        for p in payments[-10:]:
            lines.append(f"  • {p['date']} — ${p['amount']:.2f}")
    else:
        lines.append("\n*Payments:* none yet")
    tc = sum(d["amount"] for d in debts)
    tp = sum(p["amount"] for p in payments)
    lines.append(f"\nTotal charged: ${tc:.2f} | Paid: ${tp:.2f} | Owing: ${max(tc-tp,0):.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

BOT_COMMANDS = [
    ("setup",       "Configure group goals & penalties (leader only)"),
    ("rules",       "View current group rules"),
    ("mygoals",     "View & set your personal targets"),
    ("mygoal",      "Set a target e.g. /mygoal cal 1800"),
    ("cal",         "Log calories e.g. /cal 300 lunch"),
    ("run",         "Log a run e.g. /run 5.2 park"),
    ("walk",        "Log steps e.g. /walk 8500"),
    ("weight",      "Log your weight e.g. /weight 65.5"),
    ("removelog",   "Remove a log e.g. /removelog cal"),
    ("status",        "Your progress today & this week"),
    ("leaderboard",   "Everyone's stats at a glance"),
    ("newchallenge",  "Start a new challenge e.g. /newchallenge July Grind 2025-07-31"),
    ("challenge",     "Current challenge standings"),
    ("endchallenge",  "End the current challenge (leader only)"),
    ("debt",          "See all outstanding debts"),
    ("paid",        "Record a payment e.g. /paid or /paid 10"),
    ("history",     "Your charges and payment history"),
    ("help",        "Show all commands"),
]


async def _post_init(app: Application):
    from telegram import BotCommand
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    app.bot_data["db"] = db
    schedule_jobs(app)
    reschedule_all_reminders(app)
    logger.info("Bot ready.")


def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).post_init(_post_init).build()

    # Commands
    for cmd, fn in [
        ("start", start), ("help", help_cmd),
        (["setup", "editrules"], setup),
        ("rules", rules),
        ("mygoals", mygoals), ("mygoal", mygoal),
        ("cal", cal), ("run", run), ("walk", walk),
        ("weight", weight), ("removelog", removelog),
        ("status", status), ("leaderboard", leaderboard),
        ("newchallenge", newchallenge), ("challenge", challenge), ("endchallenge", endchallenge),
        ("debt", debt), ("paid", paid), ("history", history),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_reset_day,    pattern="^srd_"))
    app.add_handler(CallbackQueryHandler(cb_goal_toggle,  pattern="^gtog_"))
    app.add_handler(CallbackQueryHandler(cb_setup_daily,  pattern="^sdp_"))
    app.add_handler(CallbackQueryHandler(cb_setup_weekly, pattern="^swp_"))
    app.add_handler(CallbackQueryHandler(cb_run_unit,     pattern="^run_unit_"))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
