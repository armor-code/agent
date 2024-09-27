# Use an official Python runtime as a parent image
FROM alpine:edge

# Set the working directory in the container
WORKDIR /usr/src

# Install supervisor
RUN apk update && \
    apk upgrade && \
    apk --update --no-cache add py3-requests && \
    apk add supervisor && \
    rm -rf /var/lib/apt/lists/*

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