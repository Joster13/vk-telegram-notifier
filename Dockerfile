# MAX client is pulled ONLY at build time and pinned to a reviewed upstream commit.
FROM python:3.12-slim AS max-source
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG MAX2TG_COMMIT=4f95ff962f797bbea5cc8461384208d148e1d039
RUN git clone --quiet https://github.com/Aist/max2tg.git /vendor/max2tg \
    && git -C /vendor/max2tg checkout --quiet ${MAX2TG_COMMIT} \
    && test "$(git -C /vendor/max2tg rev-parse HEAD)" = "${MAX2TG_COMMIT}" \
    && rm -rf /vendor/max2tg/.git

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/vk-requirements.txt
COPY --from=max-source /vendor/max2tg/requirements.txt /tmp/max-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/vk-requirements.txt -r /tmp/max-requirements.txt
COPY --from=max-source /vendor/max2tg /opt/max2tg
COPY . /app
RUN python /app/patch_max_client.py /opt/max2tg
# Bothost may bind-mount Git sources over /app; keep entrypoint outside /app.
COPY entrypoint.sh /usr/local/bin/vk-max-entrypoint
RUN chmod 0755 /usr/local/bin/vk-max-entrypoint
RUN addgroup --system notifier \
    && adduser --system --ingroup notifier --home /app --no-create-home notifier \
    && mkdir -p /app/data /tmp/max2tg-logs \
    && chown -R notifier:notifier /app /tmp/max2tg-logs
ENTRYPOINT ["/usr/local/bin/vk-max-entrypoint"]
CMD ["python", "-u", "run_combined.py"]
