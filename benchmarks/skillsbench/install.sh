#!/bin/sh
set -eu

# Runs during BenchFlow provisioning as root, before the solver user is launched.
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl python3 python3-venv
# Tasks can ship Python 3.8 (Ubuntu 20.04). Keep the agent interpreter independent
# of the task's Python and libraries, using a managed runtime accessible to agent.
python3 -m venv /opt/rcodex-bootstrap
/opt/rcodex-bootstrap/bin/pip install 'uv==0.11.29'
export UV_PYTHON_INSTALL_DIR=/opt/rcodex-python
/opt/rcodex-bootstrap/bin/uv venv --python 3.12 --python-preference only-managed /opt/rcodex-venv
/opt/rcodex-bootstrap/bin/uv pip install --python /opt/rcodex-venv/bin/python \
  'openai-codex==0.147.0' 'pydantic==2.13.5' 'python-dotenv==1.2.3' 'agent-client-protocol==0.12.1'
case "$(uname -m)" in
  aarch64|arm64) target=aarch64-unknown-linux-musl ;;
  x86_64) target=x86_64-unknown-linux-musl ;;
  *) echo 'Unsupported Codex architecture' >&2; exit 1 ;;
esac
curl -fL --retry 3 "https://github.com/openai/codex/releases/download/rust-v0.153.4/codex-${target}.tar.gz" -o /tmp/rcodex-cli.tar.gz
tar -xzf /tmp/rcodex-cli.tar.gz -C /opt/rcodex-venv/bin
mv "/opt/rcodex-venv/bin/codex-${target}" /opt/rcodex-venv/bin/codex
/opt/rcodex-venv/bin/codex --version
