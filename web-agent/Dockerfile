# Use an official Python runtime as a parent image
FROM alpine:latest

# Set the working directory in the container
WORKDIR /usr/src

# Install dependencies
RUN apk update && \
    apk upgrade && \
    apk --update --no-cache add python3 py3-pip && \
    apk --update --no-cache upgrade openssl libssl3 libcrypto3 && \
    python3 -m venv /usr/src/venv && \
    /usr/src/venv/bin/pip install --upgrade pip && \
    /usr/src/venv/bin/pip install --no-cache-dir requests gevent urllib3 && \
    apk del py3-pip && \
    rm -rf /usr/src/venv/lib/python*/site-packages/pip* && \
    rm -rf /usr/src/venv/lib/python*/site-packages/setuptools* && \
    find /usr/src/venv/bin -name 'pip*' -delete



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