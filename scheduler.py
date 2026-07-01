"""
Daily penalty auto-fire.
Runs once per day (midnight UTC). For each group + member, checks if
yesterday's daily goals were missed and fires penalties accordingly.
Also checks weekly goals on the group's reset day.
"""
import logging
from datetime import datetime, timedelta, timezone
import pytz
from telegram.ext import Application

logger = logging.getLogger(__name__)

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


def _week_start(d, reset_day: int):
    from datetime import date
    days_since = (d.weekday() - reset_day) % 7
    return d - timedelta(days=days_since)


async def daily_penalty_check(context):
    """PTB JobQueue callback — fires every day at midnight UTC."""
    from db import Database
    db: Database = context.bot_data["db"]
    bot = context.bot

    now_utc = datetime.now(timezone.utc)

    for group in db.get_all_groups():
        chat_id = group["chat_id"]
        goals = {g["goal_type"]: g for g in db.get_group_goals(chat_id)}
        if not goals:
            continue

        leader_tz_str = db.get_leader_timezone(chat_id) or "UTC"
        leader_tz = pytz.timezone(leader_tz_str)
        local_now = now_utc.astimezone(leader_tz)
        yesterday = local_now.date() - timedelta(days=1)
        ws = _week_start(yesterday, group["reset_day"])
        is_reset_day = yesterday.weekday() == group["reset_day"]

        penalty_msgs = []

        for member in db.get_all_members(chat_id):
            uid = member["user_id"]
            name = member["username"]
            tz_str = member["timezone"] or leader_tz_str
            member_tz = pytz.timezone(tz_str)
            member_yesterday = now_utc.astimezone(member_tz).date() - timedelta(days=1)

            # ── Calories ──────────────────────────────────────────────────
            if "cal" in goals:
                goal = goals["cal"]
                target = db.get_member_target(uid, chat_id, "cal")
                if target and target["target"]:
                    daily_limit = int(target["target"])
                    day_total = db.get_cal_day_total(uid, chat_id, member_yesterday)
                    period_key = f"daily_{member_yesterday}"
                    if day_total > daily_limit and not db.penalty_issued(uid, chat_id, "cal", period_key):
                        over = day_total - daily_limit
                        db.add_debt(uid, chat_id, goal["daily_penalty"],
                                    f"Exceeded daily cal limit by {over} cal on {member_yesterday}")
                        db.mark_penalty(uid, chat_id, "cal", period_key)
                        penalty_msgs.append(f"⚠️ *{name}* exceeded daily calories by {over} cal → ${goal['daily_penalty']:.0f}")

                    # Weekly cal check on reset day
                    if is_reset_day:
                        week_total = db.get_cal_week_total(uid, chat_id, ws)
                        weekly_limit = daily_limit * 7
                        wkey = f"weekly_{ws}"
                        if week_total > weekly_limit and not db.penalty_issued(uid, chat_id, "cal_weekly", wkey):
                            over = week_total - weekly_limit
                            db.add_debt(uid, chat_id, goal["weekly_penalty"],
                                        f"Exceeded weekly cal limit by {over} cal (week of {ws})")
                            db.mark_penalty(uid, chat_id, "cal_weekly", wkey)
                            penalty_msgs.append(f"⚠️ *{name}* exceeded weekly calories by {over} cal → ${goal['weekly_penalty']:.0f}")

            # ── Running ───────────────────────────────────────────────────
            if "run" in goals:
                goal = goals["run"]
                target = db.get_member_target(uid, chat_id, "run")
                if target and target["target"]:
                    unit = goal["run_unit"]
                    period = target["period"]

                    if period == "daily":
                        day_val = db.get_activity_day_total(uid, chat_id, "run", member_yesterday)
                        pkey = f"daily_{member_yesterday}"
                        if day_val < target["target"] and not db.penalty_issued(uid, chat_id, "run", pkey):
                            db.add_debt(uid, chat_id, goal["daily_penalty"],
                                        f"Missed daily run target ({day_val:.1f}/{target['target']:.1f} {unit}) on {member_yesterday}")
                            db.mark_penalty(uid, chat_id, "run", pkey)
                            penalty_msgs.append(f"🏃 *{name}* missed daily run ({day_val:.1f}/{target['target']:.1f} {unit}) → ${goal['daily_penalty']:.0f}")

                    elif period == "weekly" and is_reset_day:
                        week_val = db.get_activity_week_total(uid, chat_id, "run", ws)
                        wkey = f"weekly_{ws}"
                        if week_val < target["target"] and not db.penalty_issued(uid, chat_id, "run", wkey):
                            db.add_debt(uid, chat_id, goal["weekly_penalty"],
                                        f"Missed weekly run target ({week_val:.1f}/{target['target']:.1f} {unit}, week of {ws})")
                            db.mark_penalty(uid, chat_id, "run", wkey)
                            penalty_msgs.append(f"🏃 *{name}* missed weekly run ({week_val:.1f}/{target['target']:.1f} {unit}) → ${goal['weekly_penalty']:.0f}")

                    elif period == "freq" and is_reset_day:
                        days_needed = int(target["target2"])
                        qualifying = db.get_activity_qualifying_days(uid, chat_id, "run", ws, target["target"])
                        wkey = f"freq_{ws}"
                        if qualifying < days_needed and not db.penalty_issued(uid, chat_id, "run", wkey):
                            db.add_debt(uid, chat_id, goal["weekly_penalty"],
                                        f"Missed run frequency ({qualifying}/{days_needed} days ≥{target['target']:.1f} {unit}, week of {ws})")
                            db.mark_penalty(uid, chat_id, "run", wkey)
                            penalty_msgs.append(f"🏃 *{name}* missed run frequency ({qualifying}/{days_needed} days) → ${goal['weekly_penalty']:.0f}")

            # ── Walking ───────────────────────────────────────────────────
            if "walk" in goals:
                goal = goals["walk"]
                target = db.get_member_target(uid, chat_id, "walk")
                if target and target["target"]:
                    period = target["period"]

                    if period == "daily":
                        day_val = db.get_activity_day_total(uid, chat_id, "walk", member_yesterday)
                        pkey = f"daily_{member_yesterday}"
                        if day_val < target["target"] and not db.penalty_issued(uid, chat_id, "walk", pkey):
                            db.add_debt(uid, chat_id, goal["daily_penalty"],
                                        f"Missed daily step goal ({day_val:.0f}/{target['target']:.0f} steps) on {member_yesterday}")
                            db.mark_penalty(uid, chat_id, "walk", pkey)
                            penalty_msgs.append(f"🚶 *{name}* missed step goal ({day_val:.0f}/{target['target']:.0f}) → ${goal['daily_penalty']:.0f}")

                    elif period == "weekly" and is_reset_day:
                        week_val = db.get_activity_week_total(uid, chat_id, "walk", ws)
                        wkey = f"weekly_{ws}"
                        if week_val < target["target"] and not db.penalty_issued(uid, chat_id, "walk", wkey):
                            db.add_debt(uid, chat_id, goal["weekly_penalty"],
                                        f"Missed weekly step goal ({week_val:.0f}/{target['target']:.0f} steps, week of {ws})")
                            db.mark_penalty(uid, chat_id, "walk", wkey)
                            penalty_msgs.append(f"🚶 *{name}* missed weekly steps ({week_val:.0f}/{target['target']:.0f}) → ${goal['weekly_penalty']:.0f}")

        if penalty_msgs:
            try:
                text = "🔔 *Daily check-in*\n\n" + "\n".join(penalty_msgs)
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.error("Failed to send penalty message to %s: %s", chat_id, e)

    # ── Auto-end expired challenges ────────────────────────────────────────────
    from datetime import date as _date
    for ch in db.get_all_active_challenges():
        if not ch["end_date"]:
            continue
        end = _date.fromisoformat(ch["end_date"])
        if _date.today() > end:
            chat_id = ch["chat_id"]
            stats = db.get_challenge_member_stats(ch["id"], chat_id)
            sorted_stats = sorted(stats, key=lambda x: x["debt"])
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"🏁 *{ch['name']}* has ended!\n"]
            for idx, s in enumerate(sorted_stats):
                medal = medals[idx] if idx < 3 else "  "
                debt_str = f"${s['debt']:.2f} owed" if s["debt"] > 0 else "all clear ✓"
                lines.append(f"{medal} *{s['username']}* — {s['days_logged']} days logged — {debt_str}")
            lines.append("\nStart the next one with /newchallenge 🚀")
            db.end_challenge(ch["id"])
            try:
                await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                logger.error("Failed to send challenge-end message to %s: %s", chat_id, e)


