# Contributing

## Local setup

1. Create and activate a Python 3.12 virtual environment.
2. Install development dependencies with `python -m pip install -r requirements-dev.txt`.
3. Copy `.env.example` to `.env` and provide your own Discord credentials.
4. Never commit `.env`, database files, tokens, or personal data.

## Checks

Run these commands before opening a pull request:

```bash
ruff check courtbot tests run.py
ruff format --check courtbot tests run.py
bandit -q -r courtbot run.py
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q courtbot run.py
python -m pip check
pip-audit -r requirements.txt
```

Keep template variables synchronized with `templates/manifest.json` and add or update tests for every behavior change.
