#!/usr/bin/env python
# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from __future__ import print_function

import argparse
import base64
import hashlib
import logging
import os
import signal
import socket
import sys
import threading
import time
import types

from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple, Union

import paramiko  # type: ignore[import-untyped]

from textcompat import utf_8_str_to_text

if sys.version_info[0] == 2:
    TEXT_TYPE = unicode  # type: ignore[name-defined]
    BINARY_TYPES = (str,)
else:
    TEXT_TYPE = str
    BINARY_TYPES = (bytes,)


DEFAULT_LOCAL_ED25519_KEY_USER_PATH = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
DEFAULT_LOCAL_RSA_KEY_USER_PATH = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa")
PtyValue = Union[str, int, bytes]
PtyDictionary = Dict[str, PtyValue]
AuthValue = Union[str, paramiko.PKey]
ThreadTarget = Callable[..., None]
FrameTypeOrNone = Optional[types.FrameType]
TextOrBinary = Union[str, bytes]


class ConnState(Enum):
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECT_WAIT = "RECONNECT_WAIT"
    RECONNECTING = "RECONNECTING"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


class RetryableConnectError(Exception):
    __slots__ = ()


class FatalConnectError(Exception):
    __slots__ = ()


class Config(object):
    __slots__ = (
        "host",
        "port",
        "username",
        "password",
        "rsa_key_path",
        "ed25519_key_path",
        "local_port",
        "local_rsa_key_path",
        "local_ed25519_key_path",
    )

    def __init__(
        self,
        host,
        port,
        username,
        password,
        rsa_key_path,
        ed25519_key_path,
        local_port,
        local_rsa_key_path,
        local_ed25519_key_path,
    ):
        # type: (str, int, str, Optional[str], Optional[str], Optional[str], int, Optional[str], Optional[str]) -> None
        self.host = host  # type: str
        self.port = port  # type: int
        self.username = username  # type: str
        self.password = password  # type: Optional[str]
        self.rsa_key_path = rsa_key_path  # type: Optional[str]
        self.ed25519_key_path = ed25519_key_path  # type: Optional[str]
        self.local_port = local_port  # type: int
        self.local_rsa_key_path = local_rsa_key_path  # type: Optional[str]
        self.local_ed25519_key_path = local_ed25519_key_path  # type: Optional[str]


class PendingChannel(object):
    __slots__ = (
        "chanid",
        "kind",
        "origin",
        "destination",
        "pty",
        "shell_requested",
        "exec_command",
        "subsystem",
        "ready",
    )

    def __init__(
        self,
        chanid,
        kind,
        origin=None,
        destination=None,
        pty=None,
        shell_requested=False,
        exec_command=None,
        subsystem=None,
        ready=None,
    ):
        # type: (int, str, Optional[Tuple[str, int]], Optional[Tuple[str, int]], Optional[PtyDictionary], bool, Optional[bytes], Optional[str], Optional[threading.Event]) -> None
        self.chanid = chanid  # type: int
        self.kind = kind  # type: str
        self.origin = origin  # type: Optional[Tuple[str, int]]
        self.destination = destination  # type: Optional[Tuple[str, int]]
        self.pty = pty  # type: Optional[PtyDictionary]
        self.shell_requested = shell_requested  # type: bool
        self.exec_command = exec_command  # type: Optional[bytes]
        self.subsystem = subsystem  # type: Optional[str]
        if ready is None:
            ready = threading.Event()
        self.ready = ready  # type: threading.Event


class DesiredRemoteForward(object):
    __slots__ = (
        "session_id",
        "bind_host",
        "bind_port",
        "active_port",
        "active",
    )

    def __init__(
        self, session_id, bind_host, bind_port, active_port=None, active=False
    ):
        # type: (int, str, int, Optional[int], bool) -> None
        self.session_id = session_id  # type: int
        self.bind_host = bind_host  # type: str
        self.bind_port = bind_port  # type: int
        self.active_port = active_port  # type: Optional[int]
        self.active = active  # type: bool


class DownstreamSession(object):
    __slots__ = (
        "session_id",
        "client_addr",
        "sock",
        "transport",
        "pending",
        "upstream_channels",
        "remote_forwards",
        "closed",
    )

    def __init__(self, session_id, client_addr, sock):
        # type: (int, Tuple[str, int], socket.socket) -> None
        self.session_id = session_id  # type: int
        self.client_addr = client_addr  # type: Tuple[str, int]
        self.sock = sock  # type: socket.socket
        self.transport = None  # type: Optional[paramiko.Transport]
        self.pending = {}  # type: Dict[int, PendingChannel]
        self.upstream_channels = {}  # type: Dict[int, paramiko.Channel]
        self.remote_forwards = []  # type: List[DesiredRemoteForward]
        self.closed = False  # type: bool


