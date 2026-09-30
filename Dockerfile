# Dev convenience only. There is no cairn server to deploy: the pipeline
# writes a log and renders static HTML. This image exists so contributors do
# not need a matching Python on the host.
FROM python:3.14-slim

ENV POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir poetry

# Dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --all-extras

COPY . .
RUN poetry install --all-extras

ENTRYPOINT ["cairn"]
CMD ["--help"]