async def daily_reminder(context):
    """Fires at 10pm leader's timezone. Reminds members who haven't hit today's goals."""
    from db import Database
    db: Database = context.bot_data["db"]
    bot = context.bot
    chat_id = context.job.data

    group = db.get_group(chat_id)
    if not group:
        return
    goals = {g["goal_type"]: g for g in db.get_group_goals(chat_id)}
    if not goals:
        return

    leader_tz_str = db.get_leader_timezone(chat_id) or "UTC"
    today = datetime.now(pytz.timezone(leader_tz_str)).date()
    lines = []

    for member in db.get_all_members(chat_id):
        uid = member["user_id"]
        name = member["username"]
        unmet = []

        if "cal" in goals:
            t = db.get_member_target(uid, chat_id, "cal")
            if t and t["target"]:
                total = db.get_cal_day_total(uid, chat_id, today)
                if total == 0:
                    unmet.append("🍎 haven't logged calories")
                elif total > int(t["target"]):
                    over = total - int(t["target"])
                    unmet.append(f"🍎 over by {over} cal")

        if "run" in goals:
            t = db.get_member_target(uid, chat_id, "run")
            unit = goals["run"]["run_unit"]
            if t and t["target"]:
                val = db.get_activity_day_total(uid, chat_id, "run", today)
                if t["period"] == "daily" and val < t["target"]:
                    unmet.append(f"🏃 {val:.1f}/{t['target']:.1f} {unit} run")
                elif t["period"] == "freq":
                    from datetime import date as _date
                    ws = _week_start(today, group["reset_day"])
                    qualifying = db.get_activity_qualifying_days(uid, chat_id, "run", ws, t["target"])
                    days_needed = int(t["target2"])
                    if qualifying < days_needed:
                        unmet.append(f"🏃 {qualifying}/{days_needed} run days this week (≥{t['target']:.1f} {unit} each)")

        if "walk" in goals:
            t = db.get_member_target(uid, chat_id, "walk")
            if t and t["target"] and t["period"] == "daily":
                val = int(db.get_activity_day_total(uid, chat_id, "walk", today))
                if val < int(t["target"]):
                    unmet.append(f"🚶 {val:,}/{int(t['target']):,} steps")

        if unmet:
            lines.append(f"• *{name}*: " + ", ".join(unmet))

    if lines:
        try:
            text = "⏰ *2-hour warning!* Daily goals ending soon:\n\n" + "\n".join(lines)
            await bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Reminder failed for %s: %s", chat_id, e)