class Runtime(object):
    __slots__ = (
        "config",
        "state",
        "listener",
        "upstream",
        "upstream_socket",
        "host_key",
        "auth",
        "last_error",
        "stop_requested",
        "backoff",
        "max_backoff",
        "accept_thread",
        "sessions",
        "next_session_id",
        "lock",
    )

    def __init__(self, config):
        # type: (Config) -> None
        self.config = config  # type: Config
        self.state = ConnState.STARTING  # type: ConnState
        self.listener = None  # type: Optional[socket.socket]
        self.upstream = None  # type: Optional[paramiko.Transport]
        self.upstream_socket = None  # type: Optional[socket.socket]
        self.host_key = None  # type: Optional[paramiko.PKey]
        self.auth = None  # type: Optional[Tuple[str, AuthValue]]
        self.last_error = None  # type: Optional[Exception]
        self.stop_requested = False  # type: bool
        self.backoff = 1  # type: int
        self.max_backoff = 10  # type: int
        self.accept_thread = None  # type: Optional[threading.Thread]
        self.sessions = {}  # type: Dict[int, DownstreamSession]
        self.next_session_id = 1  # type: int
        self.lock = threading.RLock()


class SignalStopHandler(object):
    __slots__ = ("ctx",)

    def __init__(self, ctx):
        # type: (Runtime) -> None
        self.ctx = ctx  # type: Runtime

    def __call__(self, signum, frame):
        # type: (int, FrameTypeOrNone) -> None
        del frame
        self.ctx.stop_requested = True
        if self.ctx.state not in (
            ConnState.SHUTTING_DOWN,
            ConnState.STOPPED,
        ):
            set_state(self.ctx, ConnState.SHUTTING_DOWN, "signal %s" % (signum,))


class LocalServer(paramiko.ServerInterface):
    __slots__ = ("ctx", "session")

    def __init__(self, ctx, session):
        # type: (Runtime, DownstreamSession) -> None
        self.ctx = ctx  # type: Runtime
        self.session = session  # type: DownstreamSession

    def check_auth_none(self, username):
        # type: (str) -> int
        del username
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_password(self, username, password):
        # type: (str, str) -> int
        del username
        del password
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        # type: (str, paramiko.PKey) -> int
        del username
        del key
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        # type: (str) -> str
        del username
        return "none,password,publickey"

    def check_channel_request(self, kind, chanid):
        # type: (str, int) -> int
        if kind == "session":
            self.session.pending[chanid] = PendingChannel(chanid=chanid, kind=kind)
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        # type: (int, Tuple[str, int], Tuple[str, int]) -> int
        pending = PendingChannel(
            chanid=chanid,
            kind="direct-tcpip",
            origin=origin,
            destination=destination,
        )
        pending.ready.set()
        self.session.pending[chanid] = pending
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(
        self, channel, term, width, height, pixelwidth, pixelheight, modes
    ):
        # type: (paramiko.Channel, str, int, int, int, int, bytes) -> bool
        pending = self.session.pending.get(channel.chanid)
        if pending is None:
            return False
        pending.pty = {
            "term": term,
            "width": width,
            "height": height,
            "pixelwidth": pixelwidth,
            "pixelheight": pixelheight,
            "modes": modes,
        }
        return True

    def check_channel_shell_request(self, channel):
        # type: (paramiko.Channel) -> bool
        pending = self.session.pending.get(channel.chanid)
        if pending is None:
            return False
        pending.shell_requested = True
        pending.ready.set()
        return True

    def check_channel_exec_request(self, channel, command):
        # type: (paramiko.Channel, bytes) -> bool
        pending = self.session.pending.get(channel.chanid)
        if pending is None:
            return False
        pending.exec_command = command
        pending.ready.set()
        return True

    def check_channel_subsystem_request(self, channel, name):
        # type: (paramiko.Channel, str) -> bool
        pending = self.session.pending.get(channel.chanid)
        if pending is None:
            return False
        pending.subsystem = name
        pending.ready.set()
        return True

    def check_channel_window_change_request(
        self, channel, width, height, pixelwidth, pixelheight
    ):
        # type: (paramiko.Channel, int, int, int, int) -> bool
        pending = self.session.pending.get(channel.chanid)
        upstream_channel = self.session.upstream_channels.get(channel.chanid)
        if pending is not None and pending.pty is not None:
            pending.pty["width"] = width
            pending.pty["height"] = height
            pending.pty["pixelwidth"] = pixelwidth
            pending.pty["pixelheight"] = pixelheight
        if upstream_channel is not None:
            try:
                upstream_channel.resize_pty(
                    width=width,
                    height=height,
                    width_pixels=pixelwidth,
                    height_pixels=pixelheight,
                )
            except Exception:
                logging.info("[bridge] ignored downstream window resize failure")
        return True

    def check_port_forward_request(self, address, port):
        # type: (str, int) -> Union[int, bool]
        if port == 0:
            logging.info("[forward] downstream requested remote port 0; refusing")
            return False
        forward = DesiredRemoteForward(
            session_id=self.session.session_id,
            bind_host=address,
            bind_port=port,
        )
        if try_activate_remote_forward(self.ctx, self.session, forward):
            self.session.remote_forwards.append(forward)
            if forward.active_port is not None:
                return forward.active_port
            return port
        return False

    def cancel_port_forward_request(self, address, port):
        # type: (str, int) -> None
        forward = None  # type: Optional[DesiredRemoteForward]
        items = list(self.session.remote_forwards)
        for item in items:
            if item.bind_host == address and (
                item.bind_port == port or item.active_port == port
            ):
                forward = item
                self.session.remote_forwards.remove(item)
                break
        if forward is not None:
            cancel_remote_forward(self.ctx, forward)

    def check_channel_env_request(self, channel, name, value):
        # type: (paramiko.Channel, bytes, bytes) -> bool
        del channel
        del name
        del value
        return True


