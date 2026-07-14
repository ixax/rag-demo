#!/bin/sh
# ollama-pull entrypoint -- pulls (and warms into memory) the Ollama models
# this stack needs. Extracted out of docker-compose.yml's inline `command:`
# so it can be edited/tested as a plain script instead of one-line shell
# glued into YAML. Bind-mounted into the ollama-pull service and run as its
# entrypoint (see docker-compose.yml).
#
# MODEL_INSTRUCT_INTERNAL is optional here, unlike MODEL_EMBED -- it's only
# needed when mcp-server's config.yml has backend.type: "ollama". A
# deployment running backend.type: anthropic_token/anthropic_subscription
# instead (see services/mcp_server/config.yml) has no local reasoning model
# to pull, so this script skips that step -- logging why -- instead of
# failing, keeping `make up`/`make pull-models` green either way.
set -eu

if [ -n "${MODEL_INSTRUCT_INTERNAL:-}" ]; then
    ollama pull "$MODEL_INSTRUCT_INTERNAL"
    # Load into memory now, not on the first real request -- see
    # OLLAMA_KEEP_ALIVE: "-1" on the ollama service in docker-compose.yml,
    # which then keeps it warm from here on.
    ollama run "$MODEL_INSTRUCT_INTERNAL" "hi"
else
    echo "ollama-pull: MODEL_INSTRUCT_INTERNAL is unset -- skipping the reasoning-model pull"
fi

ollama pull "$MODEL_EMBED"
# `ollama create`/`show` don't load weights, and there's no
# --hidethinking-style flag for embedding-only models -- a throwaway
# generate call is the only way to warm one into memory. `|| true`: some
# embedding models reject a plain chat-style prompt outright even though
# embedding calls work fine, and that shouldn't fail the whole pull.
ollama run "$MODEL_EMBED" "hi" || true
