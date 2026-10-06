FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents@sha256:efe3b0fdf97601c8a1b14b3faf31d0c919116c40687c5252cf501e5f6ff19898

ENV AGENT_ID=puppeteer \
    AGENT_NAME=Puppeteer \
    AGENT_BLURB="Talk to the Claude Code and Codex agents already running on your Mac."

LABEL org.opencontainers.image.title="Puppeteer" \
      org.opencontainers.image.description="Talk to the Claude Code and Codex agents already running on your Mac." \
      org.opencontainers.image.source="https://github.com/EnzoTironi/mypeople" \
      org.opencontainers.image.licenses="MIT"

COPY LICENSE /usr/share/licenses/puppeteer/LICENSE
COPY --chown=0:0 persona.md /opt/hermes/plow-seed/persona.md
RUN chmod 0644 /opt/hermes/plow-seed/persona.md
COPY --chown=10000:10000 skills/ /var/lib/hermes/skills/
COPY --chown=10000:10000 skills/ /opt/hermes/skills/
