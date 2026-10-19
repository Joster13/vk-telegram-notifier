# Pin the reviewed MAX adapter in the same stage for Bothost's legacy Docker builder.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git gosu ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ARG MAX2TG_COMMIT=4f95ff962f797bbea5cc8461384208d148e1d039
COPY requirements.txt /tmp/vk-requirements.txt
RUN git clone --quiet https://github.com/Aist/max2tg.git /opt/max2tg \
    && git -C /opt/max2tg checkout --quiet "${MAX2TG_COMMIT}" \
    && test "$(git -C /opt/max2tg rev-parse HEAD)" = "${MAX2TG_COMMIT}" \
    && python -m pip install --no-cache-dir -r /tmp/vk-requirements.txt -r /opt/max2tg/requirements.txt \
    && rm -rf /opt/max2tg/.git
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
