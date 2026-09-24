FROM python:3.12-bookworm@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd dbus && rm -rf /var/lib/apt/lists/*
RUN pip install --quiet pytest pydantic pyyaml httpx hatchling
RUN useradd --create-home --shell /usr/sbin/nologin steward
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
