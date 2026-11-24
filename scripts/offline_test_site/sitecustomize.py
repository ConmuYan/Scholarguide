"""Block non-loopback networking in explicitly isolated test subprocesses.

Python loads this module from PYTHONPATH at startup. This guard is a test
isolation aid, not an operating-system network sandbox.
"""
import ipaddress
import os
import socket

if os.environ.get('SG_OFFLINE_NETWORK_GUARD') == '1':
    _getaddrinfo = socket.getaddrinfo
    _gethostbyname = socket.gethostbyname
    _gethostbyname_ex = socket.gethostbyname_ex
    _gethostbyaddr = socket.gethostbyaddr
    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex
    _sendto = socket.socket.sendto

    def _loopback(host):
        if isinstance(host, bytes):
            host = host.decode('ascii', 'strict')
        if isinstance(host, str) and host.lower().rstrip('.') == 'localhost':
            return True
        try:
            return ipaddress.ip_address(host.split('%', 1)[0]).is_loopback
        except (AttributeError, ValueError, TypeError):
            return False

    def _check(address):
        # Unix-domain sockets are local; IP sockets must target loopback.
        if isinstance(address, tuple) and (not address or not _loopback(address[0])):
            raise OSError('offline test guard: non-loopback socket blocked')

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _loopback(host):
            raise socket.gaierror('offline test guard: external DNS blocked')
        return _getaddrinfo(host, *args, **kwargs)

    def _checked_lookup(lookup):
        def wrapped(host, *args, **kwargs):
            if not _loopback(host):
                raise socket.gaierror('offline test guard: external DNS blocked')
            return lookup(host, *args, **kwargs)
        return wrapped

    def connect(self, address):
        _check(address)
        return _connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return _connect_ex(self, address)

    def sendto(self, data, *args):
        if args:
            _check(args[-1])
        return _sendto(self, data, *args)

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = _checked_lookup(_gethostbyname)
    socket.gethostbyname_ex = _checked_lookup(_gethostbyname_ex)
    socket.gethostbyaddr = _checked_lookup(_gethostbyaddr)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
