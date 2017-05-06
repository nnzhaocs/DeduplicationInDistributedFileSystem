from myparser import jsonParser
import socket
import sendlib
import threading
import filesendlib
from kazoo.client import KazooClient
from kazoo.exceptions import NodeExistsError
from kazoo.exceptions import NoNodeError
from client import client
import election
import time
import os
import dedupe
from threading import Lock
import random
import kazoo
import logging


class server:
    def __init__(self, host, port, storage_path, elect, meta, zk):
        s = socket.socket()
        s.bind((host, port))
        s.listen(5)
        self.serversocket = s
        self.storage_path = storage_path
        self.elect = elect
        self.meta = meta
        self.zk = zk
        self.requesttokens = set()
        self.actual_content_hops = 2
        self.dedupe_content_hops = 2
        self.lock = Lock()
        self.ds = dedupe.deduplication(dedupepath=storage_path)
        self.host = host
        self.port = port
        kazoo.recipe.watchers.ChildrenWatch(self.zk,"/metadata", self.updatemetadata)


    #Todo check if delete a file and recreate calls this or not
    def addFiles(self,newfiles):
        for newfile in newfiles:
            print "update metadata called for file", newfile
            meta_string = zk.get("/metadata/" + newfile)
            print(str(meta_string[0]))
            self.meta[newfile]=eval(str(meta_string[0]))

    def removeFiles(self,oldfiles):
        for oldfile in oldfiles:
            print "remove old files metadata",oldfile
            del self.meta[oldfile]

    def updatemetadata(self,updatedfiles):
        print "metadata update called with files"+str(updatedfiles)
        print "current metadata"+str(self.meta.keys())
        cset = set(updatedfiles)
        s = set(file for file in self.meta)
        n = cset - s
        if len(n) > 0:
            self.addFiles(n)
        n = s - cset
        if len(n) > 0:
            self.removeFiles(n)

    # todo
    def getname(self):
        pass

    def accept(self):
        c, addr = self.serversocket.accept()
        return c

    def getclientfrominfo(self, info):
        if info is not None:
            host = info[:info.index(',')]
            port = int(info[info.index(',') + 1:])
            return client(host, port)

    def getnextclient(self):
        nextclient = self.getclientfrominfo(self.elect.childinfo)
        if nextclient is None:
            nextclient = self.getclientfrominfo(self.elect.masterinfo)
        return nextclient

    def on_child_sucess1(self, filename, threadclientsocket, isclientrequest):
        if threadclientsocket is not None:
            if not isclientrequest:
                sendlib.write_socket(threadclientsocket, "sucess2")
            else:
                self.zk.create("dedupequeue/" + filename, str(self.storage_path))
                response = self.prepare_response(200)
                sendlib.write_socket(threadclientsocket, response)

    def writetochild(self, storage_path, filename, req, childclient):
        while (True and childclient is not None):
            try:
                status = filesendlib.sendresponseandfile(filesendlib.storagepathprefix(storage_path), filename,
                                                         childclient.s, req, self.ds, False)
                if status == "terminate":
                    return "terminate", childclient
                response = sendlib.read_socket(childclient.s)
                if response != "sucess1":
                    jp = jsonParser(response)
                    hashes = jp.getValue("hashes")
                    print "requested hashes " + str(hashes)
                    for hash in hashes:
                        data = self.ds.getdatafromhash(hash)
                        sendlib.write_socket(childclient.s, data)
                break
            except socket.error:
                time.sleep(60)
                childclient = self.getnextclient()
        return "sucess1", childclient

    def handlechildwrite(self, filename, req, threadclientsocket, childclient, isclientrequest):
        storage_path = filesendlib.storagepathprefix(self.storage_path)
        response, childclient = self.writetochild(storage_path, filename, req, childclient)

        self.on_child_sucess1(filename, threadclientsocket, isclientrequest)
        while response != "terminate" and childclient is not None:
            try:
                response = sendlib.read_socket(childclient.s)
                if response == "sucess2":
                    childclient.close()
                    break
            except socket.error:
                time.sleep(60)
                childclient = self.getnextclient()
                response, childclient = self.writetochild(storage_path, filename, req, childclient)

    def create(self, filename, req, threadclientsocket, hopcount, isclientrequest):
        filesendlib.recvfile(self.storage_path, filename, threadclientsocket)
        if not isclientrequest:
            if filename.endswith("._temp"):
                actualfilename = filesendlib.actualfilename(filename)
                if self.ds.actualfileexits(actualfilename):
                    self.ds.createchunkfromactualfile(filename, actualfilename)
                    sendlib.write_socket(threadclientsocket, "sucess1")
                else:
                    missingchunkhashes = self.ds.findmissingchunk(filename)
                    response = {}
                    response["hashes"] = missingchunkhashes
                    sendlib.write_socket(threadclientsocket, str(response))
                    for missinghash in missingchunkhashes:
                        chunk = sendlib.read_socket(threadclientsocket)
                        self.ds.createChunkFile(chunk, missinghash)
            else:
                sendlib.write_socket(threadclientsocket, "sucess1")

        childclient = self.getnextclient()

        if hopcount > 0 and childclient is not None:
            self.handlechildwrite(filename, req, threadclientsocket, childclient, isclientrequest)
        else:
            self.on_child_sucess1(filename, threadclientsocket, isclientrequest)

    def getownerhostandport(self, filename):
        if zk.exists("owner/" + filename) is not None:
            owner = zk.get("owner/" + filename)
            info = owner[0].split(" ")
            host = info[1]
            port = int(info[3])
            if port==self.port and host==self.host:
                return None
            return host, port
        return None

    def read(self, filename, threadclientsocket):
        response = self.response_dic(200)
        meta = self.read_meta(filename)
        if self.ds.actualfileexits(filename) or self.ds.dedupefileexits(filename) :
                response["meta"] = self.meta[filename]
                filesendlib.sendresponseandfile(filesendlib.storagepathprefix(self.storage_path), filename,
                                                threadclientsocket,
                                                str(response), self.ds, True)
        else:
            ownerh_p=self.getownerhostandport(filename)
            if ownerh_p is not None:
                s = socket.socket()
                s.connect(ownerh_p)
                if s is not None:
                    request = {}
                    request["file_name"] = filename
                    request["operation"] = "READ"
                    sendlib.write_socket(s, str(request))
                    resp = sendlib.read_socket(s)
                    jp = jsonParser(resp)
                    if jp.getValue("status") == 200:
                        self.meta[filename] = jp.getValue("meta")
                        response["meta"] = self.meta[filename]
                        filesendlib.recvfile(filesendlib.storagepathprefix(self.storage_path), filename, s)
                        s.close()
                        filesendlib.sendresponseandfile(filesendlib.storagepathprefix(self.storage_path), filename,
                                                    threadclientsocket,
                                                    str(response), self.ds, True)
                else:
                    return 404


    def list(self, threadclientsocket):
        result = self.response_dic(200)
        # if os.path.exists(storage_path):
        #     files = [file for file in os.listdir(self.storage_path)
        #              if os.path.isfile(os.path.join(self.storage_path, file))]
        #     for i in range(len(files)):
        #         if files[i].endswith("._temp"):
        #             files[i] = files[i][:len("._temp") * -1]
        #     result["files"] = files
        # else:
        #     result["files"] = []
        files = [file for file in self.meta]
        result["files"] = files
        sendlib.write_socket(threadclientsocket, str(result))

    def response_dic(self, result):
        code = {
            200:
                "OK",
            400:
                "Bad Request",
            404:
                "Not Found",
        }

        status = result
        response = {}
        response["status"] = status
        response["message"] = code[status]

        return response

    def prepare_response(self, result):
        response = self.response_dic(result)
        return str(response)
    #todo
    def store_meta_memory(self, filename, jp):
        self.meta[filename] = jp.getValue("meta")

    def read_meta(self, filename):
        if filename not in self.meta:
            if self.ds.actualfileexits(filename):
                st = os.lstat(filesendlib.storagepathprefix(self.storage_path) + filename)
                filemeta = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                                                                    'st_gid', 'st_mode', 'st_mtime', 'st_nlink',
                                                                    'st_size',
                                                                    'st_uid'))

            elif self.ds.dedupefileexits(filename):
                st = os.lstat(filesendlib.storagepathprefix(self.storage_path) + filename + "._temp")
                filemeta = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                                                                    'st_gid', 'st_mode', 'st_mtime', 'st_nlink',
                                                                    'st_uid'))
                filemeta['st_size'] = self.ds.findfilelength(filename)
            else:
                return None
            self.meta[filename] = filemeta
        return self.meta[filename]

    def gethopcount(self, jp):
        if not jp.has("hopcount"):
            hopcount = self.actual_content_hops
        else:
            hopcount = jp.getValue("hopcount") - 1
        return hopcount

    def updatehopcountrequest(self, jp, hopcount):
        respdic = jp.getdic()
        respdic["hopcount"] = hopcount
        req = str(respdic)
        return req

    def updatesize(self, filename):
        storagepath = filesendlib.storagepathprefix(self.storage_path)
        if os.path.exists(storagepath + filename):
            size = os.path.getsize(storagepath + filename)
        else:
            size = self.ds.findfilelength(storagepath + filename)
        self.meta[filename]["st_size"] = size
        self.meta[filename]["st_ctime"] = time.time()

    def performdedupe(self):
        while (True):
            pending_files = self.zk.get_children("dedupequeue")
            for file in pending_files:
                if self.ds.actualfileexits(file):
                    try:
                        if zk.exists("dedupeserver/" + file) is None:
                            zk.create("dedupeserver/" + file, str(self.storage_path), ephemeral=True, makepath=True)
                            print "deduping file " + file
                            self.ds.write(file)
                            request = client.createrequest(file + "._temp")
                            request["meta"] = self.read_meta(file)
                            request["hopcount"] = self.dedupe_content_hops
                            requestToken = random.getrandbits(128)
                            request["token"] = requestToken
                            self.requesttokens.add(requestToken)
                            self.handlechildwrite(file + "._temp", str(request), None, self.getnextclient(), False)
                            zk.delete("dedupequeue/" + file)
                            ownerh_p=self.getownerhostandport(file)
                            if ownerh_p is not None:
                                c=client(ownerh_p[0],ownerh_p[1])
                                self.handlechildwrite(file + "._temp", str(request), None,c, False)
                                c.close()
                    except NodeExistsError:
                        pass

    def handle_client(self, threadclientsocket):
        # try:
        while (1):
            # read request from client
            req = sendlib.read_socket(threadclientsocket)
            if (req is None):
                threadclientsocket.close()
                break
            print req
            jp = jsonParser(req)
            operation = jp.getValue("operation")
            if operation == "CREATE":
                isclientrequest = False
                filename = jp.getValue("file_name")
                actualfilename = filesendlib.actualfilename(filename)
                if (jp.has("token")):
                    requestToken = jp.getValue("token")
                    if requestToken in self.requesttokens:
                        print ("sending terminate")
                        sendlib.write_socket(threadclientsocket, "terminate")
                        continue
                    else:
                        self.requesttokens.add(requestToken)
                        sendlib.write_socket(threadclientsocket, "continue")
                else:
                    isclientrequest = True
                    requestToken = random.getrandbits(128)
                    reqdic = jp.getdic()
                    reqdic["token"] = requestToken
                    self.requesttokens.add(requestToken)
                    self.store_meta_memory(actualfilename, jp)
                    if zk.exists("owner/" + filename) is None:
                        zk.create("owner/" + filename, "( " + str(self.host) + " : " + str(self.port) + " )",
                                    ephemeral=False, makepath=True)
                # hopcount- no of hops the actual file must be farwarded
                updatedhopcount = self.gethopcount(jp)
                if updatedhopcount >= 0 and updatedhopcount < 99:
                    req = self.updatehopcountrequest(jp, updatedhopcount)
                self.create(filename, req, threadclientsocket, updatedhopcount, isclientrequest)
                if filename == actualfilename:
                    if isclientrequest:
                        self.updatesize(actualfilename)
                        if zk.exists("metadata/" + filename) is None:
                            zk.create("metadata/" + filename, str(self.meta[filename]), ephemeral=True, makepath=True)
            elif operation == "READ":
                filename = jp.getValue("file_name")
                self.read(filename, threadclientsocket)
            elif operation == "LIST":
                self.list(threadclientsocket)
            elif operation == "META":
                filename = jp.getValue("file_name")
                filemeta = self.read_meta(filename)
                if filemeta is not None:
                    sendlib.write_socket(threadclientsocket, str(self.meta[filename]))
                else:
                    sendlib.write_socket(threadclientsocket, "ENOENT")
            elif operation == "EXIT":
                self.close()
                break





                # except Exception as e:
                #     print str(e)

    def close(self):
        self.serversocket.close()