def set_state(ctx, new_state, reason=""):
    # type: (Runtime, ConnState, str) -> None
    old_state = ctx.state
    ctx.state = new_state
    if reason:
        logging.info("[state] %s -> %s: %s" % (old_state.value, new_state.value, reason))
    else:
        logging.info("[state] %s -> %s" % (old_state.value, new_state.value))

def ssh_sha256_fingerprint(key):
    # type: (paramiko.PKey) -> str
    digest = hashlib.sha256(key.asbytes()).digest()
    encoded = str(base64.b64encode(digest))
    return "SHA256:%s" % (encoded.rstrip("="),)

def load_auth(config):
    # type: (Config) -> Tuple[str, AuthValue]
    if config.password is not None:
        return ("password", config.password)
    if config.rsa_key_path is not None:
        return (
            "rsa",
            paramiko.RSAKey.from_private_key_file(config.rsa_key_path),
        )
    if config.ed25519_key_path is not None:
        return (
            "ed25519",
            paramiko.Ed25519Key.from_private_key_file(config.ed25519_key_path),
        )
    raise FatalConnectError("exactly one auth method is required")

def load_local_host_key(config):
    # type: (Config) -> paramiko.PKey
    ed25519_candidates = []  # type: List[str]
    rsa_candidates = []  # type: List[str]

    if config.local_ed25519_key_path is not None:
        return paramiko.Ed25519Key.from_private_key_file(config.local_ed25519_key_path)
    elif config.local_rsa_key_path is not None:
        return paramiko.RSAKey.from_private_key_file(config.local_rsa_key_path)
    elif os.path.exists(DEFAULT_LOCAL_ED25519_KEY_USER_PATH):
        return paramiko.Ed25519Key.from_private_key_file(DEFAULT_LOCAL_ED25519_KEY_USER_PATH)
    elif os.path.exists(DEFAULT_LOCAL_RSA_KEY_USER_PATH):
        return paramiko.RSAKey.from_private_key_file(DEFAULT_LOCAL_RSA_KEY_USER_PATH)

    raise FatalConnectError(
        "no local host key found; provide --local-ed25519-key or --local-rsa-key, "
        "or place one at %s or %s"
        % (
            DEFAULT_LOCAL_ED25519_KEY_USER_PATH,
            DEFAULT_LOCAL_RSA_KEY_USER_PATH,
        )
    )

def bind_local_listener(local_port):
    # type: (int) -> socket.socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", local_port))
    sock.listen()
    return sock

