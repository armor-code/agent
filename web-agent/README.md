The purpose of this agent is to allow API invocation from ArmorCode to customer's on-prem service.

The architecture enables controlled, secure message exchange between the Agent, the ArmorCode platform, and customer's on-prem services. The interactions occur through secure authenticated channels.

## Authentication/Security Aspects:

a) The Agent is a short and simple open-source python script created by ArmorCode.

b) It communicates with Server using HTTPS. Agent authenticates with Server using API-key, generated out-of-band by customer from ArmorCode platform.

c) Agent to on-prem Service (e.g. JIRA, Coverity, etc) is over HTTPS

d) Agent communication with AWS S3 web-service is over HTTPS using pre-signed URL (received from Server) with validity of 10 minutes. The S3 bucket is a private bucket hosted in ArmorCode account.

## How It Works (Step-by-Step):

a) Message Retrieval: The Agent polls the Server over HTTPS. The Server checks authentication tokens and retrieves queued instructions from ArmorCode service. Once authenticated, the Agent receives the message intended for it.

b) Service Call: The Agent unpacks the response from server to get the API details (URL, HTTP Method e.g. GET/POST, headers, payload) and makes the call to the on-prem service.

c) Uploading Results: The Agent checks the response size received from on-prem service.
    - If the response is > than 500KB, it upload the response as file to ArmorCode Server
    - If the response is <= than 500KB, it makes a call to server with the payload.

d) Response Delivery: The Agent sends a confirmation and reference to the uploaded data back to the Server over HTTPS. AC later retrieves the processed response from the Server. At each step, time-bound tokens, encryption in transit, and restricted privileges keep the system secure.

```mermaid
sequenceDiagram
  box rgba(33, 66, 00, 0.1) "ArmorCode"
    participant AC
    participant Server
  end
  box rgba(00, 00, 00, 0.1) "Customer"
    participant Agent
    participant Service (e.g. JIRA)
  end

    Agent-->>Server: Poll for message (HTTPS call)
    AC->>Server: Send message
    AC-->>Server: Poll for response
    Agent->>Server: Retrieve message (HTTPS call)
    Agent->>Service (e.g. JIRA): Call Service
    Service (e.g. JIRA)-->>Agent: Service response

    Agent->>Server: Upload Larger Response to Server as File

    Agent-->>Server: Send response (HTTPS call)
    AC->>Server: Retrieve response
```


## Setting up the Agent with Docker
This is a docker image which can run on any OS supporting docker containers.

1. Generate apiKey from Armorcode Platform(API key type : API, Product: All, Role: Admin)
2. Download the latest Agent Image
```commandline
docker pull armorcode/armorcode-web-agent
```
3. Get Server Url of the Armorcode
4. Create a folder/volume to store Api logs
5. Run the docker Image with serverURL and api key as arguments
```commandline
docker run -d -v <folder/volume>:/tmp/armorcode armorcode/armorcode-web-agent --serverUrl='<server_url>' --apiKey='<api_key>' --timeout 30 
```
6. If you have HTTPS proxy to make calls to ArmorCode API, add this argument. ex ##
```commandline
  --outgoingProxyHttps='<https_proxy_to_access_armorcode>'
```
7. If you have HTTPS/HTTP proxy to make calls to Internal tools, add this argument. ex ##
```commandline
  --inwardProxyHttps='<https_proxy_to_access_internal_tools>' --inwardProxyHttp='<http_proxy_to_access_internal_tools>'
```
8. If the Agent being deployed is for certain env(Check in Armorcode Agent In Integration Page, if Service url is configured with evnName) pass envName as command line argument
```commandline
  --envName '<envName>'
```

[//]: # (--serverUrl='https://qa.armorcode.ai' --apiKey='afa3dfe5-11b3-4b6f-a5e2-2138c1918c29' --verify=False  --uploadToAc)


## Setting up the Agent just using the Agent Script 
Steps for customer
1. install requirements:  
```commandline
  wget -O requirements.txt 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/requirements.txt'; pip3 install -r requirements. txt
```
2. Download the script:
```commandline
  wget -O worker.py 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/app/worker.py'
```

3. Run command: 
```commandline
  python3 worker.py --serverUrl 'https://app.armorcode.com' --apiKey `<apiKey>` --index 0 --timeout 30
```
5. If you have HTTPS proxy to make calls to ArmorCode API, add this argument. ex ##
```commandline
  --outgoingProxyHttps='<https_proxy_to_access_armorcode>'
```

6. If you have HTTPS/HTTP proxy to make calls to Internal tools, add this argument. ex ##
```commandline
  --inwardProxyHttps='<https_proxy_to_access_internal_tools>' --inwardProxyHttp='<http_proxy_to_access_internal_tools>'
```
7. If the Agent being deployed is for certain env(Check in Armorcode Agent In Integration Page, if Service url is configured with evnName) pass envName as command line argument
```commandline
  --envName '<envName>'
```
8. Check logs: 
```commandline
  cd /tmp/armorcode/log ; tail -F *
```

