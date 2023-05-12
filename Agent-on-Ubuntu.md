
**Running the agent in a Ubuntu VM (no docker)**

**Step 1:**
 Create a VM of standard Ubuntu Server (any version). 

**Step 2:** 
 Copy private-key.pem file to /home/ubuntu (see main page on how to generate private-key.pem)

**Step 3:** 
 Create service file as per details sent by Armor Code team. Open vi editor and paste the service content
 
    sudo vi /etc/systemd/system/tunnel-1.service

Paste below content to the file and save and exit

    [Unit]
    Description=Service X1
    After=network.target
    
    [Service]
    User=ubuntu
    ExecStart=/usr/bin/ssh -i /home/ubuntu/private-key.pem -o ServerAliveInterval=60 -o ExitOnForwardFailure=yes -gnNT -R 22222:www.google.com:443 <user>@<server>
    RestartSec=5
    Restart=always
    KillMode=mixed

    [Install]
    WantedBy=multi-user.target

**Step 4:** 
Execute below commands

    sudo systemctl daemon-reload
    sudo systemctl enable tunnel-1
    sudo systemctl start tunnel-1

**Notes:**

 1. A separate service file is needed for each service which should be reachable from Armorcode
 2. Above instructions can be adjusted for any other Linux based OS.


