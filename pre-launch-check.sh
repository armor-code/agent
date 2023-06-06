# check connectivity
timeout 5 bash -c "cat < /dev/null > /dev/tcp/$*/22"
if [ $? != 0 ]; then
    echo "FAILED connecting to server: $*"
else
    echo "PASSED connecting to server: $*"
fi

# check file presence

if [ ! -e private-key.pem ]; then
    echo "FAILED file check: private-key.pem"
else
    echo "PASSED file check: private-key.pem"
fi

if [ ! -e supervisord.conf ]; then
    echo "FAILED file check: supervisord.conf"
else
    echo "PASSED file check: supervisord.conf"
fi
