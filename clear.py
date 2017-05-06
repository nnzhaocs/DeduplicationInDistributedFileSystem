from kazoo.client import KazooClient

zk = KazooClient(hosts='127.0.0.1:2181')
zk.start()
zk.delete("dedupequeue",recursive=True)
zk.create("dedupequeue","somevalue")
zk.delete("owner", recursive=True)
zk.delete("metadata", recursive=True)
zk.create("/metadata", "sample")
zk.create("/master", "sample")