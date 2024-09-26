## Setting up the Agent
This is a docker image which can run on any OS supporting docker containers.

1. Generate apiKey from Armorcode Platform
3. Get Server Url of the Armorcode
4. Create a folder/volume to store Api logs 
5. Run the docker Image as 
```commandline
docker run -e server_url='<server_url>' -e api_key='<api_key>'  -v <folder/volume>:/temp armorcode-web-agent
```