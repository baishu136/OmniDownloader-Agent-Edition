FROM denoland/deno:bin-2.9.8 AS deno
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 omni
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY agent-edition/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY windows/core /app/windows/core
COPY windows/engine /app/windows/engine
COPY LICENSE UPSTREAM.md /app/
COPY windows/licenses /app/licenses
COPY agent-edition /app/agent-edition
RUN mkdir -p /data /cache /home/omni/Downloads/OmniDownloader && chown -R omni:omni /data /cache /app /home/omni
ENV PYTHONPATH=/app/agent-edition PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMNI_AGENT_DATA=/data OMNI_AGENT_CACHE=/cache OMNI_AGENT_HOST=0.0.0.0 \
    DENO_DIR=/tmp/deno XDG_CACHE_HOME=/tmp/cache
USER omni
EXPOSE 58080
VOLUME ["/data", "/cache"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:58080/health', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "omni_agent"]
CMD ["--serve", "--no-browser"]
