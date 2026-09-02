import requests
import zipfile
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# --- Paste your Proxy URL here ---
# It should look exactly like: user:pass@host:port
PROXY_URL = "ulUve74DtH3Zv2H0:wifi;us;@proxy.soax.com:9000"

def verify_proxy(url):
    # Format for requests: http://user:pass@host:port
    proxies = {"http": f"http://{url}", "https": f"http://{url}"}
    try:
        response = requests.get("http://httpbin.org/ip", proxies=proxies, timeout=10)
        print("Proxy IP Details:", response.json())
        return True
    except Exception as e:
        print("Proxy Connection Failed:", e)
        return False

def create_proxy_extension(url):
    # Parse the URL string
    credentials, host_port = url.split('@')
    user, password = credentials.split(':')
    host, port = host_port.split(':')

    manifest_json = '{"version":"1.0.0","manifest_version":2,"name":"Chrome Proxy","permissions":["proxy","tabs","unlimitedStorage","storage","<all_urls>","webRequest","webRequestBlocking"],"background":{"scripts":["background.js"]}}'
    background_js = f'var config = {{mode:"fixed_servers",rules:{{singleProxy:{{scheme:"http",host:"{host}",port:parseInt({port})}}}}}};chrome.proxy.settings.set({{value:config,scope:"regular"}},function(){{}});chrome.webRequest.onAuthRequired.addListener(function(d){{return {{authCredentials:{{username:"{user}",password:"{password}"}}}};}},{{urls:["<all_urls>"]}},["blocking"]);'
    
    pluginfile = 'proxy_auth_plugin.zip'
    with zipfile.ZipFile(pluginfile, 'w') as zp:
        zp.writestr("manifest.json", manifest_json)
        zp.writestr("background.js", background_js)
    return pluginfile

# --- Execution ---
if verify_proxy(PROXY_URL):
    print("Proxy is valid. Launching browser...")
    chrome_options = Options()
    chrome_options.add_extension(create_proxy_extension(PROXY_URL))
    chrome_options.add_experimental_option("detach", True)
    
    driver = webdriver.Chrome(options=chrome_options)
    driver.get("https://whatismyipaddress.com/")
else:
    print("Failure: Proxy is invalid. Check SOAX dashboard or IP Whitelist.")