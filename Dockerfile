FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && addgroup --system courtbot \
    && adduser --system --ingroup courtbot --home /app courtbot

COPY --chown=courtbot:courtbot . .
RUN mkdir -p /app/data && chown courtbot:courtbot /app/data

USER courtbot

CMD ["python", "run.py"]
