# Bale Hermes Adapter

A plugin to connect **Bale** messenger to **[Hermes AI Agent](https://hermes-agent.nousresearch.com/)**.

> 🤖 This project was entirely written by [Hermes AI Agent](https://hermes-agent.nousresearch.com/).

---

## Features

- Full AI chat via Bale messenger
- Receive photos, files, audio, and video
- Built-in system commands (no AI token consumed)
- First-run pairing setup — no manual ID configuration needed
- Stealth mode — unauthorized users see `inactive`
- Startup notification on system boot
- Unauthorized access alerts to admin
- Inline keyboard menu

---

## Quick Start

### 1. Install Hermes

Follow the [Hermes installation guide](https://hermes-agent.nousresearch.com/).

### 2. Install this plugin

```bash
git clone https://github.com/majidabedin/bale-hermes-adapter ~/.hermes/plugins/bale
```

### 3. Configure

Add your bot token to `~/.hermes/.env`:

```env
BALE_BOT_TOKEN=your_bot_token_here
```

Get your token from **@BotFather** on Bale.

### 4. Start the gateway

```bash
hermes gateway run
```

### 5. Pair your account

On first run with no admin configured, a **one-time 8-digit pairing code** will be printed to the console:

```
==================================================
  BALE ADAPTER — SETUP REQUIRED
  No admin configured. Send this code to your bot:

      🔑  47291836

  Code expires in 10 minutes.
==================================================
```

Send that code to your bot on Bale. Once verified, your account is registered as admin and the code is invalidated.

Restart the gateway to complete setup:

```bash
hermes gateway restart
```

---

## Auto-Install via Hermes

Paste this prompt into your Hermes chat:

```
Install the Bale Hermes Adapter from: https://github.com/majidabedin/bale-hermes-adapter

Steps:
1. Clone the repo into ~/.hermes/plugins/bale
2. Ask me for my Bale bot token
3. Add BALE_BOT_TOKEN to ~/.hermes/.env
4. Restart the gateway
5. Tell me to check the console for the pairing code
```

---

## Commands

| Command | Description |
|---------|-------------|
| `/c` | Command menu (inline keyboard) |
| `/status` | Gateway status |
| `/hw` | Hardware info and resource usage |
| `/ports` | Open listening ports |
| `/ip` | Local and public IP addresses |
| `/temp` | CPU temperature |
| `/ping <host>` | Ping a host |
| `/gw` | Restart gateway |
| `/reboot` | Reboot system |
| `/shutdown` | Shut down system |

---

## Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `BALE_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `BALE_ALLOWED_USERS` | Comma-separated admin user IDs (set automatically on first pair) | ❌ |
| `BALE_HOME_CHANNEL` | Default chat ID for cron/notification delivery | ❌ |

---

## Author

[majidabedin](https://github.com/majidabedin)

## License

MIT
