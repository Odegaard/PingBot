## PingBot Usage

**PingBot** is a simple reminder bot for Discord.

---

### `/ping <time> [message] [snooze]`

Create a reminder.

#### Time formats supported
- `10s` → seconds  
- `5m` → minutes  
- `2h` → hours  
- `1d` → days  

#### Arguments
- `message` *(optional)*: text shown in the reminder  
- `snooze` *(optional)*: `active` or `inactive`

#### Examples
/ping 3d Take out the trash active
/ping 30m Check the oven

---

### When a reminder fires

- The bot sends: `<message_link>: @you` (the mention always pings)
- If snooze is active, the footer shows: `PingBot - Active Snooze`
- If a message was set, it appears in the embed

---

### Active Snooze (auto-repeat)

If you do nothing, reminders repeat automatically:

- after **10 minutes**
- then **1 hour**
- then **3 hours**
- then every **24 hours**
- auto-stops after **7 days**

---

### Snooze controls on reminder message

- **Don't snooze**: stop future repeats
- Dropdown options:
  - 1 hour
  - 24 hours
  - 48 hours (manual reschedule)

---

### `/reminders`

Show your pending reminders.

---

### `/cancel <id>`

Cancel one reminder by ID.

---

### `/help`

Show this help message.

---

### Support

https://discord.gg/ffJNStGpy9
