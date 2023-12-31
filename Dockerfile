# Based on OpenAI's mujoco-py Dockerfile

# base stage contains just binary dependencies.
# This is used in the CI build.
FROM nvidia/cuda:11.5.2-cudnn8-runtime-ubuntu20.04 AS base
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update -q \
    && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    nano \
    git \
    libgl1-mesa-dev \
    libgl1-mesa-glx \
    libglew-dev \
    libosmesa6-dev \
    net-tools \
    parallel \
    python3.8 \
    python3.8-dev \
    python3-pip \
    rsync \
    software-properties-common \
    pciutils \
    tar \
    vim \
    virtualenv \
    wget \
    xpra \
    xserver-xorg-dev \
    patchelf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV LANG C.UTF-8

#RUN    mkdir -p /root/.mujoco \
#    && wget https://github.com/deepmind/mujoco/releases/download/2.1.0/mujoco210-linux-x86_64.tar.gz \
#    && tar --no-same-owner -xzvf mujoco210-linux-x86_64.tar.gz \
#    && mv mujoco210 /root/.mujoco/mujoco210 \
#    && rm mujoco210-linux-x86_64.tar.gz
# Set the PATH to the venv before we create the venv, so it's visible in base.
# This is since we may create the venv outside of Docker, e.g. in CI
# or by binding it in for local development.


ENV PATH="/venv/bin:$PATH"
#ENV LD_LIBRARY_PATH /usr/local/nvidia/lib64:/root/.mujoco/mujoco210/bin:${LD_LIBRARY_PATH}

# Run Xdummy mock X server by default so that rendering will work.
COPY ci/xorg.conf /etc/dummy_xorg.conf
COPY ci/Xdummy-entrypoint.py /usr/bin/Xdummy-entrypoint.py
ENTRYPOINT ["/usr/bin/python3", "/usr/bin/Xdummy-entrypoint.py"]

# python-req stage contains Python venv, but not code.
# It is useful for development purposes: you can mount
# code from outside the Docker container.
FROM base as python-req

# Use this bash as default, as aour scripts throw permission denied otherwise.
#SHELL ["/usr/bin/env", "bash"]
RUN pip install protobuf==3.20.1
WORKDIR /adversarial-policy-defense/
# Copy over just setup.py and dependencies (__init__.py and README.md)
# to avoid rebuilding venv when requirements have not changed.
COPY ./setup.py ./setup.py
COPY ./README.md ./README.md
COPY ./requirements.txt /adversarial-policy-defense/
COPY ci/build_and_activate_venv.sh ./ci/build_and_activate_venv.sh
RUN  /usr/bin/env bash ci/build_and_activate_venv.sh /venv \
    && rm -rf $HOME/.cache/pip

# For some reason the option in requirements.txt is not enough, so accept the license manually here.
# ONLY BUILD THIS DOCKERFILE IF YOU OWN THE RESPECTIVE ROMS / onw a license to use them.
CMD AutoROM --accept-license

# Installing our modification to ray. The wheel is already built with the requirements.txt. Changes to RLLib are possible without
# requiring a build and compile of ray.
COPY ci/install_custom_ray.sh ./ci/install_custom_ray.sh
RUN /usr/bin/env bash ci/install_custom_ray.sh


# full stage contains everything.
# Can be used for deployment and local testing.
FROM python-req as full

# Delay copying (and installing) the code until the very end
COPY . /adversarial-policy-defense
# Build a wheel then install to avoid copying whole directory (pip issue #2195)
RUN python3 setup.py sdist bdist_wheel
RUN pip install --upgrade dist/aprl_defense-*.whl

# So the entrypoint has the same workdir as when running from commandline without docker
WORKDIR /adversarial-policy-defense/src/
RUN mkdir /adversarial-policy-defense/ray/dashboard/client/build
RUN mkdir /adversarial-policy-defense/ray/dashboard/client/build/static
CMD cd src/

# Default entrypoints
CMD echo "Hello World"
#ENTRYPOINT ["tail"]
#CMD ["-f","/dev/null"]
CMD ["python", "-m", "aprl_defense.train", "-f", "gin/icml/selfplay/laser_tag.gin", "-p", "TrialSettings.num_workers = 10", "-p", "TrialSettings.wandb_group = 'experiment'"]