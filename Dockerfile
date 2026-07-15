FROM python:3.11-slim

# Install Node.js 20 (ACP CLI requires Node >= 20) + system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install ACP CLI globally
RUN npm install -g @virtuals-protocol/acp-cli

# Create app directory
WORKDIR /app

# Copy provider code
COPY provider.py .
COPY startup.sh .

RUN chmod +x startup.sh

# Health check — if the provider process dies, Render restarts it
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD pgrep -f "python3 provider.py" > /dev/null || exit 1

CMD ["bash", "startup.sh"]
