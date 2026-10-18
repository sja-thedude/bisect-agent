# Bisect sandbox image. Every test run is an ephemeral container from this image.
# Build: docker build -t bisect-sandbox:py3.11 --build-arg PY=3.11 sandbox/
ARG PY=3.11
FROM python:${PY}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

# Build backends are preinstalled so dependency metadata can be read with
# --no-build-isolation and no network. pytest is the only runner we support.
RUN pip install \
      "pytest>=7,<9" pytest-timeout \
      setuptools wheel hatchling hatch-vcs flit_core poetry-core pdm-backend \
      setuptools_scm packaging

RUN useradd --uid 10001 --create-home runner
COPY tools/ /opt/bisect/
RUN chmod -R a+rX /opt/bisect
USER runner
WORKDIR /work
