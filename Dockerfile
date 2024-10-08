FROM nvcr.io/nvidia/jax:24.04-py3

# Create user
ARG UID
ARG MYUSER
RUN useradd -u $UID --create-home ${MYUSER}
USER ${MYUSER}

# default workdir
WORKDIR /home/${MYUSER}/
COPY --chown=${MYUSER} --chmod=765 . .

USER root

# install tmux
RUN apt-get update && \
    apt-get install -y tmux

RUN pip install git+https://github.com/DramaCow/jaxued.git
#jaxmarl from source if needed, all the requirements
RUN pip install -e .[dev,baselines]

USER ${MYUSER}

#disabling preallocation
RUN export XLA_PYTHON_CLIENT_PREALLOCATE=false
#safety measures
RUN export XLA_PYTHON_CLIENT_MEM_FRACTION=0.25 
RUN export TF_FORCE_GPU_ALLOW_GROWTH=true

# Uncomment below if you want jupyter 
# RUN pip install jupyterlab

#for secrets and debug
ENV WANDB_API_KEY="5286b9ba66a087be8583f9436fe7d67c61f4cc8f"
ENV WANDB_ENTITY="alex-plus"
RUN git config --global --add safe.directory /home/${MYUSER}