def write_banner(ctx):
    # type: (Runtime) -> None
    if ctx.host_key is None:
        raise RuntimeError("local host key unavailable")
    fingerprint = ssh_sha256_fingerprint(ctx.host_key)
    lines = [
        "Serving local SSH on 127.0.0.1:%s" % (ctx.config.local_port,),
        "Local host key fingerprint: %s" % (fingerprint,),
        "Known-hosts helpers:",
        "  ssh-keygen -R '[127.0.0.1]:%s'" % (ctx.config.local_port,),
        "  ssh-keyscan -p %s 127.0.0.1 >> ~/.ssh/known_hosts"
        % (ctx.config.local_port,),
        "Examples:",
        "  ssh  -o StrictHostKeyChecking=accept-new -p %s anything@127.0.0.1"
        % (ctx.config.local_port,),
        "  sftp -o StrictHostKeyChecking=accept-new -P %s anything@127.0.0.1"
        % (ctx.config.local_port,),
        "  ssh  -o StrictHostKeyChecking=accept-new -p %s -L 15432:localhost:5432 anything@127.0.0.1"
        % (ctx.config.local_port,),
        "  ssh  -o StrictHostKeyChecking=accept-new -p %s -R 18080:localhost:8080 anything@127.0.0.1"
        % (ctx.config.local_port,),
    ]
    logging.info("%s" % ("\n".join(lines),))

def install_signal_handlers(ctx):
    # type: (Runtime) -> None
    handler = SignalStopHandler(ctx)
    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)

def make_thread(target, args, name):
    # type: (ThreadTarget, Tuple, str) -> threading.Thread
    thread = threading.Thread(target=target, args=args, name=name)
    thread.daemon = True
    return thread

def safe_close(channel):
    # type: (Optional[paramiko.Channel]) -> None
    if channel is None:
        return
    try:
        channel.close()
    except Exception:
        logging.info("[cleanup] ignored channel close failure")

def connect_upstream(ctx):
    # type: (Runtime) -> None
    if ctx.auth is None:
        raise RuntimeError("upstream auth not initialized")
    config = ctx.config
    try:
        sock = socket.create_connection((config.host, config.port))
        transport = paramiko.Transport(sock)
        transport.start_client()
        transport.set_keepalive(15)

        auth_kind, auth_value = ctx.auth
        if auth_kind == "password":
            transport.auth_password(config.username, auth_value)
        else:
            transport.auth_publickey(config.username, auth_value)

        if not transport.is_authenticated():
            raise FatalConnectError("upstream authentication failed")

        with ctx.lock:
            ctx.upstream_socket = sock
            ctx.upstream = transport
    except paramiko.AuthenticationException as error:
        raise FatalConnectError(str(error))
    except ValueError as error:
        raise FatalConnectError(str(error))
    except Exception as error:
        raise RetryableConnectError(str(error))

def close_upstream(ctx):
    # type: (Runtime) -> None
    with ctx.lock:
        upstream = ctx.upstream
        upstream_socket = ctx.upstream_socket
        ctx.upstream = None
        ctx.upstream_socket = None
    if upstream is not None:
        try:
            upstream.close()
        except Exception:
            logging.info("[cleanup] ignored upstream transport close failure")
    if upstream_socket is not None:
        try:
            upstream_socket.close()
        except Exception:
            logging.info("[cleanup] ignored upstream socket close failure")

def upstream_is_alive(ctx):
    # type: (Runtime) -> bool
    with ctx.lock:
        upstream = ctx.upstream
    return upstream is not None and upstream.is_active()

def wait_for_connected_upstream(ctx):
    # type: (Runtime) -> paramiko.Transport
    while True:
        if ctx.stop_requested:
            raise RuntimeError("shutting down")
        with ctx.lock:
            upstream = ctx.upstream
            state = ctx.state
        if (
            state == ConnState.CONNECTED
            and upstream is not None
            and upstream.is_active()
        ):
            return upstream
        time.sleep(0.1)

def close_listener(ctx):
    # type: (Runtime) -> None
    listener = ctx.listener
    if listener is not None:
        try:
            listener.close()
        except Exception:
            logging.info("[cleanup] ignored listener close failure")
        ctx.listener = None

def cleanup(ctx):
    # type: (Runtime) -> None
    close_listener(ctx)
    close_upstream(ctx)
    with ctx.lock:
        sessions = list(ctx.sessions.values())
        ctx.sessions.clear()
    for session in sessions:
        close_downstream_session(ctx, session)

