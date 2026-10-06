"""Runtime network boundary for all Python app processes, including saved API settings.

Build/model downloads happen outside the app. This policy blocks external DNS
and direct-IP connections; configured local service addresses only are allowed.
"""
import socket
import ipaddress
import os

for _key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    os.environ.pop(_key,None)
os.environ['NO_PROXY']='*'
os.environ['no_proxy']='*'

_resolve = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_hosts = {'localhost', '127.0.0.1', '::1', 'host.docker.internal', 'whisperx-asr'}
_ips = {'127.0.0.1', '::1'}
_service_ips = {}
for _host in ('host.docker.internal', 'whisperx-asr'):
    try:
        _service_ips[_host]={row[4][0] for row in _resolve(_host, None)}
        _ips.update(_service_ips[_host])
    except OSError:
        pass

def _allowed(host):
    if isinstance(host, bytes):
        host = host.decode('ascii')
    if host in _hosts or host in _ips:
        return
    raise PermissionError('External app network destination refused')

def local_resolve(host, *args, **kwargs):
    _allowed(host)
    rows=_resolve(host, *args, **kwargs)
    if host in ('host.docker.internal','whisperx-asr'):
        _service_ips[host]={row[4][0] for row in rows}
        _ips.clear()
        _ips.update({'127.0.0.1','::1'})
        for addresses in _service_ips.values(): _ips.update(addresses)
    return rows

def local_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _allowed(address[0])
    return _connect(sock, address)

def local_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _allowed(address[0])
    return _connect_ex(sock, address)

socket.getaddrinfo = local_resolve
socket.socket.connect = local_connect
socket.socket.connect_ex = local_connect_ex
