# Feature Flags - How to Enable

**Date**: October 12, 2025
**Status**: ✅ CONFIGURED in `.env` file

---

## ✅ What Was Fixed

### Problem

Setting environment variable in PowerShell only works for that session:

```powershell
$env:APP_FEATURE_FLAGS = '{"user_feedback": true, "token_budget_dynamic": true}'
# ❌ This only lasts until you close PowerShell
```

### Solution

Added to `.env` file (permanent configuration):

```properties
APP_FEATURE_FLAGS={"user_feedback": true, "token_budget_dynamic": true}
```

### Also Updated

Changed default token budget from 4000 to 8000:

```properties
CTX_TOKEN_BUDGET=8000  # Was: 4000
```

---

## 🚀 How to Activate

### Option 1: Restart Bot (Recommended)

If bot is running, stop it and restart:

```powershell
# Stop bot (if running)
# Press Ctrl+C if running in foreground
# Or if running as background jobs:
Get-Job | Stop-Job
Get-Job | Remove-Job

# Start bot fresh
.\start_bot.ps1
```

The bot will load `.env` file on startup and enable:

- ✅ `/reportbug` command
- ✅ `/suggestion` command
- ✅ `/myfeedback` command
- ✅ Dynamic token budgets (8000 default)

---

### Option 2: Test Without Restart (Quick Check)

If you want to test the feature flag parsing without restarting:

```powershell
# Test that feature flags parse correctly
python -c "from app.feature_flags import all_flags; import json; print(json.dumps(all_flags(), indent=2))"
```

Expected output:

```json
{
  "user_feedback": true,
  "token_budget_dynamic": true,
  "adaptive_psych_tests": false,
  "profile_personalization_agent": false,
  "enhanced_text_cleaning": false,
  "prompt_layering": false,
  "multi_document_import": false,
  "conversation_memory_v2": false,
  "web_search_v2": false
}
```

---

## 🧪 Testing the Features

Once bot is restarted, test in Telegram:

### Test Bug Reporting

```
/reportbug The bot didn't respond to my last message
```

Expected response:

```
✅ Bug report submitted! (ID: 1)
We'll look into this. Thanks for helping improve the bot!
```

### Test Suggestions

```
/suggestion Add voice message support
```

Expected response:

```
✅ Suggestion submitted! (ID: 2)
We appreciate your feedback!
```

### Test Viewing Feedback

```
/myfeedback
```

Expected response:

```
📋 Your Feedback History:

**Bug Report #1** - new
Submitted: 2025-10-12 14:32
> The bot didn't respond to my last message

**Suggestion #2** - new
Submitted: 2025-10-12 14:33
> Add voice message support
```

---

## 📊 Current Configuration

### Enabled Features

- ✅ `user_feedback` - Bug reporting & suggestions
- ✅ `token_budget_dynamic` - Personality-aware token budgets

### Token Budgets (Dynamic)

- **Default**: 8000 tokens (~40-50 messages)
- **Downbad**: 12000 tokens (~60-70 messages)
- **Professional**: 6000 tokens (~30-35 messages)
- **Other modes**: 8000 tokens

### Disabled Features (Future)

- ⏸️ `adaptive_psych_tests` - Not implemented yet
- ⏸️ `profile_personalization_agent` - Not implemented yet
- ⏸️ `enhanced_text_cleaning` - Not implemented yet
- ⏸️ `prompt_layering` - Not implemented yet
- ⏸️ `multi_document_import` - Not implemented yet
- ⏸️ `conversation_memory_v2` - Not implemented yet
- ⏸️ `web_search_v2` - Not implemented yet

---

## 🔧 Changing Feature Flags Later

### Enable/Disable in .env

Edit `.env` file:

```properties
# Enable a feature
APP_FEATURE_FLAGS={"user_feedback": true, "token_budget_dynamic": true, "web_search_v2": true}

# Disable a feature
APP_FEATURE_FLAGS={"user_feedback": false, "token_budget_dynamic": true}
```

Then restart bot:

```powershell
# Stop
Get-Job | Stop-Job; Get-Job | Remove-Job

# Start
.\start_bot.ps1
```

### Runtime Override (Testing Only)

For temporary testing without editing .env:

```powershell
# Set for current PowerShell session only
$env:APP_FEATURE_FLAGS = '{"user_feedback": true, "some_test_feature": true}'

# Start bot in same session
python -m app.main_modular
```

This only lasts until PowerShell closes.

---

## 📋 Quick Reference

| Action                       | Command                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| **View current .env**        | `Get-Content .env`                                                                   |
| **Edit .env**                | `code .env` (VS Code) or `notepad .env`                                              |
| **Test flag parsing**        | `python -c "from app.feature_flags import all_flags; print(all_flags())"`            |
| **Restart bot**              | Stop (Ctrl+C or `Get-Job \| Stop-Job`) → `.\start_bot.ps1`                           |
| **Check if feature enabled** | `python -c "from app.feature_flags import enabled; print(enabled('user_feedback'))"` |

---

## 🎯 Status

✅ **Feature flags configured in .env**
✅ **Token budget updated to 8000**
✅ **Database migrated (user_feedback table exists)**
✅ **7 sessions backfilled with new budget**

**Next**: Restart bot to activate features! 🚀

---

## 🐛 Troubleshooting

### Features still not working after restart?

**Check 1**: Verify .env loaded correctly

```powershell
python -c "from app.config import settings; print(settings().feature_flags)"
```

**Check 2**: Verify feature flags parsed

```powershell
python -c "from app.feature_flags import all_flags; print(all_flags())"
```

**Check 3**: Check bot logs for errors

```powershell
Get-Content wellness_data/bot.log -Tail 50
```

### Commands not registered?

The feature flag system uses **lazy loading**. Commands are only registered if:

1. Feature flag is `true` in .env
2. Bot restarted after changing .env
3. No errors during bootstrap

Check logs:

```powershell
# Should see during startup:
# [FEATURES] user_feedback: enabled
# [FEATURES] Registered command: /reportbug
# [FEATURES] Registered command: /suggestion
# [FEATURES] Registered command: /myfeedback
```

---

## 📞 Questions?

**Q: Do I need to restart every time I change .env?**
A: Yes, .env is loaded at startup only

**Q: Can I enable features without .env?**
A: Yes, set `$env:APP_FEATURE_FLAGS` before starting bot, but it's temporary

**Q: How do I disable all features?**
A: Set `APP_FEATURE_FLAGS={}` in .env (empty JSON object)

**Q: Will this affect existing users?**
A: No, features are additive - existing functionality unchanged

**Q: Is it safe to enable in production?**
A: Yes, all features tested and migrations applied ✅
