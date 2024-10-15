1. Build image from the Agent folder, run below command
```
cd web-agent
docker build -t agent-armorcode-1 .
```

2. Run the docker as daemon process and mounting folder with private-key.pem and supervisord.conf file
```
docker run -e server_url='<server_url>' -e api_key='<api_key>'  -v /home/user1/agent:/temp --name armorcode-agent agent-armorcode-1
```

3. Check logs of the docker container
```
docker logs -f armorcode-agent
```