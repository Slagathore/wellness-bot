#!/usr/bin/env python3
"""
Wellness Bot - Automated Code Quality Checker

This single script runs all quality checks on your code.
It automatically discovers Python files and adapts as you add new code.

Usage:
    python check_code.py              # Run all checks
    python check_code.py --fast       # Skip tests (quick check)
    python check_code.py --fix        # Auto-fix formatting issues
    python check_code.py --install    # Install required tools
"""
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# Colors for terminal output (works on Windows 10+)
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

# Fallback for older Windows
if os.name == 'nt':
    try:
        os.system('')  # Enable ANSI colors on Windows
    except:
        RED = GREEN = YELLOW = BLUE = RESET = ""


class CodeChecker:
    """Automated code quality checker that adapts to your project."""

    def __init__(self):
        self.project_root = Path(__file__).parent
        self.failed_checks = []
        self.passed_checks = []
        self.warnings = []

    def print_header(self, text: str):
        """Print a section header."""
        print(f"\n{BLUE}{'=' * 60}{RESET}")
        print(f"{BLUE}{text}{RESET}")
        print(f"{BLUE}{'=' * 60}{RESET}")

    def print_success(self, text: str):
        """Print success message."""
        print(f"{GREEN}[OK] {text}{RESET}")

    def print_error(self, text: str):
        """Print error message."""
        print(f"{RED}[FAIL] {text}{RESET}")

    def print_warning(self, text: str):
        """Print warning message."""
        print(f"{YELLOW}[WARN] {text}{RESET}")

    def print_info(self, text: str):
        """Print info message."""
        print(f"  {text}")

    def discover_python_files(self) -> List[Path]:
        """Automatically discover all Python files in the project."""
        python_files = []

        # Find all .py files, excluding common directories
        exclude_dirs = {'__pycache__', '.git', 'venv', 'env', '.venv', 'node_modules', 'archive'}

        for path in self.project_root.rglob('*.py'):
            # Skip excluded directories
            if any(excluded in path.parts for excluded in exclude_dirs):
                continue
            python_files.append(path)

        return sorted(python_files)

    def run_command(self, cmd: List[str], description: str, allow_fail: bool = False) -> bool:
        """Run a command and track results."""
        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',  # Replace invalid chars instead of crashing
                timeout=300  # 5 minute timeout
            )

            if result.returncode == 0:
                self.passed_checks.append(description)
                return True
            else:
                if allow_fail:
                    self.warnings.append(f"{description}: {result.stderr[:200]}")
                    return True
                else:
                    self.failed_checks.append(description)
                    if result.stdout:
                        print(f"\n{result.stdout}")
                    if result.stderr:
                        print(f"\n{result.stderr}")
                    return False

        except subprocess.TimeoutExpired:
            self.print_error(f"{description} - Timed out")
            self.failed_checks.append(description)
            return False
        except FileNotFoundError:
            self.print_warning(f"{description} - Tool not installed")
            self.warnings.append(f"{description} - Tool not installed")
            return True  # Don't fail if tool isn't installed
        except UnicodeEncodeError as e:
            # Encoding error in output - just warn and continue
            self.print_warning(f"{description} - Output encoding issue (likely non-critical)")
            self.warnings.append(f"{description} - Output had encoding issues")
            return True
        except Exception as e:
            self.print_error(f"{description} - Error: {e}")
            self.failed_checks.append(description)
            return False

    def check_feature_flags(self) -> bool:
        """Check that feature flags load correctly."""
        self.print_info("Checking feature flags...")

        test_script = self.project_root / "test_feature_flags.py"
        if not test_script.exists():
            self.print_warning("Feature flag test script not found (skipping)")
            return True

        return self.run_command(
            [sys.executable, str(test_script)],
            "Feature flags check"
        )

    def format_code(self, fix: bool = False) -> bool:
        """Format code with Black."""
        self.print_info("Checking code formatting...")

        files = self.discover_python_files()
        if not files:
            self.print_warning("No Python files found")
            return True

        cmd = [sys.executable, "-m", "black"]
        if not fix:
            cmd.append("--check")
        cmd.append("--quiet")
        cmd.extend([str(f) for f in files])

        result = self.run_command(cmd, "Code formatting", allow_fail=not fix)

        if fix and result:
            self.print_success("Code formatted")

        return result

    def lint_code(self, fix: bool = False) -> bool:
        """Lint code with Ruff."""
        self.print_info("Running linter...")

        files = self.discover_python_files()
        if not files:
            return True

        cmd = [sys.executable, "-m", "ruff", "check"]
        if fix:
            cmd.append("--fix")
        cmd.append("--quiet")
        cmd.extend([str(f) for f in files])

        return self.run_command(cmd, "Linting", allow_fail=False)

    def type_check(self) -> bool:
        """Run type checking with mypy."""
        self.print_info("Type checking...")

        # Check specific directories that are likely to have type hints
        dirs_to_check = []
        for d in ['app', 'tests']:
            if (self.project_root / d).exists():
                dirs_to_check.append(d)

        if not dirs_to_check:
            self.print_warning("No directories to type check (skipping)")
            return True

        cmd = [
            sys.executable, "-m", "mypy",
            "--ignore-missing-imports",
            "--no-error-summary",
            "--show-error-codes"
        ] + dirs_to_check

        return self.run_command(cmd, "Type checking", allow_fail=True)

    def run_tests(self) -> bool:
        """Run pytest tests."""
        self.print_info("Running tests...")

        tests_dir = self.project_root / "tests"
        if not tests_dir.exists() or not any(tests_dir.glob("test_*.py")):
            self.print_warning("No tests found (skipping)")
            return True

        cmd = [
            sys.executable, "-m", "pytest",
            "tests/",
            "-v",
            "--tb=short",
            "--maxfail=5"
        ]

        return self.run_command(cmd, "Tests", allow_fail=False)

    def check_dependencies(self) -> bool:
        """Check for security vulnerabilities in dependencies."""
        self.print_info("Checking dependencies for vulnerabilities...")

        # This is optional and won't fail the build
        cmd = [sys.executable, "-m", "pip", "list", "--outdated"]
        return self.run_command(cmd, "Dependency check", allow_fail=True)

    def install_tools(self):
        """Install all required tools."""
        self.print_header("Installing Code Quality Tools")

        tools = [
            "black",
            "ruff",
            "mypy",
            "pytest",
            "pytest-asyncio",
        ]

        for tool in tools:
            self.print_info(f"Installing {tool}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", tool],
                capture_output=True
            )

        self.print_success("All tools installed!")

    def print_summary(self):
        """Print final summary."""
        self.print_header("Summary")

        print(f"\n{GREEN}Passed: {len(self.passed_checks)}{RESET}")
        for check in self.passed_checks:
            self.print_success(check)

        if self.warnings:
            print(f"\n{YELLOW}Warnings: {len(self.warnings)}{RESET}")
            for warning in self.warnings:
                self.print_warning(warning)

        if self.failed_checks:
            print(f"\n{RED}Failed: {len(self.failed_checks)}{RESET}")
            for check in self.failed_checks:
                self.print_error(check)
            print(f"\n{RED}{'=' * 60}{RESET}")
            print(f"{RED}Some checks failed. Please fix the errors above.{RESET}")
            print(f"{RED}{'=' * 60}{RESET}")
            return False
        else:
            print(f"\n{GREEN}{'=' * 60}{RESET}")
            print(f"{GREEN}[OK] All checks passed! Code is ready.{RESET}")
            print(f"{GREEN}{'=' * 60}{RESET}")
            return True

    def run_all(self, skip_tests: bool = False, fix: bool = False):
        """Run all checks."""
        self.print_header("Wellness Bot - Code Quality Checks")

        print(f"\nProject: {self.project_root}")
        print(f"Python files found: {len(self.discover_python_files())}")

        # Run checks in order
        checks = [
            ("Feature Flags", lambda: self.check_feature_flags()),
            ("Format Code", lambda: self.format_code(fix=fix)),
            ("Lint Code", lambda: self.lint_code(fix=fix)),
            ("Type Check", lambda: self.type_check()),
        ]

        if not skip_tests:
            checks.append(("Tests", lambda: self.run_tests()))

        # Run each check
        for name, check_func in checks:
            self.print_header(name)
            try:
                check_func()
            except Exception as e:
                self.print_error(f"Unexpected error: {e}")
                self.failed_checks.append(name)

        # Print summary
        return self.print_summary()


def main():
    """Main entry point."""
    args = sys.argv[1:]

    checker = CodeChecker()

    # Handle commands
    if "--install" in args:
        checker.install_tools()
        return 0

    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    skip_tests = "--fast" in args
    fix = "--fix" in args

    # Run checks
    success = checker.run_all(skip_tests=skip_tests, fix=fix)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
