## Setting up the Agent
1. Clone this repo

2. Generate Certificate:
```
openssl genrsa -out private-key.pem 2048  
chmod 400 private-key.pem  
ssh-keygen -y -f private-key.pem > public-key.pem  
```

3. Share public-key.pem with Armorcode support.  
  
4. Share list of on-prem servers (DNS or IP) and their ports which will be accessed by Armorcode via the Agent  
  
5. Get supervisord.conf from Armorcode support  
  
6. Copy supervisord.conf AND private-key.pem to the folder where docker-compose.yml file of this repo is present present  
  
7. Call docker compose up  
    - Also see run.sh to clean-up previous docker image before starting new one
