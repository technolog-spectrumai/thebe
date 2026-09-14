# syntax=docker/dockerfile:1
# One Dockerfile, two images: target `jupyterlab` (notebook server) and target `stats`
# (dashboard). The build context is the app dir, i.e. a copy of stack/; compose.yaml
# picks the target for each service.

ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS base

# The container user gets the desktop user's uid/gid so files in the /workspace bind
# mount belong to the same account on both sides.
ARG JLT_UID=1000
ARG JLT_GID=1000

# Lets the installer prune dangling images of this project by label.
LABEL org.opencontainers.image.vendor="jupyterlab-tailscale"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/jupyter

# --non-unique: the host gid (or uid) may already belong to an account in the base image,
# e.g. gid 100 "users"; the new names simply share the number. --no-log-init avoids a
# huge sparse /var/log/lastlog for large uids.
RUN groupadd --gid "${JLT_GID}" --non-unique jupyter \
 && useradd --uid "${JLT_UID}" --gid "${JLT_GID}" --non-unique --no-log-init \
        --create-home --home-dir /home/jupyter --shell /bin/bash jupyter


FROM base AS jupyterlab

# The lock is bind-mounted for this step only, so it adds no layer and root can read it
# whatever its mode in the app dir. --only-binary: the slim image has no compiler, so a
# missing wheel must fail the build instead of starting a source build.
RUN --mount=type=bind,source=jupyter/requirements.lock.txt,target=/tmp/requirements.lock.txt \
    pip install --no-cache-dir --only-binary=:all: --root-user-action=ignore \
        -r /tmp/requirements.lock.txt \
 && pip freeze --no-cache-dir > /opt/constraints.txt
# /opt/constraints.txt is copied into the stats image: user packages are installed there
# with --constraint, so shared dependencies keep the versions the notebooks already use.

# Only kernels see the shared custom_packages volume (PYTHONPATH/PATH in the kernelspec);
# the server process never imports from it, so a broken user package cannot take
# JupyterLab down. This overwrites the plain spec shipped in the ipykernel wheel.
# Importing IPython here (as root, HOME=/home/jupyter) leaves a root-owned ~/.ipython
# in the jupyter home; remove it.
RUN python -m ipykernel install --prefix /usr/local --name python3 \
        --env PYTHONPATH /opt/custom/site-packages \
        --env PATH /opt/custom/site-packages/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
 && rm -rf /home/jupyter/.ipython

# Symbolic mode on purpose: an octal --chmod=0644 is also applied to the parent
# directories COPY creates, leaving /srv/jupyter without the search bit. This gives
# directories 755 and the file 644 whatever its mode in the app dir.
COPY --chmod=u=rwX,go=rX jupyter/jupyter_server_config.py /srv/jupyter/jupyter_server_config.py

# Mount points owned by the container user: Docker initialises an empty named volume
# with the ownership of the image directory it is mounted over.
RUN mkdir -p /state /opt/custom/site-packages /workspace \
 && chown jupyter:jupyter /state /opt/custom /opt/custom/site-packages /workspace

# Terminals start $SHELL (else sh) as a login shell.
ENV SHELL=/bin/bash
USER jupyter
# jupyter silently skips a --config file it cannot read and starts with defaults (token
# login, localhost only) while GET /api still looks healthy; fail the build instead.
RUN test -r /srv/jupyter/jupyter_server_config.py
WORKDIR /workspace
EXPOSE 8888
CMD ["jupyter", "lab", "--config=/srv/jupyter/jupyter_server_config.py"]


FROM base AS stats

RUN --mount=type=bind,source=stats/requirements.lock.txt,target=/tmp/requirements.lock.txt \
    pip install --no-cache-dir --only-binary=:all: --root-user-action=ignore \
        -r /tmp/requirements.lock.txt

COPY --from=jupyterlab /opt/constraints.txt /opt/constraints.txt
COPY stats/*.py /srv/stats/
COPY stats/templates/ /srv/stats/templates/
COPY stats/static/ /srv/stats/static/

# The COPY lines above keep the modes the files have in the app dir; make the app
# readable by the non-root user regardless (dirs 755, files 644, executables stay
# executable). /var/cache/pip is the pip_cache volume; its tmp/ is TMPDIR for pip
# because the root filesystem is read-only and /tmp is a small tmpfs.
RUN chmod -R u=rwX,go=rX /srv/stats \
 && mkdir -p /opt/custom/site-packages /var/cache/pip/tmp \
 && chown jupyter:jupyter /opt/custom /opt/custom/site-packages /var/cache/pip /var/cache/pip/tmp

USER jupyter
WORKDIR /srv/stats
EXPOSE 8889
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8889", "--no-server-header"]
