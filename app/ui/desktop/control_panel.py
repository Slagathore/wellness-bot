"""Desktop UI bootstrap for the Tk control panel."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from app.db import db_ro, db_rw
from app.feature_flags import enabled
from app.personality.modes import PERSONALITY_MODES
from app.utils.ollama import chat
from app.utils.time_utils import get_current_time, operator_now
from app.vector_backends import get_backend

logger = logging.getLogger(__name__)

_FNAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9._-]")


class ControlPanelUI:
    """Owns the Tk root lifecycle for the desktop control panel."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def initialize_root(self) -> None:
        """Create the Tk root window and install signal handlers."""

        bot = self.bot
        bot.root = tk.Tk()
        bot.root.title("🤖 Wellness Bot Control Panel")
        bot.root.geometry("1000x700")
        bot.root.protocol("WM_DELETE_WINDOW", bot.minimize_to_tray)
        bot._install_signal_handlers()

    def build(self) -> None:
        """Build the remainder of the UI once authentication completes."""

        bot = self.bot
        bot.create_system_tray()
        self.create_gui()

    def create_gui(self) -> None:
        """Build the admin interface."""

        bot = self.bot
        root = bot.root
        control_frame = tk.Frame(root, bg="#2c3e50", height=60)
        control_frame.pack(fill=tk.X, side=tk.TOP)
        control_frame.pack_propagate(False)
        tk.Label(
            control_frame,
            text="🤖 Wellness Bot Control Panel",
            font=("Arial", 14, "bold"),
            bg="#2c3e50",
            fg="white",
        ).pack(side=tk.LEFT, padx=20, pady=15)
        bot.start_btn = tk.Button(
            control_frame,
            text="▶ Start Bot",
            command=bot.start_bot,
            bg="#27ae60",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=15,
            pady=5,
        )
        bot.start_btn.pack(side=tk.RIGHT, padx=5, pady=10)
        bot.stop_btn = tk.Button(
            control_frame,
            text="⏹ Stop Bot",
            command=bot.stop_bot,
            bg="#e74c3c",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=15,
            pady=5,
            state=tk.DISABLED,
        )
        bot.stop_btn.pack(side=tk.RIGHT, padx=5, pady=10)

        bot.notebook = self._create_notebook(root)
        bot.status_bar = tk.Label(
            root,
            text="Bot stopped",
            bd=1,
            relief=tk.SUNKEN,
            anchor=tk.W,
            bg="#e74c3c",
            fg="white",
            font=("Arial", 9),
        )
        bot.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._create_status_tab()
        self._create_messages_tab()
        self._create_users_tab()
        self._create_emotional_tab()
        self._create_crisis_tab()
        self._create_analytics_tab()
        self._create_feedback_tab()
        self._create_psych_tab()
        self._create_admin_console_tab()
        self._create_settings_tab()
        self._create_gpu_tab()

    def _create_notebook(self, root: tk.Misc):
        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        return notebook

    def _create_status_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="📊 Status")

        status_frame = ttk.LabelFrame(tab, text="System Status", padding=10)
        status_frame.pack(fill=tk.X, padx=10, pady=5)
        bot.bot_status_label = tk.Label(
            status_frame, text="Bot: Stopped", font=("Arial", 10, "bold"), fg="red"
        )
        bot.bot_status_label.grid(row=0, column=0, sticky="w", padx=10, pady=5)
        bot.ollama_status_label = tk.Label(
            status_frame, text="Ollama: Checking...", font=("Arial", 10)
        )
        bot.ollama_status_label.grid(row=0, column=1, sticky="w", padx=10, pady=5)
        bot.model_label = tk.Label(
            status_frame, text=f"Model: {bot.cfg.chat_model}", font=("Arial", 10)
        )
        bot.model_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=5)
        status_frame.grid_columnconfigure(2, weight=1)
        control_frame = tk.Frame(status_frame)
        control_frame.grid(row=0, column=2, rowspan=2, sticky="e", padx=10, pady=5)
        tk.Button(
            control_frame,
            text="🔄 Restart App",
            command=bot.restart_app,
            bg="#34495e",
            fg="white",
            relief=tk.RAISED,
        ).pack(padx=5)

        log_frame = ttk.LabelFrame(tab, text="Activity Log", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        bot.log_text = scrolledtext.ScrolledText(
            log_frame, height=25, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.log_text.pack(fill=tk.BOTH, expand=True)
        bot.check_ollama()

    def _create_messages_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="Messages")
        msg_frame = ttk.LabelFrame(tab, text="Recent Messages", padding=10)
        msg_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        bot.messages_text = scrolledtext.ScrolledText(
            msg_frame, height=30, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.messages_text.pack(fill=tk.BOTH, expand=True)
        btn_frame = tk.Frame(tab)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(btn_frame, text="Refresh", command=self.refresh_messages).pack(
            side=tk.LEFT, padx=5
        )
        self.refresh_messages()

    def _create_users_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="Users")
        users_frame = ttk.LabelFrame(tab, text="All Users", padding=10)
        users_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = (
            "ID",
            "Username",
            "Name",
            "Messages",
            "Images",
            "Timezone",
            "Last Active",
        )
        bot.user_tree = ttk.Treeview(
            users_frame,
            columns=columns,
            show="headings",
            height=20,
            selectmode="extended",
        )
        column_widths = {
            "ID": 100,
            "Username": 120,
            "Name": 120,
            "Messages": 80,
            "Images": 60,
            "Timezone": 100,
            "Last Active": 150,
        }
        for col in columns:
            bot.user_tree.heading(col, text=col)
            bot.user_tree.column(
                col, width=column_widths.get(col, 100), anchor=tk.CENTER
            )
        bot.user_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        btn_frame = tk.Frame(tab)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(btn_frame, text="Refresh", command=self.refresh_users).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(btn_frame, text="View History", command=self.view_user_history).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(btn_frame, text="View Images", command=self.view_user_images).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(btn_frame, text="View Profile", command=self.view_user_profile).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(
            btn_frame, text="Edit Profile", command=self.view_user_profile_edit
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Reminders", command=self.view_user_reminders).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(
            btn_frame,
            text="Clear History",
            command=self.clear_user_history,
            bg="#f39c12",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="Ban User", command=self.ban_user, bg="#e74c3c", fg="white"
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Unban User",
            command=self.unban_user,
            bg="#27ae60",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Delete User",
            command=self.delete_user,
            bg="#c0392b",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Bulk Delete Users",
            command=self.bulk_delete_users,
            bg="#8b0000",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Export Data", command=self.export_user_data).pack(
            side=tk.LEFT, padx=5
        )
        self.refresh_users()

    def refresh_messages(self) -> None:
        bot = self.bot
        if not hasattr(bot, "messages_text"):
            return

        bot.messages_text.delete(1.0, tk.END)
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT m.timestamp, u.telegram_username, m.role, m.content, m.user_id
                FROM messages m
                JOIN users u ON u.id = m.user_id
                ORDER BY m.timestamp DESC
                LIMIT 50
                """
            ).fetchall()

        for timestamp_raw, username, role, content, user_id in rows:
            timestamp = (
                bot.format_operator_timestamp(timestamp_raw, assume="operator")
                if hasattr(bot, "format_operator_timestamp")
                else (timestamp_raw or "Unknown")
            )
            safe_username = username or "Unknown"
            safe_role = (role or "user").upper()
            safe_content = (content or "")[:200]
            if content and len(content) > 200:
                safe_content += "..."
            bot.messages_text.insert(
                tk.END, f"[{timestamp}] @{safe_username} ({safe_role}):\n"
            )
            bot.messages_text.insert(tk.END, f"  {safe_content}\n\n")

    def refresh_users(self) -> None:
        bot = self.bot
        if not hasattr(bot, "user_tree"):
            return

        for item in bot.user_tree.get_children():
            bot.user_tree.delete(item)

        with db_ro() as conn:
            users = conn.execute(
                """
                SELECT
                    u.id,
                    u.telegram_user_id,
                    u.telegram_username,
                    u.display_name,
                    COUNT(DISTINCT m.id) as msg_count,
                    COUNT(DISTINCT img.id) as img_count,
                    tz_minutes.value as tz_offset_minutes,
                    tz_label.value as tz_label,
                    u.last_active_at
                FROM users u
                LEFT JOIN messages m ON m.user_id = u.id
                LEFT JOIN image_uploads img ON img.user_id = u.id
                LEFT JOIN profile_context tz_minutes ON tz_minutes.user_id = u.id AND tz_minutes.key = 'timezone_offset_minutes'
                LEFT JOIN profile_context tz_label ON tz_label.user_id = u.id AND tz_label.key = 'timezone'
                GROUP BY u.id
                ORDER BY u.last_active_at DESC
                """
            ).fetchall()

        for row in users:
            if isinstance(row, sqlite3.Row):
                telegram_id = row["telegram_user_id"]
                username = row["telegram_username"] or "N/A"
                display_name = row["display_name"] or "N/A"
                msg_count = row["msg_count"]
                img_count = row["img_count"]
                tz_offset = row["tz_offset_minutes"]
                tz_label = row["tz_label"]
                last_active = row["last_active_at"]
            else:
                (
                    _user_id,
                    telegram_id,
                    username,
                    display_name,
                    msg_count,
                    img_count,
                    tz_offset,
                    tz_label,
                    last_active,
                ) = (
                    row[0],
                    row[1],
                    row[2] or "N/A",
                    row[3] or "N/A",
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                )

            tz_display = tz_label or "CST"
            if not tz_label and tz_offset is not None:
                try:
                    minutes = int(float(tz_offset))
                    sign = "+" if minutes >= 0 else "-"
                    abs_minutes = abs(minutes)
                    hours, mins = divmod(abs_minutes, 60)
                    tz_display = f"CST{sign}{hours:02d}:{mins:02d}"
                except (TypeError, ValueError):
                    tz_display = "CST"

            last_display = (
                bot.format_operator_timestamp(last_active, assume="operator")
                if last_active and hasattr(bot, "format_operator_timestamp")
                else ("Never" if not last_active else last_active)
            )

            bot.user_tree.insert(
                "",
                tk.END,
                values=(
                    telegram_id,
                    username,
                    display_name,
                    msg_count,
                    img_count,
                    tz_display,
                    last_display,
                ),
            )

        self._sync_user_dropdowns()

    def _sync_user_dropdowns(self) -> None:
        bot = self.bot
        try:
            if hasattr(bot, "emotion_user_combo"):
                self.refresh_emotional_users()
            if hasattr(bot, "analytics_user_combo"):
                bot.refresh_analytics_users()
            bot.log("?? User dropdowns synced across tabs")
        except Exception as exc:
            logger.error("Error syncing user dropdowns: %s", exc)

    def _create_emotional_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="Emotional Analytics")
        selector_frame = ttk.LabelFrame(tab, text="Select User", padding=10)
        selector_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(selector_frame, text="User:").pack(side=tk.LEFT, padx=5)
        bot.emotion_user_var = tk.StringVar()
        bot.emotion_user_combo = ttk.Combobox(
            selector_frame, textvariable=bot.emotion_user_var, width=30
        )
        bot.emotion_user_combo.pack(side=tk.LEFT, padx=5)
        tk.Button(
            selector_frame, text="Load Analytics", command=self.load_emotional_analytics
        ).pack(side=tk.LEFT, padx=5)
        current_frame = ttk.LabelFrame(tab, text="Current Emotional State", padding=10)
        current_frame.pack(fill=tk.X, padx=10, pady=5)
        bot.current_emotion_label = tk.Label(
            current_frame, text="No data", font=("Arial", 12, "bold")
        )
        bot.current_emotion_label.pack(pady=5)
        metrics_frame = tk.Frame(current_frame)
        metrics_frame.pack(fill=tk.X, pady=5)
        bot.valence_label = tk.Label(metrics_frame, text="Valence: --")
        bot.valence_label.grid(row=0, column=0, padx=20, pady=5)
        bot.stress_label = tk.Label(metrics_frame, text="Stress: --")
        bot.stress_label.grid(row=0, column=1, padx=20, pady=5)
        bot.energy_label = tk.Label(metrics_frame, text="Energy: --")
        bot.energy_label.grid(row=0, column=2, padx=20, pady=5)
        bot.arousal_label = tk.Label(metrics_frame, text="Arousal: --")
        bot.arousal_label.grid(row=1, column=0, padx=20, pady=5)
        bot.dominance_label = tk.Label(metrics_frame, text="Dominance: --")
        bot.dominance_label.grid(row=1, column=1, padx=20, pady=5)
        paned = tk.PanedWindow(
            tab, orient=tk.VERTICAL, sashrelief=tk.RAISED, sashwidth=5
        )
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        trends_frame = ttk.LabelFrame(
            tab, text="Emotional Trends (Last 30 Days)", padding=10
        )
        paned.add(trends_frame, minsize=100)
        bot.trends_text = scrolledtext.ScrolledText(
            trends_frame, height=15, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.trends_text.pack(fill=tk.BOTH, expand=True)
        history_frame = ttk.LabelFrame(
            tab,
            text="Recent Emotions (V=Valence, S=Stress, E=Energy, A=Arousal, D=Dominance)",
            padding=10,
        )
        paned.add(history_frame, minsize=100)
        bot.emotion_history_text = scrolledtext.ScrolledText(
            history_frame, height=8, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.emotion_history_text.pack(fill=tk.BOTH, expand=True)
        self.refresh_emotion_users()

    def refresh_emotional_users(self) -> None:
        """Load users into the emotional analytics dropdown."""

        bot = self.bot
        if not hasattr(bot, "emotion_user_combo"):
            return
        with db_ro() as conn:
            users = conn.execute(
                """
                SELECT telegram_user_id, telegram_username FROM users
                ORDER BY last_active_at DESC
                """
            ).fetchall()
        user_list = [f"{row[1] or 'Unknown'} ({row[0]})" for row in users]
        bot.emotion_user_combo["values"] = user_list
        if user_list:
            bot.emotion_user_combo.current(0)

    def refresh_emotion_users(self) -> None:
        """Backward compatible alias."""

        self.refresh_emotional_users()

    def load_emotional_analytics(self) -> None:
        """Load emotional analytics for the selected user."""

        bot = self.bot
        if not hasattr(bot, "emotion_user_var") or not bot.emotion_user_var.get():
            messagebox.showwarning("No Selection", "Please select a user")
            return

        selection = bot.emotion_user_var.get()
        try:
            tg_id = int(selection.split("(")[1].split(")")[0])
        except Exception:  # noqa: BLE001
            messagebox.showerror("Error", "Invalid user selection")
            return

        with db_ro() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]
            latest = conn.execute(
                """
                SELECT s.valence, s.arousal, s.dominance, s.stress_level, s.energy_level,
                       s.emotion_label, s.secondary_emotions, m.timestamp
                FROM sentiments s
                JOIN messages m ON m.id = s.message_id
                WHERE m.user_id = ?
                ORDER BY s.processed_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if latest:
                emotion = latest["emotion_label"] or "Unknown"
                bot.current_emotion_label.config(text=f"Current Emotion: {emotion}")
                bot.valence_label.config(
                    text=(
                        f"Valence: {latest['valence']:+.2f}"
                        if latest["valence"]
                        else "Valence: --"
                    )
                )
                bot.stress_label.config(
                    text=(
                        f"Stress: {latest['stress_level']:.2f}"
                        if latest["stress_level"]
                        else "Stress: --"
                    )
                )
                bot.energy_label.config(
                    text=(
                        f"Energy: {latest['energy_level']:.2f}"
                        if latest["energy_level"]
                        else "Energy: --"
                    )
                )
                bot.arousal_label.config(
                    text=(
                        f"Arousal: {latest['arousal']:.2f}"
                        if latest["arousal"]
                        else "Arousal: --"
                    )
                )
                bot.dominance_label.config(
                    text=(
                        f"Dominance: {latest['dominance']:.2f}"
                        if latest["dominance"]
                        else "Dominance: --"
                    )
                )
            else:
                bot.current_emotion_label.config(text="No sentiment data yet")
                bot.valence_label.config(text="Valence: --")
                bot.stress_label.config(text="Stress: --")
                bot.energy_label.config(text="Energy: --")
                bot.arousal_label.config(text="Arousal: --")
                bot.dominance_label.config(text="Dominance: --")

            bot.trends_text.delete(1.0, tk.END)
            sentiments = conn.execute(
                """
                SELECT s.valence, s.stress_level, s.energy_level, s.emotion_label, s.processed_at
                FROM sentiments s
                JOIN messages m ON m.id = s.message_id
                WHERE m.user_id = ?
                AND s.processed_at >= datetime('now', '-30 days')
                ORDER BY s.processed_at DESC
                """,
                (user_id,),
            ).fetchall()

            if sentiments:
                valences = [
                    s["valence"] for s in sentiments if s["valence"] is not None
                ]
                stresses = [
                    s["stress_level"]
                    for s in sentiments
                    if s["stress_level"] is not None
                ]
                energies = [
                    s["energy_level"]
                    for s in sentiments
                    if s["energy_level"] is not None
                ]
                avg_valence = sum(valences) / len(valences) if valences else 0
                avg_stress = sum(stresses) / len(stresses) if stresses else 0
                avg_energy = sum(energies) / len(energies) if energies else 0

                from collections import Counter

                emotions = [
                    s["emotion_label"] for s in sentiments if s["emotion_label"]
                ]
                emotion_counts = Counter(emotions)

                bot.trends_text.insert(tk.END, f"{'=' * 60}\n")
                bot.trends_text.insert(tk.END, "EMOTIONAL TRENDS - LAST 30 DAYS\n")
                bot.trends_text.insert(tk.END, f"{'=' * 60}\n\n")
                bot.trends_text.insert(
                    tk.END, f"Total Analyzed Messages: {len(sentiments)}\n\n"
                )
                bot.trends_text.insert(tk.END, "AVERAGES:\n")
                bot.trends_text.insert(
                    tk.END,
                    f"  Valence:      {avg_valence:+.3f}  "
                    f"{'(Positive)' if avg_valence > 0 else '(Negative)'}\n",
                )
                bot.trends_text.insert(
                    tk.END,
                    "  Stress Level: "
                    f"{avg_stress:.3f}   "
                    f"{'(High)' if avg_stress > 0.6 else '(Moderate)' if avg_stress > 0.3 else '(Low)'}\n",
                )
                bot.trends_text.insert(
                    tk.END,
                    "  Energy Level: "
                    f"{avg_energy:.3f}   "
                    f"{'(High)' if avg_energy > 0.6 else '(Moderate)' if avg_energy > 0.3 else '(Low)'}\n\n",
                )
                bot.trends_text.insert(tk.END, "TOP EMOTIONS:\n")
                for emotion, count in emotion_counts.most_common(10):
                    pct = (count / len(sentiments)) * 100
                    bot.trends_text.insert(
                        tk.END, f"  {emotion:15s}: {count:3d} ({pct:5.1f}%)\n"
                    )

                bot.trends_text.insert(tk.END, "\nWEEKLY BREAKDOWN:\n")
                now_reference = get_current_time()
                for week in range(4):
                    week_start = now_reference - timedelta(days=(week + 1) * 7)
                    week_end = now_reference - timedelta(days=week * 7)
                    week_sentiments = [
                        s
                        for s in sentiments
                        if week_start.isoformat()
                        <= s["processed_at"]
                        <= week_end.isoformat()
                    ]
                    if week_sentiments:
                        week_valences = [
                            s["valence"]
                            for s in week_sentiments
                            if s["valence"] is not None
                        ]
                        week_stress = [
                            s["stress_level"]
                            for s in week_sentiments
                            if s["stress_level"] is not None
                        ]
                        avg_v = (
                            sum(week_valences) / len(week_valences)
                            if week_valences
                            else 0
                        )
                        avg_s = (
                            sum(week_stress) / len(week_stress) if week_stress else 0
                        )
                        bot.trends_text.insert(
                            tk.END,
                            f"  Week {4 - week}: {len(week_sentiments)} msgs | "
                            f"Valence: {avg_v:+.2f} | Stress: {avg_s:.2f}\n",
                        )
            else:
                bot.trends_text.insert(tk.END, "No sentiment data for last 30 days\n")

            bot.emotion_history_text.delete(1.0, tk.END)
            recent = conn.execute(
                """
                SELECT s.emotion_label, s.secondary_emotions, s.valence, s.stress_level,
                       m.content, m.timestamp
                FROM sentiments s
                JOIN messages m ON m.id = s.message_id
                WHERE m.user_id = ?
                ORDER BY s.processed_at DESC
                LIMIT 20
                """,
                (user_id,),
            ).fetchall()
            for row in recent:
                ts = row["timestamp"][:16] if row["timestamp"] else "Unknown"
                emotion = row["emotion_label"] or "unknown"
                valence = row["valence"] or 0
                stress = row["stress_level"] or 0
                content = row["content"] or ""
                msg = content[:50] + "..." if len(content) > 50 else content
                secondary = (
                    json.loads(row["secondary_emotions"])
                    if row["secondary_emotions"]
                    else []
                )
                sec_str = f" +{','.join(secondary[:2])}" if secondary else ""
                bot.emotion_history_text.insert(
                    tk.END,
                    f'[{ts}] {emotion}{sec_str} | V:{valence:+.2f} S:{stress:.2f}\n  "{msg}"\n\n',
                )

    def _create_crisis_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="Crisis Alerts")
        alerts_frame = ttk.LabelFrame(tab, text="Active Alerts", padding=10)
        alerts_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("ID", "User", "Timestamp", "Type", "Severity", "Emotion", "Details")
        bot.crisis_tree = ttk.Treeview(
            alerts_frame,
            columns=columns,
            show="headings",
            height=15,
            selectmode="extended",
        )
        column_widths = {
            "ID": 60,
            "User": 140,
            "Timestamp": 160,
            "Type": 110,
            "Severity": 80,
            "Emotion": 110,
            "Details": 220,
        }
        for col in columns:
            bot.crisis_tree.heading(col, text=col)
            bot.crisis_tree.column(col, width=column_widths.get(col, 100))
        scrollbar = ttk.Scrollbar(
            alerts_frame, orient=tk.VERTICAL, command=bot.crisis_tree.yview
        )
        bot.crisis_tree.configure(yscrollcommand=scrollbar.set)
        bot.crisis_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bot.crisis_tree.bind(
            "<<TreeviewSelect>>", lambda _evt: self._show_crisis_details()
        )
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        details_frame = ttk.LabelFrame(tab, text="Event Details", padding=10)
        details_frame.pack(fill=tk.X, padx=10, pady=5)
        bot.crisis_details_text = scrolledtext.ScrolledText(
            details_frame, height=6, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.crisis_details_text.pack(fill=tk.BOTH, expand=True)

        btn_frame = tk.Frame(tab)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(btn_frame, text="Refresh", command=self.refresh_crisis).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(
            btn_frame,
            text="Resolve",
            command=self.resolve_crisis,
            bg="#27ae60",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Bulk Resolve",
            command=self.bulk_resolve_crisis,
            bg="#229954",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Resolve All Open",
            command=self.resolve_all_emergencies,
            bg="#1a7a3a",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Delete Event",
            command=self.delete_crisis,
            bg="#c0392b",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="View Message", command=self.view_crisis_message
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="View User", command=self.view_crisis_user).pack(
            side=tk.LEFT, padx=5
        )
        self.refresh_crisis()

    def refresh_crisis(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return

        bot.crisis_tree.delete(*bot.crisis_tree.get_children())
        try:
            with db_ro() as conn:
                alerts = conn.execute(
                    """
                    SELECT m.id, u.telegram_username, m.timestamp, m.event_type, m.severity, m.details, m.resolved
                    FROM moderation_events m
                    JOIN users u ON u.id = m.user_id
                    ORDER BY m.resolved ASC, m.severity DESC, m.timestamp DESC
                    """
                ).fetchall()
        except sqlite3.OperationalError as exc:
            messagebox.showerror(
                "Database Error", f"Failed to load crisis alerts: {exc}"
            )
            return

        severity_labels = {
            1: "1 - Low", 2: "2 - Moderate", 3: "3 - High",
            4: "4 - Critical", 5: "5 - Emergency",
        }
        type_labels = {
            "crisis_detected": "Mental Health Crisis",
            "disruption_attempt": "System Disruption",
            "declining_mood_trend": "Mood Decline",
            "rate_limit_warning": "Rate Limit Warning",
            "rate_limit_ban": "Rate Limit Ban",
        }

        crisis_count = 0
        disruption_count = 0
        unresolved_count = 0

        for alert in alerts:
            details = json.loads(alert["details"]) if alert["details"] else {}
            event_type = alert["event_type"]
            resolved = bool(alert["resolved"])

            if event_type == "disruption_attempt":
                disruption_count += 1
                categories = details.get("categories", [])
                detail_str = ", ".join(categories) if categories else "Unknown"
                emotion_str = details.get("detection", "N/A")
            else:
                crisis_count += 1
                detail_str = details.get("emotion", "N/A")
                emotion_str = details.get("emotion", "N/A")

            message_preview = (details.get("message") or "No message")[:50]
            timestamp_raw = alert["timestamp"]
            timestamp_display = (
                bot.format_operator_timestamp(timestamp_raw, assume="operator")
                if timestamp_raw and hasattr(bot, "format_operator_timestamp")
                else (timestamp_raw or "Unknown")
            )

            sev = alert["severity"] or 0
            sev_display = severity_labels.get(sev, f"{sev}")
            type_display = type_labels.get(event_type, event_type)

            tag = (
                "resolved"
                if resolved
                else ("disruption" if event_type == "disruption_attempt" else "crisis")
            )
            bot.crisis_tree.insert(
                "",
                tk.END,
                values=(
                    alert["id"],
                    alert["telegram_username"] or "Unknown",
                    timestamp_display,
                    type_display,
                    sev_display,
                    emotion_str,
                    detail_str or message_preview,
                ),
                tags=(tag,),
            )

            if not resolved:
                unresolved_count += 1

        bot.crisis_tree.tag_configure(
            "crisis", background="#f8d7da", foreground="#721c24"
        )
        bot.crisis_tree.tag_configure(
            "disruption", background="#fff3cd", foreground="#856404"
        )
        bot.crisis_tree.tag_configure(
            "resolved", background="#d4edda", foreground="#155724"
        )

        total_count = len(alerts)
        resolved_count = total_count - unresolved_count
        bot.log(
            f"?? Alerts: {crisis_count} crisis, {disruption_count} disruption | "
            f"{unresolved_count} active, {resolved_count} resolved ({total_count} total)"
        )

    def _show_crisis_details(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return

        selection = bot.crisis_tree.selection()
        if not selection:
            return

        item = bot.crisis_tree.item(selection[0])
        values = item.get("values")
        if not values:
            return
        event_id = values[0]

        try:
            with db_ro() as conn:
                event = conn.execute(
                    """
                    SELECT m.*, u.telegram_username, u.telegram_user_id
                    FROM moderation_events m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.id = ?
                    """,
                    (event_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            messagebox.showerror(
                "Database Error", f"Failed to load event details: {exc}"
            )
            return

        if not event or not hasattr(bot, "crisis_details_text"):
            return

        bot.crisis_details_text.delete(1.0, tk.END)
        details = json.loads(event["details"]) if event["details"] else {}
        type_labels = {
            "crisis_detected": "Mental Health Crisis",
            "disruption_attempt": "System Disruption",
            "declining_mood_trend": "Mood Decline",
            "rate_limit_warning": "Rate Limit Warning",
            "rate_limit_ban": "Rate Limit Ban",
        }
        severity_labels = {
            1: "Low", 2: "Moderate", 3: "High", 4: "Critical", 5: "Emergency",
        }
        event_type = event["event_type"] or "unknown"
        sev = event["severity"] or 0
        type_display = type_labels.get(event_type, event_type)
        sev_display = severity_labels.get(sev, str(sev))
        header = type_display.upper()  # type: ignore[union-attr]

        bot.crisis_details_text.insert(tk.END, f"{'=' * 60}\n")
        bot.crisis_details_text.insert(tk.END, f"{header} #{event_id}\n")
        bot.crisis_details_text.insert(tk.END, f"{'=' * 60}\n\n")
        bot.crisis_details_text.insert(
            tk.END,
            f"User: @{event['telegram_username']} (ID: {event['telegram_user_id']})\n",
        )
        bot.crisis_details_text.insert(tk.END, f"Time: {event['timestamp']}\n")
        bot.crisis_details_text.insert(tk.END, f"Type: {type_display}\n")
        bot.crisis_details_text.insert(tk.END, f"Severity: {sev}/5 - {sev_display}\n\n")

        if event["event_type"] == "disruption_attempt":
            categories = details.get("categories", [])
            detection = details.get("detection_time", "N/A")
            if categories:
                bot.crisis_details_text.insert(
                    tk.END, f"Categories: {', '.join(categories)}\n"
                )
            bot.crisis_details_text.insert(tk.END, f"Detection: {detection}\n\n")
        else:
            bot.crisis_details_text.insert(
                tk.END, f"Emotion: {details.get('emotion', 'N/A')}\n"
            )
            bot.crisis_details_text.insert(
                tk.END, f"Valence: {details.get('valence', 'N/A')}\n"
            )
            bot.crisis_details_text.insert(
                tk.END, f"Stress: {details.get('stress_level', 'N/A')}\n"
            )
            bot.crisis_details_text.insert(
                tk.END, f"Energy: {details.get('energy_level', 'N/A')}\n"
            )
            bot.crisis_details_text.insert(
                tk.END, f"Level: {details.get('level', 'N/A')}\n\n"
            )

        bot.crisis_details_text.insert(
            tk.END, f"MESSAGE:\n{details.get('message', 'No message')}\n"
        )

    def resolve_crisis(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return
        selection = bot.crisis_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a crisis event")
            return
        values = bot.crisis_tree.item(selection[0])["values"]
        event_id = values[0]
        if not messagebox.askyesno("Confirm", "Mark this crisis event as resolved?"):
            return
        with db_rw() as conn:
            conn.execute(
                "UPDATE moderation_events SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
                (event_id,),
            )
        bot.crisis_tree.item(selection[0], tags=("resolved",))
        bot.crisis_tree.tag_configure(
            "resolved", background="#d4edda", foreground="#155724"
        )
        bot.log(f"? Crisis event #{event_id} marked as resolved")

    def delete_crisis(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return
        selection = list(bot.crisis_tree.selection())
        if not selection:
            messagebox.showwarning("No Selection", "Please select a crisis event")
            return
        if len(selection) > 1:
            self.bulk_delete_crisis()
            return
        values = bot.crisis_tree.item(selection[0])["values"]
        event_id = values[0]
        username = values[1]
        if not messagebox.askyesno(
            "Delete Event",
            f"Delete crisis event #{event_id} for @{username or 'unknown'}?",
            icon="warning",
        ):
            return
        try:
            event_id_int = int(event_id)
        except (TypeError, ValueError):
            event_id_int = event_id
        with db_rw() as conn:
            conn.execute("DELETE FROM moderation_events WHERE id = ?", (event_id_int,))
        self.refresh_crisis()
        if hasattr(bot, "crisis_details_text"):
            bot.crisis_details_text.delete(1.0, tk.END)
        bot.log(f"Deleted crisis event #{event_id_int}")
        messagebox.showinfo(
            "Crisis Event Deleted", f"Crisis event #{event_id_int} has been deleted."
        )

    def bulk_delete_crisis(self) -> None:
        bot = self.bot
        selection = bot.crisis_tree.selection()
        if not selection:
            messagebox.showwarning(
                "No Selection", "Please select at least one crisis event"
            )
            return
        if len(selection) == 1:
            self.delete_crisis()
            return

        events = []
        for item_id in selection:
            values = bot.crisis_tree.item(item_id)["values"]
            event_id = values[0]
            username = values[1]
            timestamp = values[2]
            try:
                event_id_int = int(event_id)
            except (TypeError, ValueError):
                event_id_int = event_id
            events.append((event_id_int, username or "", timestamp))

        preview = "\n".join(
            f"#{eid} @{user or 'unknown'} ({ts})" for eid, user, ts in events[:10]
        )
        if len(events) > 10:
            preview += f"\n...and {len(events) - 10} more"

        if not messagebox.askyesno(
            "Confirm Bulk Delete",
            f"You are about to delete {len(events)} crisis events.\n\n{preview}\n\nThis cannot be undone.",
            icon="warning",
        ):
            return

        event_ids = [event_id for event_id, _user, _ts in events]
        placeholders = ",".join("?" for _ in event_ids)
        with db_rw() as conn:
            conn.execute(
                f"DELETE FROM moderation_events WHERE id IN ({placeholders})",
                event_ids,
            )

        self.refresh_crisis()
        if hasattr(bot, "crisis_details_text"):
            bot.crisis_details_text.delete(1.0, tk.END)
        bot.log(f"Deleted {len(events)} crisis events")
        messagebox.showinfo(
            "Crisis Events Deleted", f"Deleted {len(events)} crisis events."
        )

    def bulk_resolve_crisis(self) -> None:
        bot = self.bot
        selection = bot.crisis_tree.selection()
        if not selection:
            messagebox.showwarning(
                "No Selection", "Please select at least one crisis event"
            )
            return
        if len(selection) == 1:
            self.resolve_crisis()
            return

        events = []
        for item_id in selection:
            values = bot.crisis_tree.item(item_id)["values"]
            event_id = values[0]
            username = values[1]
            try:
                event_id_int = int(event_id)
            except (TypeError, ValueError):
                event_id_int = event_id
            events.append((event_id_int, username or ""))

        preview = "\n".join(
            f"#{event_id} @{username or 'unknown'}"
            for event_id, username in events[:10]
        )
        if len(events) > 10:
            preview += f"\n...and {len(events) - 10} more"

        if not messagebox.askyesno(
            "Confirm Bulk Resolve",
            f"Mark {len(events)} crisis events as resolved?\n\n{preview}",
            icon="question",
        ):
            return

        event_ids = [event_id for event_id, _username in events]
        placeholders = ",".join("?" for _ in event_ids)
        with db_rw() as conn:
            conn.execute(
                f"UPDATE moderation_events SET resolved = 1, resolved_at = datetime('now') WHERE id IN ({placeholders})",
                event_ids,
            )

        self.refresh_crisis()
        if hasattr(bot, "crisis_details_text"):
            bot.crisis_details_text.delete(1.0, tk.END)
        bot.log(f"Bulk resolved {len(events)} crisis events")
        messagebox.showinfo(
            "Crisis Events Resolved", f"Marked {len(events)} crisis events as resolved."
        )

    def resolve_all_emergencies(self) -> None:
        """Resolve ALL open (unresolved) crisis events at once."""
        bot = self.bot
        with db_ro() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM moderation_events WHERE resolved = 0"
            ).fetchone()
        open_count = row["cnt"] if row else 0
        if open_count == 0:
            messagebox.showinfo("No Open Events", "There are no unresolved crisis events.")
            return

        if not messagebox.askyesno(
            "Resolve All Open Emergencies",
            f"Mark ALL {open_count} open crisis events as resolved?",
            icon="warning",
        ):
            return

        with db_rw() as conn:
            conn.execute(
                "UPDATE moderation_events SET resolved = 1, resolved_at = datetime('now') "
                "WHERE resolved = 0"
            )

        self.refresh_crisis()
        if hasattr(bot, "crisis_details_text"):
            bot.crisis_details_text.delete(1.0, tk.END)
        bot.log(f"Resolved all {open_count} open crisis events")
        messagebox.showinfo(
            "All Resolved", f"Marked {open_count} crisis events as resolved."
        )

    def view_crisis_user(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return
        selection = bot.crisis_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a crisis event")
            return
        values = bot.crisis_tree.item(selection[0])["values"]
        username = values[1]
        try:
            tab_index = bot.notebook.tabs().index(bot.notebook.select())
        except Exception:
            tab_index = None
        bot.notebook.select(2)
        for item in bot.user_tree.get_children():
            if bot.user_tree.item(item)["values"][1] == username:
                bot.user_tree.selection_set(item)
                bot.user_tree.see(item)
                self.view_user_details()
                break
        if tab_index is not None:
            bot.notebook.select(tab_index)

    def view_user_details(self) -> None:
        bot = self.bot
        if not hasattr(bot, "user_tree"):
            bot.log("? User tree widget not initialized; cannot show user details.")
            return
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        try:
            values = bot.user_tree.item(selection[0])["values"]
        except Exception as exc:
            logger.error("Failed to access user selection: %s", exc, exc_info=True)
            messagebox.showerror("Error", "Unable to read the selected user entry.")
            return
        if not values:
            messagebox.showerror("Error", "Unable to determine the selected user.")
            return
        try:
            tg_id = int(values[0])
        except (TypeError, ValueError):
            messagebox.showerror("Error", "Selected user has an invalid Telegram ID.")
            return
        try:
            with db_ro() as conn:
                user = conn.execute(
                    """
                    SELECT id, telegram_username, display_name, onboarding_completed,
                           last_active_at, created_at, is_banned
                    FROM users
                    WHERE telegram_user_id = ?
                    """,
                    (tg_id,),
                ).fetchone()
                if not user:
                    messagebox.showerror("Error", "User not found in database.")
                    return
                stats = conn.execute(
                    """
                    SELECT COUNT(*) AS total_messages,
                           SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_messages,
                           SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS bot_messages,
                           MAX(timestamp) AS last_message_at
                    FROM messages
                    WHERE user_id = ?
                    """,
                    (user["id"],),
                ).fetchone()
                reminders = conn.execute(
                    """
                    SELECT COUNT(*) AS reminder_count
                    FROM reminders
                    WHERE user_id = ? AND enabled = 1
                    """,
                    (user["id"],),
                ).fetchone()
        except Exception as exc:
            logger.error("Error loading user details: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to load user details: {exc}")
            return

        user_dict = dict(user) if isinstance(user, sqlite3.Row) else dict(user)
        banned_flag = user_dict.get("is_banned") or 0
        stats_total = (
            stats["total_messages"]
            if stats and stats["total_messages"] is not None
            else 0
        )
        stats_user = (
            stats["user_messages"]
            if stats and stats["user_messages"] is not None
            else 0
        )
        stats_bot = (
            stats["bot_messages"] if stats and stats["bot_messages"] is not None else 0
        )
        last_message_at = (
            stats["last_message_at"] if stats and stats["last_message_at"] else "N/A"
        )
        reminder_count = (
            reminders["reminder_count"]
            if reminders and reminders["reminder_count"] is not None
            else 0
        )
        username_display = user_dict.get("telegram_username") or "Unknown"

        summary_lines = [
            f"Username: @{username_display}",
            f"Display Name: {user_dict.get('display_name') or 'N/A'}",
            f"Telegram ID: {tg_id}",
            f"Created: {user_dict.get('created_at') or 'Unknown'}",
            f"Last Active: {user_dict.get('last_active_at') or 'Unknown'}",
            f"Onboarding Complete: {'Yes' if user_dict.get('onboarding_completed') else 'No'}",
            f"Banned: {'Yes' if banned_flag else 'No'}",
            f"Total Messages: {stats_total}",
            f"User Messages: {stats_user}",
            f"Bot Messages: {stats_bot}",
            f"Last Message: {last_message_at}",
            f"Active Reminders: {reminder_count}",
        ]

        summary_text = "\n".join(summary_lines)
        text_widget = getattr(bot, "user_details_text", None)
        if isinstance(text_widget, tk.Text):
            text_widget.configure(state=tk.NORMAL)
            text_widget.delete("1.0", tk.END)
            text_widget.insert(tk.END, summary_text)
            text_widget.configure(state=tk.DISABLED)
        else:
            messagebox.showinfo("User Details", summary_text)
        bot.log(f"?? Viewing details for @{username_display}")

    def view_crisis_message(self) -> None:
        bot = self.bot
        if not hasattr(bot, "crisis_tree"):
            return
        selection = bot.crisis_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a crisis event")
            return
        values = bot.crisis_tree.item(selection[0])["values"]
        event_id = values[0]
        with db_ro() as conn:
            event = conn.execute(
                "SELECT details FROM moderation_events WHERE id = ?", (event_id,)
            ).fetchone()
        if not event:
            messagebox.showerror("Error", "Could not load crisis message details.")
            return
        details = json.loads(event["details"]) if event["details"] else {}
        message_id = details.get("message_id")
        message_text = details.get("message", "No message")

        popup = tk.Toplevel(bot.root)
        popup.title(f"Crisis Message - Event #{event_id}")
        popup.geometry("600x400")
        text = scrolledtext.ScrolledText(popup, wrap=tk.WORD, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, f"Crisis Event #{event_id}\n")
        text.insert(tk.END, f"{'=' * 60}\n\n")
        text.insert(tk.END, f"Message ID: {message_id}\n\n")
        text.insert(tk.END, f"Content:\n{message_text}\n")

    def view_user_history(self) -> None:
        bot = self.bot
        if not hasattr(bot, "user_tree"):
            return
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]

        popup = tk.Toplevel(bot.root)
        popup.title(f"Conversation History - @{username}")
        popup.geometry("900x700")

        search_frame = ttk.Frame(popup)
        search_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT, padx=(0, 5))
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var, width=40)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))

        role_var = tk.StringVar(value="all")
        ttk.Radiobutton(search_frame, text="All", variable=role_var, value="all").pack(
            side=tk.LEFT, padx=5
        )
        ttk.Radiobutton(
            search_frame, text="User", variable=role_var, value="user"
        ).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(
            search_frame, text="Bot", variable=role_var, value="assistant"
        ).pack(side=tk.LEFT, padx=5)

        date_frame = ttk.Frame(popup)
        date_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(date_frame, text="From:").pack(side=tk.LEFT, padx=(0, 5))
        from_date_var = tk.StringVar()
        ttk.Entry(date_frame, textvariable=from_date_var, width=15).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        ttk.Label(date_frame, text="(YYYY-MM-DD)").pack(side=tk.LEFT, padx=(0, 20))
        ttk.Label(date_frame, text="To:").pack(side=tk.LEFT, padx=(0, 5))
        to_date_var = tk.StringVar()
        ttk.Entry(date_frame, textvariable=to_date_var, width=15).pack(
            side=tk.LEFT, padx=(0, 10)
        )

        text = scrolledtext.ScrolledText(popup, wrap=tk.WORD, font=("Consolas", 9))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        status_label = ttk.Label(popup, text="")
        status_label.pack(fill=tk.X, padx=10, pady=(0, 10))

        def do_search():
            text.delete("1.0", tk.END)
            keyword = search_var.get().strip()
            role_filter = role_var.get()
            from_date = from_date_var.get().strip()
            to_date = to_date_var.get().strip()

            query = "SELECT id, session_id, timestamp, role, content FROM messages WHERE user_id = ?"
            params = [user_id]
            if keyword:
                query += " AND content LIKE ?"
                params.append(f"%{keyword}%")
            if role_filter != "all":
                query += " AND role = ?"
                params.append(role_filter)
            if from_date:
                query += " AND timestamp >= ?"
                params.append(from_date)
            if to_date:
                query += " AND timestamp <= ?"
                params.append(to_date + " 23:59:59")
            query += " ORDER BY timestamp ASC, id ASC"

            try:
                with db_ro() as conn:
                    messages = conn.execute(query, params).fetchall()
                status_label.config(text=f"Found {len(messages)} messages")
                text.tag_config("highlight", background="yellow", foreground="black")
                text.tag_config("header", font=("Consolas", 9, "bold"))
                text.tag_config(
                    "session_header", font=("Consolas", 9, "bold"), foreground="#3498db"
                )

                current_session = None
                for msg_id, session_id, timestamp, role, content in messages:
                    if session_id != current_session:
                        current_session = session_id
                        text.insert(
                            tk.END, f"--- Session #{session_id} ---\n", "session_header"
                        )
                    ts_display = bot.format_operator_timestamp(
                        timestamp, assume="operator"
                    )
                    role_display = (role or "user").upper()
                    content = content or ""

                    if keyword and keyword.lower() in content.lower():
                        text.insert(
                            tk.END, f"[{ts_display}] {role_display}:\n", "header"
                        )
                        lower_content = content.lower()
                        start_idx = 0
                        while True:
                            idx = lower_content.find(keyword.lower(), start_idx)
                            if idx == -1:
                                text.insert(tk.END, content[start_idx:])
                                break
                            text.insert(tk.END, content[start_idx:idx])
                            text.insert(
                                tk.END, content[idx : idx + len(keyword)], "highlight"
                            )
                            start_idx = idx + len(keyword)
                        text.insert(tk.END, "\n\n")
                    else:
                        text.insert(
                            tk.END, f"[{ts_display}] {role_display}:\n{content}\n\n"
                        )
            except Exception as exc:
                status_label.config(text=f"Error: {exc}")
                messagebox.showerror("Search Error", str(exc))

        ttk.Button(search_frame, text="Search", command=do_search).pack(
            side=tk.LEFT, padx=10
        )

        def clear_search():
            search_var.set("")
            from_date_var.set("")
            to_date_var.set("")
            role_var.set("all")
            do_search()

        ttk.Button(search_frame, text="Clear", command=clear_search).pack(side=tk.LEFT)
        do_search()
        search_entry.bind("<Return>", lambda _evt: do_search())

    def view_user_images(self) -> None:
        bot = self.bot
        if not hasattr(bot, "user_tree"):
            return
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]
            try:
                images = conn.execute(
                    """
                    SELECT file_path, caption, vision_analysis, uploaded_at
                    FROM image_uploads
                    WHERE user_id = ?
                    ORDER BY uploaded_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            except Exception as exc:
                logger.error("Error fetching images: %s", exc)
                images_dir = Path(bot.cfg.data_root) / "users" / str(tg_id) / "images"
                if not images_dir.exists() or not list(images_dir.glob("*.jpg")):
                    messagebox.showinfo(
                        "No Images",
                        f"User {tg_id} (@{username}) hasn't uploaded any images yet",
                    )
                    return
                images = []

        if not images:
            images_dir = Path(bot.cfg.data_root) / "users" / str(tg_id) / "images"
            if not images_dir.exists() or not list(images_dir.glob("*.jpg")):
                messagebox.showinfo(
                    "No Images",
                    f"User {tg_id} (@{username}) hasn't uploaded any images yet",
                )
            else:
                messagebox.showinfo(
                    "Images on Disk",
                    f"Found images in filesystem but not in database for user {tg_id}. Try re-uploading.",
                )
            return

        gallery = tk.Toplevel(bot.root)
        gallery.title(f"Image Gallery - @{username} ({len(images)} images)")
        gallery.geometry("1000x700")
        canvas = tk.Canvas(gallery)
        scrollbar = ttk.Scrollbar(gallery, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        pil_image_mod = pil_image_tk_mod = None
        try:
            from PIL import Image as _PilImage, ImageTk as _PilImageTk  # type: ignore[import]
        except ImportError:
            has_pil = False
            messagebox.showwarning(
                "PIL Not Installed",
                "Install Pillow for image thumbnails: pip install Pillow\nShowing file paths only.",
            )
        else:
            has_pil = True
            pil_image_mod = _PilImage
            pil_image_tk_mod = _PilImageTk

        row = col = 0
        for idx, img_data in enumerate(images):
            file_path = img_data["file_path"]
            caption = img_data["caption"] or "No caption"
            vision = img_data["vision_analysis"] or "No analysis"
            timestamp = (
                img_data["uploaded_at"][:19] if img_data["uploaded_at"] else "Unknown"
            )

            img_frame = ttk.LabelFrame(
                scrollable_frame, text=f"Image {idx + 1} - {timestamp}", padding=10
            )
            img_frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")

            if (
                has_pil
                and pil_image_mod
                and pil_image_tk_mod
                and Path(file_path).exists()
            ):
                try:
                    img = pil_image_mod.open(file_path)
                    img.thumbnail((280, 280))
                    photo = pil_image_tk_mod.PhotoImage(img)
                    label = ttk.Label(img_frame, image=photo)
                    label.image = photo  # type: ignore[attr-defined]
                    label.pack()
                except Exception as exc:
                    logger.error("Failed to load image %s: %s", file_path, exc)
            else:
                ttk.Label(img_frame, text=file_path).pack()

            ttk.Label(
                img_frame, text=f"Caption: {caption}", wraplength=260, justify=tk.LEFT
            ).pack(anchor="w", pady=5)
            ttk.Label(
                img_frame,
                text=f"Vision Analysis: {vision}",
                wraplength=260,
                justify=tk.LEFT,
            ).pack(
                anchor="w",
                pady=5,
            )

            col += 1
            if col >= 3:
                col = 0
                row += 1

    def view_user_profile(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]
            ban_status = conn.execute(
                """
                SELECT status, blocked_until FROM rate_limits
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            profile_data = conn.execute(
                "SELECT key, value FROM profile_context WHERE user_id = ? ORDER BY key",
                (user_id,),
            ).fetchall()
            sentiments = conn.execute(
                """
                SELECT s.emotion_label, s.valence, s.arousal, m.timestamp
                FROM sentiments s
                JOIN messages m ON s.message_id = m.id
                WHERE m.user_id = ?
                ORDER BY m.timestamp DESC LIMIT 10
                """,
                (user_id,),
            ).fetchall()
            stats = conn.execute(
                """
                SELECT COUNT(*) as total_messages,
                       COUNT(DISTINCT session_id) as total_sessions,
                       MIN(timestamp) as first_message,
                       MAX(timestamp) as last_message
                FROM messages WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            reminders_count = conn.execute(
                "SELECT COUNT(*) as count FROM reminders WHERE user_id = ? AND enabled = 1",
                (user_id,),
            ).fetchone()["count"]

        window = tk.Toplevel(bot.root)
        window.title(f"User Profile - @{username}")
        window.geometry("800x600")
        text = scrolledtext.ScrolledText(window, wrap=tk.WORD, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        text.insert(tk.END, f"{'=' * 70}\nUSER PROFILE: @{username}\n{'=' * 70}\n\n")
        text.insert(tk.END, f"Telegram ID: {tg_id}\n")
        text.insert(tk.END, f"Display Name: {user['display_name']}\n")
        onboarding = (
            "✅ Completed" if user["onboarding_completed"] else "⚠️ Not completed"
        )
        text.insert(tk.END, f"Onboarding: {onboarding}\n")
        text.insert(tk.END, f"Created: {user['created_at']}\n")
        text.insert(tk.END, f"Last Active: {user['last_active_at']}\n")
        if ban_status and ban_status["status"] in {"blocked", "banned"}:
            status_line = f"Status: 🚫 {ban_status['status'].upper()}"
            if ban_status["blocked_until"]:
                status_line += f" (until {ban_status['blocked_until']})"
            text.insert(tk.END, status_line + "\n")
        else:
            text.insert(tk.END, "Status: ✅ Active\n")

        text.insert(tk.END, f"\n{'-' * 70}\nACTIVITY STATS\n{'-' * 70}\n\n")
        text.insert(tk.END, f"Total Messages: {stats['total_messages']}\n")
        text.insert(tk.END, f"Total Sessions: {stats['total_sessions']}\n")
        text.insert(tk.END, f"First Message: {stats['first_message']}\n")
        text.insert(tk.END, f"Last Message: {stats['last_message']}\n")
        text.insert(tk.END, f"Active Reminders: {reminders_count}\n\n")

        text.insert(tk.END, f"{'-' * 70}\nPROFILE CONTEXT\n{'-' * 70}\n\n")
        excluded_keys = {
            "psychological_profile",
            "estimated_iq",
            "cognitive_metrics",
            "mental_health_indicators",
            "dark_triad",
            "big_five",
        }
        filtered = [item for item in profile_data if item["key"] not in excluded_keys]
        if filtered:
            for item in filtered:
                text.insert(tk.END, f"• {item['key']}: {item['value']}\n")
        else:
            text.insert(tk.END, "No profile data collected yet\n")

        text.insert(tk.END, f"\n{'-' * 70}\nRECENT EMOTIONAL STATE\n{'-' * 70}\n\n")
        if sentiments:
            for record in sentiments:
                indicator = (
                    "🙂"
                    if record["valence"] > 0.3
                    else "😐" if record["valence"] > -0.3 else "🙁"
                )
                timestamp = (
                    bot.format_operator_timestamp(
                        record["timestamp"], assume="operator"
                    )
                    if hasattr(bot, "format_operator_timestamp")
                    else record["timestamp"]
                )
                text.insert(
                    tk.END,
                    f"{indicator} {record['emotion_label']} (valence: {record['valence']:.2f}, "
                    f"arousal: {record['arousal']:.2f}) - {timestamp}\n",
                )
        else:
            text.insert(tk.END, "No sentiment data yet\n")

        text.insert(tk.END, f"\n{'=' * 70}\n")
        text.configure(state=tk.DISABLED)

    def view_user_profile_edit(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]
            profile_data = conn.execute(
                "SELECT key, value FROM profile_context WHERE user_id = ? ORDER BY key",
                (user_id,),
            ).fetchall()

        edit_win = tk.Toplevel(bot.root)
        edit_win.title(f"Edit Profile - @{username}")
        edit_win.geometry("700x600")
        tree_frame = ttk.Frame(edit_win)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        columns = ("Key", "Value")
        profile_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", height=20
        )
        for col, width in zip(columns, (200, 450)):
            profile_tree.heading(col, text=col)
            profile_tree.column(col, width=width)
        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=profile_tree.yview
        )
        profile_tree.configure(yscrollcommand=scrollbar.set)
        profile_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for item in profile_data:
            profile_tree.insert("", "end", values=(item["key"], item["value"]))

        btn_frame = ttk.Frame(edit_win)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        def add_entry():
            add_win = tk.Toplevel(edit_win)
            add_win.title("Add Profile Entry")
            add_win.geometry("400x250")
            tk.Label(add_win, text="Key:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            key_entry = ttk.Entry(add_win, width=50)
            key_entry.pack(padx=10, pady=5)
            tk.Label(add_win, text="Value:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            value_entry = scrolledtext.ScrolledText(add_win, height=4, wrap=tk.WORD)
            value_entry.pack(fill=tk.X, padx=10, pady=5)

            def save_entry():
                key = key_entry.get().strip()
                value = value_entry.get("1.0", "end-1c").strip()
                if not key or not value:
                    messagebox.showerror("Error", "Both key and value are required")
                    return
                with db_rw() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO profile_context (user_id, key, value) VALUES (?, ?, ?)",
                        (user_id, key, value),
                    )
                messagebox.showinfo("Success", f"Added: {key}")
                add_win.destroy()
                edit_win.destroy()
                self.view_user_profile_edit()

            ttk.Button(add_win, text="Save Entry", command=save_entry).pack(pady=10)

        def edit_entry():
            selection = profile_tree.selection()
            if not selection:
                messagebox.showwarning("No Selection", "Please select an entry to edit")
                return
            current_key, current_value = profile_tree.item(selection[0])["values"]
            entry_win = tk.Toplevel(edit_win)
            entry_win.title(f"Edit: {current_key}")
            entry_win.geometry("400x250")
            tk.Label(entry_win, text="Key:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            key_entry = ttk.Entry(entry_win, width=50)
            key_entry.insert(0, current_key)
            key_entry.pack(padx=10, pady=5)
            tk.Label(entry_win, text="Value:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            value_entry = scrolledtext.ScrolledText(entry_win, height=4, wrap=tk.WORD)
            value_entry.insert("1.0", current_value)
            value_entry.pack(fill=tk.X, padx=10, pady=5)

            def save_changes():
                new_key = key_entry.get().strip()
                new_value = value_entry.get("1.0", "end-1c").strip()
                if not new_key or not new_value:
                    messagebox.showerror("Error", "Both key and value are required")
                    return
                with db_rw() as conn:
                    if new_key != current_key:
                        conn.execute(
                            "DELETE FROM profile_context WHERE user_id = ? AND key = ?",
                            (user_id, current_key),
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO profile_context (user_id, key, value) VALUES (?, ?, ?)",
                        (user_id, new_key, new_value),
                    )
                messagebox.showinfo("Success", "Entry updated!")
                entry_win.destroy()
                edit_win.destroy()
                self.view_user_profile_edit()

            ttk.Button(entry_win, text="Save Changes", command=save_changes).pack(
                pady=10
            )

        def delete_entry():
            selection = profile_tree.selection()
            if not selection:
                messagebox.showwarning(
                    "No Selection", "Please select an entry to delete"
                )
                return
            key = profile_tree.item(selection[0])["values"][0]
            if messagebox.askyesno("Confirm Delete", f"Delete profile entry '{key}'?"):
                with db_rw() as conn:
                    conn.execute(
                        "DELETE FROM profile_context WHERE user_id = ? AND key = ?",
                        (user_id, key),
                    )
                bot.log(f"🧾 Deleted profile entry '{key}' for user {tg_id}")
                edit_win.destroy()
                self.view_user_profile_edit()

        ttk.Button(btn_frame, text="Add Entry", command=add_entry).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Edit Entry", command=edit_entry).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Delete Entry", command=delete_entry).pack(
            side=tk.LEFT, padx=5
        )

    def view_user_reminders(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            user_id = user["id"]
            reminders = conn.execute(
                """
                SELECT id, kind, payload, next_run_at, last_delivered_at, enabled, cadence_cron, created_at
                FROM reminders
                WHERE user_id = ?
                ORDER BY enabled DESC, next_run_at ASC
                """,
                (user_id,),
            ).fetchall()

        if not reminders:
            messagebox.showinfo(
                "No Reminders", f"User @{username} has no reminders set"
            )
            return

        rem_window = tk.Toplevel(bot.root)
        rem_window.title(f"Reminders - @{username} ({len(reminders)} total)")
        rem_window.geometry("900x600")
        tree_frame = ttk.Frame(rem_window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        columns = (
            "ID",
            "Type",
            "Text",
            "Frequency",
            "Status",
            "Next Run",
            "Last Sent",
            "Created",
        )
        rem_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=20)
        for col, width in zip(columns, (50, 100, 250, 100, 80, 150, 150, 150)):
            rem_tree.heading(col, text=col)
            rem_tree.column(col, width=width)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=rem_tree.yview)
        rem_tree.configure(yscrollcommand=scrollbar.set)
        rem_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for reminder in reminders:
            payload = json.loads(reminder["payload"]) if reminder["payload"] else {}
            text = payload.get("text") or payload.get("reminder_text") or "(no text)"
            frequency = (
                payload.get("frequency") or reminder.get("cadence_cron") or "custom"
            )
            status = "Enabled" if reminder["enabled"] else "Disabled"
            rem_tree.insert(
                "",
                tk.END,
                values=(
                    reminder["id"],
                    reminder["kind"],
                    text,
                    frequency,
                    status,
                    reminder["next_run_at"],
                    reminder["last_delivered_at"],
                    reminder["created_at"],
                ),
            )

        btn_frame = ttk.Frame(rem_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        def edit_reminder():
            selection = rem_tree.selection()
            if not selection:
                messagebox.showwarning("No Selection", "Select a reminder first")
                return
            values = rem_tree.item(selection[0])["values"]
            reminder_id = values[0]
            with db_ro() as conn:
                reminder = conn.execute(
                    "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
                ).fetchone()
            if not reminder:
                messagebox.showerror("Error", "Reminder not found")
                return
            payload = json.loads(reminder["payload"]) if reminder["payload"] else {}

            edit_win = tk.Toplevel(rem_window)
            edit_win.title(f"Edit Reminder #{reminder_id}")
            edit_win.geometry("500x400")
            tk.Label(edit_win, text="Reminder Text:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            text_entry = scrolledtext.ScrolledText(edit_win, height=4, wrap=tk.WORD)
            text_entry.insert("1.0", payload.get("text", ""))
            text_entry.pack(fill=tk.X, padx=10, pady=5)
            tk.Label(edit_win, text="Frequency:", font=("Arial", 10, "bold")).pack(
                anchor="w", padx=10, pady=(10, 0)
            )
            freq_entry = ttk.Entry(edit_win, width=30)
            freq_entry.insert(0, payload.get("frequency", "daily"))
            freq_entry.pack(padx=10, pady=5)

            def save_changes():
                new_text = text_entry.get("1.0", "end-1c").strip()
                new_freq = freq_entry.get().strip()
                if not new_text or not new_freq:
                    messagebox.showerror("Error", "Text and frequency required")
                    return
                payload.update({"text": new_text, "frequency": new_freq})
                with db_rw() as conn:
                    conn.execute(
                        "UPDATE reminders SET payload = ?, updated_at = datetime('now') WHERE id = ?",
                        (json.dumps(payload), reminder_id),
                    )
                messagebox.showinfo("Saved", "Reminder updated")
                edit_win.destroy()
                rem_window.destroy()
                self.view_user_reminders()

            ttk.Button(edit_win, text="Save", command=save_changes).pack(pady=10)

        def toggle_reminder():
            selection = rem_tree.selection()
            if not selection:
                messagebox.showwarning("No Selection", "Select a reminder first")
                return
            values = rem_tree.item(selection[0])["values"]
            reminder_id = values[0]
            new_status = values[4] != "Enabled"
            with db_rw() as conn:
                conn.execute(
                    "UPDATE reminders SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
                    (1 if new_status else 0, reminder_id),
                )
            bot.log(
                f"Reminder #{reminder_id} toggled to {'enabled' if new_status else 'disabled'}"
            )
            rem_window.destroy()
            self.view_user_reminders()

        def delete_reminder():
            selection = rem_tree.selection()
            if not selection:
                messagebox.showwarning("No Selection", "Select a reminder first")
                return
            values = rem_tree.item(selection[0])["values"]
            reminder_id = values[0]
            if not messagebox.askyesno(
                "Confirm Delete", f"Delete reminder #{reminder_id}?"
            ):
                return
            with db_rw() as conn:
                conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            messagebox.showinfo("Deleted", "Reminder deleted successfully")
            rem_window.destroy()
            self.view_user_reminders()

        ttk.Button(btn_frame, text="Edit", command=edit_reminder).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Enable/Disable", command=toggle_reminder).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Delete", command=delete_reminder).pack(
            side=tk.LEFT, padx=5
        )

    def ban_user(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]
        if not messagebox.askyesno(
            "Confirm Ban", f"Ban user @{username}? This blocks access to the bot."
        ):
            return
        with db_rw() as conn:
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "UPDATE users SET is_banned = 1 WHERE telegram_user_id = ?", (tg_id,)
            )
        bot.log(f"🚫 Banned user @{username}")
        messagebox.showinfo("Success", f"User @{username} has been banned")
        self.refresh_users()

    def unban_user(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]
        with db_rw() as conn:
            conn.execute(
                "UPDATE users SET is_banned = 0 WHERE telegram_user_id = ?", (tg_id,)
            )
        bot.log(f"✅ Unbanned user @{username}")
        messagebox.showinfo("Success", f"User @{username} has been unbanned")
        self.refresh_users()

    def _delete_user_artifacts(self, tg_id, username, name, msg_count):
        bot = self.bot
        deleted_files = 0
        try:
            with db_rw() as conn:
                user = conn.execute(
                    "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
                ).fetchone()
                if not user:
                    return False, "User not found in database", deleted_files
                user_id = user["id"]
                media_files = conn.execute(
                    "SELECT DISTINCT media_path FROM messages WHERE user_id = ? AND media_path IS NOT NULL",
                    (user_id,),
                ).fetchall()
                shard_files = conn.execute(
                    "SELECT DISTINCT path FROM transcript_shards WHERE user_id = ?",
                    (user_id,),
                ).fetchall()

                data_root = Path(getattr(bot.cfg, "data_root", "wellness_data"))
                user_dir = data_root / "users" / str(tg_id)

                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.execute(
                    """
                    INSERT INTO audit_log (actor, action, target_user_id, details)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "admin_gui",
                        "delete_user",
                        user_id,
                        json.dumps(
                            {
                                "telegram_id": tg_id,
                                "username": username,
                                "name": name,
                                "message_count": msg_count,
                            }
                        ),
                    ),
                )

            for row in media_files:
                media_path = row["media_path"]
                if not media_path:
                    continue
                try:
                    file_path = Path(media_path)
                    if file_path.exists():
                        file_path.unlink()
                        deleted_files += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not delete media file %s: %s", media_path, exc
                    )

            for row in shard_files:
                shard_path_value = row["path"]
                if not shard_path_value:
                    continue
                try:
                    shard_path = Path(shard_path_value)
                    if shard_path.exists():
                        shard_path.unlink()
                        deleted_files += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not delete shard file %s: %s", shard_path_value, exc
                    )

            user_dir = (
                Path(getattr(bot.cfg, "data_root", "wellness_data"))
                / "users"
                / str(tg_id)
            )
            if user_dir.exists():
                try:
                    import shutil

                    shutil.rmtree(user_dir)
                    bot.log(f"Deleted user directory: {user_dir}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not delete user directory %s: %s", user_dir, exc
                    )

            bot.log(
                f"Deleted user @{username} (ID: {tg_id}) - "
                f"{msg_count} messages, {deleted_files} files removed",
            )
            return True, "", deleted_files
        except Exception as exc:  # noqa: BLE001
            logger.error("Error deleting user %s: %s", tg_id, exc, exc_info=True)
            return False, str(exc), deleted_files

    def delete_user(self) -> None:
        bot = self.bot
        selected_items = list(bot.user_tree.selection())
        if not selected_items:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        if len(selected_items) > 1:
            self.bulk_delete_users()
            return

        item_id = selected_items[0]
        values = bot.user_tree.item(item_id)["values"]
        tg_id = values[0]
        username = values[1]
        name = values[2]
        msg_count = values[3]

        try:
            tg_id_int = int(tg_id)
        except (TypeError, ValueError):
            tg_id_int = tg_id
        try:
            msg_count_int = int(msg_count)
        except (TypeError, ValueError):
            msg_count_int = msg_count

        summary = (
            "PERMANENT DELETION WARNING\n\n"
            f"User: @{username or 'unknown'}\n"
            f"Name: {name or 'N/A'}\n"
            f"Telegram ID: {tg_id}\n"
            f"Messages: {msg_count}\n\n"
            "This will permanently delete all history, analytics, reminders, and media.\n\n"
            "THIS CANNOT BE UNDONE!\n"
        )
        if not messagebox.askyesno(
            "Confirm Permanent Deletion", summary, icon="warning"
        ):
            return
        if not messagebox.askyesno(
            "Final Confirmation",
            f"Delete user @{username or 'unknown'} ({tg_id}) forever?",
            icon="warning",
        ):
            return

        success, error_message, deleted_files = self._delete_user_artifacts(
            tg_id_int,
            username or "",
            name or "",
            msg_count_int,
        )
        if success:
            messagebox.showinfo(
                "User Deleted",
                f"User @{username} deleted.\nRemoved {deleted_files} media files.",
            )
            self.refresh_users()
        else:
            messagebox.showerror("Deletion Failed", error_message)

    def bulk_delete_users(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select at least one user")
            return
        if len(selection) == 1:
            self.delete_user()
            return

        users = []
        for item_id in selection:
            values = bot.user_tree.item(item_id)["values"]
            tg_id = values[0]
            username = values[1] or ""
            name = values[2] or ""
            msg_count = values[3]
            try:
                tg_id_int = int(tg_id)
            except (TypeError, ValueError):
                tg_id_int = tg_id
            try:
                msg_count_int = int(msg_count)
            except (TypeError, ValueError):
                msg_count_int = msg_count
            users.append((tg_id_int, username, name, msg_count_int))

        preview_lines = [
            f"@{u[1] or 'unknown'} ({u[0]}) - {u[3]} messages" for u in users
        ]
        preview_text = "\n".join(preview_lines[:10])
        if len(preview_lines) > 10:
            preview_text += f"\n...and {len(preview_lines) - 10} more"

        confirm_text = (
            f"You are about to permanently delete {len(users)} users.\n\n"
            f"{preview_text}\n\nAll history, analytics, and files will be removed."
        )
        if not messagebox.askyesno(
            "Confirm Bulk Deletion", confirm_text, icon="warning"
        ):
            return
        if not messagebox.askyesno(
            "Final Confirmation",
            "This action cannot be undone. Delete all selected users?",
            icon="warning",
        ):
            return

        successes = 0
        total_deleted_files = 0
        errors = []

        for tg_id_val, username, name, msg_count_val in users:
            success, error_message, deleted_files = self._delete_user_artifacts(
                tg_id_val,
                username,
                name,
                msg_count_val,
            )
            if success:
                successes += 1
                total_deleted_files += deleted_files
            else:
                errors.append(
                    f"@{username or 'unknown'} ({tg_id_val}): {error_message}"
                )

        self.refresh_users()
        bot.log(
            f"Bulk delete completed: {successes} success, {len(errors)} failure(s), "
            f"{total_deleted_files} files removed",
        )

        if successes:
            messagebox.showinfo(
                "Users Deleted",
                f"Deleted {successes} user(s) and removed {total_deleted_files} files.",
            )
        if errors:
            messagebox.showwarning("Some deletions failed", "\n".join(errors))

    def export_user_data(self) -> None:
        bot = self.bot
        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return
        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        with db_ro() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (tg_id,)
            ).fetchone()
            if not user:
                messagebox.showerror("Error", "User not found")
                return
            messages = conn.execute(
                """
                SELECT timestamp, role, content FROM messages
                WHERE user_id = ?
                ORDER BY timestamp ASC
                """,
                (user["id"],),
            ).fetchall()

        export_data = {
            "user": dict(user),
            "messages": [dict(msg) for msg in messages],
            "exported_at": datetime.now().isoformat(),
        }
        export_dir = Path("wellness_data") / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            export_dir
            / f"user_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(filename, "w", encoding="utf-8") as fh:
            json.dump(export_data, fh, indent=2, ensure_ascii=False)
        bot.log(f"📤 Exported data for @{username} to {filename}")
        messagebox.showinfo("Success", f"Data exported to:\n{filename}")

    def clear_user_history(self) -> None:
        """Clear all message history for the selected user (profile/settings stay intact)."""

        bot = self.bot
        if not hasattr(bot, "user_tree"):
            return

        selection = bot.user_tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a user first")
            return

        values = bot.user_tree.item(selection[0])["values"]
        tg_id = values[0]
        username = values[1]

        confirm_text = (
            f"Are you sure you want to clear ALL message history for @{username}?\n\n"
            "This will delete:\n"
            "• All user + assistant messages\n"
            "• Psychological profiles\n"
            "• Sentiment analytics + embedding links\n"
            "• Sessions and transcript shards\n\n"
            "User profile + settings will be preserved.\n\n"
            "⚠️ THIS CANNOT BE UNDONE!"
        )
        if not messagebox.askyesno(
            "Clear Message History?", confirm_text, icon="warning"
        ):
            return

        try:
            with db_rw() as conn:
                user = conn.execute(
                    "SELECT id FROM users WHERE telegram_user_id = ?", (tg_id,)
                ).fetchone()
                if not user:
                    messagebox.showerror("Error", "User not found")
                    return

                user_id = user["id"]
                msg_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["cnt"]

                conn.execute(
                    "DELETE FROM psychological_profiles WHERE user_id = ?", (user_id,)
                )
                conn.execute(
                    "DELETE FROM sentiments WHERE message_id IN (SELECT id FROM messages WHERE user_id = ?)",
                    (user_id,),
                )
                conn.execute(
                    "DELETE FROM embedding_links WHERE message_id IN (SELECT id FROM messages WHERE user_id = ?)",
                    (user_id,),
                )
                conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                conn.execute(
                    "DELETE FROM transcript_shards WHERE user_id = ?", (user_id,)
                )

                bot.invalidate_profile_cache(user_id)

            user_dir = Path(bot.cfg.data_root) / "users" / str(tg_id)
            if user_dir.exists():
                try:
                    shutil.rmtree(user_dir)
                    bot.log(f"🗑️ Deleted transcript directory: {user_dir}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed removing transcript dir %s: %s", user_dir, exc
                    )

            bot.log(
                f"🧹 CLEARED HISTORY for @{username} (ID: {tg_id}) - {msg_count} messages deleted"
            )
            messagebox.showinfo(
                "History Cleared",
                f"Message history cleared for @{username}\n\n"
                f"Deleted {msg_count} messages and associated data.\n"
                "User profile and settings preserved.",
            )
            self.refresh_users()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error clearing user history: %s", exc, exc_info=True)
            messagebox.showerror(
                "Clear Failed",
                "An error occurred while clearing history.\n\n"
                f"{exc}\n\nCheck the logs for details.",
            )

    def _create_analytics_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="📈 Analytics")
        canvas = tk.Canvas(tab, highlightthickness=0)
        v_scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        h_scrollbar = ttk.Scrollbar(tab, orient="horizontal", command=canvas.xview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        selector_frame = ttk.LabelFrame(
            scrollable_frame, text="User Selection", padding=10
        )
        selector_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(selector_frame, text="Select User:").pack(side=tk.LEFT, padx=5)
        bot.analytics_user_var = tk.StringVar()
        bot.analytics_user_combo = ttk.Combobox(
            selector_frame,
            textvariable=bot.analytics_user_var,
            width=30,
            state="readonly",
        )
        bot.analytics_user_combo.pack(side=tk.LEFT, padx=5)
        tk.Button(
            selector_frame,
            text="🔄 Load Analytics",
            command=self._load_analytics,
            bg="#3498db",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)

        bot.charts_frame = ttk.LabelFrame(
            scrollable_frame, text="Visualizations", padding=10
        )
        bot.charts_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        bot.analytics_placeholder = tk.Label(
            bot.charts_frame,
            text="Select a user and click 'Load Analytics' to view charts",
            font=("Arial", 12),
            fg="gray",
        )
        bot.analytics_placeholder.pack(pady=50)
        bot.refresh_analytics_users()

    def _load_analytics(self) -> None:
        bot = self.bot
        if not bot.analytics_user_var.get():
            messagebox.showwarning("No User Selected", "Please select a user first")
            return
        user_id = bot.analytics_user_map.get(bot.analytics_user_var.get())
        if not user_id:
            return

        for widget in bot.charts_frame.winfo_children():
            widget.destroy()
        loading_label = tk.Label(
            bot.charts_frame, text="📈 Generating charts...", font=("Arial", 12)
        )
        loading_label.pack(pady=20)
        bot.root.update()

        try:
            try:
                import matplotlib  # type: ignore[import]

                matplotlib.use("TkAgg", force=True)
                import matplotlib.pyplot as plt  # type: ignore[import]
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # type: ignore[import]
                from matplotlib.figure import Figure  # type: ignore[import]
            except ImportError as exc:
                loading_label.destroy()
                tk.Label(
                    bot.charts_frame,
                    text=(
                        "📉 Matplotlib not installed correctly.\n\n"
                        "Please run: pip install --upgrade --force-reinstall matplotlib\n\n"
                        f"Error: {exc}"
                    ),
                    font=("Arial", 10),
                    fg="red",
                    justify="left",
                ).pack(pady=20)
                logger.error("Matplotlib import error: %s", exc)
                return

            from datetime import datetime, timedelta  # noqa: F401  (used in charts)

            try:
                import pandas as pd  # type: ignore[import]
            except ImportError as exc:
                loading_label.destroy()
                tk.Label(
                    bot.charts_frame,
                    text=(
                        "📉 Pandas not installed correctly.\n\n"
                        "Please run: pip install --upgrade pandas\n\n"
                        f"Error: {exc}"
                    ),
                    font=("Arial", 10),
                    fg="red",
                    justify="left",
                ).pack(pady=20)
                logger.error("Pandas import error: %s", exc)
                return

            with db_ro() as conn:
                sent_rows = conn.execute(
                    """
                    SELECT DATE(m.timestamp) as date, s.valence, s.arousal, s.emotion_label
                    FROM sentiments s
                    JOIN messages m ON s.message_id = m.id
                    WHERE m.user_id = ?
                    ORDER BY m.timestamp
                    """,
                    (user_id,),
                ).fetchall()
                sentiments = [
                    {
                        "date": row[0],
                        "valence": row[1],
                        "arousal": row[2],
                        "emotion_label": row[3],
                    }
                    for row in sent_rows
                ]

                freq_rows = conn.execute(
                    """
                    SELECT DATE(timestamp) as date, COUNT(*) as count
                    FROM messages
                    WHERE user_id = ? AND role = 'user'
                    GROUP BY DATE(timestamp)
                    ORDER BY date
                    """,
                    (user_id,),
                ).fetchall()
                message_freq = [{"date": row[0], "count": row[1]} for row in freq_rows]

                mood_rows = conn.execute(
                    """
                    SELECT DATE(timestamp) as date, mood_score
                    FROM mood_journal
                    WHERE user_id = ?
                    ORDER BY timestamp
                    """,
                    (user_id,),
                ).fetchall()
                moods = [{"date": row[0], "mood_score": row[1]} for row in mood_rows]

                hour_rows = conn.execute(
                    """
                    SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, COUNT(*) as count
                    FROM messages
                    WHERE user_id = ? AND role = 'user'
                    GROUP BY hour
                    ORDER BY hour
                    """,
                    (user_id,),
                ).fetchall()
                hour_dist = [{"hour": row[0], "count": row[1]} for row in hour_rows]

            loading_label.destroy()
            fig = Figure(figsize=(14, 10))

            ax1 = fig.add_subplot(3, 2, 1)
            if sentiments:
                df_sent = pd.DataFrame(sentiments)
                df_sent["date"] = pd.to_datetime(df_sent["date"])
                daily_valence = df_sent.groupby("date")["valence"].mean()
                ax1.plot(
                    daily_valence.index,
                    daily_valence.values,
                    marker="o",
                    color="#3498db",
                    linewidth=2,
                )
                ax1.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
                ax1.set_title(
                    "Emotional Valence Over Time", fontsize=12, fontweight="bold"
                )
                ax1.set_xlabel("Date")
                ax1.set_ylabel("Valence (-1 to +1)")
                ax1.grid(True, alpha=0.3)
                ax1.tick_params(axis="x", rotation=45)
            else:
                ax1.text(
                    0.5,
                    0.5,
                    "No sentiment data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax1.set_title(
                    "Emotional Valence Over Time", fontsize=12, fontweight="bold"
                )
                ax1.axis("off")

            ax2 = fig.add_subplot(3, 2, 2)
            if message_freq:
                df_freq = pd.DataFrame(message_freq)
                df_freq["date"] = pd.to_datetime(df_freq["date"])
                ax2.bar(df_freq["date"], df_freq["count"], color="#2ecc71", alpha=0.7)
                ax2.set_title("Message Activity", fontsize=12, fontweight="bold")
                ax2.set_xlabel("Date")
                ax2.set_ylabel("Messages")
                ax2.grid(True, alpha=0.3, axis="y")
                ax2.tick_params(axis="x", rotation=45)
            else:
                ax2.text(
                    0.5,
                    0.5,
                    "No message data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax2.set_title("Message Activity", fontsize=12, fontweight="bold")
                ax2.axis("off")

            ax3 = fig.add_subplot(3, 2, 3)
            if moods:
                df_moods = pd.DataFrame(moods)
                df_moods["date"] = pd.to_datetime(df_moods["date"])
                ax3.plot(
                    df_moods["date"],
                    df_moods["mood_score"],
                    marker="o",
                    color="#e74c3c",
                    linewidth=2,
                )
                ax3.set_title("Mood Ratings (1-10)", fontsize=12, fontweight="bold")
                ax3.set_xlabel("Date")
                ax3.set_ylabel("Mood Score")
                ax3.set_ylim(0, 11)
                ax3.grid(True, alpha=0.3)
                ax3.tick_params(axis="x", rotation=45)
            else:
                ax3.text(
                    0.5,
                    0.5,
                    "No mood data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax3.set_title("Mood Ratings (1-10)", fontsize=12, fontweight="bold")
                ax3.axis("off")

            ax4 = fig.add_subplot(3, 2, 4)
            if sentiments:
                df_emotions = pd.DataFrame(sentiments)
                emotion_counts = df_emotions["emotion_label"].value_counts().head(6)
                colors = [
                    "#3498db",
                    "#2ecc71",
                    "#e74c3c",
                    "#f39c12",
                    "#9b59b6",
                    "#1abc9c",
                ]
                ax4.pie(
                    emotion_counts.values,
                    labels=emotion_counts.index.tolist(),
                    autopct="%1.1f%%",
                    colors=colors,
                    startangle=90,
                )
                ax4.set_title("Top Emotions", fontsize=12, fontweight="bold")
            else:
                ax4.text(
                    0.5,
                    0.5,
                    "No emotion data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax4.set_title("Top Emotions", fontsize=12, fontweight="bold")
                ax4.axis("off")

            ax5 = fig.add_subplot(3, 2, 5)
            if hour_dist:
                df_hours = pd.DataFrame(hour_dist)
                ax5.bar(df_hours["hour"], df_hours["count"], color="#9b59b6", alpha=0.7)
                ax5.set_title("Activity by Hour of Day", fontsize=12, fontweight="bold")
                ax5.set_xlabel("Hour (24-hour)")
                ax5.set_ylabel("Message Count")
                ax5.set_xticks(range(0, 24, 2))
                ax5.grid(True, alpha=0.3, axis="y")
            else:
                ax5.text(
                    0.5,
                    0.5,
                    "No activity data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax5.set_title("Activity by Hour of Day", fontsize=12, fontweight="bold")
                ax5.axis("off")

            ax6 = fig.add_subplot(3, 2, 6)
            if sentiments:
                df_sent_scatter = pd.DataFrame(sentiments)
                ax6.scatter(
                    df_sent_scatter["valence"],
                    df_sent_scatter["arousal"],
                    alpha=0.5,
                    c=df_sent_scatter.index,
                    cmap="viridis",
                    s=50,
                )
                ax6.set_title("Emotional State Map", fontsize=12, fontweight="bold")
                ax6.set_xlabel("Valence (Negative ↔ Positive)")
                ax6.set_ylabel("Arousal (Calm ↔ Excited)")
                ax6.axhline(y=0, color="gray", linestyle="--", alpha=0.3)
                ax6.axvline(x=0, color="gray", linestyle="--", alpha=0.3)
                ax6.grid(True, alpha=0.3)
                ax6.text(0.7, 0.7, "Excited\nPositive", ha="center", alpha=0.3)
                ax6.text(-0.7, 0.7, "Excited\nNegative", ha="center", alpha=0.3)
                ax6.text(0.7, -0.7, "Calm\nPositive", ha="center", alpha=0.3)
                ax6.text(-0.7, -0.7, "Calm\nNegative", ha="center", alpha=0.3)
            else:
                ax6.text(
                    0.5,
                    0.5,
                    "No sentiment data available",
                    ha="center",
                    va="center",
                    fontsize=12,
                )
                ax6.set_title("Emotional State Map", fontsize=12, fontweight="bold")
                ax6.axis("off")

            fig.tight_layout()
            try:
                canvas = FigureCanvasTkAgg(fig, master=bot.charts_frame)
                canvas.draw()
                canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                bot.log(f"📈 Analytics loaded for user {user_id}")
            finally:
                with suppress(Exception):
                    from matplotlib import pyplot as plt  # type: ignore[import]

                    plt.close(fig)
        except Exception as exc:  # noqa: BLE001
            logger.error("Analytics error: %s", exc, exc_info=True)
            for widget in bot.charts_frame.winfo_children():
                widget.destroy()
            tk.Label(
                bot.charts_frame,
                text=f"Failed to generate analytics:\n{exc}",
                fg="red",
                font=("Arial", 10),
            ).pack(pady=20)

    def _create_feedback_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="📝 Feedback")
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=1)

        header = ttk.Label(
            tab,
            text=(
                "Review submissions from /reportbug and /suggestion. "
                "Use the controls on the right to update status and capture admin notes."
            ),
            wraplength=920,
            padding=10,
            justify=tk.LEFT,
        )
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 0))

        filter_frame = ttk.Frame(tab)
        filter_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(5, 0))
        filter_frame.columnconfigure(2, weight=1)

        tk.Label(filter_frame, text="Status Filter:").grid(row=0, column=0, sticky="w")
        bot.feedback_status_filter_var = tk.StringVar(value="All")
        bot.feedback_status_filter = ttk.Combobox(
            filter_frame,
            textvariable=bot.feedback_status_filter_var,
            values=["All", "new", "reviewing", "resolved", "wont_fix"],
            state="readonly",
            width=15,
        )
        bot.feedback_status_filter.grid(row=0, column=1, sticky="w", padx=(5, 20))
        bot.feedback_status_filter.bind(
            "<<ComboboxSelected>>", lambda _evt: self._refresh_feedback_table()
        )

        tk.Button(
            filter_frame,
            text="Refresh",
            command=self._refresh_feedback_table,
            bg="#3498db",
            fg="white",
        ).grid(row=0, column=3, sticky="e")

        if not enabled("user_feedback"):
            warning = ttk.Label(
                filter_frame,
                text="⚠ Telegram feedback commands are currently disabled (feature flag). Admin logging is still available.",
                foreground="#c0392b",
                wraplength=520,
                justify=tk.LEFT,
            )
            warning.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        table_frame = ttk.Frame(tab)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(40, 10))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("id", "type", "user", "status", "created", "summary")
        bot.feedback_tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", height=18
        )
        headings = {
            "id": "ID",
            "type": "Type",
            "user": "User",
            "status": "Status",
            "created": "Created",
            "summary": "Summary",
        }
        widths = {
            "id": 60,
            "type": 100,
            "user": 180,
            "status": 110,
            "created": 160,
            "summary": 360,
        }
        for key in columns:
            bot.feedback_tree.heading(key, text=headings[key])
            bot.feedback_tree.column(key, width=widths[key], anchor="w")
        feedback_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=bot.feedback_tree.yview
        )
        bot.feedback_tree.configure(yscrollcommand=feedback_scroll.set)
        bot.feedback_tree.grid(row=0, column=0, sticky="nsew")
        feedback_scroll.grid(row=0, column=1, sticky="ns")

        table_btns = ttk.Frame(tab)
        table_btns.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        tk.Button(
            table_btns,
            text="✅ Mark Resolved",
            command=lambda: self._quick_update_feedback("resolved"),
            bg="#27ae60",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            table_btns,
            text="⚠ Mark Won't Fix",
            command=lambda: self._quick_update_feedback("wont_fix"),
            bg="#e67e22",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            table_btns,
            text="🕒 Mark Reviewing",
            command=lambda: self._quick_update_feedback("reviewing"),
            bg="#2980b9",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)

        detail_frame = ttk.LabelFrame(tab, text="Feedback Detail", padding=10)
        detail_frame.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=10, pady=10)
        detail_frame.columnconfigure(0, weight=1)
        bot.feedback_detail_text = scrolledtext.ScrolledText(
            detail_frame,
            height=12,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        bot.feedback_detail_text.grid(row=0, column=0, sticky="nsew")

        status_frame = ttk.Frame(detail_frame)
        status_frame.grid(row=1, column=0, sticky="ew", pady=(10, 5))
        tk.Label(status_frame, text="Status:").pack(side=tk.LEFT)
        bot.feedback_status_update_var = tk.StringVar(value="new")
        bot.feedback_status_combo = ttk.Combobox(
            status_frame,
            textvariable=bot.feedback_status_update_var,
            values=["new", "reviewing", "resolved", "wont_fix"],
            state="readonly",
            width=15,
        )
        bot.feedback_status_combo.pack(side=tk.LEFT, padx=5)
        tk.Button(
            status_frame,
            text="💾 Save Update",
            command=self._update_feedback_entry,
            bg="#3498db",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)

        tk.Label(detail_frame, text="Admin Notes:").grid(row=2, column=0, sticky="w")
        bot.feedback_notes_text = scrolledtext.ScrolledText(
            detail_frame, height=6, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.feedback_notes_text.grid(row=3, column=0, sticky="nsew", pady=(0, 10))

        entry_frame = ttk.LabelFrame(tab, text="Log Admin Feedback", padding=10)
        entry_frame.grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10)
        )
        entry_frame.columnconfigure(1, weight=1)
        tk.Label(entry_frame, text="User:").grid(row=0, column=0, sticky="w")
        bot.feedback_user_var = tk.StringVar()
        bot.feedback_user_combo = ttk.Combobox(
            entry_frame, textvariable=bot.feedback_user_var, width=35, state="readonly"
        )
        bot.feedback_user_combo.grid(row=0, column=1, sticky="w", padx=5)

        type_frame = ttk.Frame(entry_frame)
        type_frame.grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Label(type_frame, text="Type:").pack(side=tk.LEFT)
        bot.feedback_new_type_var = tk.StringVar(value="bug")
        tk.Radiobutton(
            type_frame, text="Bug", value="bug", variable=bot.feedback_new_type_var
        ).pack(side=tk.LEFT)
        tk.Radiobutton(
            type_frame,
            text="Suggestion",
            value="suggestion",
            variable=bot.feedback_new_type_var,
        ).pack(side=tk.LEFT)

        tk.Label(entry_frame, text="Details:").grid(
            row=1, column=0, sticky="nw", pady=(8, 0)
        )
        bot.feedback_new_text = scrolledtext.ScrolledText(
            entry_frame, height=4, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.feedback_new_text.grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0)
        )

        def submit_feedback_entry():
            content = bot.feedback_new_text.get("1.0", tk.END).strip()
            if not content:
                messagebox.showinfo(
                    "Missing Details", "Provide feedback details before submitting."
                )
                return

            user_label = bot.feedback_user_var.get()
            user_id = getattr(bot, "feedback_user_map", {}).get(user_label)
            if not user_id:
                messagebox.showerror(
                    "Select User",
                    "Choose the associated user so we can link the entry.",
                )
                return

            feedback_type = (
                bot.feedback_new_type_var.get()
                if hasattr(bot, "feedback_new_type_var")
                else "bug"
            )
            if feedback_type not in {"bug", "suggestion"}:
                feedback_type = "bug"

            try:
                feedback_id = bot.create_feedback_entry_record(
                    int(user_id), feedback_type, content
                )
            except RuntimeError as exc:
                messagebox.showerror("Database Error", str(exc))
                return

            bot.feedback_new_text.delete("1.0", tk.END)
            if hasattr(bot, "feedback_new_type_var"):
                bot.feedback_new_type_var.set("bug")
            bot.feedback_status_filter_var.set("All")
            if (
                hasattr(bot, "feedback_status_filter")
                and bot.feedback_status_filter["values"]
            ):
                try:
                    bot.feedback_status_filter.current(0)
                except tk.TclError:
                    pass
            self._refresh_feedback_table(target_id=feedback_id)
            bot.log(f"?? Logged feedback #{feedback_id} ({feedback_type})")
            if hasattr(bot, "status_bar"):
                bot.status_bar.config(text=f"Created feedback #{feedback_id}")

        tk.Button(
            entry_frame,
            text="➕ Submit Entry",
            command=submit_feedback_entry,
            bg="#27ae60",
            fg="white",
        ).grid(row=2, column=1, sticky="w", pady=10)

        bot.feedback_tree.bind(
            "<<TreeviewSelect>>", lambda _evt: self._on_feedback_selection()
        )
        bot.feedback_records = {}
        bot.refresh_feedback_users()
        self._refresh_feedback_table()

    def _format_timestamp(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value[:19]

    def _refresh_feedback_table(self, target_id: int | None = None) -> None:
        bot = self.bot
        if not hasattr(bot, "feedback_tree"):
            return

        status_filter = (bot.feedback_status_filter_var.get() or "All").lower()
        params: list[str] = []
        where_clause = ""
        if status_filter != "all":
            where_clause = "WHERE f.status = ?"
            params.append(status_filter)

        try:
            with db_ro() as conn:
                rows = conn.execute(
                    f"""
                    SELECT
                        f.id,
                        f.feedback_type,
                        f.status,
                        f.content,
                        f.created_at,
                        COALESCE(f.admin_notes, '') AS admin_notes,
                        f.user_id,
                        COALESCE(u.display_name, u.telegram_username, 'anonymous') AS username
                    FROM user_feedback f
                    LEFT JOIN users u ON u.id = f.user_id
                    {where_clause}
                    ORDER BY f.created_at DESC
                    LIMIT 500
                    """,
                    params,
                ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.error("Feedback table missing or inaccessible: %s", exc)
            bot.feedback_tree.delete(*bot.feedback_tree.get_children())
            bot.feedback_detail_text.configure(state=tk.NORMAL)
            bot.feedback_detail_text.delete("1.0", tk.END)
            bot.feedback_detail_text.insert(
                tk.END,
                "Feedback data unavailable. Run migrations to enable this feature.",
            )
            bot.feedback_detail_text.configure(state=tk.DISABLED)
            bot.feedback_notes_text.delete("1.0", tk.END)
            bot.feedback_records = {}
            return

        bot.feedback_tree.delete(*bot.feedback_tree.get_children())
        bot.feedback_records = {}

        selected_iid = None
        for row in rows:
            feedback_id = row["id"]
            bot.feedback_records[feedback_id] = dict(row)
            content = (row["content"] or "").replace("\n", " ").strip()
            summary = f"{content[:110]}." if len(content) > 110 else content
            created_disp = self._format_timestamp(row.get("created_at"))

            iid = str(feedback_id)
            bot.feedback_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    feedback_id,
                    row["feedback_type"],
                    row["username"],
                    row["status"],
                    created_disp,
                    summary,
                ),
            )

            if target_id and feedback_id == target_id:
                selected_iid = iid

        if selected_iid:
            bot.feedback_tree.selection_set(selected_iid)
            bot.feedback_tree.focus(selected_iid)
            self._on_feedback_selection()
        else:
            bot.feedback_detail_text.configure(state=tk.NORMAL)
            bot.feedback_detail_text.delete("1.0", tk.END)
            bot.feedback_detail_text.insert(
                tk.END, "Select a feedback entry to view details."
            )
            bot.feedback_detail_text.configure(state=tk.DISABLED)
            bot.feedback_notes_text.delete("1.0", tk.END)
            bot.feedback_status_update_var.set("new")

        if hasattr(bot, "status_bar"):
            bot.status_bar.config(text=f"Loaded {len(rows)} feedback item(s)")

    def _on_feedback_selection(self) -> None:
        bot = self.bot
        if not hasattr(bot, "feedback_tree"):
            return

        selection = bot.feedback_tree.selection()
        if not selection:
            return

        feedback_id = int(selection[0])
        record = bot.feedback_records.get(feedback_id)
        if not record:
            return

        created_disp = self._format_timestamp(record.get("created_at"))
        details = [
            f"ID: {record.get('id')}",
            f"Type: {record.get('feedback_type')}",
            f"Status: {record.get('status')}",
            f"User: {record.get('username')}",
            f"Created: {created_disp or 'unknown'}",
            "",
            "--- Content ---",
            record.get("content", "(empty)") or "(empty)",
        ]
        notes = record.get("admin_notes")
        if notes:
            details.extend(["", "--- Admin Notes ---", notes])

        bot.feedback_detail_text.configure(state=tk.NORMAL)
        bot.feedback_detail_text.delete("1.0", tk.END)
        bot.feedback_detail_text.insert(tk.END, "\n".join(details))
        bot.feedback_detail_text.configure(state=tk.DISABLED)

        bot.feedback_status_update_var.set(record.get("status", "new"))
        bot.feedback_notes_text.delete("1.0", tk.END)
        if notes:
            bot.feedback_notes_text.insert(tk.END, notes)

    def _update_feedback_entry(self) -> None:
        bot = self.bot
        if not hasattr(bot, "feedback_tree"):
            return

        selection = bot.feedback_tree.selection()
        if not selection:
            messagebox.showinfo("Select Feedback", "Choose a feedback item first.")
            return

        feedback_id = int(selection[0])
        new_status = bot.feedback_status_update_var.get()
        if new_status not in {"new", "reviewing", "resolved", "wont_fix"}:
            messagebox.showerror(
                "Invalid Status", "Pick a valid status option before saving."
            )
            return

        admin_notes = bot.feedback_notes_text.get("1.0", tk.END).strip()

        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    UPDATE user_feedback
                    SET status = ?, admin_notes = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (new_status, admin_notes or None, feedback_id),
                )
        except sqlite3.OperationalError as exc:
            messagebox.showerror("Database Error", f"Could not update feedback: {exc}")
            return

        bot.log(f"📥 Feedback #{feedback_id} updated to {new_status}")
        self._refresh_feedback_table(target_id=feedback_id)
        if hasattr(bot, "status_bar"):
            bot.status_bar.config(text=f"Feedback #{feedback_id} marked {new_status}")

    def _quick_update_feedback(self, new_status: str) -> None:
        if new_status not in {"new", "reviewing", "resolved", "wont_fix"}:
            return
        self.bot.feedback_status_update_var.set(new_status)
        self._update_feedback_entry()

    def _create_psych_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="🧠 Psych Profile")
        selector_frame = ttk.LabelFrame(
            tab, text="Select User for Analysis", padding=10
        )
        selector_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(selector_frame, text="User:").pack(side=tk.LEFT, padx=5)
        bot.psych_user_var = tk.StringVar()
        bot.psych_user_combo = ttk.Combobox(
            selector_frame, textvariable=bot.psych_user_var, width=30, state="readonly"
        )
        bot.psych_user_combo.pack(side=tk.LEFT, padx=5)
        tk.Label(selector_frame, text="Model:").pack(side=tk.LEFT, padx=(20, 5))
        saved_model = getattr(bot, "psych_model_preference", bot.cfg.chat_model)
        bot.psych_model_var = tk.StringVar(value=saved_model)
        psych_models = bot.get_ollama_models(model_type="chat")
        bot.psych_model_combo = ttk.Combobox(
            selector_frame,
            textvariable=bot.psych_model_var,
            values=psych_models,
            width=20,
        )
        bot.psych_model_combo.pack(side=tk.LEFT, padx=5)
        tk.Button(
            selector_frame,
            text="💾",
            command=self.save_psych_model_preference,
            bg="#27ae60",
            fg="white",
            font=("Arial", 8, "bold"),
            width=2,
        ).pack(side=tk.LEFT, padx=2)
        tk.Button(
            selector_frame,
            text="🔄 Analyze Profile",
            command=self.update_psych_profile,
            bg="#9b59b6",
            fg="white",
            font=("Arial", 10, "bold"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            selector_frame,
            text="📊 Load Existing",
            command=self.load_psych_profile,
            bg="#3498db",
            fg="white",
        ).pack(side=tk.LEFT, padx=5)

        profile_frame = ttk.LabelFrame(tab, text="Psychological Profile", padding=10)
        profile_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        bot.psych_profile_text = scrolledtext.ScrolledText(
            profile_frame, height=30, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.psych_profile_text.pack(fill=tk.BOTH, expand=True)
        info_label = tk.Label(
            tab,
            text="ℹ Deep psychological analysis runs automatically every night. Use 'Analyze Profile' for immediate updates.",
            font=("Arial", 9),
            fg="gray",
        )
        info_label.pack(padx=10, pady=5)
        self.refresh_psych_users()

    def save_psych_model_preference(self) -> None:
        """Persist the selected psych model for future sessions."""

        bot = self.bot
        try:
            selected_model = bot.psych_model_var.get()
            if not selected_model:
                messagebox.showwarning("No Model", "Please select a model first")
                return

            bot.psych_model_preference = selected_model

            config_path = Path("wellness_data") / "config.json"
            config = {}
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
            config["psych_model"] = selected_model
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)

            bot.log(f"💾 Saved psych model preference: {selected_model}")
            messagebox.showinfo(
                "Saved", f"Psych model preference saved:\n{selected_model}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save psych model preference: %s", exc)
            messagebox.showerror("Save Failed", str(exc))

    def refresh_psych_users(self) -> None:
        """Reload the user list for psych profile analysis."""

        bot = self.bot
        try:
            with db_ro() as conn:
                users = conn.execute(
                    """
                    SELECT u.id, u.telegram_username, u.display_name, COUNT(m.id) as msg_count
                    FROM users u
                    LEFT JOIN messages m ON u.id = m.user_id
                    WHERE m.role = 'user'
                    GROUP BY u.id
                    HAVING msg_count >= 20
                    ORDER BY msg_count DESC
                    """
                ).fetchall()

            user_options: list[str] = []
            bot.psych_user_map = {}
            for user in users:
                display = f"{user['display_name'] or user['telegram_username']} ({user['msg_count']} messages)"
                user_options.append(display)
                bot.psych_user_map[display] = user["id"]

            bot.psych_user_combo["values"] = user_options
            if user_options:
                bot.psych_user_combo.current(0)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load psych users: %s", exc)

    def update_psych_profile(self) -> None:
        """Trigger an immediate psychological profile analysis for a user."""

        bot = self.bot
        if not bot.psych_user_var.get():
            messagebox.showwarning("No User Selected", "Please select a user first")
            return

        user_id = bot.psych_user_map.get(bot.psych_user_var.get())
        if not user_id:
            return

        bot.psych_profile_text.delete(1.0, tk.END)
        bot.psych_profile_text.insert(
            tk.END, "⏳ Analyzing psychological profile...\n\n"
        )
        bot.psych_profile_text.insert(
            tk.END, "This may take 30-60 seconds depending on message history.\n"
        )
        bot.root.update()

        selected_model = bot.psych_model_var.get() or bot.cfg.chat_model

        def analyze() -> None:
            try:
                bot._analyze_psychological_profile(user_id, model=selected_model)
                bot.root.after(0, self.load_psych_profile)
                bot.root.after(
                    0,
                    lambda: bot.log(
                        f"✅ Psych profile analysis complete for user {user_id} using {selected_model}",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Psych profile analysis error: %s", exc, exc_info=True)
                error_text = f"Error: {exc}"
                bot.root.after(
                    0,
                    lambda msg=error_text: messagebox.showerror("Analysis Failed", msg),
                )

        threading.Thread(target=analyze, daemon=True).start()

    def load_psych_profile(self) -> None:
        """Load the latest psychological profile for the selected user."""

        bot = self.bot
        if not bot.psych_user_var.get():
            messagebox.showwarning("No User Selected", "Please select a user first")
            return

        user_id = bot.psych_user_map.get(bot.psych_user_var.get())
        if not user_id:
            return

        try:
            with db_ro() as conn:
                profile = conn.execute(
                    """
                    SELECT profile_data, created_at
                    FROM psychological_profiles
                    WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()

            if not profile:
                bot.psych_profile_text.delete(1.0, tk.END)
                bot.psych_profile_text.insert(
                    tk.END, "No psychological profile found.\n\n"
                )
                bot.psych_profile_text.insert(
                    tk.END, "Click 'Analyze Profile' to generate a new one."
                )
                return

            profile_data = json.loads(profile["profile_data"])
            analyzed_at = profile["created_at"]

            def get_metric(
                data: dict, key: str, default: float = 0.0
            ) -> tuple[float, float]:
                """Extract a value/confidence pair from various storage formats."""

                if key not in data:
                    return default, 0.0
                val = data[key]
                if isinstance(val, dict):
                    if "value" in val:
                        return float(val["value"]), float(val.get("confidence", 0.0))
                    return default, 0.0
                if isinstance(val, (int, float)):
                    return float(val), 0.0
                return default, 0.0

            bot.psych_profile_text.delete(1.0, tk.END)
            bot.psych_profile_text.insert(tk.END, "=" * 90 + "\n")
            bot.psych_profile_text.insert(
                tk.END, "COMPREHENSIVE PSYCHOLOGICAL PROFILE\n"
            )
            analyzed_display = bot.format_operator_timestamp(analyzed_at)
            bot.psych_profile_text.insert(
                tk.END, f"Last analyzed: {analyzed_display}\n"
            )
            bot.psych_profile_text.insert(tk.END, "=" * 90 + "\n\n")

            summary = profile_data.get("executive_summary")
            if summary:
                bot.psych_profile_text.insert(tk.END, "📊 EXECUTIVE SUMMARY\n")
                bot.psych_profile_text.insert(tk.END, "=" * 90 + "\n\n")
                if "overview" in summary:
                    bot.psych_profile_text.insert(tk.END, "Overview:\n")
                    bot.psych_profile_text.insert(tk.END, summary["overview"] + "\n\n")
                if "most_prominent_traits" in summary:
                    bot.psych_profile_text.insert(tk.END, "Most Prominent Traits:\n")
                    for trait in summary["most_prominent_traits"]:
                        bot.psych_profile_text.insert(tk.END, f"  • {trait}\n")
                    bot.psych_profile_text.insert(tk.END, "\n")
                if "core_strengths" in summary:
                    bot.psych_profile_text.insert(tk.END, "Core Strengths:\n")
                    for strength in summary["core_strengths"]:
                        bot.psych_profile_text.insert(tk.END, f"  ✓ {strength}\n")
                    bot.psych_profile_text.insert(tk.END, "\n")
                if "core_weaknesses" in summary:
                    bot.psych_profile_text.insert(tk.END, "Core Challenges:\n")
                    for weakness in summary["core_weaknesses"]:
                        bot.psych_profile_text.insert(tk.END, f"  ⚠ {weakness}\n")
                    bot.psych_profile_text.insert(tk.END, "\n")
                if "overall_functioning" in summary:
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"Overall Functioning: {summary['overall_functioning']}\n\n",
                    )
                if "therapeutic_recommendations" in summary:
                    bot.psych_profile_text.insert(
                        tk.END, "Therapeutic Recommendations:\n"
                    )
                    for rec in summary["therapeutic_recommendations"]:
                        bot.psych_profile_text.insert(tk.END, f"  → {rec}\n")
                    bot.psych_profile_text.insert(tk.END, "\n")
                msgs_analyzed = summary.get("messages_analyzed", 0)
                msgs_needed = summary.get("estimated_messages_for_95_confidence", 0)
                bot.psych_profile_text.insert(
                    tk.END, f"Messages Analyzed: {msgs_analyzed}\n"
                )
                if msgs_needed > 0:
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"Messages Needed for 95% Confidence: {msgs_needed} more\n",
                    )
                else:
                    bot.psych_profile_text.insert(
                        tk.END,
                        "Confidence Level: ✅ Sufficient data for high confidence\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n" + "=" * 90 + "\n\n")

            typing = profile_data.get("personality_typing")
            if typing:
                bot.psych_profile_text.insert(tk.END, "🧩 PERSONALITY TYPING\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                mb = typing.get("myers_briggs")
                if mb:
                    mbti_type = mb.get("type", "Unknown")
                    mbti_conf = mb.get("confidence", 0.0)
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Myers-Briggs Type: {mbti_type} (confidence: {mbti_conf:.0%})\n",
                    )
                    dims = mb.get("dimensions") or {}
                    for dim_name, dim_data in dims.items():
                        if not isinstance(dim_data, dict):
                            continue
                        score = dim_data.get("score", 0.0)
                        conf = dim_data.get("confidence", 0.0)
                        if "introversion" in dim_name:
                            letter = "E" if score > 0 else "I"
                            strength = abs(score)
                        elif "sensing" in dim_name:
                            letter = "N" if score > 0 else "S"
                            strength = abs(score)
                        elif "thinking" in dim_name:
                            letter = "F" if score > 0 else "T"
                            strength = abs(score)
                        elif "judging" in dim_name:
                            letter = "P" if score > 0 else "J"
                            strength = abs(score)
                        else:
                            continue
                        filled = "█" * int(strength * 10)
                        empty = "░" * (10 - int(strength * 10))
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"    {dim_name:30s}: {letter} [{filled}{empty}] (conf: {conf:.0%})\n",
                        )
                enn = typing.get("enneagram")
                if enn:
                    enn_type = enn.get("primary_type", "?")
                    wing = enn.get("wing", "")
                    conf = enn.get("confidence", 0.0)
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Enneagram: Type {enn_type}{wing} (confidence: {conf:.0%})\n",
                    )
                    if enn.get("instinctual_variant"):
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"    Instinctual Variant: {enn['instinctual_variant']}\n",
                        )
                    if enn.get("integration_direction"):
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"    Growth Direction: → Type {enn['integration_direction']}\n",
                        )
                    if enn.get("disintegration_direction"):
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"    Stress Direction: → Type {enn['disintegration_direction']}\n",
                        )
                intro_val, intro_conf = get_metric(typing, "introversion_level")
                intro_type = "Introverted" if intro_val > 0.5 else "Extraverted"
                intro_bar = "█" * int(intro_val * 20) + "░" * (20 - int(intro_val * 20))
                bot.psych_profile_text.insert(
                    tk.END,
                    f"  Introversion: {intro_type} [{intro_bar}] {intro_val:.2f} (conf: {intro_conf:.0%})\n",
                )
                bot.psych_profile_text.insert(tk.END, "\n")
            mental_health = profile_data.get("mental_health_indicators")
            if mental_health:
                bot.psych_profile_text.insert(tk.END, "🧠 MENTAL HEALTH INDICATORS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for indicator in mental_health:
                    val, conf = get_metric(mental_health, indicator)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {indicator:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            dark_triad = profile_data.get("dark_triad")
            if dark_triad:
                bot.psych_profile_text.insert(tk.END, "⚠ DARK TRIAD TRAITS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for trait in dark_triad:
                    val, conf = get_metric(dark_triad, trait)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {trait:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            big_five = profile_data.get("big_five")
            if big_five:
                bot.psych_profile_text.insert(
                    tk.END, "🧬 BIG FIVE PERSONALITY (OCEAN)\n"
                )
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for trait in big_five:
                    val, conf = get_metric(big_five, trait)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {trait:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            emotional_intelligence = profile_data.get("emotional_intelligence")
            if emotional_intelligence:
                bot.psych_profile_text.insert(tk.END, "💗 EMOTIONAL INTELLIGENCE\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for component in emotional_intelligence:
                    val, conf = get_metric(emotional_intelligence, component)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {component:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            cognitive_metrics = profile_data.get("cognitive_metrics")
            if cognitive_metrics:
                bot.psych_profile_text.insert(tk.END, "🧠 COGNITIVE METRICS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for metric in cognitive_metrics:
                    val, conf = get_metric(cognitive_metrics, metric)
                    if metric == "estimated_iq":
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"  {metric:30s}: {val:.0f} (conf: {conf:.0%})\n",
                        )
                    else:
                        bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"  {metric:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                        )
                bot.psych_profile_text.insert(tk.END, "\n")

            attachment_style = profile_data.get("attachment_style")
            if attachment_style:
                bot.psych_profile_text.insert(tk.END, "🤝 ATTACHMENT STYLE\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                primary = attachment_style.get("primary_type", "Unknown")
                att_conf_val = attachment_style.get("confidence", 0.0)
                if isinstance(att_conf_val, dict):
                    att_conf = att_conf_val.get("value", 0.0)
                else:
                    att_conf = (
                        att_conf_val if isinstance(att_conf_val, (int, float)) else 0.0
                    )
                bot.psych_profile_text.insert(
                    tk.END,
                    f"  Primary Type: {primary} (confidence: {att_conf:.0%})\n",
                )
                for dim in [
                    "security_score",
                    "anxiety_dimension",
                    "avoidance_dimension",
                    "disorganization_level",
                ]:
                    if dim in attachment_style:
                        val, conf = get_metric(attachment_style, dim)
                        bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"  {dim:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                        )
                bot.psych_profile_text.insert(tk.END, "\n")

            cognitive_distortions = profile_data.get("cognitive_distortions")
            if cognitive_distortions:
                bot.psych_profile_text.insert(tk.END, "🌀 COGNITIVE DISTORTIONS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for distortion in cognitive_distortions:
                    val, conf = get_metric(cognitive_distortions, distortion)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {distortion:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            defense_mechanisms = profile_data.get("defense_mechanisms")
            if defense_mechanisms:
                bot.psych_profile_text.insert(tk.END, "🛡 DEFENSE MECHANISMS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                if isinstance(defense_mechanisms, dict):
                    if defense_mechanisms.get("mature_adaptive"):
                        bot.psych_profile_text.insert(
                            tk.END, "  Mature/Adaptive (Healthy):\n"
                        )
                        for mech in defense_mechanisms["mature_adaptive"]:
                            bot.psych_profile_text.insert(tk.END, f"    ✓ {mech}\n")
                    if defense_mechanisms.get("neurotic_intermediate"):
                        bot.psych_profile_text.insert(
                            tk.END, "  Neurotic/Intermediate:\n"
                        )
                        for mech in defense_mechanisms["neurotic_intermediate"]:
                            bot.psych_profile_text.insert(tk.END, f"    • {mech}\n")
                    if defense_mechanisms.get("immature_maladaptive"):
                        bot.psych_profile_text.insert(
                            tk.END, "  Immature/Maladaptive:\n"
                        )
                        for mech in defense_mechanisms["immature_maladaptive"]:
                            bot.psych_profile_text.insert(tk.END, f"    ⚠ {mech}\n")
                    if defense_mechanisms.get("primary_mechanisms"):
                        bot.psych_profile_text.insert(
                            tk.END, "  Most Frequently Used:\n"
                        )
                        for mech in defense_mechanisms["primary_mechanisms"]:
                            bot.psych_profile_text.insert(tk.END, f"    → {mech}\n")
                else:
                    for mechanism in defense_mechanisms:
                        bot.psych_profile_text.insert(tk.END, f"  • {mechanism}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            motivation = profile_data.get("motivation_drivers")
            if motivation:
                bot.psych_profile_text.insert(tk.END, "🔥 MOTIVATION DRIVERS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for driver in motivation:
                    val, conf = get_metric(motivation, driver)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {driver:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            psych_traits = profile_data.get("psychological_traits")
            if psych_traits:
                bot.psych_profile_text.insert(tk.END, "🧠 PSYCHOLOGICAL TRAITS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for trait in psych_traits:
                    val, conf = get_metric(psych_traits, trait)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {trait:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            bot.psych_profile_text.insert(tk.END, "🧾 BEHAVIORAL STYLES\n")
            bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")

            learning_style = profile_data.get("learning_style")
            if learning_style:
                if isinstance(learning_style, dict):
                    style = learning_style.get("primary", "Unknown")
                    conf = learning_style.get("confidence", 0.0)
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Learning Style: {style} (conf: {conf:.0%})\n",
                    )
                else:
                    bot.psych_profile_text.insert(
                        tk.END, f"  Learning Style: {learning_style}\n"
                    )

            conflict_style = profile_data.get("conflict_resolution_style")
            if conflict_style:
                if isinstance(conflict_style, dict):
                    style = conflict_style.get("primary", "Unknown")
                    conf = conflict_style.get("confidence", 0.0)
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Conflict Resolution: {style} (conf: {conf:.0%})\n",
                    )
                else:
                    bot.psych_profile_text.insert(
                        tk.END, f"  Conflict Resolution: {conflict_style}\n"
                    )

            decision_style = profile_data.get("decision_making_style")
            if decision_style:
                if isinstance(decision_style, dict):
                    style = decision_style.get("primary", "Unknown")
                    conf = decision_style.get("confidence", 0.0)
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Decision Making: {style} (conf: {conf:.0%})\n",
                    )
                else:
                    bot.psych_profile_text.insert(
                        tk.END, f"  Decision Making: {decision_style}\n"
                    )

            for metric_name, label in [
                ("locus_of_control", "Locus of Control"),
                ("growth_mindset", "Growth Mindset"),
                ("risk_tolerance", "Risk Tolerance"),
            ]:
                if metric_name in profile_data:
                    val, conf = get_metric(profile_data, metric_name)
                    if metric_name == "locus_of_control":
                        type_str = "Internal" if val > 0.5 else "External"
                    elif metric_name == "growth_mindset":
                        type_str = "Growth" if val > 0.5 else "Fixed"
                    else:
                        type_str = "Risk-Seeking" if val > 0.5 else "Risk-Averse"
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {label:30s}: {type_str} [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
            bot.psych_profile_text.insert(tk.END, "\n")

            time_perspective = profile_data.get("time_perspective")
            if time_perspective:
                bot.psych_profile_text.insert(tk.END, "⏱ TIME PERSPECTIVE\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for focus in ["past_focus", "present_focus", "future_focus"]:
                    if focus in time_perspective:
                        val, conf = get_metric(time_perspective, focus)
                        bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                        bot.psych_profile_text.insert(
                            tk.END,
                            f"  {focus:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                        )
                bot.psych_profile_text.insert(tk.END, "\n")

            communication_patterns = profile_data.get("communication_patterns")
            if communication_patterns:
                bot.psych_profile_text.insert(tk.END, "💬 COMMUNICATION PATTERNS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for pattern in communication_patterns:
                    val, conf = get_metric(communication_patterns, pattern)
                    bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  {pattern:30s}: [{bar}] {val:.2f} (conf: {conf:.0%})\n",
                    )
                bot.psych_profile_text.insert(tk.END, "\n")

            blindspots = profile_data.get("blindspots") or []
            if blindspots:
                bot.psych_profile_text.insert(tk.END, "👁️ POTENTIAL BLINDSPOTS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for blindspot in blindspots:
                    bot.psych_profile_text.insert(tk.END, f"  ⚠ {blindspot}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            strengths = profile_data.get("notable_strengths") or []
            if strengths:
                bot.psych_profile_text.insert(tk.END, "💪 NOTABLE STRENGTHS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for strength in strengths:
                    bot.psych_profile_text.insert(tk.END, f"  ✓ {strength}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            growth = profile_data.get("areas_for_growth") or []
            if growth:
                bot.psych_profile_text.insert(tk.END, "📈 AREAS FOR GROWTH\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for area in growth:
                    bot.psych_profile_text.insert(tk.END, f"  → {area}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            partner_profile = profile_data.get("ideal_partner_profile") or {}
            if partner_profile:
                bot.psych_profile_text.insert(tk.END, "💞 IDEAL PARTNER PROFILE\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                partner_traits = partner_profile.get("personality_traits") or []
                if partner_traits:
                    bot.psych_profile_text.insert(tk.END, "  Personality Traits:\n")
                    for trait in partner_traits:
                        bot.psych_profile_text.insert(tk.END, f"    • {trait}\n")
                if partner_profile.get("communication_style"):
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Communication Style: {partner_profile['communication_style']}\n",
                    )
                aligned_values = partner_profile.get("values_alignment") or []
                if aligned_values:
                    bot.psych_profile_text.insert(tk.END, "  Values Alignment:\n")
                    for value in aligned_values:
                        bot.psych_profile_text.insert(tk.END, f"    ✓ {value}\n")
                if partner_profile.get("attachment_compatibility"):
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Attachment Compatibility: {partner_profile['attachment_compatibility']}\n",
                    )
                deal_breakers = partner_profile.get("deal_breakers") or []
                if deal_breakers:
                    bot.psych_profile_text.insert(tk.END, "  Deal Breakers:\n")
                    for breaker in deal_breakers:
                        bot.psych_profile_text.insert(tk.END, f"    ⚠ {breaker}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            career_profile = profile_data.get("career_recommendations") or {}
            if career_profile:
                bot.psych_profile_text.insert(tk.END, "💼 CAREER RECOMMENDATIONS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                roles = career_profile.get("suitable_roles") or []
                if roles:
                    bot.psych_profile_text.insert(tk.END, "  Suitable Roles:\n")
                    for role in roles:
                        bot.psych_profile_text.insert(tk.END, f"    • {role}\n")
                if career_profile.get("work_environment"):
                    bot.psych_profile_text.insert(
                        tk.END,
                        f"  Ideal Environment: {career_profile['work_environment']}\n",
                    )
                skills = career_profile.get("skills_to_develop") or []
                if skills:
                    bot.psych_profile_text.insert(tk.END, "  Skills To Develop:\n")
                    for skill in skills:
                        bot.psych_profile_text.insert(tk.END, f"    ⚠ {skill}\n")
                values = career_profile.get("career_values") or []
                if values:
                    bot.psych_profile_text.insert(tk.END, "  Career Values:\n")
                    for value in values:
                        bot.psych_profile_text.insert(tk.END, f"    ⚠ {value}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            insights_profile = profile_data.get("important_insights") or {}
            if insights_profile:
                bot.psych_profile_text.insert(tk.END, "💡 IMPORTANT INSIGHTS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for key, label in [
                    ("user_should_know", "What They Should Know"),
                    ("how_to_communicate", "How To Communicate"),
                    ("timing_considerations", "Timing Considerations"),
                ]:
                    entries = insights_profile.get(key) or []
                    if not entries:
                        continue
                    bot.psych_profile_text.insert(tk.END, f"  {label}:\n")
                    for entry in entries:
                        bot.psych_profile_text.insert(tk.END, f"    • {entry}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            idiosyncrasies = profile_data.get("idiosyncrasies") or []
            if idiosyncrasies:
                bot.psych_profile_text.insert(tk.END, "🌀 IDIOSYNCRASIES & QUIRKS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for quirk in idiosyncrasies:
                    bot.psych_profile_text.insert(tk.END, f"  • {quirk}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            interests = profile_data.get("interests_topics") or []
            if interests:
                bot.psych_profile_text.insert(tk.END, "🎯 INTERESTS & TOPICS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for topic in interests:
                    bot.psych_profile_text.insert(tk.END, f"  • {topic}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            coping = profile_data.get("coping_mechanisms") or []
            if coping:
                bot.psych_profile_text.insert(tk.END, "🛠 COPING MECHANISMS\n")
                bot.psych_profile_text.insert(tk.END, "-" * 90 + "\n")
                for mechanism in coping:
                    bot.psych_profile_text.insert(tk.END, f"  • {mechanism}\n")
                bot.psych_profile_text.insert(tk.END, "\n")

            bot.psych_profile_text.insert(tk.END, "=" * 90 + "\n")
            bot.psych_profile_text.insert(tk.END, "END OF PROFILE\n")
            bot.psych_profile_text.insert(tk.END, "=" * 90 + "\n")

        except Exception as exc:  # noqa: BLE001
            logger.error("Error loading psych profile: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to load profile: {exc}")

    def _create_admin_console_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="🔧 Admin Console")
        broadcast_frame = ttk.LabelFrame(
            tab, text="📢 Broadcast Message to All Users", padding=10
        )
        broadcast_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(broadcast_frame, text="Message:").pack(anchor="w", padx=5, pady=(0, 5))
        bot.broadcast_text = scrolledtext.ScrolledText(
            broadcast_frame, height=4, wrap=tk.WORD, font=("Arial", 10)
        )
        bot.broadcast_text.pack(fill=tk.X, padx=5, pady=5)
        broadcast_btn_frame = tk.Frame(broadcast_frame)
        broadcast_btn_frame.pack(fill=tk.X, padx=5, pady=5)
        tk.Button(
            broadcast_btn_frame,
            text="📣 Send to All Users",
            command=self.broadcast_message,
            bg="#e67e22",
            fg="white",
            font=("Arial", 10, "bold"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Label(
            broadcast_btn_frame,
            text="⚠ This will send immediately to all active users",
            fg="red",
            font=("Arial", 9),
        ).pack(side=tk.LEFT, padx=10)

        llm_frame = ttk.LabelFrame(
            tab, text="💬 Direct LLM Chat (Admin Testing)", padding=10
        )
        llm_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tk.Label(llm_frame, text="Chat as Admin (for testing bot responses):").pack(
            anchor="w", padx=5
        )
        bot.admin_chat_text = scrolledtext.ScrolledText(
            llm_frame, height=15, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.admin_chat_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        input_frame = tk.Frame(llm_frame)
        input_frame.pack(fill=tk.X, padx=5, pady=5)
        tk.Label(input_frame, text="You:").pack(side=tk.LEFT, padx=(0, 5))
        bot.admin_llm_input = tk.Entry(input_frame, font=("Arial", 10))
        bot.admin_llm_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        tk.Button(
            input_frame,
            text="Send",
            command=self.send_admin_llm_message,
            bg="#3498db",
            fg="white",
            font=("Arial", 9, "bold"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            input_frame,
            text="Clear",
            command=lambda: bot.admin_chat_text.delete(1.0, tk.END),
            bg="#95a5a6",
            fg="white",
        ).pack(side=tk.LEFT)
        bot.admin_llm_input.bind("<Return>", lambda e: self.send_admin_llm_message())

        system_frame = ttk.LabelFrame(tab, text="🔍 System Diagnostics", padding=10)
        system_frame.pack(fill=tk.X, padx=10, pady=5)
        diag_btn_frame = tk.Frame(system_frame)
        diag_btn_frame.pack(fill=tk.X)
        tk.Button(
            diag_btn_frame, text="📊 View DB Stats", command=bot.show_db_stats
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            diag_btn_frame,
            text="🧠 View Memory Index",
            command=bot.show_conversation_memory,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            diag_btn_frame,
            text="📂 Export Memory",
            command=bot.export_conversation_memory,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            diag_btn_frame,
            text="🧹 Prune Memory",
            command=bot.prune_conversation_memory,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(diag_btn_frame, text="🔄 Clear Cache", command=bot.clear_cache).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(
            diag_btn_frame, text="💾 Backup Now", command=bot.trigger_backup
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(diag_btn_frame, text="📑 Export Logs", command=bot.export_logs).pack(
            side=tk.LEFT, padx=5
        )

    def broadcast_message(self) -> None:
        """Broadcast a message to all active users."""

        bot = self.bot
        message = bot.broadcast_text.get(1.0, tk.END).strip()
        if not message:
            messagebox.showwarning(
                "Empty Message", "Please enter a message to broadcast"
            )
            return

        with db_ro() as conn:
            user_count = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()[
                "cnt"
            ]

        if not messagebox.askyesno(
            "Confirm Broadcast",
            f'Send this message to {user_count} users?\n\n"{message[:100]}..."',
        ):
            return

        try:
            with db_ro() as conn:
                users = conn.execute(
                    "SELECT id, telegram_user_id FROM users"
                ).fetchall()

            with db_rw() as conn:
                for user in users:
                    conn.execute(
                        "INSERT INTO telegram_outbox (user_id, chat_id, message_text, sent) VALUES (?, ?, ?, 0)",
                        (user["id"], user["telegram_user_id"], message),
                    )

            bot.log(f"📣 Broadcast queued for {len(users)} users")
            messagebox.showinfo("Success", f"Message queued for {len(users)} users")
            bot.broadcast_text.delete(1.0, tk.END)
        except Exception as exc:  # noqa: BLE001
            logger.error("Broadcast error: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to queue broadcast: {exc}")

    def _append_admin_console_output(
        self, prefix: str, content: str, tag: str = "admin_tool"
    ) -> None:
        bot = self.bot

        def writer() -> None:
            if not hasattr(bot, "admin_chat_text"):
                return
            color_map = {
                "admin_tool": "#8e44ad",
                "admin_error": "#e74c3c",
                "admin_data": "#16a085",
            }
            message = f"\n[{prefix}] {content}\n"
            widget = bot.admin_chat_text
            widget.insert(tk.END, message, tag)
            widget.tag_config(
                tag, foreground=color_map.get(tag, "#8e44ad"), font=("Consolas", 9)
            )
            widget.see(tk.END)

        bot.root.after(0, writer)

    def _handle_admin_console_command(self, message: str) -> bool:
        """Process admin console commands entered via the chat."""

        bot = self.bot
        cleaned = (message or "").strip()
        if not cleaned.startswith("!"):
            return False

        lower = cleaned.lower()
        if lower in {"!help", "!commands"}:
            help_text = "Commands: !help, !sql <SELECT>, !profile <user_id>, !memory <user_id>, !context <user_id>"
            self._append_admin_console_output("ADMIN", help_text, tag="admin_tool")
            return True

        if lower.startswith("!sql "):
            query = cleaned[5:].strip()
            if not query.lower().startswith("select"):
                self._append_admin_console_output(
                    "DB",
                    "Only read-only SELECT statements are permitted.",
                    tag="admin_error",
                )
                return True
            try:
                with db_ro() as conn:
                    rows = conn.execute(query).fetchmany(25)
            except Exception as exc:  # noqa: BLE001
                self._append_admin_console_output(
                    "DB", f"SQL error: {exc}", tag="admin_error"
                )
                return True
            if not rows:
                self._append_admin_console_output(
                    "DB", "Query returned no rows.", tag="admin_tool"
                )
                return True
            headers = list(rows[0].keys())
            header_line = " | ".join(str(h) for h in headers)
            body_lines = [" | ".join(str(row[h]) for h in headers) for row in rows]
            lines = [header_line, *body_lines]
            self._append_admin_console_output("DB", "\n".join(lines), tag="admin_data")
            return True

        if lower.startswith("!profile"):
            parts = cleaned.split(maxsplit=1)
            if len(parts) < 2:
                self._append_admin_console_output(
                    "PROFILE", "Usage: !profile <user_id>", tag="admin_error"
                )
                return True
            try:
                user_id = int(parts[1])
            except ValueError:
                self._append_admin_console_output(
                    "PROFILE", "User ID must be an integer.", tag="admin_error"
                )
                return True
            data = bot._get_psych_profile_data(user_id)
            if not data:
                self._append_admin_console_output(
                    "PROFILE", "No psychological profile found.", tag="admin_tool"
                )
                return True
            mental = data.get("mental_health_indicators") or {}
            if not isinstance(mental, dict):
                mental = {}
            depression = bot._extract_metric_value(mental, "depression_likelihood", 0)
            anxiety = bot._extract_metric_value(mental, "anxiety_likelihood", 0)
            summary = (
                data.get("summary", {}).get("overview")
                if isinstance(data.get("summary"), dict)
                else None
            )
            analyzed = data.get("analysis_metadata", {}).get("created_at")
            lines = []
            if analyzed:
                lines.append(f"Analyzed: {analyzed}")
            lines.append(f"Depression: {depression:.2f} | Anxiety: {anxiety:.2f}")
            if summary:
                lines.append(f"Summary: {summary}")
            self._append_admin_console_output(
                "PROFILE", "\n".join(lines), tag="admin_data"
            )
            return True

        if lower.startswith("!memory"):
            parts = cleaned.split(maxsplit=1)
            if len(parts) < 2:
                self._append_admin_console_output(
                    "MEMORY", "Usage: !memory <user_id>", tag="admin_error"
                )
                return True
            try:
                user_id = int(parts[1])
            except ValueError:
                self._append_admin_console_output(
                    "MEMORY", "User ID must be an integer.", tag="admin_error"
                )
                return True
            try:
                with db_ro() as conn:
                    rows = conn.execute(
                        """
                        SELECT created_at, summary, topics
                        FROM conversation_embeddings
                        WHERE user_id = ?
                        ORDER BY created_at DESC
                        LIMIT 5
                        """,
                        (user_id,),
                    ).fetchall()
            except Exception as exc:  # noqa: BLE001
                self._append_admin_console_output(
                    "MEMORY", f"Error: {exc}", tag="admin_error"
                )
                return True
            if not rows:
                self._append_admin_console_output(
                    "MEMORY", "No conversation memory entries found.", tag="admin_tool"
                )
                return True
            formatted = []
            for row in rows:
                topics: list[str] = []
                raw_topics = row.get("topics")
                if raw_topics:
                    try:
                        loaded = json.loads(raw_topics) or []
                        topics = [str(topic) for topic in loaded]
                    except json.JSONDecodeError:
                        topics = []
                topic_preview = (
                    ", ".join(str(topic) for topic in topics[:3]) if topics else "—"
                )
                summary = row.get("summary") or "(no summary)"
                created_at = row.get("created_at")
                formatted.append(f"{created_at}: {summary} | topics: {topic_preview}")
            self._append_admin_console_output(
                "MEMORY", "\n".join(formatted), tag="admin_data"
            )
            return True

        if lower.startswith("!context"):
            parts = cleaned.split(maxsplit=1)
            if len(parts) < 2:
                self._append_admin_console_output(
                    "CONTEXT", "Usage: !context <user_id>", tag="admin_error"
                )
                return True
            try:
                user_id = int(parts[1])
            except ValueError:
                self._append_admin_console_output(
                    "CONTEXT", "User ID must be an integer.", tag="admin_error"
                )
                return True
            try:
                with db_ro() as conn:
                    rows = conn.execute(
                        """
                        SELECT key, value
                        FROM profile_context
                        WHERE user_id = ?
                        ORDER BY key
                        LIMIT 25
                        """,
                        (user_id,),
                    ).fetchall()
            except Exception as exc:  # noqa: BLE001
                self._append_admin_console_output(
                    "CONTEXT", f"Error: {exc}", tag="admin_error"
                )
                return True
            if not rows:
                self._append_admin_console_output(
                    "CONTEXT", "No profile context entries found.", tag="admin_tool"
                )
                return True
            lines = [f"{row['key']}: {row['value']}" for row in rows]
            self._append_admin_console_output(
                "CONTEXT", "\n".join(lines), tag="admin_data"
            )
            return True

        self._append_admin_console_output(
            "ADMIN", f"Unknown admin command: {cleaned}", tag="admin_error"
        )
        return True

    def send_admin_llm_message(self) -> None:
        """Send message to LLM for admin testing."""

        bot = self.bot
        message = bot.admin_llm_input.get().strip()
        if not message:
            return

        bot.admin_chat_text.insert(tk.END, f"\n[ADMIN] You: {message}\n", "user")
        bot.admin_chat_text.tag_config(
            "user", foreground="#3498db", font=("Consolas", 9, "bold")
        )
        bot.admin_llm_input.delete(0, tk.END)
        bot.admin_chat_text.see(tk.END)
        bot.root.update()

        if self._handle_admin_console_command(message):
            return

        if not hasattr(bot, "admin_chat_history"):
            bot.admin_chat_history = []
        bot.admin_chat_history.append({"role": "user", "content": message})
        if len(bot.admin_chat_history) > 20:
            bot.admin_chat_history = bot.admin_chat_history[-20:]

        def get_response() -> None:
            try:
                system_prompt = (
                    "You are Mira, the admin-console assistant. Be candid and proactive. "
                    "You can issue admin commands by replying with lines that start with '!'. "
                    "Available commands: !help, !sql <SELECT>, !profile <user_id>, !memory <user_id>, !context <user_id>. "
                    "The console will execute those commands and show you the results. "
                    "Use them whenever the admin requests data you don't already have, then summarize the findings."
                )

                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(bot.admin_chat_history)

                response = chat(messages, model=bot.cfg.chat_model)

                response_text = None
                if isinstance(response, dict):
                    response_text = (
                        response.get("text")
                        or response.get("message", {}).get("content")
                        or response.get("content")
                        or response.get("response")
                    )
                elif isinstance(response, str):
                    response_text = response
                else:
                    response_text = str(response)

                if not response_text:
                    response_text = "[No response received from LLM]"

                lines_out = response_text.splitlines()
                remaining_lines = []
                for line in lines_out:
                    stripped = line.strip()
                    if stripped.startswith("!") and self._handle_admin_console_command(
                        stripped
                    ):
                        continue
                    remaining_lines.append(line)
                response_text = (
                    "\n".join(remaining_lines).strip() or "[Executed admin command]"
                )

                bot.admin_chat_history.append(
                    {"role": "assistant", "content": response_text}
                )
                bot.root.after(
                    0,
                    lambda: bot.admin_chat_text.insert(
                        tk.END,
                        f"\n[BOT] Mira: {response_text}\n\n",
                        "bot",
                    ),
                )
                bot.root.after(
                    0,
                    lambda: bot.admin_chat_text.tag_config(
                        "bot", foreground="#27ae60", font=("Consolas", 9)
                    ),
                )
                bot.root.after(0, lambda: bot.admin_chat_text.see(tk.END))
            except Exception as exc:  # noqa: BLE001
                logger.error("Admin LLM chat error: %s", exc, exc_info=True)
                error_msg = f"Admin console LLM error: {str(exc)[:200]}"
                self._append_admin_console_output("BOT", error_msg, tag="admin_error")

        threading.Thread(target=get_response, daemon=True).start()

    def show_db_stats(self) -> None:
        """Show high-level counts for major tables."""

        try:
            with db_ro() as conn:
                users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                sentiments = conn.execute("SELECT COUNT(*) FROM sentiments").fetchone()[
                    0
                ]
                reminders = conn.execute(
                    "SELECT COUNT(*) FROM reminders WHERE enabled = 1"
                ).fetchone()[0]
                crises = conn.execute(
                    "SELECT COUNT(*) FROM moderation_events WHERE resolved = 0"
                ).fetchone()[0]
                images = conn.execute("SELECT COUNT(*) FROM image_uploads").fetchone()[
                    0
                ]

            stats = (
                "📊 DATABASE STATISTICS\n\n"
                f"👥 Users: {users}\n"
                f"💬 Messages: {messages}\n"
                f"🧠 Sentiments analyzed: {sentiments}\n"
                f"⏰ Active reminders: {reminders}\n"
                f"🚨 Open crises: {crises}\n"
                f"🖼️ Images uploaded: {images}\n"
            )
            messagebox.showinfo("Database Stats", stats)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to compute DB stats: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to get stats: {exc}")

    def _fetch_conversation_memory_rows(
        self,
        user_id: int,
        limit: int | None,
        role: str = "all",
        keyword: str | None = None,
    ):
        """Fetch conversation memory rows with optional filters."""

        role = (role or "all").strip().lower()
        keyword_normalized = (keyword or "").strip().lower() or None

        base_query = [
            "SELECT",
            "    ce.message_id,",
            "    ce.role,",
            "    ce.summary,",
            "    ce.topics,",
            "    ce.created_at,",
            "    m.content",
            "FROM conversation_embeddings AS ce",
            "LEFT JOIN messages AS m ON m.id = ce.message_id",
            "WHERE ce.user_id = ?",
        ]
        params: list = [user_id]

        if role in {"user", "assistant"}:
            base_query.append("AND ce.role = ?")
            params.append(role)

        if keyword_normalized:
            base_query.append(
                "AND (LOWER(COALESCE(ce.summary, '')) LIKE ? OR LOWER(COALESCE(m.content, '')) LIKE ?)"
            )
            like_val = f"%{keyword_normalized}%"
            params.extend([like_val, like_val])

        base_query.append("ORDER BY ce.created_at DESC")
        if limit:
            base_query.append("LIMIT ?")
            params.append(limit)

        query = "\n".join(base_query)
        with db_ro() as conn:
            return conn.execute(query, params).fetchall()

    def show_conversation_memory(self) -> None:
        """Display recent conversation memory entries for a user."""

        user_id = simpledialog.askinteger("Conversation Memory", "Enter user ID:")
        if not user_id:
            return

        limit = simpledialog.askinteger(
            "Conversation Memory", "Number of entries:", initialvalue=20
        )
        if limit is None or limit <= 0:
            limit = 20

        role = simpledialog.askstring(
            "Conversation Memory",
            "Role filter (user/assistant/all):",
            initialvalue="all",
        )
        role = (role or "all").strip().lower()
        if role not in {"all", "user", "assistant"}:
            messagebox.showerror(
                "Conversation Memory", "Role filter must be user, assistant, or all."
            )
            return

        keyword = simpledialog.askstring(
            "Conversation Memory", "Keyword filter (optional):"
        )
        keyword = (keyword or "").strip()

        rows = self._fetch_conversation_memory_rows(user_id, limit, role, keyword)
        if not rows:
            messagebox.showinfo(
                "Conversation Memory",
                "No conversation memory entries found for this user.",
            )
            return

        bot = self.bot
        window = tk.Toplevel(bot.root)
        title_bits = [f"User {user_id}"]
        if role != "all":
            title_bits.append(role)
        if keyword:
            title_bits.append(f"keyword '{keyword}'")
        window.title("Memory Index - " + ", ".join(title_bits))
        text_widget = scrolledtext.ScrolledText(
            window, width=100, height=30, wrap=tk.WORD, font=("Consolas", 10)
        )
        text_widget.pack(fill=tk.BOTH, expand=True)

        text_widget.insert(tk.END, f"Total entries: {len(rows)}\n\n")
        for row in rows:
            try:
                topics = json.loads(row["topics"]) if row["topics"] else []
            except (TypeError, json.JSONDecodeError):
                topics = []

            text_widget.insert(
                tk.END,
                f"[{row['created_at']}] message #{row['message_id']} ({row['role']})\n",
            )
            if row["summary"]:
                text_widget.insert(tk.END, f"  Summary: {row['summary']}\n")
            if topics:
                text_widget.insert(tk.END, f"  Topics: {', '.join(topics)}\n")
            if row["content"]:
                text_widget.insert(tk.END, f"  Excerpt: {row['content'][:200]}\n")
            text_widget.insert(tk.END, "\n")
        text_widget.configure(state=tk.DISABLED)

    @staticmethod
    def _sanitize_filename_component(
        component: str, fallback: str = "data", max_length: int = 80
    ) -> str:
        """Return a filesystem-safe filename component."""

        if component is None:
            return fallback
        cleaned = _FNAME_CLEAN_RE.sub("_", str(component))
        cleaned = cleaned.strip("._")
        if not cleaned:
            cleaned = fallback
        return cleaned[:max_length]

    def export_conversation_memory(self) -> None:
        """Export conversation memory entries to a JSON file."""

        user_id = simpledialog.askinteger("Export Memory", "Enter user ID:")
        if not user_id:
            return

        limit = simpledialog.askinteger(
            "Export Memory", "Number of entries (0 = all):", initialvalue=200
        )
        if limit is None:
            return
        if limit <= 0:
            limit = 0

        role = simpledialog.askstring(
            "Export Memory", "Role filter (user/assistant/all):", initialvalue="all"
        )
        role = (role or "all").strip().lower()
        if role not in {"all", "user", "assistant"}:
            messagebox.showerror(
                "Export Memory", "Role filter must be user, assistant, or all."
            )
            return

        keyword = simpledialog.askstring("Export Memory", "Keyword filter (optional):")
        keyword = (keyword or "").strip()

        rows = self._fetch_conversation_memory_rows(
            user_id, limit or None, role, keyword
        )
        if not rows:
            messagebox.showinfo(
                "Export Memory",
                "No entries found for this user with the given filters.",
            )
            return

        bot = self.bot
        export_dir = Path(bot.cfg.data_root) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = operator_now().strftime("%Y%m%d_%H%M%S")
        suffix = [self._sanitize_filename_component(f"user{user_id}", "user")]
        if role != "all":
            suffix.append(self._sanitize_filename_component(role, role))
        if keyword:
            suffix.append(self._sanitize_filename_component(keyword, "keyword"))
        file_name = f"memory_{'_'.join(suffix)}_{timestamp}.json"
        file_path = export_dir / file_name

        export_payload = []
        for row in rows:
            try:
                topics = json.loads(row["topics"]) if row["topics"] else []
            except (TypeError, json.JSONDecodeError):
                topics = []
            export_payload.append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "summary": row["summary"],
                    "topics": topics,
                    "created_at": row["created_at"],
                    "content": row["content"],
                }
            )

        file_path.write_text(
            json.dumps(export_payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        bot.log(f"🗃️ Exported {len(export_payload)} memory entries to {file_path}")
        messagebox.showinfo(
            "Export Memory", f"Exported {len(export_payload)} entries to:\n{file_path}"
        )

    def prune_conversation_memory(self) -> None:
        """Prune older conversation memory entries for a user."""

        user_id = simpledialog.askinteger("Prune Memory", "Enter user ID:")
        if not user_id:
            return

        keep_count = simpledialog.askinteger(
            "Prune Memory", "Keep how many most recent entries?", initialvalue=200
        )
        if keep_count is None:
            return
        if keep_count < 0:
            messagebox.showerror("Prune Memory", "Keep count must be zero or positive.")
            return

        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT message_id
                FROM conversation_embeddings
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
                """,
                (user_id, keep_count),
            ).fetchall()

        if not rows:
            messagebox.showinfo("Prune Memory", "No entries to prune for this user.")
            return

        message_ids = [row["message_id"] for row in rows]
        backend = get_backend()
        removed = 0
        for message_id in message_ids:
            try:
                backend.delete(message_id)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to delete memory vector for message %s: %s", message_id, exc
                )

        with db_rw() as conn:
            conn.executemany(
                "DELETE FROM conversation_embeddings WHERE message_id = ?",
                [(mid,) for mid in message_ids],
            )

        self.bot.log(
            f"🧹 Pruned {removed} memory entries for user {user_id} (kept latest {keep_count})"
        )
        messagebox.showinfo(
            "Prune Memory", f"Removed {removed} entries for user {user_id}."
        )

    def clear_cache(self) -> None:
        """Clear profile context + preference caches."""

        bot = self.bot
        bot.profile_context_service.clear()
        bot.preference_service.clear_cache()
        bot.log("🧼 Cleared profile context cache")
        messagebox.showinfo("Success", "Profile cache cleared")

    def trigger_backup(self) -> None:
        """Trigger immediate database backup."""

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = Path("wellness_data") / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"wellness_{timestamp}.db"

            shutil.copy("wellness_data/wellness.db", backup_path)

            self.bot.log(f"💾 Backup created: {backup_path}")
            messagebox.showinfo("Success", f"Backup created:\n{backup_path}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Backup error: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Backup failed: {exc}")

    def export_logs(self) -> None:
        """Export bot logs to file."""

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_dir = Path("wellness_data") / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            export_path = export_dir / f"logs_{timestamp}.txt"

            shutil.copy("wellness_data/bot.log", export_path)

            self.bot.log(f"📤 Logs exported to {export_path}")
            messagebox.showinfo("Success", f"Logs exported to:\n{export_path}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Log export error: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to export logs: {exc}")

    def _create_settings_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="? Settings")

        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_settings_tab_mousewheel(event):
            if canvas.winfo_exists():
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all("<MouseWheel>", on_settings_tab_mousewheel),
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        settings_frame = ttk.LabelFrame(
            scrollable_frame, text="Bot Configuration", padding=20
        )
        settings_frame.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(settings_frame, text="Chat Model:", font=("Arial", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=10
        )
        bot.model_var = tk.StringVar(value=bot.cfg.chat_model)
        chat_models = bot.get_ollama_models(model_type="chat")
        bot.model_combo = ttk.Combobox(
            settings_frame,
            textvariable=bot.model_var,
            values=chat_models,
            width=47,
            font=("Arial", 10),
        )
        bot.model_combo.grid(row=0, column=1, sticky="w", padx=10)
        tk.Button(
            settings_frame,
            text="?? Reload",
            command=lambda: self.reload_model("chat"),
            bg="#3498db",
            fg="white",
            font=("Arial", 9, "bold"),
        ).grid(row=0, column=2, padx=5)

        tk.Label(settings_frame, text="Vision Model:", font=("Arial", 10, "bold")).grid(
            row=1, column=0, sticky="w", pady=10
        )
        bot.vision_var = tk.StringVar(value=bot.cfg.vision_model)
        vision_models = bot.get_ollama_models(model_type="vision")
        bot.vision_combo = ttk.Combobox(
            settings_frame,
            textvariable=bot.vision_var,
            values=vision_models,
            width=47,
            font=("Arial", 10),
        )
        bot.vision_combo.grid(row=1, column=1, sticky="w", padx=10)
        tk.Button(
            settings_frame,
            text="?? Reload",
            command=lambda: self.reload_model("vision"),
            bg="#3498db",
            fg="white",
            font=("Arial", 9, "bold"),
        ).grid(row=1, column=2, padx=5)

        tk.Label(
            settings_frame, text="Admin Username:", font=("Arial", 10, "bold")
        ).grid(row=2, column=0, sticky="w", pady=10)
        bot.admin_var = tk.StringVar(value=bot.cfg.admin_username)
        tk.Entry(
            settings_frame, textvariable=bot.admin_var, width=50, font=("Arial", 10)
        ).grid(row=2, column=1, sticky="w", padx=10)

        tk.Label(
            settings_frame, text="Your Personality Mode:", font=("Arial", 10, "bold")
        ).grid(row=3, column=0, sticky="w", pady=10)
        try:
            with db_ro() as conn:
                admin_user = conn.execute(
                    "SELECT id, personality FROM users WHERE telegram_username = ?",
                    (bot.cfg.admin_username,),
                ).fetchone()
                if admin_user:
                    bot.admin_user_id = admin_user["id"]
                    current_personality = bot.personality_manager.get_user_personality(
                        bot.admin_user_id
                    )
                else:
                    current_personality = "friendly"
                    bot.admin_user_id = None
        except Exception:
            current_personality = "friendly"
            bot.admin_user_id = None

        personality_names = list(PERSONALITY_MODES.keys())
        personality_display = [
            f"{PERSONALITY_MODES[p]['emoji']} {PERSONALITY_MODES[p]['name']}"
            for p in personality_names
        ]
        current_config = PERSONALITY_MODES.get(current_personality, {})
        current_display = f"{current_config.get('emoji', '??')} {current_config.get('name', 'Friendly')}"

        bot.personality_var = tk.StringVar(value=current_display)
        bot.personality_combo = ttk.Combobox(
            settings_frame,
            textvariable=bot.personality_var,
            values=personality_display,
            width=47,
            font=("Arial", 10),
            state="readonly",
        )
        bot.personality_combo.grid(row=3, column=1, sticky="w", padx=10)

        def apply_personality():
            if bot.admin_user_id is None:
                messagebox.showwarning(
                    "No Admin User",
                    "Admin user not found in database. Please send a message to the bot first.",
                )
                return

            selected_display = bot.personality_var.get()
            for name, config in PERSONALITY_MODES.items():
                if f"{config['emoji']} {config['name']}" == selected_display:
                    success = bot.personality_manager.set_user_personality(
                        bot.admin_user_id, name
                    )
                    if success:
                        reminder_status = (
                            "enabled"
                            if config.get("enable_reminders", True)
                            else "disabled"
                        )
                        messagebox.showinfo(
                            "Personality Changed",
                            f"Your personality is now: {config['emoji']} {config['name']}\n\n"
                            f"Settings:\n"
                            f" Temperature: {config['temperature']}\n"
                            f" Repeat Penalty: {config['repeat_penalty']}\n"
                            f" Reminders: {reminder_status}\n\n"
                            f"This only affects YOUR conversations!",
                        )
                        bot.log(f"?? Admin personality changed to {name}")
                    else:
                        messagebox.showerror(
                            "Error", "Failed to change personality. Please try again."
                        )
                    break

        tk.Button(
            settings_frame,
            text="? Apply Personality",
            command=apply_personality,
            bg="#27ae60",
            fg="white",
            font=("Arial", 9, "bold"),
        ).grid(row=3, column=2, padx=5)

        tk.Button(
            settings_frame,
            text="?? Save Settings",
            command=self.save_settings,
            bg="#3498db",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=20,
            pady=10,
        ).grid(row=4, column=0, columnspan=2, pady=20)

        info_frame = ttk.LabelFrame(scrollable_frame, text="Database Info", padding=20)
        info_frame.pack(fill=tk.X, padx=10, pady=10)
        with db_ro() as conn:
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        tk.Label(
            info_frame, text=f"Total Users: {user_count}", font=("Arial", 10)
        ).pack(anchor="w", pady=5)
        tk.Label(
            info_frame, text=f"Total Messages: {msg_count}", font=("Arial", 10)
        ).pack(anchor="w", pady=5)
        tk.Label(
            info_frame, text=f"Total Sessions: {session_count}", font=("Arial", 10)
        ).pack(anchor="w", pady=5)

        advanced_frame = ttk.LabelFrame(
            scrollable_frame, text="? Advanced Model Settings", padding=20
        )
        advanced_frame.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(advanced_frame, text="Temperature:", font=("Arial", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=10
        )
        bot.temp_var = tk.DoubleVar(value=0.8)
        tk.Scale(
            advanced_frame,
            from_=0.0,
            to=2.0,
            resolution=0.1,
            orient=tk.HORIZONTAL,
            length=300,
            variable=bot.temp_var,
        ).grid(row=0, column=1, sticky="w", padx=10)
        tk.Label(
            advanced_frame,
            text="(0.0=Focused, 0.8=Balanced, 2.0=Creative)",
            font=("Arial", 8),
            fg="gray",
        ).grid(row=0, column=2, sticky="w", padx=5)

        tk.Label(
            advanced_frame, text="Context Window:", font=("Arial", 10, "bold")
        ).grid(row=1, column=0, sticky="w", pady=10)
        bot.ctx_var = tk.StringVar(value="8192")
        ttk.Combobox(
            advanced_frame,
            textvariable=bot.ctx_var,
            values=["2048", "4096", "8192", "16384", "32768"],
            state="readonly",
            width=15,
        ).grid(row=1, column=1, sticky="w", padx=10)
        tk.Label(
            advanced_frame,
            text="(Higher = more memory, slower)",
            font=("Arial", 8),
            fg="gray",
        ).grid(row=1, column=2, sticky="w", padx=5)

        tk.Label(
            advanced_frame, text="Top P (Nucleus Sampling):", font=("Arial", 10, "bold")
        ).grid(row=2, column=0, sticky="w", pady=10)
        bot.top_p_var = tk.DoubleVar(value=0.9)
        tk.Scale(
            advanced_frame,
            from_=0.0,
            to=1.0,
            resolution=0.05,
            orient=tk.HORIZONTAL,
            length=300,
            variable=bot.top_p_var,
        ).grid(row=2, column=1, sticky="w", padx=10)
        tk.Label(
            advanced_frame, text="(0.9 recommended)", font=("Arial", 8), fg="gray"
        ).grid(row=2, column=2, sticky="w", padx=5)

        tk.Label(
            advanced_frame, text="Repeat Penalty:", font=("Arial", 10, "bold")
        ).grid(row=3, column=0, sticky="w", pady=10)
        bot.repeat_penalty_var = tk.DoubleVar(value=1.1)
        tk.Scale(
            advanced_frame,
            from_=1.0,
            to=2.0,
            resolution=0.05,
            orient=tk.HORIZONTAL,
            length=300,
            variable=bot.repeat_penalty_var,
        ).grid(row=3, column=1, sticky="w", padx=10)
        tk.Label(
            advanced_frame,
            text="(Higher = less repetition)",
            font=("Arial", 8),
            fg="gray",
        ).grid(row=3, column=2, sticky="w", padx=5)

        tk.Label(
            advanced_frame, text="Custom System Prompt:", font=("Arial", 10, "bold")
        ).grid(row=4, column=0, sticky="nw", pady=10)
        bot.system_prompt_text = scrolledtext.ScrolledText(
            advanced_frame, height=6, wrap=tk.WORD, font=("Arial", 9)
        )
        bot.system_prompt_text.grid(
            row=4, column=1, columnspan=2, sticky="we", padx=10, pady=10
        )
        default_prompt = """You are Mira, a compassionate AI wellness companion.
Be warm, empathetic, and supportive. Always validate emotions before offering advice.
Ask thoughtful questions to understand better. Prioritize safety in crisis situations."""
        bot.system_prompt_text.insert("1.0", default_prompt)

        tk.Label(
            advanced_frame, text="Quick Personality:", font=("Arial", 10, "bold")
        ).grid(row=5, column=0, sticky="w", pady=10)

        def set_professional():
            bot.temp_var.set(0.5)
            bot.top_p_var.set(0.85)
            bot.system_prompt_text.delete("1.0", tk.END)
            bot.system_prompt_text.insert(
                "1.0",
                "You are Mira, a professional wellness assistant. Be concise, evidence-based, and structured.",
            )

        def set_friendly():
            bot.temp_var.set(0.8)
            bot.top_p_var.set(0.9)
            bot.system_prompt_text.delete("1.0", tk.END)
            bot.system_prompt_text.insert("1.0", default_prompt)

        def set_creative():
            bot.temp_var.set(1.2)
            bot.top_p_var.set(0.95)
            bot.system_prompt_text.delete("1.0", tk.END)
            bot.system_prompt_text.insert(
                "1.0",
                "You are Mira, a creative wellness companion. Use metaphors, stories, and imaginative suggestions. Be playful yet supportive.",
            )

        def set_therapeutic():
            bot.temp_var.set(0.7)
            bot.top_p_var.set(0.9)
            bot.repeat_penalty_var.set(1.15)
            bot.system_prompt_text.delete("1.0", tk.END)
            bot.system_prompt_text.insert(
                "1.0",
                """You are Mira in Therapeutic Mode. Use reflective listening, Socratic questioning, and CBT techniques.
Always validate before advising. Help identify patterns and coping strategies. Encourage self-compassion.""",
            )

        mode_frame = tk.Frame(advanced_frame)
        mode_frame.grid(row=5, column=1, columnspan=2, sticky="w", padx=10)
        tk.Button(
            mode_frame, text="?? Professional", command=set_professional, width=12
        ).pack(side=tk.LEFT, padx=2)
        tk.Button(mode_frame, text="?? Friendly", command=set_friendly, width=12).pack(
            side=tk.LEFT, padx=2
        )
        tk.Button(mode_frame, text="?? Creative", command=set_creative, width=12).pack(
            side=tk.LEFT, padx=2
        )
        tk.Button(
            mode_frame, text="?? Therapeutic", command=set_therapeutic, width=12
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            advanced_frame,
            text="?? Save Model Settings",
            command=self.save_model_settings,
            bg="#3498db",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=20,
            pady=10,
        ).grid(row=6, column=0, columnspan=3, pady=20)

    def save_settings(self) -> None:
        """Persist base chat, vision, and admin settings."""

        bot = self.bot
        try:
            config_path = Path(bot.cfg.data_root) / "config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
            else:
                config = {}

            config["chat_model"] = bot.model_var.get()
            config["vision_model"] = bot.vision_var.get()
            config["admin_username"] = bot.admin_var.get()

            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)

            bot.cfg.chat_model = bot.model_var.get()
            bot.cfg.vision_model = bot.vision_var.get()
            bot.cfg.admin_username = bot.admin_var.get()

            messagebox.showinfo(
                "Settings",
                (
                    f"Settings saved to {config_path}!\n\n"
                    f"Chat Model: {bot.model_var.get()}\n"
                    f"Vision Model: {bot.vision_var.get()}"
                ),
            )
            bot.log(
                f"[Config] Settings saved: {bot.model_var.get()}, {bot.vision_var.get()}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error saving settings: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to save settings: {exc}")

    def save_model_settings(self) -> None:
        """Persist advanced model parameters."""

        bot = self.bot
        try:
            config_path = Path(bot.cfg.data_root) / "config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
            else:
                config = {}

            config["model_params"] = {
                "temperature": bot.temp_var.get(),
                "top_p": bot.top_p_var.get(),
                "repeat_penalty": bot.repeat_penalty_var.get(),
                "num_ctx": int(bot.ctx_var.get()),
                "system_prompt": bot.system_prompt_text.get("1.0", "end-1c").strip(),
            }

            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)

            messagebox.showinfo(
                "Success",
                (
                    "Model settings saved!\n\n"
                    f"Temperature: {bot.temp_var.get()}\n"
                    f"Context: {bot.ctx_var.get()} tokens\n"
                    f"Top P: {bot.top_p_var.get()}\n"
                    f"Repeat Penalty: {bot.repeat_penalty_var.get()}"
                ),
            )
            bot.log(
                f"?? Model settings updated: temp={bot.temp_var.get()}, ctx={bot.ctx_var.get()}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error saving model settings: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to save settings: {exc}")

    def reload_model(self, model_type: str = "chat") -> None:
        """Apply a model change and preload it through Ollama."""

        bot = self.bot
        try:
            new_model = (
                bot.model_var.get() if model_type == "chat" else bot.vision_var.get()
            )
            old_model = (
                bot.cfg.chat_model if model_type == "chat" else bot.cfg.vision_model
            )
            if new_model == old_model:
                bot.log(f"ℹ {model_type.title()} model already set to {new_model}")
                return

            if not messagebox.askyesno(
                "Change Model",
                (
                    f"Change {model_type} model from:\n{old_model}\n\n"
                    f"To:\n{new_model}\n\nThis will reload the model."
                ),
            ):
                return

            bot.log(f"🔄 Changing {model_type} model to {new_model}...")
            config_path = Path(bot.cfg.data_root) / "config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
            else:
                config = {}
            if model_type == "chat":
                config["chat_model"] = new_model
                bot.cfg.chat_model = new_model
            else:
                config["vision_model"] = new_model
                bot.cfg.vision_model = new_model
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)

            bot.log(f"📥 Loading {new_model} into VRAM...")
            result = subprocess.run(
                ["ollama", "run", new_model, "Hi"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
            if result.returncode == 0:
                bot.log(f"✅ {model_type.title()} model changed to {new_model}")
                messagebox.showinfo(
                    "Success",
                    f"{model_type.title()} model changed to:\n{new_model}\n\nModel is now loaded and ready!",
                )
            else:
                bot.log(f"⚠ Model loaded but with warnings: {result.stderr}")
                messagebox.showwarning(
                    "Model Changed",
                    f"Model changed to {new_model}\n\nMay have warnings - check Activity Log",
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error reloading model: %s", exc, exc_info=True)
            messagebox.showerror("Error", f"Failed to reload model: {exc}")

    def refresh_gpu_status(self) -> None:
        """Populate the GPU status text widget with nvidia-smi output."""

        bot = self.bot
        if not hasattr(bot, "gpu_status_text"):
            return
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu_info = result.stdout.strip().split(", ")
                if len(gpu_info) >= 6:
                    idx, name, util, mem_used, mem_total, temp = gpu_info
                    used_pct = int((float(mem_used) / float(mem_total)) * 100)
                    status_text = (
                        "╔══════════════════════════════════════════╗\n"
                        "║           GPU STATUS                     ║\n"
                        "╚══════════════════════════════════════════╝\n"
                        f"GPU: {name}\n"
                        f"Index: {idx}\n"
                        f"💪 Utilization: {util}%\n"
                        f"🧠 VRAM: {mem_used}MB / {mem_total}MB ({used_pct}% used)\n"
                        f"🌡  Temperature: {temp}°C\n"
                        f"Status: {'🟢 OPTIMAL' if float(temp) < 80 else '🟡 WARM' if float(temp) < 85 else '🔴 HOT'}\n"
                    )
                else:
                    status_text = result.stdout
            else:
                status_text = (
                    "╔══════════════════════════════════════════╗\n"
                    "║           GPU STATUS                     ║\n"
                    "╚══════════════════════════════════════════╝\n"
                    "⚠ nvidia-smi not found or no NVIDIA GPU detected\n"
                    "This is normal if you don't have an NVIDIA GPU.\n"
                    "Ollama can still run on CPU, though slower.\n"
                    "To check if you have an NVIDIA GPU:\n"
                    "1. Open Device Manager\n"
                    '2. Look under "Display adapters"\n'
                    "3. If you see NVIDIA, install GPU drivers from nvidia.com\n"
                )
            bot.gpu_status_text.delete("1.0", tk.END)
            bot.gpu_status_text.insert("1.0", status_text)
        except FileNotFoundError:
            status_text = "⚠ nvidia-smi not found. Install NVIDIA drivers if you have an NVIDIA GPU."
            bot.gpu_status_text.delete("1.0", tk.END)
            bot.gpu_status_text.insert("1.0", status_text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error refreshing GPU status: %s", exc)
            bot.gpu_status_text.delete("1.0", tk.END)
            bot.gpu_status_text.insert("1.0", f"Error: {exc}")

    def launch_nvidia_smi(self) -> None:
        """Launch nvidia-smi in a new console window."""

        bot = self.bot
        try:
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoExit",
                    "-Command",
                    'nvidia-smi; echo ""; echo "Press Ctrl+C to refresh, or close window to exit."',
                ],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            bot.log("🚀 Launched nvidia-smi in new window")
        except FileNotFoundError:
            bot.log("⚠ nvidia-smi not found. Install NVIDIA drivers first.")
            messagebox.showwarning(
                "nvidia-smi Not Found",
                "nvidia-smi not found.\n\nInstall NVIDIA GPU drivers from nvidia.com if you have an NVIDIA GPU.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error launching nvidia-smi: %s", exc)
            bot.log(f"❌ Error launching nvidia-smi: {exc}")

    def refresh_ollama_env(self) -> None:
        """Display key Ollama environment variables."""

        bot = self.bot
        if not hasattr(bot, "ollama_env_text"):
            return
        try:
            env_vars = {
                "OLLAMA_NUM_PARALLEL": os.environ.get(
                    "OLLAMA_NUM_PARALLEL", "Not set (default: 1)"
                ),
                "OLLAMA_MAX_LOADED_MODELS": os.environ.get(
                    "OLLAMA_MAX_LOADED_MODELS", "Not set (default: 1)"
                ),
                "OLLAMA_FLASH_ATTENTION": os.environ.get(
                    "OLLAMA_FLASH_ATTENTION", "Not set (default: disabled)"
                ),
                "OLLAMA_GPU_OVERHEAD": os.environ.get(
                    "OLLAMA_GPU_OVERHEAD", "Not set (default: enabled)"
                ),
                "OLLAMA_HOST": os.environ.get(
                    "OLLAMA_HOST", "Not set (default: 127.0.0.1:11434)"
                ),
            }
            status_text = (
                "╔══════════════════════════════════════════╗\n"
                "║      OLLAMA ENVIRONMENT VARIABLES        ║\n"
                "╚══════════════════════════════════════════╝\n"
            )
            for var, value in env_vars.items():
                status_text += f"{var}:\n  {value}\n\n"
            status_text += (
                "📝 Note: These are current SYSTEM environment variables.\n"
                "   Changes made below require Ollama service restart to take effect.\n"
                '⚠ If values show "Not set", Ollama is using defaults.\n'
                '   Click "Apply & Restart Ollama" to activate optimizations.\n'
            )
            bot.ollama_env_text.delete("1.0", tk.END)
            bot.ollama_env_text.insert("1.0", status_text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error refreshing Ollama env: %s", exc)
            bot.ollama_env_text.delete("1.0", tk.END)
            bot.ollama_env_text.insert("1.0", f"Error: {exc}")

    def apply_gpu_settings(self) -> None:
        """Apply GPU-related environment variables and restart Ollama."""

        bot = self.bot
        required_attrs = (
            "parallel_var",
            "max_models_var",
            "flash_attention_var",
            "gpu_overhead_var",
        )
        if not all(hasattr(bot, attr) for attr in required_attrs):
            return
        try:
            num_parallel = bot.parallel_var.get()
            max_models = bot.max_models_var.get()
            flash_attention = "1" if bot.flash_attention_var.get() else "0"
            gpu_overhead = "0.0" if bot.gpu_overhead_var.get() else "0.5"

            confirm = messagebox.askyesno(
                "Apply GPU Settings",
                (
                    "This will apply the following settings and RESTART Ollama service:\n\n"
                    f"• Parallel Requests: {num_parallel}\n"
                    f"• Max Loaded Models: {max_models}\n"
                    f"• Flash Attention: {'Enabled' if flash_attention == '1' else 'Disabled'}\n"
                    f"• GPU Overhead: {'Minimized' if gpu_overhead == '0.0' else 'Default'}\n\n"
                    "⚠ This will briefly interrupt active conversations.\n\nContinue?"
                ),
            )
            if not confirm:
                return

            bot.log("🔧 Applying GPU settings...")
            commands = [
                f"setx OLLAMA_NUM_PARALLEL {num_parallel}",
                f"setx OLLAMA_MAX_LOADED_MODELS {max_models}",
                f"setx OLLAMA_FLASH_ATTENTION {flash_attention}",
                f"setx OLLAMA_GPU_OVERHEAD {gpu_overhead}",
            ]
            for cmd in commands:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    bot.log(f"⚠ Warning setting env var: {result.stderr}")
            bot.log("✅ Environment variables set")

            bot.log("🔄 Attempting to restart Ollama service...")
            restart_result = subprocess.run(
                [
                    "powershell.exe",
                    "-Command",
                    "Restart-Service",
                    "Ollama",
                    "-ErrorAction",
                    "Stop",
                ],
                capture_output=True,
                text=True,
            )
            if restart_result.returncode == 0:
                bot.log("✅ Ollama service restarted successfully!")
                messagebox.showinfo(
                    "Settings Applied",
                    (
                        "GPU optimization settings applied!\n\n"
                        "Ollama service restarted.\n"
                        "New settings are now active.\n\n"
                        "💡 Tip: Use 'Pre-warm Models' to load models into VRAM for instant responses."
                    ),
                )
            else:
                bot.log("⚠ Could not restart Ollama service automatically")
                messagebox.showwarning(
                    "Manual Restart Required",
                    (
                        "Environment variables have been set, but Ollama service could not be restarted automatically.\n\n"
                        "Please restart Ollama manually:\n"
                        "1. Close Ollama (system tray icon)\n"
                        "2. Reopen Ollama\n\n"
                        "Or restart your computer for changes to take effect."
                    ),
                )
            self.refresh_ollama_env()
            self.refresh_gpu_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error applying GPU settings: %s", exc)
            bot.log(f"❌ Error: {exc}")
            messagebox.showerror("Error", f"Failed to apply settings:\n{exc}")

    def prewarm_models(self) -> None:
        """Load configured chat and vision models into VRAM."""

        bot = self.bot
        try:
            chat_model = bot.cfg.chat_model
            vision_model = bot.cfg.vision_model
            bot.log("🔥 Pre-warming models (loading into VRAM)...")
            bot.log("   This may take 30-60 seconds...")
            messagebox.showinfo(
                "Pre-warming Models",
                (
                    "Loading models into VRAM:\n\n"
                    f"• {chat_model}\n"
                    f"• {vision_model}\n\n"
                    "This will take 30-60 seconds.\n"
                    "Watch the Activity Log for progress."
                ),
            )
            bot.log(f"   Loading {chat_model}...")
            result = subprocess.run(
                ["ollama", "run", chat_model, "Hello"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode == 0:
                bot.log(f"   ✅ {chat_model} loaded")
            else:
                bot.log(f"   ⚠ {chat_model} load error: {result.stderr}")

            bot.log(f"   Loading {vision_model}...")
            result = subprocess.run(
                ["ollama", "run", vision_model, "Test"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode == 0:
                bot.log(f"   ✅ {vision_model} loaded")
            else:
                bot.log(f"   ⚠ {vision_model} load error: {result.stderr}")

            bot.log("🎉 Pre-warming complete! Models are in VRAM.")
            bot.log("   First user requests will now be instant!")
            self.refresh_gpu_status()
            messagebox.showinfo(
                "Pre-warming Complete",
                (
                    "Models loaded into VRAM:\n\n"
                    f"✅ {chat_model}\n"
                    f"✅ {vision_model}\n\n"
                    "First requests will now be instant!\n"
                    "Check GPU Status tab to see VRAM usage."
                ),
            )
        except subprocess.TimeoutExpired:
            bot.log("⚠ Pre-warming timed out (models may be too large)")
            messagebox.showwarning(
                "Timeout",
                "Pre-warming took too long.\n\nModels may be too large for your GPU.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error pre-warming models: %s", exc)
            bot.log(f"❌ Pre-warm error: {exc}")
            messagebox.showerror("Error", f"Failed to pre-warm models:\n{exc}")

    def reset_gpu_settings(self) -> None:
        """Reset GPU optimization controls to defaults."""

        bot = self.bot
        required_attrs = (
            "parallel_var",
            "max_models_var",
            "flash_attention_var",
            "gpu_overhead_var",
        )
        if not all(hasattr(bot, attr) for attr in required_attrs):
            return
        try:
            if not messagebox.askyesno(
                "Reset GPU Settings",
                (
                    "Reset all GPU optimization settings to safe defaults?\n\n"
                    "Defaults:\n"
                    "• Parallel Requests: 2\n"
                    "• Max Loaded Models: 2\n"
                    "• Flash Attention: Enabled\n"
                    "• GPU Overhead: Minimized\n\n"
                    "This will NOT restart Ollama.\n"
                    "Click 'Apply & Restart Ollama' after resetting to activate."
                ),
            ):
                return
            bot.parallel_var.set("2")
            bot.max_models_var.set("2")
            bot.flash_attention_var.set(True)
            bot.gpu_overhead_var.set(True)
            bot.log("🔄 GPU settings reset to defaults")
            bot.log("   Click 'Apply & Restart Ollama' to activate")
            messagebox.showinfo(
                "Settings Reset",
                "GPU settings reset to safe defaults.\n\nClick 'Apply & Restart Ollama' to activate the changes.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error resetting GPU settings: %s", exc)
            bot.log(f"❌ Reset error: {exc}")

    def _create_gpu_tab(self) -> None:
        bot = self.bot
        tab = ttk.Frame(bot.notebook)
        bot.notebook.add(tab, text="?? GPU Control")

        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_gpu_tab_mousewheel(event):
            if canvas.winfo_exists():
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind(
            "<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_gpu_tab_mousewheel)
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        status_frame = ttk.LabelFrame(scrollable_frame, text="GPU Status", padding=20)
        status_frame.pack(fill=tk.X, padx=10, pady=10)
        bot.gpu_status_text = scrolledtext.ScrolledText(
            status_frame, height=8, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.gpu_status_text.pack(fill=tk.BOTH, expand=True)

        btn_frame = tk.Frame(status_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        tk.Button(
            btn_frame,
            text="?? Refresh GPU Status",
            command=self.refresh_gpu_status,
            bg="#3498db",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=15,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="?? Launch nvidia-smi",
            command=self.launch_nvidia_smi,
            bg="#9b59b6",
            fg="white",
            font=("Arial", 10, "bold"),
            padx=15,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

        ollama_frame = ttk.LabelFrame(
            scrollable_frame, text="Ollama GPU Optimization", padding=20
        )
        ollama_frame.pack(fill=tk.X, padx=10, pady=10)

        current_frame = tk.Frame(ollama_frame)
        current_frame.pack(fill=tk.X, pady=10)
        tk.Label(
            current_frame,
            text="Current Ollama Environment:",
            font=("Arial", 10, "bold"),
        ).pack(anchor="w")
        bot.ollama_env_text = scrolledtext.ScrolledText(
            current_frame, height=6, wrap=tk.WORD, font=("Consolas", 9)
        )
        bot.ollama_env_text.pack(fill=tk.BOTH, expand=True, pady=5)

        controls_frame = tk.Frame(ollama_frame)
        controls_frame.pack(fill=tk.X, pady=10)
        tk.Label(controls_frame, text="Parallel Requests:", font=("Arial", 10)).grid(
            row=0, column=0, sticky="w", padx=5, pady=5
        )
        bot.parallel_var = tk.StringVar(value="2")
        tk.Spinbox(
            controls_frame,
            from_=1,
            to=8,
            textvariable=bot.parallel_var,
            width=10,
            font=("Arial", 10),
        ).grid(
            row=0,
            column=1,
            sticky="w",
            padx=5,
        )
        tk.Label(
            controls_frame,
            text="(How many requests to process simultaneously)",
            font=("Arial", 8),
            fg="gray",
        ).grid(row=0, column=2, sticky="w", padx=10)
        tk.Label(controls_frame, text="Max Loaded Models:", font=("Arial", 10)).grid(
            row=1, column=0, sticky="w", padx=5, pady=5
        )
        bot.max_models_var = tk.StringVar(value="2")
        tk.Spinbox(
            controls_frame,
            from_=1,
            to=5,
            textvariable=bot.max_models_var,
            width=10,
            font=("Arial", 10),
        ).grid(
            row=1,
            column=1,
            sticky="w",
            padx=5,
        )
        tk.Label(
            controls_frame,
            text="(Keep models loaded in VRAM)",
            font=("Arial", 8),
            fg="gray",
        ).grid(row=1, column=2, sticky="w", padx=10)
        bot.flash_attention_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            controls_frame,
            text="Enable Flash Attention (Faster)",
            variable=bot.flash_attention_var,
            font=("Arial", 10),
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        bot.gpu_overhead_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            controls_frame,
            text="Minimize GPU Overhead (More VRAM for model)",
            variable=bot.gpu_overhead_var,
            font=("Arial", 10),
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        apply_frame = tk.Frame(ollama_frame)
        apply_frame.pack(fill=tk.X, pady=10)
        tk.Button(
            apply_frame,
            text="? Apply & Restart Ollama",
            command=self.apply_gpu_settings,
            bg="#27ae60",
            fg="white",
            font=("Arial", 11, "bold"),
            padx=20,
            pady=10,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            apply_frame,
            text="?? Pre-warm Models",
            command=self.prewarm_models,
            bg="#f39c12",
            fg="white",
            font=("Arial", 11, "bold"),
            padx=20,
            pady=10,
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            apply_frame,
            text="? Reset to Defaults",
            command=self.reset_gpu_settings,
            bg="#e74c3c",
            fg="white",
            font=("Arial", 11, "bold"),
            padx=20,
            pady=10,
        ).pack(side=tk.LEFT, padx=5)

        tips_frame = ttk.LabelFrame(
            scrollable_frame, text="?? Performance Tips", padding=20
        )
        tips_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tips_text = scrolledtext.ScrolledText(
            tips_frame, height=10, wrap=tk.WORD, font=("Arial", 10)
        )
        tips_text.pack(fill=tk.BOTH, expand=True)
        tips_text.insert(
            tk.END,
            "\U0001F680 Quick Tips for Maximum Speed:\n"
            "1. PARALLEL REQUESTS: Set to 2-3 if you have 12GB+ VRAM\n"
            "   - More parallel = faster for multiple users\n"
            "   - Each request uses ~5GB VRAM (depends on model)\n"
            "   - Calculate: Model size x Parallel = Total VRAM needed\n"
            "2. FLASH ATTENTION: Always enable (unless crashes occur)\n"
            "   - 20-30% faster inference\n"
            "   - No quality loss\n"
            "   - Requires modern GPU (RTX 20-series or newer)\n"
            "3. PRE-WARM MODELS: Click to load models into VRAM\n"
            "   - First message after cold start is slow (loading from disk)\n"
            "   - Pre-warming keeps models in VRAM\n"
            "   - Instant responses after warming\n"
            "4. MAX LOADED MODELS: Keep at 2 for chat + vision\n"
            "   - Models stay in VRAM = instant switching\n"
            "   - Only unload if running out of VRAM\n"
            "5. FASTER MODELS: Go to Settings tab\n"
            "   - gemma2:9b = 2x faster than llama3.1:8b\n"
            "   - moondream = 3x faster vision\n"
            "   - Same quality, less computation\n"
            '6. MONITOR: Click "Launch nvidia-smi" to watch GPU\n'
            "   - Should see 80-100% usage when processing\n"
            "   - Check temperature (should be <85�C)\n"
            "   - Monitor VRAM usage\n"
            "\U0001F4CA Expected Speeds:\n"
            "- llama3.1:8b: ~10 seconds\n"
            "- gemma2:9b: ~5 seconds  \U00002B50 RECOMMENDED\n"
            "- phi3:mini: ~3 seconds\n"
            "\U0001F4BE VRAM Requirements:\n"
            "- 8GB: parallel=1, use smaller models\n"
            "- 12GB: parallel=2, standard models \U00002B50\n"
            "- 16GB+: parallel=3-4, larger models\n"
            "\U000026A0\U0000FE0F IMPORTANT: Changes require Ollama restart!\n"
            'Settings are applied when you click "Apply & Restart Ollama"\n',
        )
        tips_text.config(state=tk.DISABLED)

        bot.root.after(500, self.refresh_gpu_status)
        bot.root.after(1000, self.refresh_ollama_env)