def schedule_reminder(app: Application, chat_id: int, tz_str: str):
    """Register (or replace) the 10pm reminder job for a group."""
    from datetime import time as dtime
    job_queue = app.job_queue
    for job in job_queue.get_jobs_by_name(f"reminder_{chat_id}"):
        job.schedule_removal()
    try:
        tz = pytz.timezone(tz_str)
    except Exception:
        tz = timezone.utc
    job_queue.run_daily(
        daily_reminder,
        time=dtime(22, 0, tzinfo=tz),
        name=f"reminder_{chat_id}",
        data=chat_id,
    )
    logger.info("Reminder scheduled for chat %s at 22:00 %s", chat_id, tz_str)


def reschedule_all_reminders(app: Application):
    """Re-register reminder jobs for all groups after a bot restart."""
    from db import Database
    db: Database = app.bot_data["db"]
    for group in db.get_all_groups():
        tz_str = db.get_leader_timezone(group["chat_id"]) or "UTC"
        schedule_reminder(app, group["chat_id"], tz_str)


def schedule_jobs(app: Application):
    """Register the daily penalty job. Call once after app is built."""
    from datetime import time as dtime
    job_queue = app.job_queue
    job_queue.run_daily(daily_penalty_check, time=dtime(hour=0, minute=0, tzinfo=timezone.utc))
    logger.info("Daily penalty job scheduled.")
