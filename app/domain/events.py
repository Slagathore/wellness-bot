"""Canonical event names shared across services."""

# User-facing flows
EVENT_USER_MESSAGE = "conversation.user_message"
EVENT_CONVERSATION_MESSAGE = "conversation.message"
EVENT_SEND_REPLY = "conversation.send_reply"
EVENT_TURN_FOLLOWUP = "conversation.turn_followup"

# Reminders / scheduling
EVENT_REMINDER_DUE = "reminder.due"
EVENT_REMINDER_SENT = "reminder.sent"
EVENT_REMINDER_UPDATE_NEXT = "reminder.update_next"
EVENT_CHECKIN_DUE = "checkin.due"

# Safety / moderation
EVENT_CRISIS_DETECTED = "safety.crisis_detected"

# Admin / ops
EVENT_ADMIN_RESTART = "admin.restart_requested"
EVENT_SYSTEM_HEARTBEAT = "system.heartbeat"
EVENT_ADMIN_DISABLE = "admin.disable_requested"
EVENT_ADMIN_ENABLE = "admin.enable_requested"