def start_accept_thread(ctx):
    # type: (Runtime) -> threading.Thread
    thread = make_thread(accept_loop, (ctx,), "accept-loop")
    thread.start()
    return thread

def accept_loop(ctx):
    # type: (Runtime) -> None
    listener = ctx.listener
    if listener is None:
        raise RuntimeError("listener not initialized")
    while not ctx.stop_requested:
        try:
            client_sock, client_addr = listener.accept()
        except OSError:
            break
        except IOError:
            break
        thread = make_thread(
            handle_downstream_client,
            (ctx, client_sock, client_addr),
            "downstream-client-%s" % (client_addr[1],),
        )
        thread.start()

def handle_downstream_client(ctx, client_sock, client_addr):
    # type: (Runtime, socket.socket, Tuple[str, int]) -> None
    with ctx.lock:
        session_id = ctx.next_session_id
        ctx.next_session_id += 1
    session = DownstreamSession(
        session_id=session_id, client_addr=client_addr, sock=client_sock
    )
    with ctx.lock:
        ctx.sessions[session_id] = session

    try:
        transport = paramiko.Transport(client_sock)
        if ctx.host_key is None:
            raise RuntimeError("local host key not initialized")
        transport.add_server_key(ctx.host_key)
        session.transport = transport
        server = LocalServer(ctx, session)
        transport.start_server(server=server)
        logging.info(
            "[downstream %s] connected from %s:%s"
            % (
                session_id,
                client_addr[0],
                client_addr[1],
            )
        )

        while not ctx.stop_requested and transport.is_active():
            channel = transport.accept()
            if channel is None:
                continue
            thread = make_thread(
                handle_downstream_channel,
                (ctx, session, channel),
                "downstream-channel-%s-%s" % (session_id, channel.chanid),
            )
            thread.start()
    except Exception as error:
        logging.info("[downstream %s] error: %s" % (session_id, error))
    finally:
        close_downstream_session(ctx, session)
        with ctx.lock:
            ctx.sessions.pop(session_id, None)
        logging.info("[downstream %s] closed" % (session_id,))

def close_downstream_session(ctx, session):
    # type: (Runtime, DownstreamSession) -> None
    if session.closed:
        return
    session.closed = True
    for forward in list(session.remote_forwards):
        cancel_remote_forward(ctx, forward)
    session.remote_forwards[:] = []
    for channel in list(session.upstream_channels.values()):
        try:
            channel.close()
        except Exception:
            logging.info("[cleanup] ignored upstream channel close failure")
    session.upstream_channels.clear()
    if session.transport is not None:
        try:
            session.transport.close()
        except Exception:
            logging.info("[cleanup] ignored downstream transport close failure")
        session.transport = None
    try:
        session.sock.close()
    except Exception:
        logging.info("[cleanup] ignored downstream socket close failure")

def handle_downstream_channel(ctx, session, downstream_channel):
    # type: (Runtime, DownstreamSession, paramiko.Channel) -> None
    pending = session.pending.get(downstream_channel.chanid)
    if pending is None:
        downstream_channel.close()
        return

    try:
        pending.ready.wait()

        if pending.kind == "direct-tcpip":
            bridge_direct_tcpip(ctx, downstream_channel, pending)
        elif pending.kind == "session":
            if pending.shell_requested:
                bridge_shell(ctx, session, downstream_channel, pending)
            elif pending.exec_command is not None:
                bridge_exec(ctx, session, downstream_channel, pending)
            elif pending.subsystem == "sftp":
                bridge_subsystem(ctx, session, downstream_channel, pending)
            else:
                downstream_channel.send_stderr(b"unsupported session request\n")
                downstream_channel.send_exit_status(1)
                downstream_channel.close()
        else:
            downstream_channel.close()
    except Exception as error:
        try:
            downstream_channel.send_stderr(
                ("bridge error: %s\n" % (error,)).encode("utf-8")
            )
            downstream_channel.send_exit_status(1)
        except Exception:
            logging.info("[bridge] ignored downstream error reporting failure")
        try:
            downstream_channel.close()
        except Exception:
            logging.info("[bridge] ignored downstream close failure after channel error")
        logging.info(
            "[downstream %s] channel %s error: %s"
            % (
                session.session_id,
                downstream_channel.chanid,
                error,
            )
        )
    finally:
        session.pending.pop(downstream_channel.chanid, None)
        session.upstream_channels.pop(downstream_channel.chanid, None)

