# Miloco 2.0 User Guide

Miloco 2.0 is Xiaomi's open-source whole-home AI solution, built on [OpenClaw](https://openclaw.ai) or [Hermes Agent](https://hermes-agent.nousresearch.com) — an AI housekeeper that observes, reasons, and understands your home better the longer you live with it.

This guide focuses on how to use Miloco once it's installed. For product positioning, the full feature list, and installation details, see the [README](README.md); below, we first get acquainted with Miloco, then start using it.

# 1. Getting to Know Miloco 2.0

## 1.1 What Is Miloco 2.0

Miloco 2.0 uses the video and audio from Xiaomi Home cameras as its omni-modal perception input, the self-developed MiMo Large Model as its intelligent brain, and runs as an Agent plugin on top of [OpenClaw](https://openclaw.ai) or [Hermes Agent](https://hermes-agent.nousresearch.com): it perceives what happens at home, makes proactive judgments based on common sense, and acts by orchestrating Xiaomi Home devices — providing personalized service for each member through Identity Recognition and Family Memory.

## 1.2 Five Key Features

Miloco's capabilities are built on five features: **General Common Sense, Identity Recognition, Family Memory, Family Tasks, and Proactive Intelligence**. See [Core Features in the README](README.md#core-features) for details on each.

## 1.3 Two Core Capabilities

These five features come together into two core capabilities:

- **Proactive Intelligence Powered by Common Sense and Memory**: Combining General Common Sense, Identity Recognition, and Family Memory, Miloco continuously observes your home — issuing tiered alerts when it detects danger or anomalies, and serving each family member according to their preferences. No rule-by-rule configuration; it works out of the box and gets to know you better over time.
- **Complex Family Tasks**: Miloco breaks vague, long-term goals (drink 8 glasses of water a day, get reminded after sitting too long, take medication on time) into trackable family tasks — automatically counting, tracking progress, and reminding on schedule, with the Agent interpreting intent and executing autonomously.

For how to use these two core capabilities, see Chapter 2, "Getting Started," below.

# 2. Getting Started

Daily use comes down to one guiding principle: **just tell your Agent what you want**. Recognizing people, remembering habits, managing tasks, proactively watching over your home with common sense — Miloco decides which of these capabilities (see Chapter 1) to invoke on its own. You don't need to memorize commands or worry about which feature is at work behind the scenes.

> [!TIP]
> **Raise your own Miloco.** Its out-of-the-box behavior won't always match your taste—just tell Miloco through your Agent (e.g. "don't remind me when the place is messy"), and it remembers your preference and adjusts what it does proactively. Every remark "raises" a Miloco that's tuned to your home, and it knows you better the longer you live with it.

## 2.1 Just Say What You Want

Simply follow the examples below:

- **Introduce Family Members**: "Add a family member named Wang Lei, he's Dad," "Get to know me," "Who's registered in our home?"

  ![Introduce family members](assets/chat_add_member_en.png)

- **Remember Preferences**: "Remember, Dad doesn't like the lights too bright," "Remember, we usually go to bed at 11."

  ![Remember preferences](assets/chat_remember_pref_en.png)

- **Tune Proactive Reminders**: "Don't bother reminding me when the place is messy," "Stop nagging me to tidy up." — Tell it which kinds of proactive reminders you do or don't want; it records this in your family profile, and from then on the camera perception judges accordingly and simply stops raising those reminders. **Every offhand remark like this "raises" Miloco**—the longer you live with it, the better it knows you and the less it interrupts.

  ![Tune proactive reminders](assets/chat_tune_reminder_en.png)

- **Build Habits / Check-Ins**: "Drink 8 glasses of water a day," "Make sure I exercise once a day."

  ![Build habits / check-ins](assets/chat_build_habit_en.png)
- **Set Reminders**: "Remind me to check the stove in 30 minutes," "Wake me up at 7 a.m. on weekdays."
- **Track Duration**: "Remind me when my kid plays the iPad for 1 hour straight," "Remind me to get up and move after sitting for 50 minutes."
- **Query / Control Devices**: "Is anyone in the living room?" "Turn off the bedroom light."

It then confirms in plain language. For example, if you say "drink 8 glasses of water a day," it might reply:

> Done! I'll remind you to drink water at 10:00, 12:00, 14:00, 16:00, 18:00, and 20:00 every day, and reset your progress automatically each midnight.

## 2.2 Managing & Reviewing

- **Manage Tasks**: "What tasks do I have now?" "Pause the water reminder," "Change the water goal to 10 glasses," "Stop tracking my water intake."
- **Review What It Did**: Open the "Logs" page of the Web Dashboard (see Chapter 3) to see the full record of every "perceive → judge → act" cycle.

**Note**: Habit-tracking and behavior-counting tasks rely on camera perception. If your home has no camera and no microphone-equipped device, such tasks can't be created. Pure scheduled reminders (e.g., "wake me at 8 tomorrow") don't require a camera.

# 3. Web Home Dashboard

Besides conversation, Miloco offers a Web dashboard you can view and operate directly on your phone, tablet, or computer.

## 3.1 How to Access

- **Method 1**: Once the backend service is running, open `http://127.0.0.1:1810/` in a browser (on the host machine); from other devices on the same Wi-Fi, visit `http://<host-IP>:1810/`.
- **Method 2**: Run `miloco-cli dashboard` in your terminal to open the dashboard in your browser automatically — no need to type the address.

## 3.2 What the Dashboard Offers

- **Overview**: Watch live "right now at home" feeds from online cameras with one click; works across browsers on every platform.

  ![Overview page](assets/panel_overview_en.png)

- **Devices**: See the status of all your devices at a glance, organized by room.

  ![Devices page](assets/panel_devices_en.png)

- **Family**: Manage identity registration, family profiles / member habits, family tasks, and Dreaming memories directly in the browser.

  ![Family page](assets/panel_family_en_1.png)

  ![Family page](assets/panel_family_en_2.png)

- **Logs**: "What Happened at Home Today" — review the full "perceive → judge → act" record along a timeline.

  ![Logs page](assets/panel_logs_en.png)

- **Models**: View model selection, usage, and performance data directly (today's total tokens, model breakdown, consumption types, time distribution, and more).

  ![Models page](assets/panel_models_1_en.png)

  ![Models page](assets/panel_models_2_en.png)

# 4. Command Reference

Daily use is conversation-first; the commands below are only for troubleshooting or manual operations. `miloco-cli` has 16 command groups and nearly 100 subcommands, but for most everyday tasks you can simply talk to your housekeeper.

## 4.1 Service Management

```bash
miloco-cli service start | stop | restart | status
miloco-cli service logs -f          # Live logs
```

## 4.2 Account

```bash
miloco-cli account status           # Binding status
miloco-cli account bind             # Start binding
miloco-cli account authorize "<auth-code>"
miloco-cli account unbind           # Unbind
```

## 4.3 Configuration

```bash
miloco-cli config show              # Show config (keys masked)
miloco-cli config set model.omni.api_key sk-xxxx
miloco-cli config get model.omni.model
miloco-cli config list-paths        # List all configurable paths
```

## 4.4 Devices

```bash
miloco-cli device list              # List devices (optionally --room / --category / --online)
miloco-cli device spec <did>        # Show device capability spec
miloco-cli device control <did> --set <iid> <value>
```

## 4.5 Members & Perception

```bash
miloco-cli person list              # List members
miloco-cli person add --name "John" --role "Dad"
miloco-cli perceive devices         # List perception devices
miloco-cli perceive query --source <did> --query "Is anyone in the living room?"
```

## 4.6 Rules & Notifications

```bash
miloco-cli rule list                # List automation rules
miloco-cli notify push --text "test push"   # Push to the Xiaomi Home app
miloco-cli scope camera list        # Camera allow/deny list
```

# 5. Appendix

## 5.1 License

This project is for non-commercial use only. Without written authorization from Xiaomi, it may not be used to develop applications, web services, or other software. See the repository's [LICENSE.md](LICENSE.md) for the full terms.

## 5.2 Resources

- Code repository: [github.com/XiaoMi/xiaomi-miloco](https://github.com/XiaoMi/xiaomi-miloco)
- OpenClaw website: [openclaw.ai](https://openclaw.ai)
- Hermes Agent website: [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com)
- MiMo model platform: [platform.xiaomimimo.com](https://platform.xiaomimimo.com)

---

This guide is updated alongside Miloco 2.0. For anything not covered here, the fastest answer is always to just ask your AI housekeeper.
