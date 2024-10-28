## Setting up the Agent just using the Agent Script (preferred)
Steps for customer
1. install requirements: wget -O requirements.txt 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/ENG-56930/web-agent/requirements.txt'; pip3 install -r requirements. txt
2. Download the script: wget -O worker.py 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/ENG-56930/web-agent/app/worker.py'
3. Run command: python3 worker.py --serverUrl 'https://app.armorcode.com/' --apiKey `<apiKey>` --index 0 --timeout 25 --verify False
4. Check logs: cd /tmp/armorcode/log ; tail -F *


## Setting up the Agent with Docker
This is a docker image which can run on any OS supporting docker containers.

1. Generate apiKey from Armorcode Platform
3. Get Server Url of the Armorcode
4. Create a folder/volume to store Api logs 
5. Run the docker Image as 
```commandline
docker run -e server_url='<server_url>' -e api_key='<api_key>'  -v <folder/volume>:/tmp/armorcode armorcode/armorcode-web-agent
```
6. If you don't want to do certificates validations (needed in case if VM don't have any certs assigned and making https request) pass env variable
    ``verify`` 