def cleanup(zk):
    print("performing cleanup")
    zk.delete("dedupequeue", recursive=True)
    zk.create("dedupequeue", "somevalue")
    zk.delete("owner", recursive=True)
    zk.delete("metadata", recursive=True)
    zk.create("metadata", "somevalue")







if __name__ == '__main__':
    logging.basicConfig()

    # storage_path=raw_input("enter server name")


    root_path = "/root"
    leader_path = root_path + "/leader"

    peer_port = random.randrange(49152, 65535)

    zk = KazooClient(hosts='127.0.0.1:2181')

    zk.start()

    try:
        l = zk.get_children(root_path)
        if len(l) == 0:
            cleanup(zk)
    except NoNodeError:
        cleanup(zk)

    print("started with port", peer_port)

    storage_path = str(peer_port)

    if not os.path.exists(storage_path):
        os.makedirs(storage_path)

    host = socket.gethostbyname(socket.gethostname())

    e = election.election(zk, leader_path, host + "," + str(peer_port))

    meta = {}





    s1 = server(host, peer_port, storage_path, e, meta, zk)

    e.perform()


    print("this is a dedupeserver")
    t = threading.Thread(target=s1.performdedupe)
    t.daemon = True
    t.start()

    while True:
        c = s1.accept()
        t = threading.Thread(target=s1.handle_client, args=(c,))
        t.daemon = True
        t.start()

    s1.close()
    zk.stop()
    zk.close()