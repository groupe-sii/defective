# Conformity assistant agent — AgentCore Runtime container image.
# Adapted from patterns/strands-single-agent/Dockerfile.
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Configure UV for container environment
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    DOCKER_CONTAINER=1 \
    OTEL_PYTHON_LOG_CORRELATION=true \
    PYTHONUNBUFFERED=1

# Copy pyproject.toml and shared packages for installation
COPY pyproject.toml .
COPY gateway/ gateway/
COPY tools/ agentcore_tools/

# Copy and install agent-specific requirements first
COPY patterns/conformity-agent/requirements.txt requirements.txt
RUN uv pip install --no-cache -r requirements.txt && \
    uv pip install --no-cache aws-opentelemetry-distro==0.16.0

# Install FAST package with only core dependencies (no dev/agent optional deps)
RUN uv pip install --no-cache -e . --no-deps && \
    uv pip install --no-cache requests>=2.31.0

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

# Copy agent code and tools.
# NOTE: unlike the FAST baseline, this pattern ships its OWN utils/ package
# (locales, contacts, ssm, auth). It is copied AFTER patterns/utils/ so that
# the agent's own modules win; both are on the import path as `utils.*`.
COPY patterns/conformity-agent/basic_agent.py .
COPY patterns/conformity-agent/tools/ tools/
COPY patterns/utils/ utils/
COPY patterns/conformity-agent/utils/ utils/

# Healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

# Start agent with OpenTelemetry instrumentation
CMD ["opentelemetry-instrument", "python", "-m", "basic_agent"]
