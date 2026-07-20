FROM python:3.11-slim

# Install Node.js 20 from official NodeSource deb repo (more reliable than setup_20.x script)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install ACP CLI globally
RUN npm install -g @virtuals-protocol/acp-cli

# Create app directory
WORKDIR /app

# Copy provider code + A2A/MCP/x402 layer
COPY provider.py .
COPY startup.sh .
COPY x402_flask.py .
COPY x402_server.py .
COPY a2a_agent_card.py .
COPY mcp_server.py .
COPY a2a_broadcast.py .
COPY requirements-x402.txt .

# Install Python deps for x402 server (includes gunicorn)
RUN pip install --no-cache-dir -r requirements-x402.txt

RUN chmod +x startup.sh

# Default: run as worker (provider.py event listener)
# The web service overrides CMD via render.yaml dockerCommand.
CMD ["bash", "startup.sh"]
