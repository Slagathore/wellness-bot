# Discord Bot Setup - Step-by-Step Guide

**Goal**: Set up a Discord bot that works alongside your Telegram wellness bot, sharing the same database and AI pipeline.

**Time Required**: ~10 minutes

---

## Step 1: Create Discord Application

### 1.1 Go to Discord Developer Portal

1. Open: https://discord.com/developers/applications
2. Click **"New Application"** (top right)
3. Name it: `Wellness Bot` (or whatever you prefer)
4. Click **"Create"**

### 1.2 Navigate to Bot Section

1. In the left sidebar, click **"Bot"**
2. Click **"Add Bot"** button
3. Confirm: **"Yes, do it!"**

---

## Step 2: Configure Bot Settings

### 2.1 Basic Bot Settings

In the **Bot** section:

1. **Username**: Change if you want (e.g., "WellnessCompanion")
2. **Icon**: Upload a profile picture (optional)
3. **Public Bot**:
   - ✅ **DISABLE** "Public Bot" (unless you want anyone to add it)
   - This makes it private - only you can invite it to servers
   - ⚠️ **EXPECTED WARNING**: "Private application cannot have a default authorization link"
     - **This is normal!** Safe to ignore.
     - It just means you'll use the custom OAuth2 URL from Step 3 instead of a default link

### 2.2 Privileged Gateway Intents

Scroll down to **"Privileged Gateway Intents"**:

1. ✅ **ENABLE** "Message Content Intent"

   - **WHY**: Bot needs this to read DM contents
   - **CRITICAL**: Without this, bot can't see message text!

2. ❌ **DISABLE** "Presence Intent" (not needed)
3. ❌ **DISABLE** "Server Members Intent" (not needed)

**Click "Save Changes"** at the bottom!

### 2.3 Copy Bot Token

1. Scroll back up to **"TOKEN"** section
2. Click **"Reset Token"** (or "Copy" if first time)
3. **IMPORTANT**: Copy the token - you'll need it for `.env`
   - Token looks like: `YOUR_DISCORD_BOT_TOKEN_HERE`
   - **NEVER share this token publicly!**

---

## Step 3: Configure OAuth2 Permissions

### 3.1 Go to OAuth2 Section

1. In left sidebar, click **"OAuth2"** → **"URL Generator"**

### 3.2 Select Scopes

In **"SCOPES"** section, check:

- ✅ **bot** (allow bot to join servers)
- ✅ **applications.commands** (optional, for future slash commands)

### 3.3 Select Bot Permissions

In **"BOT PERMISSIONS"** section, check:

- ✅ **Send Messages** (required - bot needs to reply)
- ✅ **Read Message History** (recommended - for context)
- ❌ **Manage Messages** (not needed)
- ❌ **Mention Everyone** (not needed)
- ❌ **Administrator** (NEVER enable this!)

**Minimal required**: Just "Send Messages"

### 3.4 Copy Invite URL

1. Scroll down to **"GENERATED URL"**
2. Copy the entire URL (looks like: `https://discord.com/oauth2/authorize?client_id=...`)
3. **Save this URL** - you'll use it to invite the bot

---

## Step 4: Invite Bot to Your Server (or Enable DMs)

### Option A: Add to Your Discord Server

1. Open the URL from Step 3.4 in your browser
2. Select your server from dropdown
3. Click **"Authorize"**
4. Complete captcha if prompted
5. Bot should now appear in your server's member list (offline until you start it)

### Option B: DMs Only (No Server)

- Skip this step entirely!
- Bot will respond to DMs from any user who can find it
- **NOTE**: With "Public Bot" disabled, only you can start DMs with it

---

## Step 5: Add Token to `.env` File

### 5.1 Open Your `.env` File

Location: `wellness_bot_V1.0/.env`

### 5.2 Add Discord Token

Add this line (or update if it exists):

```properties
DISCORD_BOT_TOKEN=YOUR_TOKEN_HERE
```

**Example**:

```properties
DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN_HERE
```

### 5.3 Enable Discord Feature Flag

Find the `APP_FEATURE_FLAGS` line and ensure `"discord_bot": true`:

**Before**:

```json
APP_FEATURE_FLAGS={"user_feedback": true, "token_budget_dynamic": true, "discord_bot": false}
```

**After**:

```json
APP_FEATURE_FLAGS={"user_feedback": true, "token_budget_dynamic": true, "discord_bot": true, "nsfw_preferences": true}
```

### 5.4 Save `.env` File

---

## Step 6: Install Dependencies

### 6.1 Install discord.py

