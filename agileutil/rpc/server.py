#coding=utf-8

from agileutil.rpc.protocal import TcpProtocal, UdpProtocal
import multiprocessing
from agileutil.rpc.exception import FuncNotFoundException
import queue
import threading
import select
import socket
import tornado.ioloop
from tornado.iostream import IOStream
from agileutil.rpc.discovery import DiscoveryConfig, ConsulRpcDiscovery
import struct
import functools


class RpcServer(object):
    
    def __init__(self):
        self.funcMap = {}
        self.funcList = []
        self.discoveryConfig = None
        self.discovery = None

    def regist(self, func):
        self.funcMap[ func.__name__ ] = func
        self.funcList = self.funcMap.keys()

    def run(self, func, args):
        try:
            if func not in self.funcList:
                return FuncNotFoundException('func not found')
            funcobj = self.funcMap[func]
            if len(args) == 0:
                resp = funcobj()
            else:
                resp = funcobj(args)
            return resp
        except Exception as ex:
            return Exception('server exception, ' + str(ex))

    def setDiscoverConfig(self, config: DiscoveryConfig):
        self.discoveryConfig = config
        self.discovery = ConsulRpcDiscovery(self.discoveryConfig.consulHost, self.discoveryConfig.consulPort)


class SimpleTcpRpcServer(RpcServer):
    
    def __init__(self, host, port):
        RpcServer.__init__(self)
        self.host = host
        self.port = port
        self.protocal = TcpProtocal(host, port)
    
    def serve(self):
        self.protocal.transport.bind()
        while 1:
            msg = self.protocal.transport.recv()
            request = self.protocal.unserialize(msg)
            func, args = self.protocal.parseRequest(request)
            resp = self.run(func, args)
            self.protocal.transport.send(self.protocal.serialize(resp))


class TcpRpcServer(SimpleTcpRpcServer):

    def __init__(self, host, port, workers = multiprocessing.cpu_count()):
        SimpleTcpRpcServer.__init__(self, host, port)
        self.worker = workers
        self.queueMaxSize = 100000
        self.queue = queue.Queue(self.queueMaxSize)
        

    def handle(self, conn):
        while 1:
            try:
                msg = self.protocal.transport.recv(conn)
                request = self.protocal.unserialize(msg)
                func, args = self.protocal.parseRequest(request)
                resp = self.run(func, args)                    
                self.protocal.transport.send(self.protocal.serialize(resp), conn)
            except Exception as ex:
                conn.close()
                return

    def serve(self):
        self.protocal.transport.bind()
        if self.discovery and self.discoveryConfig:
            self.discovery.regist(self.discoveryConfig.serviceName, self.discoveryConfig.serviceHost, self.discoveryConfig.servicePort, ttlHeartBeat=True)
        while 1:
            conn, _ = self.protocal.transport.accept()
            t = threading.Thread(target=self.handle, args=(conn,) )
            t.start()


class AsyncTcpRpcServer(TcpRpcServer):

    def __init__(self, host, port):
        TcpRpcServer.__init__(self, host, port)
        self.host = host
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addr = (self.host, self.port)
        self.timeout = 10
        self.epoll = select.epoll()
        self.fdToSocket = {self.socket.fileno():self.socket}
        self.fdResp = {}

    def bind(self):
        self.socket.bind(self.addr)
        self.socket.listen(10)
        self.socket.setblocking(False)
        self.epoll.register(self.socket.fileno(), select.EPOLLIN)

    def serve(self):
        self.bind()
        while 1:
            events = self.epoll.poll(self.timeout)
            if not events:
                continue
            for fd, event in events:
                socket = self.fdToSocket[fd]
                if socket == self.socket:
                    conn, addr = self.socket.accept()
                    conn.setblocking(False)
                    self.epoll.register(conn.fileno(), select.EPOLLIN)
                    self.fdToSocket[conn.fileno()] = conn
                elif event & select.EPOLLHUP:
                    self.epoll.unregister(fd)
                    self.fdToSocket[fd].close()
                    del self.fdToSocket[fd]
                elif event & select.EPOLLIN:
                    msg = self.protocal.transport.recv(socket)
                    request = self.protocal.unserialize(msg)
                    func, args = self.protocal.parseRequest(request)
                    resp = self.run(func, args)
                    self.fdResp[fd] = resp
                    self.epoll.modify(fd, select.EPOLLOUT)
                elif event & select.EPOLLOUT:
                    if fd in self.fdResp:
                        resp = self.fdResp[fd]
                        self.protocal.transport.send(self.protocal.serialize(resp), socket)
                        self.epoll.modify(fd, select.EPOLLIN)
                        socket.close()


class TornadoTcpRpcServer(TcpRpcServer):

    def __init__(self, host, port):
        TcpRpcServer.__init__(self, host, port)

    async def handleConnection(self, connection, address):
        stream = IOStream(connection)
        while 1:
            try:
                toread = 4
                readn = 0
                lengthbyte = b''
                while 1:
                    bufsize = toread - readn
                    if bufsize <= 0:
                        break
                    bytearr = await stream.read_bytes(bufsize, partial=True)
                    lengthbyte = lengthbyte + bytearr
                    readn = readn + len(bytearr)
                toread = struct.unpack("i", lengthbyte)[0]
                readn = 0
                msg = b''
                while 1:
                    bufsize = toread - readn
                    if bufsize <= 0:
                        break
                    bytearr = await stream.read_bytes(bufsize, partial=True)
                    msg = msg + bytearr
                    readn = readn + len(bytearr)
                request = self.protocal.unserialize(msg)
                func, args = self.protocal.parseRequest(request)
                resp = self.run(func, args)
                await stream.write( self.protocal.transport.getSendByte( self.protocal.serialize(resp) ) )
            except Exception as ex:
                connection.close()
                break


    def connectionReady(self, sock, fd, events):
        while True:
            try:
                connection, address = sock.accept()
                connection.setblocking(0)
                io_loop = tornado.ioloop.IOLoop.current()
                io_loop.spawn_callback(self.handleConnection, connection, address)
            except BlockingIOError:
                return
            

    def serve(self):
        self.protocal.transport.bind()
        self.protocal.transport.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.protocal.transport.socket.setblocking(0)
        self.ioLoop = tornado.ioloop.IOLoop.current()
        callback = functools.partial(self.connectionReady, self.protocal.transport.socket)
        self.ioLoop.add_handler(self.protocal.transport.socket.fileno(), callback, self.ioLoop.READ)
        self.ioLoop.start()


class UdpRpcServer(RpcServer):

    def __init__(self, host, port, workers = multiprocessing.cpu_count()):
        RpcServer.__init__(self)
        self.protocal = UdpProtocal(host, port)
        self.worker = workers
        self.queueMaxSize = 100000
        self.queue = queue.Queue(self.queueMaxSize)

    def startWorkers(self):
        for i in range(self.worker):
            t = threading.Thread(target=self.handle)
            t.start()

    def handle(self):
        while 1:
            body = self.queue.get()
            addr = body.get('addr')
            msg = body.get('msg')
            request = self.protocal.unserialize(msg)
            func, args = self.protocal.parseRequest(request)
            resp = self.run(func, args)
            self.protocal.transport.send(self.protocal.serialize(resp), addr = addr)
    
    def serve(self):
        self.startWorkers()
        self.protocal.transport.bind()
        while 1:
            msg, cliAddr = self.protocal.transport.recv()
            self.queue.put({'msg' : msg, 'addr' : cliAddr})

            