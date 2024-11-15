# Use an official Python runtime as a parent image
FROM alpine:latest

# Set the working directory in the container
WORKDIR /usr/src

# Install supervisor
RUN apk update && \
    apk upgrade && \
    apk --update --no-cache add python3 py3-pip && \
    python3 -m venv /usr/src/venv && \
    /usr/src/venv/bin/pip install --upgrade requests && \
    apk --update --no-cache upgrade openssl libssl3 libcrypto3 && \
    rm -rf /var/lib/apt/lists/* && \
    apk del py3-pip && \
    rm -rf /usr/lib/python3.12/ensurepip /usr/src/venv/bin/pip /usr/src/venv/bin/pip3.12 /usr/src/venv/bin/pip3 /usr/src/venv/lib/python3.12/site-packages/pip-24.2.dist-info/ /usr/src/venv/lib/python3.12/site-packages/pip/


WORKDIR /usr/src/app

COPY app/* ./

# Make the Python script executable (optional)
RUN chmod +x /usr/src/app/worker.py

RUN mkdir -p /tmp/armorcode/

# Copy the start script for running worker.py
COPY start_all.sh /usr/src/app/start_all.sh
RUN chmod +x /usr/src/app/start_all.sh

# Run the start script on container launch
CMD ["sh", "/usr/src/app/start_all.sh"]











