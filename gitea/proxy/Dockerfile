FROM gitea/gitea

# The Gitea image does not include python3; install it for the forwarder.
USER root
RUN apk add --no-cache python3

COPY forward.py /app/gitea-webhook-proxy/forward.py
RUN chmod +x /app/gitea-webhook-proxy/forward.py

# Return to the git user for runtime consistency with the upstream image.
USER git

ENV LISTEN_PORT=3030
ENV LISTEN_HOST=0.0.0.0
ENV FORWARD_TO=""

EXPOSE 3030

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', int('${LISTEN_PORT:-3030}'))); s.close()"

CMD ["python3", "/app/gitea-webhook-proxy/forward.py"]
