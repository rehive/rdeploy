from kubernetes import client, config

proxy_url = 'http://127.0.0.1:8123'

# Configs can be set in Configuration class directly or using helper utility
config.load_kube_config()
host = config.kube_config.Configuration().host
client.Configuration._default.proxy = proxy_url

print(host)
print(proxy_url)
print("TRUTTHHH")

v1 = client.CoreV1Api()
print("Listing pods with their IPs:")
ret = v1.list_pod_for_all_namespaces(watch=False)
for i in ret.items:
    print("%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name))
