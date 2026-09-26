# The Instagram comment agent: one Claude session in tmux, in a box of its own.
# It answers public comments on its own, so anyone commenting can try to steer it. It gets no host
# mounts and nothing but the reply token; the plugin that owns it builds and starts it.
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends tmux curl python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

USER node
WORKDIR /home/node
COPY --chown=node CLAUDE.md persona.md ./
COPY --chown=node skills /home/node/.claude/skills
COPY --chown=node entrypoint.sh /home/node/entrypoint.sh
CMD ["bash", "/home/node/entrypoint.sh"]