def try_activate_remote_forward(ctx, session, forward):
    # type: (Runtime, DownstreamSession, DesiredRemoteForward) -> bool
    if session.closed:
        return False
    if forward.bind_port == 0:
        return False
    try:
        upstream = wait_for_connected_upstream(ctx)
    except Exception as error:
        logging.info(
            "[forward] failed remote forward %s:%s for downstream session %s: %s"
            % (
                forward.bind_host,
                forward.bind_port,
                session.session_id,
                error,
            )
        )
        return False

    def handler(upstream_channel, origin, server):
        # type: (paramiko.Channel, Tuple[str, int], Tuple[str, int]) -> None
        handle_upstream_forwarded_connection(
            ctx, session, forward, upstream_channel, origin, server
        )

    try:
        active_port = upstream.request_port_forward(
            forward.bind_host, forward.bind_port, handler=handler
        )
    except Exception as error:
        logging.info(
            "[forward] remote forward refused %s:%s for downstream session %s: %s"
            % (
                forward.bind_host,
                forward.bind_port,
                session.session_id,
                error,
            )
        )
        return False

    forward.active = True
    forward.active_port = active_port
    logging.info(
        "[forward] active remote %s:%s for downstream session %s"
        % (
            forward.bind_host,
            active_port,
            session.session_id,
        )
    )
    return True

def cancel_remote_forward(ctx, forward):
    # type: (Runtime, DesiredRemoteForward) -> None
    if not forward.active:
        return
    with ctx.lock:
        upstream = ctx.upstream
    if (
        upstream is not None
        and upstream.is_active()
        and forward.active_port is not None
    ):
        try:
            upstream.cancel_port_forward(forward.bind_host, forward.active_port)
        except Exception:
            logging.info(
                "[forward] ignored remote forward cancel failure for %s:%s"
                % (
                    forward.bind_host,
                    forward.active_port,
                )
            )
    forward.active = False
    forward.active_port = None

def replay_remote_forwards(ctx):
    # type: (Runtime) -> None
    with ctx.lock:
        sessions = list(ctx.sessions.values())
    for session in sessions:
        if session.closed:
            continue
        if session.transport is None or not session.transport.is_active():
            continue
        for forward in session.remote_forwards:
            forward.active = False
            forward.active_port = None
            try:
                if not try_activate_remote_forward(ctx, session, forward):
                    logging.info(
                        "[forward] failed to replay %s:%s for session %s"
                        % (
                            forward.bind_host,
                            forward.bind_port,
                            session.session_id,
                        )
                    )
            except Exception as error:
                logging.info(
                    "[forward] failed to replay %s:%s for session %s: %s"
                    % (
                        forward.bind_host,
                        forward.bind_port,
                        session.session_id,
                        error,
                    )
                )

def handle_upstream_forwarded_connection(
    ctx, session, forward, upstream_channel, origin, server
):
    # type: (Runtime, DownstreamSession, DesiredRemoteForward, paramiko.Channel, Tuple[str, int], Tuple[str, int]) -> None
    del ctx
    del forward
    if session.closed or session.transport is None or not session.transport.is_active():
        safe_close(upstream_channel)
        return
    try:
        downstream_channel = session.transport.open_forwarded_tcpip_channel(
            origin, server
        )
    except Exception as error:
        logging.info("[forward] failed to open downstream forwarded channel: %s" % (error,))
        safe_close(upstream_channel)
        return
    thread = make_thread(
        bidirectional_bridge,
        (downstream_channel, upstream_channel),
        "forwarded-bridge-%s" % (session.session_id,),
    )
    thread.start()

def bridge_shell(ctx, session, downstream_channel, pending):
    # type: (Runtime, DownstreamSession, paramiko.Channel, PendingChannel) -> None
    upstream = wait_for_connected_upstream(ctx)
    upstream_channel = upstream.open_session()
    session.upstream_channels[downstream_channel.chanid] = upstream_channel

    if pending.pty is not None:
        upstream_channel.get_pty(
            term=pending.pty["term"],
            width=pending.pty["width"],
            height=pending.pty["height"],
            width_pixels=pending.pty["pixelwidth"],
            height_pixels=pending.pty["pixelheight"],
        )
    upstream_channel.invoke_shell()
    bidirectional_bridge(downstream_channel, upstream_channel)

