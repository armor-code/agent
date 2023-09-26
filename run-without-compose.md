
1. Build image from the Agent folder, run below command
```
docker build -t agent-armorcode-tunnel-1 .
```

2. Run the docker as daemon process and mounting folder with private-key.pem and supervisord.conf file
```
docker run -v /home/user1/agent:/etc/armorcode --name armorcode-agent agent-armorcode-tunnel-1
```

3. Check logs of the docker container
```
docker logs -f armorcode-agent
```