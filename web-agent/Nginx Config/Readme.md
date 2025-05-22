```mermaid
sequenceDiagram
    box rgba(220, 220, 220, 0.5) User Network
        participant User
    end
    box rgba(220, 220, 220, 0.5) Customer Network 1
        participant GitHub
    end
    box rgba(220, 220, 220, 0.5) Customer Network 2
        participant ReverseProxy as "git.armorcode.<customer-domain>.com<br/>(nginx reverse proxy)"
        participant WebAgent as "web-agent"
    end
    box rgba(220, 220, 220, 0.5) ArmorCode Network
        participant ArmorCode as "app.armorcode.com"
    end
    
    User->>GitHub: App installation via UI
    GitHub->>ReverseProxy: webhook call
    ReverseProxy->>WebAgent: webhook call
    WebAgent->>ArmorCode: webhook call

```



1. Start web-agent in a network which can access app.armorcode.com and the ON-prem Github. Readme for web-agent https://github.com/armor-code/agent/tree/main/web-agent
2. Add web-agent config from UI for github url in Armorcode Agent page
3. Test web-agent working
4. Start Nginx in another/same VM in the same network where web-agent i.e. app.armorcode.com and the ON-prem Github are accessible
5. (Optional)  Put a domain like git.armorcode.paypal.com in Github app and this resolves to nginx
6. Add Nginx config like below
7. Test nginx config from localhost
8. Now while setting up Github app replace in Webhook Url "https://app.armorcode.ai" with "https://git.armorcode.paypal.com" (Or any other domain that is used)