## Authentication and Security aspects
 a) Armorcode Agent uses open-source tools (autossh and supervisord) and a configuration file. There is no armorcode specific code in the Agent.

b) Authentication between Agent and Armorcode server is done using standard public-private key authentication mechanism used by SSH. Customer generates public-private key and shares public key with Armorcode via email.

c) Connectivity from the agent to internal services in customer's datacenter is governed by the configuration file used by the customer. There is no extra security configuration required. Customers may want to restrict internal services Agent can access via firewall as per their security/compliance policy.

d) The docker image can be hosted in a VM or K8 cluster as per convenience. The only expectation is that Agent is able to reach armorcode service on port 22 to establish SSH connection and Agent should be able to reach the service (e.g. Jira).


## How it works
Armorcode Agent uses the concept of Reverse SSH tunneling to establish connectivity from Armorcode Platform to customer's on-prem tools/services.

This is a very common technique with many articles available on internet on how it works. Below is a short explanation of the same.

Lets assume, customer is running JIRA service in internal network and Armorcode has to connect to this JIRA service via Armorcode Agent. It requires following steps

1) Customer starts the Agent in their on-prem enviornment from where JIRA service is reachable.
    a) It uses a configuration to create a reverse tunnel on port 5000

2) Agent creates a TCP connection to port 22 of Armorcode Server (SSH connection).

3) Due to reverse tunnel configuration, port 5000 opens-up for listening on the Armorcode Server and any traffic sent to this port will reach the JIRA service in customer enviornment.

4) Armorcode platform now initiates connection to customer's JIRA service by making a connection to this port 5000

Same is illustrated with a sequence diagram 

<h1 align="left">
  <img src="flow.png" alt="flow" width="700px"></a>
  <br>
</h1>

## Additional notes if outbound port 22 is blocked
Armorcode provides SSH Server hosted on port 443 as well in such case. Everything mentioned above remains same except replace port 22 with port 443.
 


## Setting up the Agent
This is a docker image which can run on any OS supporting docker containers.

1. Generate Certificate:
```
openssl genrsa -out private-key.pem 2048  
```
```
chmod 400 private-key.pem   
```
``` 
ssh-keygen -y -f private-key.pem > public-key.pem  
```

2. Share public-key.pem with Armorcode support
  
3. Share list of on-prem services which will be accessed by Armorcode via the Agent. Include protocol, hostname and port
 ``` 
 e.g. https://sonarqube.acme.com:8443
 ```

4. Get supervisord.conf from Armorcode support

5. Clone this repo:
```
git clone https://github.com/armor-code/agent.git
```
  
6. Copy below two files inside cloned repo folder
    - supervisord.conf
    - private-key.pem  

7. Optional step to check all things are in order. Run command: 
```
bash pre-launch-check.sh <server>
```

8. Run below command (Also see run.sh to clean-up previous docker image before starting new one)
```
docker compose up
```

If running docker container directly on a VM using docker command, ensure it restarts automatically on VM restart. Example command is below
```
docker run --restart unless-stopped -d <image-name>
```

## Issues and their solution

 1. Unable to connect with armorcode server on port 22 from docker container
    
    **Solution:** Work with your IT team to allow outgoing connection to armorcode server on port 22

 2. Error log: exit status 1; not expected

    **Solution:** Check you have copied private-key.pem file in the folder from where you are running "docker compose up" and autossh is able to read the file.
    
    You may have to run docker with sudo privileges if you are mounting the folder which has private-key due to user incompatability between host and container.


 3. “No such config file : /etc/supervisord/supervisord.conf” error generated when using docker-compose.yml on RHEL 8/9 with PODMAN \
    **Solution:** This error is caused due to binding volume failure. To solve it, append line no. 7 of docker-compose.yml with “:Z” flag then execute the “docker-compose up” command.
    File will look like :
    ```
    version: “3.9"
    services:
        armorcode-tunnel-1:
        build: .
        restart: always
        volumes:
            - .:/etc/armorcode:Z
    
