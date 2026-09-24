# Optional deploy skeleton — SETUP.md asks whether to keep or delete this.
# Pin the uv version to the one used in local dev (uv --version) for reproducible builds.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# Dependency layer first so code-only changes don't invalidate it.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# config/ and .env are deliberately not copied in: runtime config is bind-mounted
# read-only and secrets come from env_file, so neither ever lands in the image.
# Add one COPY line per source module as they land (per docs/DESIGN.md §4.2):
COPY main.py ./
RUN uv sync --locked --no-install-project --no-dev

RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app
USER app

# Plain python, not `uv run`: signal handlers (SIGTERM/SIGINT) only fire reliably when
# this process is PID 1 itself. `init: true` in docker-compose.yml is the backstop.
ENTRYPOINT ["python", "main.py"]
