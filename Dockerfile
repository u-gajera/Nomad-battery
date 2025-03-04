#
# Copyright The NOMAD Authors.
#
# This file is part of NOMAD. See https://nomad-lab.eu for further info.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# This dockerfile describes an image that can be used to run the
# - nomad processing worker
# - nomad app (incl serving the gui)

# The dockerfile is multistaged to use a fat, more convinient build image and
# copy only necessities to a slim final image


FROM node:16.15 AS base_node
FROM python:3.9-slim AS base_python


# ================================================================================
# Built the GUI in the gui build image
# ================================================================================

FROM base_node AS dev_node

WORKDIR /app/gui

ENV PATH /app/node_modules/.bin:$PATH
ENV NODE_OPTIONS "--max_old_space_size=4096"

# Fetch and cache all (but only) the dependencies
COPY gui/yarn.lock gui/package.json ./
COPY gui/materia ./materia
COPY gui/crystcif-parse ./crystcif-parse

RUN yarn --network-timeout 1200000

# Artifact for running the tests
COPY tests/states/archives/dft.json  /app/tests/states/archives/dft.json

# Copy and build the applicaion itself
COPY gui .
RUN echo "REACT_APP_BACKEND_URL=/fairdi/nomad/latest" > .env

RUN yarn run build


# ================================================================================
# Build all python stuff in a python build image
# ================================================================================

FROM base_python AS dev_python

# Linux applications and libraries
RUN apt-get update \
 && apt-get install --yes --quiet --no-install-recommends \
      libgomp1 \
      libmagic-dev \
      curl \
      gcc \
      build-essential \
      make \
      cmake \
      swig \
      libnetcdf-dev \
      zip \
      vim \
      git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1

# Python environment
COPY requirements-dev.txt .

RUN pip install build \
 && pip install --progress-bar off --prefer-binary -r requirements-dev.txt

COPY dependencies ./dependencies
COPY docs ./docs
COPY examples ./examples
COPY nomad ./nomad
COPY scripts ./scripts
COPY tests ./tests
COPY .coveragerc \
     AUTHORS \
     LICENSE \
     MANIFEST.in \
     mkdocs.yml \
     pyproject.toml \
     pytest.ini \
     README.md \
     README.parsers.md \
     requirements.txt \
     setup.py \
     ./

# Files requiered for artifact generation/testing
COPY ops/docker-compose ./ops/docker-compose

COPY gui/tests/nomad.yaml ./gui/tests/nomad.yaml
COPY gui/tests/env.js ./gui/tests/env.js
COPY gui/tests/artifacts.js ./gui/tests/artifacts.js

# build the example upload files
RUN ./scripts/generate_example_uploads.sh

# Copy the built gui code
COPY --from=dev_node /app/gui/build nomad/app/static/gui

# Set up the version as a build argument (default: '0.0')
ARG SETUPTOOLS_SCM_PRETEND_VERSION='0.0'

# Build documentation

# This is a temporary workaround because atomisticparsers installs an older version
# of nomad-lab via pip install git+...still containing pynxtools as a submodule
RUN pip uninstall -y pynxtools
RUN pip install ".[parsing,infrastructure,dev]"

RUN ./scripts/generate_docs_artifacts.sh \
 && mkdocs build \
 && mkdir -p nomad/app/static/docs \
 && cp -r site/* nomad/app/static/docs

# Build the python source distribution package
RUN python -m build --sdist

# (Re)install the full packages docs included
RUN pip install dist/nomad-lab-*.tar.gz


# ================================================================================
# We use slim for the final image
# ================================================================================

FROM base_python AS builder

RUN apt-get update \
 && apt-get install --yes --quiet --no-install-recommends \
      libgomp1 \
      libmagic1 \
      file \
      gcc \
      build-essential \
      curl \
      zip \
      unzip \
      git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1

# Python environment
COPY requirements.txt .

RUN pip install --progress-bar off --prefer-binary -r requirements.txt

# install
COPY --from=dev_python /app/dist/nomad-lab-*.tar.gz .
RUN pip install nomad-lab-*.tar.gz
# This is a temporary workaround because atomisticparsers installs an older version
# of nomad-lab via pip install git+...still containing pynxtools as a submodule.
RUN pip uninstall -y pynxtools
RUN pip install pynxtools[convert]
# This is a temporary workaround because pynxtools installs an incompatible
# version of h5grove
RUN pip uninstall -y h5grove
RUN pip install h5grove[fastapi]==1.3.0

# Reduce the size of the packages
RUN find /usr/local/lib/python3.9/ -type d -name 'tests' ! -path '*/networkx/*' -exec rm -r '{}' + \
 && find /usr/local/lib/python3.9/ -type d -name 'test' -exec rm -r '{}' + \
 && find /usr/local/lib/python3.9/site-packages/ -name '*.so' ! -path '*/h5py/*' ! -path '*/quippy*/*' -print -exec sh -c 'file "{}" | grep -q "not stripped" && strip -s "{}"' \;


# ================================================================================
# We use slim for the final image
# ================================================================================

FROM base_python AS final

RUN apt-get update \
 && apt-get install --yes --quiet --no-install-recommends \
       libgomp1 \
       libmagic1 \
       curl \
       zip \
       unzip \
 && curl -fsSL https://deb.nodesource.com/setup_16.x | bash - \
 && apt-get install --yes --quiet --no-install-recommends \
       nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g configurable-http-proxy \
 && npm uninstall -g npm

WORKDIR /app

# transfer installed packages from the build stage
COPY --chown=nomad:1000 scripts/run.sh .
COPY --chown=nomad:1000 nomad/jupyterhub_config.py ./nomad/jupyterhub_config.py

COPY --chown=nomad:1000 --from=dev_python /app/examples/data/uploads /app/examples/data/uploads
COPY --chown=nomad:1000 --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --chown=nomad:1000 --from=builder /usr/local/share/jupyterhub /usr/local/share/jupyterhub
COPY --chown=nomad:1000 --from=builder /usr/local/bin/nomad /usr/local/bin/nomad

RUN useradd -ms /bin/bash nomad \
 && mkdir -p /app/.volumes/fs \
 && chown -R nomad:1000 /app \
 && chown -R nomad:1000 /usr/local/lib/python3.9/site-packages/nomad

USER nomad

# The application ports
EXPOSE 8000
EXPOSE 9000

ENV PYTHONPATH=/app/plugins

VOLUME /app/.volumes/fs
