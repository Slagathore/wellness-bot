# Personality System - Integration Guide

## Overview

The personality system has been extracted into a separate module to improve maintainability and reduce the size of `unified_bot.py` (currently 7,481 lines).

## New Module Structure

```
app/personality/
├── __init__.py          # Public API exports
├── modes.py             # Personality mode definitions
└── manager.py           # PersonalityManager class
```

## Integration Steps

### 1. Import the Module (unified_bot.py)

```python
from app.personality import PersonalityManager, PERSONALITY_MODES

class UnifiedWellnessBot:
    def __init__(self):
        # ... existing code ...

        # Initialize personality manager
        config_path = Path('wellness_data') / 'config.json'
        self.personality_manager = PersonalityManager(config_path)
```

### 2. Update Settings Tab UI

Add personality dropdown after model selection:

```python
def create_settings_tab(self):
    # ... existing model selection ...

    # Personality selection
    tk.Label(settings_frame, text="Personality Mode:",
             font=('Arial', 10, 'bold')).grid(row=3, column=0, sticky='w', pady=10)

    personality_names = self.personality_manager.get_personality_names()
    personality_display = [f"{PERSONALITY_MODES[p]['emoji']} {PERSONALITY_MODES[p]['name']}"
                          for p in personality_names]

    self.personality_var = tk.StringVar(
        value=f"{PERSONALITY_MODES[self.personality_manager.selected_personality]['emoji']} "
              f"{PERSONALITY_MODES[self.personality_manager.selected_personality]['name']}"
    )

    self.personality_combo = ttk.Combobox(
        settings_frame,
        textvariable=self.personality_var,
        values=personality_display,
        width=47,
        font=('Arial', 10)
    )
    self.personality_combo.grid(row=3, column=1, sticky='w', padx=10)
    self.personality_combo.bind('<<ComboboxSelected>>', self.on_personality_selected)

    # Apply button
    tk.Button(settings_frame, text="✅ Apply Personality",
             command=self.apply_personality,
             bg='#27ae60', fg='white', font=('Arial', 9, 'bold')).grid(row=3, column=2, padx=5)
```

### 3. Wire Up Slider Auto-Save

Modify slider creation to auto-save on change:

```python
def create_settings_tab(self):
    # ... existing sliders ...

    # Temperature slider with auto-save
    temp_slider = tk.Scale(
        advanced_frame,
        from_=0.0, to=2.0, resolution=0.1,
        orient=tk.HORIZONTAL, length=300,
        variable=self.temp_var,
        command=lambda val: self.on_slider_change('temperature', float(val))
    )

    # Repeat penalty slider with auto-save
    repeat_slider = tk.Scale(
        advanced_frame,
        from_=1.0, to=2.0, resolution=0.05,
        orient=tk.HORIZONTAL, length=300,
        variable=self.repeat_penalty_var,
        command=lambda val: self.on_slider_change('repeat_penalty', float(val))
    )

    # Top-P slider with auto-save
    top_p_slider = tk.Scale(
        advanced_frame,
        from_=0.0, to=1.0, resolution=0.05,
        orient=tk.HORIZONTAL, length=300,
        variable=self.top_p_var,
        command=lambda val: self.on_slider_change('top_p', float(val))
    )
```

### 4. Add Callback Functions

```python
def on_personality_selected(self, event):
    """Load settings for selected personality (preview mode)."""
    # Parse personality name from display string
    selected_display = self.personality_var.get()
    for name, config in PERSONALITY_MODES.items():
        if f"{config['emoji']} {config['name']}" == selected_display:
            # Select for preview
            self.personality_manager.select_personality(name)

            # Load settings into UI
            config = self.personality_manager.get_selected_config()
            self.temp_var.set(config.get('temperature', 0.8))
            self.repeat_penalty_var.set(config.get('repeat_penalty', 1.1))
            self.top_p_var.set(config.get('top_p', 0.9))
            self.system_prompt_text.delete('1.0', tk.END)
            self.system_prompt_text.insert('1.0', config.get('system_prompt', ''))

            self.log(f"📋 Previewing personality: {name}")
            break

def on_slider_change(self, setting_name, value):
    """Auto-save slider changes to selected personality config."""
    self.personality_manager.update_selected_setting(setting_name, value)
    # No log spam - just save silently

def apply_personality(self):
    """Apply selected personality to bot (makes it active)."""
    self.personality_manager.apply_personality()
    active = self.personality_manager.active_personality
    config = PERSONALITY_MODES[active]

    self.log(f"✅ Personality applied: {config['emoji']} {config['name']}")
    messagebox.showinfo(
        "Personality Applied",
        f"Bot is now using: {config['emoji']} {config['name']}\n\n"
        f"Settings:\n"
        f"• Temperature: {self.temp_var.get()}\n"
        f"• Repeat Penalty: {self.repeat_penalty_var.get()}\n"
        f"• Reminders: {'Enabled' if config.get('enable_reminders', True) else 'Disabled'}"
    )
```

### 5. Update Message Handling

Use active personality config when generating responses:

```python
async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... existing code ...

    # Get active personality config
    personality_config = self.personality_manager.get_active_config()

    llm_options = {
        'temperature': personality_config.get('temperature', 0.8),
        'repeat_penalty': personality_config.get('repeat_penalty', 1.1),
        'top_p': personality_config.get('top_p', 0.9),
        'num_ctx': self._get_context_window_for_model(user_model)
    }

    # Use personality's system prompt
    system_prompt = personality_config.get('system_prompt', '')
    messages.insert(0, {"role": "system", "content": system_prompt})

    response_data = chat(messages, model=user_model, options=llm_options)
```

### 6. Block Reminders in Downbad Mode

In `_parse_reminder_intent()`:

```python
def _parse_reminder_intent(self, user_id, message_text, reference_time=None):
    """Parse message for reminder creation intent using LLM"""

    # Skip reminder parsing if current personality doesn't allow reminders
    if not self.personality_manager.should_enable_reminders():
        logger.info(f"[Reminder] Skipped - {self.personality_manager.active_personality} mode")
        return {
            'is_reminder': False,
            'is_checkin_config': False,
            'needs_clarification': False
        }

    # ... rest of existing code ...
```

### 7. Weight Downbad Messages in Psych Profile

In `_analyze_psychological_profile()`:

```python
def _analyze_psychological_profile(self, user_id, model=None):
    """Generate comprehensive psychological profile for a user"""

    # ... existing code to fetch messages ...

    # Weight messages based on personality mode they were sent in
    weighted_messages = []
    for msg in messages:
        # Get personality mode at time of message
        # For now, use current active personality weight
        # TODO: Store personality mode with each message for accurate weighting
        weight = self.personality_manager.get_psych_profile_weight()

        # Apply weight to message importance
        # Downbad messages (0.25 weight) will have less influence on profile
        if weight < 1.0:
            # Include message but mark it as less representative
            msg_text = f"[Low confidence sample, weight={weight}] {msg['content']}"
        else:
            msg_text = msg['content']

        weighted_messages.append(msg_text)

    # Use weighted messages for profile analysis
    # ... rest of existing code ...
```

### 8. Update /personality Command

Sync with the new system:

```python
async def handle_personality(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /personality command"""
    args = context.args
    if not args:
        # Show current
        active = self.personality_manager.active_personality
        config = PERSONALITY_MODES[active]
        await update.message.reply_text(
            f"Current personality: {config['emoji']} **{config['name']}**\n\n"
            f"Available personalities:\n" +
            '\n'.join([f"• {PERSONALITY_MODES[p]['emoji']} {p}"
                      for p in self.personality_manager.get_personality_names()])
        )
        return

    mode = args[0].lower()
    if mode in PERSONALITY_MODES:
        # Apply personality
        self.personality_manager.select_personality(mode)
        self.personality_manager.apply_personality()

        config = PERSONALITY_MODES[mode]
        await update.message.reply_text(
            f"Personality changed to: {config['emoji']} **{config['name']}**"
        )
    else:
        await update.message.reply_text(
            f"Unknown personality: {mode}\n\n"
            f"Available: {', '.join(self.personality_manager.get_personality_names())}"
        )
```

## Benefits of This Architecture

### ✅ Modularity

- Personality logic separate from UI code
- Easy to test independently
- Can reuse in other projects

### ✅ Maintainability

- 7,481 line file → broken into focused modules
- Changes to personality don't touch UI code
- Clear separation of concerns

### ✅ Extensibility

- Add new personalities by editing modes.py
- No changes to manager or UI needed
- Can easily add new features (e.g., custom user personalities)

### ✅ Safety

- Preview mode prevents accidental changes
- Settings auto-save per personality
- Active vs selected keeps bot stable

## Testing Checklist

- [ ] Import PersonalityManager in unified_bot.py
- [ ] Personality dropdown shows all modes
- [ ] Selecting personality loads its settings (preview)
- [ ] Changing sliders auto-saves to selected personality
- [ ] Apply button activates personality for bot
- [ ] Messages use active personality's temperature/prompt
- [ ] Downbad mode blocks reminder creation
- [ ] Downbad messages weighted at 25% in psych profile
- [ ] /personality command syncs with manager
- [ ] Config.json persists across restarts

## Migration Notes

### Backward Compatibility

The new system reads existing config.json files and migrates them automatically. If no `personalities` key exists, it creates default configurations.

### Config.json Format

```json
{
  "chat_model": "huihui_ai/gemma3n-abliterated:e2b-fp16",
  "vision_model": "llava-llama3",
  "admin_username": "cole",
  "active_personality": "friendly",
  "personalities": {
    "professional": {
      "temperature": 0.5,
      "repeat_penalty": 1.2,
      "top_p": 0.85,
      "system_prompt": "...",
      "enable_reminders": true,
      "psych_profile_weight": 1.0
    },
    "downbad": {
      "temperature": 1.5,
      "repeat_penalty": 1.0,
      "top_p": 0.95,
      "system_prompt": "...",
      "enable_reminders": false,
      "psych_profile_weight": 0.25
    }
  }
}
```

## Future Enhancements

### #todo User-Specific Personalities

Allow users to create custom personalities via Telegram commands

### #todo Personality Scheduling

Auto-switch personalities based on time of day or user state

### #todo Personality Analytics

Track which personalities users prefer and outcomes

### #todo Personality Inheritance

Allow personalities to inherit from base configs

### #todo Per-User Personality Override

Let individual users have different default personalities
