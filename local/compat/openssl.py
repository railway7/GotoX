# coding:utf-8

import socket
import errno
import ssl
from OpenSSL import SSL, crypto
from select import select
from ssl import _dnsname_match, _ipaddress_match
try:
    from ssl import _inet_paton as ip_address # py3.7+
except ImportError:
    from ipaddress import ip_address

SSL.TLSv1_3_METHOD = SSL.TLSv1_2_METHOD + 1
res_ciphers = f'{ssl._RESTRICTED_SERVER_CIPHERS}:!SSLv3'.encode()
# py3.10+ 兼容一些旧的应用和系统
def_ciphers = res_ciphers.replace(b':!SHA1:', b':')

zero_errno = errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTSOCK
zero_EOF_error = -1, 'Unexpected EOF'

try:
    SSL.OP_ALL |= SSL.OP_IGNORE_UNEXPECTED_EOF
except AttributeError:
    pass

class SSLConnection:
    '''API-compatibility wrapper for Python OpenSSL's Connection-class.'''

    def __init__(self, context, sock):
        self._sock = sock
        self._connection = SSL.Connection(context, sock)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __iowait(self, io_func, *args, **kwargs):
        timeout = self._sock.gettimeout()
        fd = self._sock.fileno()
        while self._connection:
            try:
                return io_func(*args, **kwargs)
            except (SSL.WantReadError, SSL.WantX509LookupError):
                rd, _, ed = select([fd], [], [fd], timeout)
                if ed:
                    raise socket.error(ed)
                if not rd:
                    raise socket.timeout('The read operation timed out')
            except SSL.WantWriteError:
                _, wd, ed = select([], [fd], [fd], timeout)
                if ed:
                    raise socket.error(ed)
                if not wd:
                    raise socket.timeout('The write operation timed out')
            except SSL.SysCallError as e:
                if e.args[0] in socket._blocking_errnos:
                    rd, wd, ed = select([fd], [fd], [fd], timeout)
                    if ed:
                        raise socket.error(ed)
                    if not rd and not wd:
                        raise socket.timeout('The socket operation timed out')
                else:
                    raise
            except SSL.ZeroReturnError:
                sstate = self._connection.get_shutdown()
                if io_func.__name__ == 'send':
                    if sstate:
                        raise ConnectionAbortedError(errno.ECONNABORTED, 'Software caused connection abort')
                else:
                    if not sstate & SSL.RECEIVED_SHUTDOWN:
                        raise ConnectionResetError(errno.ECONNRESET, 'Connection reset by remote')
                raise

    def accept(self):
        sock, addr = self._sock.accept()
        client = SSLConnection(self._context, sock)
        client.set_accept_state()
        return client, addr

    def session_reused(self):
        return SSL._lib.SSL_session_reused(self._ssl) == 1

    def get_session(self):
        session = SSL._lib.SSL_get1_session(self._ssl)
        if session != SSL._ffi.NULL:
            return SSL._ffi.gc(session, SSL._lib.SSL_SESSION_free)

    def set_session(self, session):
        SSL._lib.SSL_set_session(self._ssl, session)

    def do_handshake(self):
        with self._sock.iplock:
            self.__iowait(self._connection.do_handshake)

    def do_handshake_server_side(self):
        self.set_accept_state()
        self.__iowait(self._connection.do_handshake)

    def connect(self, addr):
        self.__iowait(self._connection.connect, addr)

    def send(self, data, flags=0):
        if data:
            try:
                return self.__iowait(self._connection.send, data)
            except SSL.ZeroReturnError:
                return 0
        else:
            return 0

    write = send

    def sendall(self, data, flags=0):
        total_sent = 0
        total_to_send = len(data)
        if not hasattr(data, 'tobytes'):
            data = memoryview(data)
        while total_sent < total_to_send:
            sent = self.send(data[total_sent:total_sent + 32768]) # 32K
            total_sent += sent

    def recv(self, bufsiz, flags=None):
        pending = self._connection.pending()
        if pending:
            return self._connection.recv(min(pending, bufsiz))
        try:
            return self.__iowait(self._connection.recv, bufsiz, flags)
        except SSL.ZeroReturnError:
            return b''
        except SSL.SysCallError as e:
            if e.args == zero_EOF_error or e.args[0] in zero_errno:
                return b''
            raise

    read = recv

    def recv_into(self, buffer, nbytes=None, flags=None):
        pending = self._connection.pending()
        if pending:
            return self._connection.recv_into(buffer)
        try:
            return self.__iowait(self._connection.recv_into, buffer, nbytes, flags)
        except SSL.ZeroReturnError:
            return 0
        except SSL.SysCallError as e:
            if e.args == zero_EOF_error or e.args[0] in zero_errno:
                return 0
            raise

    readinto = recv_into

    def close(self):
        if hasattr(self._sock, 'close'):
            self._sock.close()
            self._sock = None

    def makefile(self, *args, **kwargs):
        return socket.socket.makefile(self, *args, **kwargs)

