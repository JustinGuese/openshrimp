FROM python:3.13-slim

RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./

ENV UV_SYSTEM_PYTHON=1
RUN uv sync --no-dev --frozen

COPY src/ ./src/

CMD ["python", "telegram_bot.py"]