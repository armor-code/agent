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
    /usr/src/venv/bin/pip install --upgrade gevent && \
    /usr/src/venv/bin/pip install --upgrade urllib3 && \
    apk --update --no-cache upgrade openssl libssl3 libcrypto3 && \
    rm -rf /var/lib/apt/lists/* && \
    apk del py3-pip && \
    rm -rf /usr/lib/python3.12/ensurepip /usr/src/venv/bin/pip /usr/src/venv/bin/pip3.12 /usr/src/venv/bin/pip3 /usr/src/venv/lib/python3.12/site-packages/pip-24.2.dist-info/ /usr/src/venv/lib/python3.12/site-packages/pip/



RUN addgroup -g 1001 appgroup && adduser -D -u 1001 -G appgroup 1001


WORKDIR /usr/src/app

COPY entrypoint.sh /usr/src/entrypoint.sh
RUN chmod +x /usr/src/entrypoint.sh

COPY app/* ./

RUN chmod +x /usr/src/app/worker.py && \
    mkdir -p /tmp/armorcode/ && \
    mkdir -p /tmp/armorcode/log/ && \
    chown -R 1001:1001 /usr/src /tmp/armorcode

USER 1001

ENTRYPOINT ["/usr/src/entrypoint.sh"]

CMD []