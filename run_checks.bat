@echo off
REM Wellness Bot - Code Quality Checks
REM Run this before committing code changes

setlocal enabledelayedexpansion

echo ============================================
echo Wellness Bot - Code Quality Checks
echo ============================================
echo.

set FAILED=0

echo [1/5] Checking feature flags...
if exist test_feature_flags.py (
    python test_feature_flags.py
    if errorlevel 1 (
        echo   [FAIL] Feature flag check failed
        set FAILED=1
    ) else (
        echo   [OK] Feature flags OK
    )
) else (
    echo   [WARN] test_feature_flags.py missing ^(skipping^)
)
echo.

echo [2/5] Formatting code...
python -m black app/ tests/ --quiet
if errorlevel 1 (
    echo   [FAIL] Code formatting failed
    set FAILED=1
) else (
    echo   [OK] Code formatted
)
echo.

echo [3/5] Running linter...
python -m ruff check app/ tests/ --quiet
if errorlevel 1 (
    echo   [FAIL] Linting failed
    set FAILED=1
) else (
    echo   [OK] Linting passed
)
echo.

echo [4/5] Type checking...
python -m mypy app/ --ignore-missing-imports --no-error-summary 2>nul
if errorlevel 1 (
    echo   [WARN] Type check warnings ^(non-blocking^)
) else (
    echo   [OK] Type check passed
)
echo.

echo [5/5] Running tests...
if exist tests\ (
    python -m pytest tests/ -v --tb=short
    if errorlevel 1 (
        echo   [FAIL] Tests failed
        set FAILED=1
    ) else (
        echo   [OK] Tests passed
    )
) else (
    echo   [WARN] No tests directory found ^(skipping^)
)
echo.

echo ============================================
if !FAILED!==0 (
    echo [OK] All checks passed. Safe to commit.
    echo ============================================
    exit /b 0
) else (
    echo [FAIL] Some checks failed. Fix errors before committing.
    echo ============================================
    exit /b 1
)
