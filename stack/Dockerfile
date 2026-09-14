# syntax=docker/dockerfile:1
# One Dockerfile, two images: target `jupyterlab` (notebook server, also used by the `deps`
# package runner) and target `stats` (dashboard). The build context is the app dir, i.e. a
# copy of stack/; compose.yaml picks the target for each service.

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
 && pip freeze --all --no-cache-dir > /opt/constraints.txt
# /opt/constraints.txt: the deps runner installs custom packages with --constraint, so a
# package the image already has keeps its version and is not installed a second time.
# --all also pins pip: a requirement such as pip==25.1 would otherwise put another pip into
# the venv, and every later job would run that one.

# Kernels start through kernel_launcher.py, which appends the custom packages venv
# (the custom_packages volume) after the image's site-packages; PATH finds the venv's
# console scripts first. The server process never imports from the venv, so a broken user
# package cannot take JupyterLab down. This overwrites the plain spec shipped in the
# ipykernel wheel; -Xfrozen_modules=off is what `ipykernel install` writes (the debugger
# needs it). Importing IPython here (as root, HOME=/home/jupyter) leaves a root-owned
# ~/.ipython in the jupyter home; remove it.
RUN python -m ipykernel install --prefix /usr/local --name python3 \
        --env PATH /opt/custom/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
 && python -c 'import json, pathlib; path = pathlib.Path("/usr/local/share/jupyter/kernels/python3/kernel.json"); spec = json.loads(path.read_text()); spec["argv"] = ["/usr/local/bin/python", "-Xfrozen_modules=off", "/srv/jupyter/kernel_launcher.py", "-f", "{connection_file}"]; path.write_text(json.dumps(spec, indent=1) + "\n")' \
 && rm -rf /home/jupyter/.ipython

# Symbolic mode on purpose: an octal --chmod=0644 is also applied to the parent
# directories COPY creates, leaving /srv/jupyter without the search bit. This gives
# directories 755 and the files 644 whatever their mode in the app dir.
COPY --chmod=u=rwX,go=rX jupyter/jupyter_server_config.py /srv/jupyter/jupyter_server_config.py
COPY --chmod=u=rwX,go=rX jupyter/kernel_launcher.py jupyter/deps_runner.py jupyter/deps_healthcheck.py /srv/jupyter/

# Mount points owned by the container user: Docker initialises an empty named volume
# with the ownership of the image directory it is mounted over (custom_packages at
# /opt/custom, pip_cache at /var/cache/pip in the deps container).
RUN mkdir -p /state /opt/custom /var/cache/pip /workspace \
 && chown jupyter:jupyter /state /opt/custom /var/cache/pip /workspace

# Terminals start $SHELL (else sh) as a login shell.
ENV SHELL=/bin/bash
USER jupyter
# jupyter silently skips a --config file it cannot read and starts with defaults (token
# login, localhost only) while GET /api still looks healthy; a kernelspec pointing at an
# unreadable launcher only fails when a notebook opens. Fail the build instead.
RUN test -r /srv/jupyter/jupyter_server_config.py \
 && test -r /srv/jupyter/kernel_launcher.py \
 && test -r /srv/jupyter/deps_runner.py \
 && test -r /srv/jupyter/deps_healthcheck.py \
 && test -w /opt/custom && test -w /var/cache/pip \
 && python -c 'import json; spec = json.load(open("/usr/local/share/jupyter/kernels/python3/kernel.json")); open(spec["argv"][2]).close(); assert "PYTHONPATH" not in spec["env"]'
WORKDIR /workspace
EXPOSE 8888
CMD ["jupyter", "lab", "--config=/srv/jupyter/jupyter_server_config.py"]


FROM base AS stats

RUN --mount=type=bind,source=stats/requirements.lock.txt,target=/tmp/requirements.lock.txt \
    pip install --no-cache-dir --only-binary=:all: --root-user-action=ignore \
        -r /tmp/requirements.lock.txt

COPY stats/*.py /srv/stats/
COPY stats/templates/ /srv/stats/templates/
COPY stats/static/ /srv/stats/static/

# The COPY lines above keep the modes the files have in the app dir; make the app
# readable by the non-root user regardless (dirs 755, files 644, executables stay
# executable).
RUN chmod -R u=rwX,go=rX /srv/stats

# zenobia's theme files. THEME in compose.yaml picks one at start-up, so switching themes only
# recreates the container. Symbolic mode for the same reason as for /srv/jupyter above.
COPY --chmod=u=rwX,go=rX theme/ /srv/stats/theme/

USER jupyter
# The app reads its templates and the theme once at start-up, and an unreadable theme file only
# shows up as the fallback colours. Fail the build instead.
RUN test -r /srv/stats/app.py && test -r /srv/stats/theme.py && test -r /srv/stats/templates/base.html \
 && test -r /srv/stats/static/oya.css && test -r /srv/stats/theme/amazing.json \
 && for file in /srv/stats/theme/*.json; do test -r "$file" || exit 1; done
WORKDIR /srv/stats
EXPOSE 8889
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8889", "--no-server-header"]
