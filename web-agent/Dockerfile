# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /usr/src

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install supervisor
RUN apt-get update && apt-get install -y supervisor && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/supervisor/conf.d/

RUN mkdir -p /temp

RUN mkdir -p /var/log


# Copy the supervisor configuration file
COPY supervisord.conf.sample /etc/supervisor/conf.d/supervisord.conf

WORKDIR /usr/src/app

COPY app/* ./

# Make the Python script executable (optional)
RUN chmod +x /usr/src/app/worker.py

# Command to run supervisor
CMD ["/usr/bin/supervisord"]
