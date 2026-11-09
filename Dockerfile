FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/edq
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml alembic.ini ./
COPY app app
COPY ui ui
COPY scripts scripts
COPY config config
COPY fixtures fixtures
COPY migrations migrations
RUN pip install --no-deps . && mkdir .runtime && chgrp -R 0 /opt/edq && chmod -R g=u /opt/edq
USER 10001
EXPOSE 8000 8501
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
