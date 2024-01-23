#sudo python3 -m pip install flask
#sudo python3 armorcode_route_via_https_proxy.py
from flask import Flask, request
import requests
import os

app = Flask(__name__)
proxy_url = os.environ.get('https_proxy')

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])

def proxy(path):
    target_url = request.url
    print(target_url)

    response = requests.request(
        method=request.method,
        url=target_url,
        headers={key: value for (key, value) in request.headers if key != 'Host'},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False,
        proxies={'https': proxy_url},
        verify=False
    )
    
    excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
    headers = [(name, value) for (name, value) in response.raw.headers.items()
               if name.lower() not in excluded_headers]

    return response.content, response.status_code, headers

if __name__ == '__main__':
    if proxy_url == None:
        exit("proxy not set. Exiting...")
    print("Using proxy: " + proxy_url)
    app.run(ssl_context='adhoc', port=443)
