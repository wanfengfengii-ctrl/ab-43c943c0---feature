FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /srv

# Standard library only: no dependency installation step is required.
COPY app ./app
COPY tests ./tests
COPY verify ./verify

EXPOSE 8000

CMD ["python", "-m", "app.server"]
