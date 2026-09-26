#!/usr/bin/env bash
# One persistent Claude session in tmux window ig:agent. If Claude exits it comes straight back,
# so the plugin always has a tab to deliver into. The container lives as long as the session.
set -eu
# First boot: skip onboarding and the trust / bypass prompts, which would block the tab forever.
[ -f ~/.claude.json ] || cat > ~/.claude.json <<EOF
{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true,
 "projects": {"/home/node": {"hasTrustDialogAccepted": true}}}
EOF
tmux new-session -d -s ig -n agent -x 200 -y 50 \
  "while true; do claude --dangerously-skip-permissions --model '${IG_AGENT_MODEL:-claude-opus-5-5}'; sleep 2; done"
exec tmux wait-for ig-agent-stop
