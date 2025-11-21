FROM alpine:latest

RUN apk update && \
    apk upgrade && \
	apk add --no-cache autossh supervisor openssl && \
	rm -rf /var/cache/apk/*
RUN mkdir /etc/armorcode

RUN adduser -D appuser

RUN chown -R appuser /etc/armorcode

USER appuser

ENTRYPOINT ["supervisord", "--nodaemon", "--configuration", "/etc/armorcode/supervisord.conf"]