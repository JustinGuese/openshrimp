FROM python:3.13-slim

RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --frozen

ENV PATH="/app/.venv/bin:$PATH"

COPY src/ .

CMD ["python", "telegram_bot.py"]