In your terminal (in project root):

```powershell
pip install -r requirements.txt
```

This installs `discord.py` (already in requirements.txt).

### 6.2 Verify Installation

```powershell
python -c "import discord; print('Discord.py version:', discord.__version__)"
```

Should print: `Discord.py version: 2.x.x`

---

## Step 7: Start the Bot

### 7.1 Restart Your Wellness Bot

If bot is already running:

```powershell
# Stop it (Ctrl+C)
# Then restart:
python unified_bot.py
```

Or if using scripts:

```powershell
.\start_bot.ps1
```

### 7.2 Check Logs

You should see:

```
Discord bridge started in background thread.
Discord bridge logged in as WellnessBot#1234 (id=123456789012345678)
```

If you see this, **SUCCESS!** ✅

---

## Step 8: Test the Bot

### 8.1 Send a DM

1. Open Discord
2. Find your bot in:
   - Your server's member list (if you added it to a server)
   - Or search for it by exact username (if DMs only)
3. Click on bot → **"Message"**
4. Type: `Hey, how are you?`

### 8.2 Expected Response

Bot should reply with something like:

```
I'm here with you. How are you feeling today?
```

### 8.3 Test Help Command

Type: `!help`

Should show Discord-specific help text.

---

## Troubleshooting

### "Private application cannot have a default authorization link" Warning

**This is normal!** ✅ Safe to ignore.

**What it means**:

- Discord can't auto-generate a simple invite link for private bots
- You'll use the custom OAuth2 URL from Step 3 instead

**What to do**:

- Nothing! Just continue with the setup
- Use the URL Generator in OAuth2 section to create your invite link

### Bot Not Responding to DMs

**Check 1: Message Content Intent**

- Go to Discord Developer Portal → Bot → Privileged Gateway Intents
- Ensure **"Message Content Intent"** is ✅ **ENABLED**
- Click "Save Changes"
- **Restart your wellness bot**

**Check 2: Token Correct**

```powershell
# In project root
python -c "from app.config import settings; print('Token:', settings().discord_bot_token[:20] + '...')"
```

Should print first 20 chars of your token.

**Check 3: Feature Flag Enabled**

```powershell
python -c "from app.feature_flags import enabled; print('Discord enabled:', enabled('discord_bot'))"
```

Should print: `Discord enabled: True`

**Check 4: discord.py Installed**

```powershell
python -c "import discord; print('OK')"
```

Should print: `OK`

If errors, run: `pip install discord.py`

### Bot Offline in Server

**This is normal!** Discord bots don't show online status reliably. Try sending a DM anyway.

### "Missing Permissions" Error When Inviting

**Solution**: Go back to OAuth2 → URL Generator and ensure you selected **"Send Messages"** permission. Copy new URL.

### Bot Joins Server but Doesn't Respond

**Issue**: Bot only responds to **DMs** (direct messages), not server channels.

**Why**: Code has this check:

```python
if message.guild is not None:
    # Only respond to direct messages for now.
    return
```

**To enable server channels**: Ask me to modify `app/features/discord/bootstrap.py` to respond in channels.

---

## What the Bot Can Do

### ✅ Currently Supported (via DMs)

- Natural conversation (same AI as Telegram)
- Emotional support and wellness check-ins
- Remembers conversation context
- Shares same database as Telegram bot
- Bug reporting (`/reportbug`)
- Help command (`!help`)

### ❌ Not Yet Supported

- Reminders (Telegram-only for now)
- Onboarding flow (Telegram-only)
- Sleep/activity file uploads (Telegram-only)
- Server channel conversations (DMs only)

---

## Summary Checklist

- [ ] Created Discord application at developer portal
- [ ] Enabled "Message Content Intent" (CRITICAL!)
- [ ] Copied bot token
- [ ] Added `DISCORD_BOT_TOKEN=...` to `.env`
- [ ] Set `"discord_bot": true` in `APP_FEATURE_FLAGS`
- [ ] Ran `pip install -r requirements.txt`
- [ ] Restarted wellness bot
- [ ] Saw "Discord bridge started" in logs
- [ ] Sent test DM to bot
- [ ] Bot responded successfully

---

## Quick Reference

**Developer Portal**: https://discord.com/developers/applications

**Required Intent**: Message Content Intent ✅

**Required Permission**: Send Messages

**Test Command**: `!help`

**Bot Location in Code**: `app/features/discord/bootstrap.py`

**Feature Flag**: `"discord_bot": true` in `.env`

---

**Last Updated**: 2025-10-14
**Author**: GitHub Copilot
