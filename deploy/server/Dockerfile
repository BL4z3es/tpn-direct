# tpn-server: the control API. tailscaled itself runs in the sibling `tailscale` container
# (see docker-compose.yml); this image only needs the `tailscale` CLI to talk to its socket.
FROM tailscale/tailscale:stable AS tailscale

FROM python:3.12-slim
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["tpn"]
CMD ["serve"]
