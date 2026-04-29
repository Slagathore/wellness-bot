# Testing and Validation Guide

## Install Dependencies
```bash
pip install -r requirements.txt
```

## Run Entire Test Suite
```bash
pytest -q
```

## Run Targeted Tests
```bash
pytest tests/test_e2e_reminders.py -q
pytest tests/test_e2e_rate_limit.py -q
pytest tests/test_e2e_context.py -q
pytest tests/test_e2e_onboarding.py -q
pytest tests/test_e2e_crisis.py -q
pytest tests/test_e2e_sleep_upload.py -q
```

## Manual QA Checklist
1. Send a Telegram message to confirm polling ingestion and LLM response.
2. Complete onboarding flow and verify hydration reminder creation.
3. Trigger a reminder and validate natural-language injection.
4. Upload sample sleep data and confirm server_event creation.
5. Run `python scripts/bootstrap.py --create-admin <TELEGRAM_USER_ID>` for admin seeding (once).
6. Review logs via `journalctl -u telegram-consumer -f` and check for errors.
