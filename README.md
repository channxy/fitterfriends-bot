# FitterFriends Bot

A Telegram group accountability bot that tracks fitness goals, fires penalties when goals are missed, and runs time-boxed challenges with a full history.

---

## Features

- **Calorie tracking** — daily limit with overage penalties
- **Running goals** — daily, weekly total, or frequency-based (e.g. 3 days/week)
- **Walking goals** — daily or weekly step targets
- **Weight tracking** — logs progress toward a gain or loss goal
- **Penalty system** — automatic daily/weekly debt charges when goals are missed
- **Backlogging** — log up to 3 days back; past penalties auto-adjust
- **Challenges** — named, time-boxed challenges with automatic end summaries
- **Debt tracking** — running tally of what each member owes
- **Daily reminders** — 10pm nudge to anyone who hasn't hit their goals
- **Leaderboard** — group-wide compliance and debt overview

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/fitterfriends-bot.git
cd fitterfriends-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file:

```
BOT_TOKEN=your_telegram_bot_token
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your_turso_token
```

- `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` — from [Turso](https://turso.tech) (free tier)
- If Turso vars are not set, falls back to a local `fitterfriends.db` SQLite file

### 3. Run

```bash
python3 bot.py
```

---

## Deployment (Oracle Cloud Free Tier)

See the full deployment guide in the conversation history. Summary:

1. Create an Oracle Cloud account → spin up an Ampere A1 VM (free forever)
2. SSH in, clone repo, install deps, create `.env`
3. Set up a `systemd` service for auto-restart
4. To update: `git pull && sudo systemctl restart fitterfriends`

---

## Commands

### Group setup (leader only)
| Command | Description |
|---|---|
| `/setup` | Configure group goals and penalties |
| `/editrules` | Edit existing group rules |
| `/rules` | View current group rules |

### Personal targets (each member)
| Command | Description |
|---|---|
| `/mygoals` | See what goals are available and how to set targets |
| `/mygoal cal 1800` | Set daily calorie limit |
| `/mygoal run 5 daily` | Run 5 km every day |
| `/mygoal run 3 3x` | Run 3 km on 3 days a week |
| `/mygoal run 20 weekly` | Run 20 km total across the week |
| `/mygoal walk 10000 daily` | Walk 10,000 steps every day |
| `/mygoal weight 65` | Set a weight target |

### Logging
| Command | Description |
|---|---|
| `/cal 300 lunch` | Log 300 calories (label optional) |
| `/cal -1 300 lunch` | Backlog yesterday's calories |
| `/run 5.2 park` | Log a 5.2 km run |
| `/walk 8500` | Log 8,500 steps |
| `/weight 65.5` | Log today's weight |
| `/removelog cal` | Remove today's calorie log |
| `/removelog cal #42` | Remove a specific log entry by ID |
| `/removelog cal -1` | Clear yesterday's calorie log |

### Stats
| Command | Description |
|---|---|
| `/status` | Your progress today and this week |
| `/leaderboard` | Everyone's compliance and debt at a glance |

### Challenges
| Command | Description |
|---|---|
| `/newchallenge July Grind 2025-07-31` | Start a new challenge (leader only) |
| `/newchallenge Summer Cut` | Start an open-ended challenge |
| `/challenge` | Current challenge standings |
| `/endchallenge` | End the challenge early (leader only) |

### Payments
| Command | Description |
|---|---|
| `/debt` | See all outstanding debts in the group |
| `/paid` | Mark your full debt as settled |
| `/paid 10` | Record a partial payment of $10 |
| `/history` | Your full charge and payment history |

---

## How challenges work

- A **challenge** is a named period (e.g. "July Grind") with an optional end date
- All logs (calories, runs, walks, weight) and all penalties are tagged with the current `challenge_id`
- When a challenge ends (manually via `/endchallenge` or automatically at midnight on the end date), the bot posts a final summary with rankings
- Starting a new challenge via `/newchallenge` automatically closes the current one and posts its summary
- Past challenges are preserved — all historical data is queryable by `challenge_id`
- If no challenge is active, logs still work — they're just tagged `NULL` for challenge_id

---

## Database schema

| Table | Key columns |
|---|---|
| `groups` | `chat_id`, `leader_id`, `reset_day` |
| `group_goals` | `chat_id`, `goal_type`, `daily_penalty`, `weekly_penalty` |
| `members` | `user_id`, `chat_id`, `username` |
| `member_targets` | `user_id`, `chat_id`, `goal_type`, `target`, `period` |
| `challenges` | `id`, `chat_id`, `name`, `start_date`, `end_date`, `status` |
| `calorie_logs` | `user_id`, `chat_id`, `challenge_id`, `log_date`, `calories` |
| `activity_logs` | `user_id`, `chat_id`, `challenge_id`, `goal_type`, `log_date`, `value` |
| `weight_logs` | `user_id`, `chat_id`, `challenge_id`, `log_date`, `weight_kg` |
| `debts` | `user_id`, `chat_id`, `challenge_id`, `amount`, `reason` |
| `payments` | `user_id`, `chat_id`, `amount`, `date` |
| `penalty_log` | `user_id`, `chat_id`, `goal_type`, `period_key` |

All logs include `challenge_id` for future analytics or a web dashboard scoped per challenge.
