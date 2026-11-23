# co2watch container: CLI for calibration, scoring, training, labelling.
# Multi-arch (amd64 for lap1, arm64 for pi1 / RK1); see .github/workflows/image.yml
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
# sqlite3 CLI for inspecting the database from the toolbox pod
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin co2watch
USER 10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Amsterdam
WORKDIR /data
ENTRYPOINT ["co2watch"]
CMD ["--help"]
