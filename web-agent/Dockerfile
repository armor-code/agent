# Use an official Python runtime as a parent image
FROM alpine:edge

# Set the working directory in the container
WORKDIR /usr/src

# Install supervisor
RUN apk update && \
    apk upgrade && \
    apk --update --no-cache add supervisor py3-pip && \
    python3 -m venv /usr/src/venv && \
    /usr/src/venv/bin/pip install --upgrade requests && \
    apk --update --no-cache upgrade openssl libssl3 libcrypto3 && \
    rm -rf /var/lib/apt/lists/* && \
    apk del py3-pip && \
    rm -rf /usr/lib/python3.12/ensurepip /usr/src/venv/bin/pip /usr/src/venv/bin/pip3.12 /usr/src/venv/bin/pip3 /usr/src/venv/lib/python3.12/site-packages/pip-24.2.dist-info/ /usr/src/venv/lib/python3.12/site-packages/pip/


RUN mkdir -p /etc/supervisor/conf.d/

RUN mkdir -p /temp/






# Copy the supervisor configuration file
COPY supervisord.conf.sample /etc/supervisor/conf.d/supervisord.conf

WORKDIR /usr/src/app

COPY app/* ./

# Make the Python script executable (optional)
RUN chmod +x /usr/src/app/worker.py

# Command to run supervisor
CMD ["/usr/bin/supervisord","-c","/etc/supervisor/conf.d/supervisord.conf"]