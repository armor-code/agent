FROM alpine:latest

RUN apk update && \
    apk upgrade && \
        apk add --no-cache autossh supervisor openssl && \
        rm -rf /var/cache/apk/* && \
        adduser -D appuser

RUN mkdir /etc/armorcode

COPY /* /etc/armorcode/

RUN chown -R appuser:appuser /etc/armorcode

RUN sed -i 's/user\s*=\s*root/user=appuser/g' /etc/armorcode/supervisord.conf

RUN touch /supervisord.log /supervisord.pid

RUN chown appuser:appuser /supervisord.log /supervisord.pid

USER appuser

ENTRYPOINT ["supervisord", "--nodaemon", "--configuration", "/etc/armorcode/supervisord.conf"]