def bridge_exec(ctx, session, downstream_channel, pending):
    # type: (Runtime, DownstreamSession, paramiko.Channel, PendingChannel) -> None
    upstream = wait_for_connected_upstream(ctx)
    upstream_channel = upstream.open_session()
    session.upstream_channels[downstream_channel.chanid] = upstream_channel
    if pending.pty is not None:
        upstream_channel.get_pty(
            term=pending.pty["term"],
            width=pending.pty["width"],
            height=pending.pty["height"],
            width_pixels=pending.pty["pixelwidth"],
            height_pixels=pending.pty["pixelheight"],
        )
    command_bytes = pending.exec_command
    if command_bytes is None:
        raise RuntimeError("exec command unavailable")
    upstream_channel.exec_command(command_bytes)

    stdout_thread = make_thread(
        pump_stdout, (upstream_channel, downstream_channel), "pump-stdout"
    )
    stderr_thread = make_thread(
        pump_stderr, (upstream_channel, downstream_channel), "pump-stderr"
    )
    stdin_thread = make_thread(
        pump_bytes, (downstream_channel, upstream_channel), "pump-stdin"
    )
    stdout_thread.start()
    stderr_thread.start()
    stdin_thread.start()

    exit_status = upstream_channel.recv_exit_status()
    stdout_thread.join()
    stderr_thread.join()
    stdin_thread.join(1.0)
    try:
        downstream_channel.send_exit_status(exit_status)
    except Exception:
        logging.info("[bridge] ignored downstream exit status delivery failure")
    safe_close(upstream_channel)
    safe_close(downstream_channel)

def bridge_subsystem(ctx, session, downstream_channel, pending):
    # type: (Runtime, DownstreamSession, paramiko.Channel, PendingChannel) -> None
    upstream = wait_for_connected_upstream(ctx)
    upstream_channel = upstream.open_session()
    session.upstream_channels[downstream_channel.chanid] = upstream_channel
    upstream_channel.invoke_subsystem(pending.subsystem)
    bidirectional_bridge(downstream_channel, upstream_channel)

def bridge_direct_tcpip(ctx, downstream_channel, pending):
    # type: (Runtime, paramiko.Channel, PendingChannel) -> None
    upstream = wait_for_connected_upstream(ctx)
    if pending.destination is None:
        raise RuntimeError("direct-tcpip destination unavailable")
    if pending.origin is None:
        raise RuntimeError("direct-tcpip origin unavailable")
    upstream_channel = upstream.open_channel(
        kind="direct-tcpip",
        dest_addr=pending.destination,
        src_addr=pending.origin,
    )
    bidirectional_bridge(downstream_channel, upstream_channel)

def pump_bytes(src, dst):
    # type: (paramiko.Channel, paramiko.Channel) -> None
    try:
        while True:
            data = src.recv(32768)
            if not data:
                try:
                    dst.shutdown_write()
                except Exception:
                    logging.info(
                        "[bridge] ignored shutdown_write failure while draining channel"
                    )
                return
            dst.sendall(data)
    except Exception:
        try:
            dst.shutdown_write()
        except Exception:
            logging.info("[bridge] ignored shutdown_write failure after pump error")

def pump_stdout(src, dst):
    # type: (paramiko.Channel, paramiko.Channel) -> None
    try:
        while True:
            data = src.recv(32768)
            if not data:
                return
            dst.sendall(data)
    except Exception:
        return

def pump_stderr(src, dst):
    # type: (paramiko.Channel, paramiko.Channel) -> None
    try:
        while True:
            data = src.recv_stderr(32768)
            if not data:
                return
            dst.sendall_stderr(data)
    except Exception:
        return

def bidirectional_bridge(left, right):
    # type: (paramiko.Channel, paramiko.Channel) -> None
    left_to_right = make_thread(pump_bytes, (left, right), "bridge-left-to-right")
    right_to_left = make_thread(pump_bytes, (right, left), "bridge-right-to-left")
    left_to_right.start()
    right_to_left.start()
    left_to_right.join()
    right_to_left.join()
    safe_close(left)
    safe_close(right)

