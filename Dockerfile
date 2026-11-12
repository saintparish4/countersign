FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY countersign countersign
RUN pip install --no-cache-dir ".[agent]"
COPY agent agent
COPY evals evals
COPY tests tests
COPY scripts scripts