"""
Enhanced LLM Console Tools for Omniscient Admin Capabilities

Provides 10+ tools for the LLM console to read/edit files, query/modify database,
manage users, search messages, and execute system functions.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.infra.db.session import db_ro, db_rw

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()


@dataclass
class ToolResult:
    """Result from tool execution"""

    success: bool
    data: Any
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class LLMConsoleTools:
    """Collection of tools available to the LLM console"""

    def __init__(
        self, project_root: Optional[Path] = None, allow_external_files: bool = False
    ):
        self.project_root = project_root or PROJECT_ROOT
        self.allow_external_files = allow_external_files
        self.active_edits: Dict[str, Dict[str, Any]] = {}  # Track sandboxed edits

    # ========================================================================
    # FILE OPERATIONS
    # ========================================================================

    def read_file(self, file_path: str, max_lines: int = 1000) -> ToolResult:
        """
        Read contents of a file.

        Args:
            file_path: Absolute or relative path to file
            max_lines: Maximum lines to read (default: 1000)

        Returns:
            ToolResult with file contents
        """
        try:
            path = Path(file_path)
            if not path.is_absolute():
                path = self.project_root / path

            # Security check
            if not self.allow_external_files and not self._is_within_project(path):
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Access denied: {path} is outside project directory. Admin can lift this restriction.",
                )

            if not path.exists():
                return ToolResult(
                    success=False, data=None, error=f"File not found: {path}"
                )

            if not path.is_file():
                return ToolResult(success=False, data=None, error=f"Not a file: {path}")

            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            if total_lines > max_lines:
                content = "".join(lines[:max_lines])
                truncated_msg = (
                    f"\n... [Truncated: showing {max_lines}/{total_lines} lines]"
                )
                content += truncated_msg
            else:
                content = "".join(lines)

            return ToolResult(
                success=True,
                data=content,
                metadata={
                    "path": str(path),
                    "lines": total_lines,
                    "truncated": total_lines > max_lines,
                },
            )

        except Exception as exc:
            logger.error(f"read_file error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def edit_file(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
        create_backup: bool = True,
        rollback_timeout: int = 60,
    ) -> ToolResult:
        """
        Edit a file with sandboxed rollback mechanism.

        Args:
            file_path: Path to file to edit
            old_content: Content to replace (must match exactly)
            new_content: New content to insert
            create_backup: Create timestamped backup (default: True)
            rollback_timeout: Auto-rollback after N seconds if not confirmed (default: 60)

        Returns:
            ToolResult with edit status and backup info
        """
        try:
            path = Path(file_path)
            if not path.is_absolute():
                path = self.project_root / path

            # Security check
            if not self.allow_external_files and not self._is_within_project(path):
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Access denied: {path} is outside project directory",
                )

            if not path.exists():
                return ToolResult(
                    success=False, data=None, error=f"File not found: {path}"
                )

            # Read current content
            with open(path, "r", encoding="utf-8") as f:
                current_content = f.read()

            # Verify old_content matches
            if old_content not in current_content:
                return ToolResult(
                    success=False,
                    data=None,
                    error="old_content not found in file. Content must match exactly.",
                )

            # Create backup
            backup_path = None
            if create_backup:
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                backup_path = path.parent / f"{path.name}.backup_{timestamp}"
                shutil.copy2(path, backup_path)

            # Perform edit
            new_file_content = current_content.replace(old_content, new_content, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_file_content)

            # Track for rollback
            edit_id = f"{path}_{int(time.time())}"
            self.active_edits[edit_id] = {
                "file_path": str(path),
                "backup_path": str(backup_path) if backup_path else None,
                "created_at": time.time(),
                "rollback_timeout": rollback_timeout,
                "confirmed": False,
            }

            return ToolResult(
                success=True,
                data=f"File edited successfully. Backup: {backup_path}",
                metadata={
                    "edit_id": edit_id,
                    "backup_path": str(backup_path) if backup_path else None,
                    "rollback_timeout": rollback_timeout,
                    "message": f"Edit will auto-rollback in {rollback_timeout}s unless confirmed",
                },
            )

        except Exception as exc:
            logger.error(f"edit_file error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def confirm_edit(self, edit_id: str) -> ToolResult:
        """Confirm an edit to prevent auto-rollback"""
        if edit_id not in self.active_edits:
            return ToolResult(success=False, data=None, error="Edit ID not found")

        edit_info = self.active_edits.pop(edit_id)
        backup_path = edit_info.get("backup_path")
        if backup_path:
            try:
                Path(backup_path).unlink(missing_ok=True)
            except Exception as exc:
                logger.debug("Failed cleaning confirmed backup %s: %s", backup_path, exc)
        return ToolResult(
            success=True, data=f"Edit {edit_id} confirmed. Will not auto-rollback."
        )

    def rollback_edit(self, edit_id: str) -> ToolResult:
        """Manually rollback an edit"""
        if edit_id not in self.active_edits:
            return ToolResult(success=False, data=None, error="Edit ID not found")

        edit_info = self.active_edits[edit_id]
        if not edit_info.get("backup_path"):
            return ToolResult(
                success=False, data=None, error="No backup available for rollback"
            )

        try:
            shutil.copy2(edit_info["backup_path"], edit_info["file_path"])
            del self.active_edits[edit_id]
            try:
                Path(edit_info["backup_path"]).unlink(missing_ok=True)
            except Exception as cleanup_exc:
                logger.debug(
                    "Failed deleting rollback backup %s: %s",
                    edit_info["backup_path"],
                    cleanup_exc,
                )
            return ToolResult(
                success=True, data=f"Rolled back to backup: {edit_info['backup_path']}"
            )
        except Exception as exc:
            logger.error(f"rollback_edit error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def check_rollbacks(self) -> List[str]:
        """Check for edits that need auto-rollback"""
        now = time.time()
        to_rollback = []

        for edit_id, info in list(self.active_edits.items()):
            if info["confirmed"]:
                continue

            elapsed = now - info["created_at"]
            if elapsed > info["rollback_timeout"]:
                result = self.rollback_edit(edit_id)
                if result.success:
                    to_rollback.append(edit_id)
                    logger.warning(
                        f"Auto-rolled back edit {edit_id} after {elapsed:.1f}s"
                    )

        return to_rollback

    def list_directory(
        self, dir_path: str = ".", pattern: str = "*", max_items: int = 100
    ) -> ToolResult:
        """
        List files in a directory.

        Args:
            dir_path: Directory path (default: current project root)
            pattern: Glob pattern to filter (default: *)
            max_items: Maximum items to return (default: 100)

        Returns:
            ToolResult with list of files/directories
        """
        try:
            path = Path(dir_path)
            if not path.is_absolute():
                path = self.project_root / path

            if not self.allow_external_files and not self._is_within_project(path):
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Access denied: {path} is outside project directory",
                )

            if not path.exists():
                return ToolResult(
                    success=False, data=None, error=f"Directory not found: {path}"
                )

            if not path.is_dir():
                return ToolResult(
                    success=False, data=None, error=f"Not a directory: {path}"
                )

            items = list(path.glob(pattern))[:max_items]
            item_list = []
            for item in items:
                item_list.append(
                    {
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "size": item.stat().st_size if item.is_file() else None,
                        "modified": datetime.fromtimestamp(
                            item.stat().st_mtime
                        ).isoformat(),
                    }
                )

            return ToolResult(
                success=True,
                data=item_list,
                metadata={
                    "path": str(path),
                    "pattern": pattern,
                    "count": len(item_list),
                },
            )

        except Exception as exc:
            logger.error(f"list_directory error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    # ========================================================================
    # DATABASE OPERATIONS
    # ========================================================================

    def query_database(
        self, sql: str, params: Optional[List[Any]] = None
    ) -> ToolResult:
        """
        Execute a SELECT query on the database.

        Args:
            sql: SQL query (must be SELECT)
            params: Query parameters (default: None)

        Returns:
            ToolResult with query results
        """
        try:
            # Security: Only allow SELECT
            if not sql.strip().upper().startswith("SELECT"):
                return ToolResult(
                    success=False,
                    data=None,
                    error="Only SELECT queries allowed. Use update_database for modifications.",
                )

            with db_ro() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(sql, params or [])
                rows = cursor.fetchall()
                results = [dict(row) for row in rows]

            return ToolResult(
                success=True,
                data=results,
                metadata={"row_count": len(results), "query": sql},
            )

        except Exception as exc:
            logger.error(f"query_database error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def update_database(
        self, sql: str, params: Optional[List[Any]] = None, dry_run: bool = False
    ) -> ToolResult:
        """
        Execute an UPDATE/INSERT/DELETE query on the database.

        Args:
            sql: SQL query (UPDATE, INSERT, DELETE)
            params: Query parameters (default: None)
            dry_run: If True, show what would be affected without executing (default: False)

        Returns:
            ToolResult with affected row count
        """
        try:
            # Security: Block dangerous operations
            sql_upper = sql.strip().upper()
            if sql_upper.startswith("DROP") or sql_upper.startswith("TRUNCATE"):
                return ToolResult(
                    success=False,
                    data=None,
                    error="DROP and TRUNCATE are not allowed for safety",
                )

            if not any(
                sql_upper.startswith(kw) for kw in ["UPDATE", "INSERT", "DELETE"]
            ):
                return ToolResult(
                    success=False,
                    data=None,
                    error="Only UPDATE/INSERT/DELETE allowed. Use query_database for SELECT.",
                )

            if dry_run:
                return ToolResult(
                    success=True,
                    data={"dry_run": True, "message": "Dry run - query not executed"},
                    metadata={"query": sql, "params": params},
                )

            with db_rw() as conn:
                cursor = conn.execute(sql, params or [])
                affected_rows = cursor.rowcount

            return ToolResult(
                success=True,
                data={"affected_rows": affected_rows},
                metadata={"query": sql, "params": params},
            )

        except Exception as exc:
            logger.error(f"update_database error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    # ========================================================================
    # SYSTEM/USER OPERATIONS
    # ========================================================================

    def list_users(self, limit: int = 50, offset: int = 0) -> ToolResult:
        """List all users in the system"""
        try:
            with db_ro() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, telegram_user_id, telegram_username, display_name,
                           last_active_at, onboarding_completed
                    FROM users
                    ORDER BY last_active_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()

            users = [dict(row) for row in rows]
            return ToolResult(success=True, data=users, metadata={"count": len(users)})

        except Exception as exc:
            logger.error(f"list_users error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def get_user_detail(self, user_id: int) -> ToolResult:
        """Get detailed information about a specific user"""
        try:
            with db_ro() as conn:
                conn.row_factory = sqlite3.Row
                user = conn.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()

                if not user:
                    return ToolResult(
                        success=False, data=None, error=f"User {user_id} not found"
                    )

                # Get message count
                msg_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ?", (user_id,)
                ).fetchone()["cnt"]

                # Get reminder count
                reminder_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM reminders WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["cnt"]

            result = dict(user)
            result["message_count"] = msg_count
            result["reminder_count"] = reminder_count

            return ToolResult(success=True, data=result)

        except Exception as exc:
            logger.error(f"get_user_detail error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def search_messages(
        self, query: str, user_id: Optional[int] = None, limit: int = 50
    ) -> ToolResult:
        """Search message content"""
        try:
            params: List[Any] = [f"%{query}%"]
            where_clauses = ["content LIKE ?"]

            if user_id is not None:
                where_clauses.append("user_id = ?")
                params.append(user_id)

            where_sql = " AND ".join(where_clauses)

            with db_ro() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.user_id, m.role, m.content, m.timestamp, u.display_name
                    FROM messages m
                    LEFT JOIN users u ON u.id = m.user_id
                    WHERE {where_sql}
                    ORDER BY m.timestamp DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                ).fetchall()

            results = [dict(row) for row in rows]
            return ToolResult(
                success=True,
                data=results,
                metadata={"query": query, "count": len(results)},
            )

        except Exception as exc:
            logger.error(f"search_messages error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    def get_system_status(self) -> ToolResult:
        """Get system status and health checks"""
        try:
            status: Dict[str, Any] = {}

            # Database check
            with db_ro() as conn:
                status["db"] = "ok"
                status["user_count"] = conn.execute(
                    "SELECT COUNT(*) FROM users"
                ).fetchone()[0]
                status["message_count"] = conn.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]

            # Disk usage
            if hasattr(shutil, "disk_usage"):
                usage = shutil.disk_usage(str(self.project_root))
                status["disk"] = {
                    "total_gb": round(usage.total / (1024**3), 2),
                    "used_gb": round(usage.used / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "percent": round((usage.used / usage.total) * 100, 1),
                }

            return ToolResult(success=True, data=status)

        except Exception as exc:
            logger.error(f"get_system_status error: {exc}", exc_info=True)
            return ToolResult(success=False, data=None, error=str(exc))

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def _is_within_project(self, path: Path) -> bool:
        """Check if path is within project directory"""
        try:
            path.resolve().relative_to(self.project_root.resolve())
            return True
        except ValueError:
            return False


# ========================================================================
# TOOL REGISTRY
# ========================================================================

TOOL_DEFINITIONS = {
    "read_file": {
        "description": "Read contents of a file",
        "params": {
            "file_path": "Path to file (absolute or relative to project)",
            "max_lines": "Maximum lines to read (default: 1000)",
        },
    },
    "edit_file": {
        "description": "Edit a file with sandboxed rollback. Changes auto-rollback after timeout unless confirmed.",
        "params": {
            "file_path": "Path to file",
            "old_content": "Content to replace (must match exactly)",
            "new_content": "New content to insert",
            "rollback_timeout": "Seconds before auto-rollback (default: 60)",
        },
    },
    "confirm_edit": {
        "description": "Confirm an edit to prevent auto-rollback",
        "params": {"edit_id": "Edit ID from edit_file result"},
    },
    "rollback_edit": {
        "description": "Manually rollback an edit",
        "params": {"edit_id": "Edit ID to rollback"},
    },
    "list_directory": {
        "description": "List files in a directory",
        "params": {
            "dir_path": "Directory path (default: project root)",
            "pattern": "Glob pattern (default: *)",
            "max_items": "Maximum items (default: 100)",
        },
    },
    "query_database": {
        "description": "Execute SELECT query on database",
        "params": {"sql": "SELECT query", "params": "Query parameters (optional)"},
    },
    "update_database": {
        "description": "Execute UPDATE/INSERT/DELETE on database",
        "params": {
            "sql": "UPDATE/INSERT/DELETE query",
            "params": "Query parameters (optional)",
            "dry_run": "Preview without executing (default: false)",
        },
    },
    "list_users": {
        "description": "List all users",
        "params": {
            "limit": "Max users to return (default: 50)",
            "offset": "Offset for pagination (default: 0)",
        },
    },
    "get_user_detail": {
        "description": "Get detailed user information",
        "params": {"user_id": "User ID"},
    },
    "search_messages": {
        "description": "Search message content",
        "params": {
            "query": "Search query",
            "user_id": "Filter by user ID (optional)",
            "limit": "Max results (default: 50)",
        },
    },
    "get_system_status": {"description": "Get system status and health", "params": {}},
}
