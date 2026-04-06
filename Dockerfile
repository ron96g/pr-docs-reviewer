FROM python:3.13-slim

# Install git (needed by LocalBackend) and gh CLI (needed for PR creation)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl jq && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml README.md ./
COPY pr_docs_reviewer/ ./pr_docs_reviewer/
RUN pip install --no-cache-dir .

# Copy the pipeline driver and entrypoint scripts
COPY run_pipeline.py /app/run_pipeline.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