class CertificateError(SSL.Error, ssl.CertificateError):
    pass

# https://www.openssl.org/docs/manmaster/man3/X509_verify_cert_error_string.html
CertificateErrorTab = {
    SSL._lib.X509_V_ERR_CERT_HAS_EXPIRED: (
        'expired',
        lambda cert: f'has expired: {cert.get_notAfter().decode()}'),
    SSL._lib.X509_V_ERR_DEPTH_ZERO_SELF_SIGNED_CERT: (
        'self signed',
        lambda cert: f'self signed, issuer: {str(cert.get_issuer())[18:-2]}'),
    SSL._lib.X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN: (
        'self signed',
        lambda cert: f'self signed in chain, issuer: {str(cert.get_issuer())[18:-2]}'),
    SSL._lib.X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT: (
        'untrusted CA',
        lambda cert: f'untrusted CA in chain, issuer: {str(cert.get_issuer())[18:-2]}'),
    SSL._lib.X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY: (
        'untrusted CA',
        lambda cert: f'untrusted root CA, issuer: {str(cert.get_issuer())[18:-2]}'),
    SSL._lib.X509_V_ERR_UNABLE_TO_VERIFY_LEAF_SIGNATURE: (
        'untrusted CA',
        lambda cert: f'leaf signature occasioned untrusted CA, issuer: {str(cert.get_issuer())[18:-2]}')
}

def match_hostname(cert, hostname):
    try:
        host_ip = ip_address(hostname)
    except ValueError:
        # Not an IP address (common case)
        host_ip = None
    dnsnames = []
    san = cert.get_subject_alt_name()
    for key, value in san:
        if key == 'DNS':
            if host_ip is None and _dnsname_match(value, hostname):
                return
            dnsnames.append(value)
        elif key == 'IP Address':
            if host_ip is not None and _ipaddress_match(value, host_ip):
                return
            dnsnames.append(value)
    if not dnsnames:
    # The subject is only checked when there is no dNSName entry in subjectAltName
    # XXX according to RFC 2818, the most specific Common Name must be used.
        value = cert.get_subject().commonName
        if value is not None:
            if _dnsname_match(value, hostname):
                return
            dnsnames.append(value)
        elif cert.get_issuer().organizationName == hostname:
            # If there is no common name, then check issuer's organization name
            # e.g. CloudFlare ddos-guard
            return
    if len(dnsnames) > 1:
        raise CertificateError(-1, "hostname %r doesn't match either of %s"
                % (hostname, ', '.join(map(repr, dnsnames))))
    elif len(dnsnames) == 1:
        raise CertificateError(-1, "hostname %r doesn't match %r"
                % (hostname, dnsnames[0]))
    else:
        raise CertificateError(-1, "no appropriate commonName or "
                "subjectAltName fields were found")

def get_subject_alt_name(self):
    for i in range(self.get_extension_count()):
        ext = self.get_extension(i)
        if ext._nid == SSL._lib.NID_subject_alt_name:
            for s in ext._subjectAltNameString().split(', '):
                yield s.split(':', 1)

crypto.X509.get_subject_alt_name = get_subject_alt_name

if not hasattr(SSL.Context, 'set_cert_store'):

    def set_cert_store(self, store):
        X509_STORE_up_ref = getattr(SSL._lib, 'X509_STORE_up_ref', None)
        if X509_STORE_up_ref:
            rc = X509_STORE_up_ref(store._store)
            SSL._openssl_assert(rc == 1)
        SSL._lib.SSL_CTX_set_cert_store(self._context, store._store)

    SSL.Context.set_cert_store = set_cert_store