def app(ctx):
    # type: (Runtime) -> int
    install_signal_handlers(ctx)

    while True:
        if ctx.state == ConnState.STARTING:
            ctx.auth = load_auth(ctx.config)
            ctx.host_key = load_local_host_key(ctx.config)
            ctx.listener = bind_local_listener(ctx.config.local_port)
            ctx.accept_thread = start_accept_thread(ctx)
            write_banner(ctx)
            set_state(ctx, ConnState.CONNECTING, "local listener ready")

        elif ctx.state == ConnState.CONNECTING:
            try:
                connect_upstream(ctx)
                ctx.backoff = 1
                logging.info(
                    "Connected upstream to %s:%s as %s"
                    % (
                        ctx.config.host,
                        ctx.config.port,
                        ctx.config.username,
                    )
                )
                set_state(ctx, ConnState.CONNECTED, "initial upstream connect ok")
            except RetryableConnectError as error:
                ctx.last_error = error
                set_state(
                    ctx, ConnState.RECONNECT_WAIT, "connect failed: %s" % (error,)
                )

        elif ctx.state == ConnState.CONNECTED:
            if ctx.stop_requested:
                set_state(ctx, ConnState.SHUTTING_DOWN, "shutdown requested")
            elif not upstream_is_alive(ctx):
                close_upstream(ctx)
                set_state(ctx, ConnState.RECONNECT_WAIT, "upstream lost")
            else:
                time.sleep(0.2)

        elif ctx.state == ConnState.RECONNECT_WAIT:
            if ctx.stop_requested:
                set_state(
                    ctx,
                    ConnState.SHUTTING_DOWN,
                    "shutdown requested during reconnect wait",
                )
            else:
                delay = ctx.backoff
                logging.info("[reconnect] waiting %ss" % (delay,))
                time.sleep(delay)
                ctx.backoff = min(ctx.backoff * 2, ctx.max_backoff)
                set_state(ctx, ConnState.RECONNECTING, "retrying upstream connect")

        elif ctx.state == ConnState.RECONNECTING:
            try:
                connect_upstream(ctx)
                ctx.backoff = 1
                replay_remote_forwards(ctx)
                logging.info(
                    "Reconnected upstream to %s:%s as %s"
                    % (
                        ctx.config.host,
                        ctx.config.port,
                        ctx.config.username,
                    )
                )
                set_state(ctx, ConnState.CONNECTED, "upstream reconnected")
            except RetryableConnectError as error:
                ctx.last_error = error
                set_state(
                    ctx, ConnState.RECONNECT_WAIT, "reconnect failed: %s" % (error,)
                )

        elif ctx.state == ConnState.SHUTTING_DOWN:
            cleanup(ctx)
            set_state(ctx, ConnState.STOPPED, "cleanup complete")

        elif ctx.state == ConnState.STOPPED:
            return 0

        else:
            raise RuntimeError("Unhandled state: %s" % (ctx.state,))

def parse_args():
    # type: () -> Config
    parser = argparse.ArgumentParser(
        description="Ephemeral localhost SSH shim over one upstream SSH connection"
    )
    parser.add_argument(
        "--host",
        required=True,
        help="upstream SSH server host name or address"
    )
    parser.add_argument(
        "--port",
        required=False,
        type=int,
        default=22,
        help="upstream SSH server port number"
    )
    parser.add_argument("--username", required=True, help="upstream SSH username")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument(
        "--password",
        help="password used to connect to the remote SSH server"
    )
    auth.add_argument(
        "--rsa-key",
        help="user-facing path to the RSA private key used for the upstream SSH server",
    )
    auth.add_argument(
        "--ed25519-key",
        help="user-facing path to the Ed25519 private key used for the upstream SSH server",
    )
    parser.add_argument(
        "--local-port",
        required=True,
        type=int,
        help="localhost TCP port on which the shim listens",
    )
    parser.add_argument(
        "--local-rsa-key",
        help="user-facing path to the local shim host RSA key; default lookup: %s"
        % (DEFAULT_LOCAL_RSA_KEY_USER_PATH,),
    )
    parser.add_argument(
        "--local-ed25519-key",
        help="user-facing path to the local shim host Ed25519 key; default lookup: %s"
        % (DEFAULT_LOCAL_ED25519_KEY_USER_PATH,),
    )
    args = parser.parse_args()

    return Config(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        rsa_key_path=args.rsa_key,
        ed25519_key_path=args.ed25519_key,
        local_port=args.local_port,
        local_rsa_key_path=args.local_rsa_key,
        local_ed25519_key_path=args.local_ed25519_key,
    )

def main():
    # type: () -> int
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = parse_args()
    ctx = Runtime(config=config)
    return app(ctx)

if __name__ == "__main__":
    main()
