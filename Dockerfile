FROM python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 aku \
    && useradd --uid 10001 --gid aku --no-create-home --shell /usr/sbin/nologin aku \
    && mkdir /data && chown aku:aku /data
COPY --chown=aku:aku akubot ./akubot
USER 10001:10001
ENTRYPOINT ["python", "-m", "akubot"]
CMD ["run"]

