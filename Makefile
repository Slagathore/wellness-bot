# Convenience commands

install:
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt

test:
	pytest -q

test-e2e:
	pytest tests/test_e2e_reminders.py -q
	pytest tests/test_e2e_rate_limit.py -q
	pytest tests/test_e2e_onboarding.py -q

lint:
	python -m compileall app

run-runtime:
	python -m app.main_modular
