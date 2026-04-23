SSH connection reuse through a temporary localhost SSH endpoint.

It opens one upstream SSH connection to a remote host, exposes a local SSH server on `127.0.0.1:<port>`, and lets you keep using standard tools like `ssh`, `sftp`, `scp`, `ssh -L`, and `ssh -R` against that local endpoint.

The goal is simple: **verify once, authenticate once, reuse everywhere**.

## Installation

```bash
pip install sshshim
```

## Usage

`sshshim` is a lightweight foreground tool. It keeps one real upstream SSH connection alive, reconnects automatically after transient network drops, and replays requested remote forwards after reconnect.

### Why use it

SSH workflows often reconnect more than necessary:

- `ssh` for an interactive shell
- `sftp` or `scp` for file transfer
- `ssh -L` for local port forwarding
- `ssh -R` for reverse port forwarding

Even when all of those target the same machine, they often trigger separate connection setup, repeated authentication, and repeated host verification prompts. `sshshim` changes the workflow: instead of treating SSH as command-first, it treats it as connection-first.

### Basic idea

1. Start `sshshim` once.
2. It connects upstream to the real remote SSH server.
3. It serves a temporary localhost SSH endpoint.
4. Use your existing SSH tools against localhost.

### Generate a localhost host key

First, generate a dedicated localhost host key for the shim if you do not already have one.

Ed25519:

```bash
ssh-keygen -t ed25519 -N ''
```

RSA:

```bash
ssh-keygen -t rsa -b 4096 -N ''
```

If you save the key at the default OpenSSH locations `~/.ssh/id_ed25519` or `~/.ssh/id_rsa`, `sshshim` will look there automatically.

### Start the shim

Using an Ed25519 key for the upstream remote host:

```bash
python sshshim.py \
  --host prod.example.com \
  --port 22 \
  --username ubuntu \
  --ed25519-key /path/to/your/remote_server_ed25519 \
  --local-port 2222
```

If the localhost shim host key is stored somewhere else, pass it explicitly:

```bash
python sshshim.py \
  --host prod.example.com \
  --port 22 \
  --username ubuntu \
  --ed25519-key /path/to/your/remote_server_ed25519 \
  --local-port 2222 \
  --local-ed25519-key /path/to/your/local_shim_ed25519
```

You can also authenticate upstream with a password:

```bash
python sshshim.py \
  --host prod.example.com \
  --port 22 \
  --username ubuntu \
  --password your-password \
  --local-port 2222
```

Or with an RSA private key:

```bash
python sshshim.py \
  --host prod.example.com \
  --port 22 \
  --username ubuntu \
  --rsa-key /path/to/your/remote_server_rsa \
  --local-port 2222
```

### Use your normal SSH tools against localhost

Interactive shell:

```bash
ssh -p 2222 anything@127.0.0.1
```

SFTP:

```bash
sftp -P 2222 anything@127.0.0.1
```

SCP:

```bash
scp -P 2222 file.txt anything@127.0.0.1:/tmp/
```

Local forwarding:

```bash
ssh -p 2222 -L 15432:localhost:5432 anything@127.0.0.1
```

Reverse forwarding:

```bash
ssh -p 2222 -R 18080:localhost:8080 anything@127.0.0.1
```

### What the prototype does

- opens one upstream SSH connection
- exposes one temporary localhost SSH endpoint
- supports interactive shell sessions
- supports remote command execution
- supports `sftp`/`scp`-style file transfer through standard SSH clients
- supports `ssh -L` local forwarding
- supports `ssh -R` remote forwarding
- reconnects automatically after transient upstream failures
- replays requested remote forwards after reconnect

### Command-line interface

`sshshim.py` accepts:

- `--host`
- `--port`
- `--username`
- exactly one of:
  - `--password`
  - `--rsa-key`
  - `--ed25519-key`
- `--local-port`
- optionally:
  - `--local-rsa-key`
  - `--local-ed25519-key`

### Quick smoke-check workflow

In one terminal, start the shim:

```bash
python sshshim.py \
  --host prod.example.com \
  --port 22 \
  --username ubuntu \
  --ed25519-key /path/to/your/remote_server_ed25519 \
  --local-port 2222
```

In another terminal, verify that standard tools reuse the local endpoint:

```bash
ssh -p 2222 anything@127.0.0.1 uname -a
sftp -P 2222 anything@127.0.0.1
ssh -p 2222 -L 15432:localhost:5432 anything@127.0.0.1
ssh -p 2222 -R 18080:localhost:8080 anything@127.0.0.1
```

Conceptually, this is tmux-like multiplexing for SSH capabilities, but exposed through standard SSH clients instead of a new portal, daemon, or browser UI.

## Motivation

Modern work is mobile and distributed: cloud VMs, laptops, and edge devices. SSH is the universal way to reach them, but even with a single remote relationship, it often still requires too many separate SSH connections. Shell access, file transfer, and port forwarding may all target the same machine, yet in practice, they often trigger separate setups, separate authentication, and separate friction.

Common pain points include:

- reconnecting for `ssh`, `scp`, `sftp`, and forwarding workflows
- repeated host verification and password prompts
- brittle private key permission requirements

Some existing solutions improve parts of the experience, but still miss the mark for a lightweight local-first workflow:

- OpenSSH multiplexing helps reuse connections, but the UX is still fragmented across commands and flags
- Apache Guacamole offers a polished browser-based experience, but it is much heavier than a simple local tool
- Teleport appears closer to an infrastructure platform or control plane than a minimal localhost SSH helper

If improving SSH requires deploying a web app, a daemon, a proxy, or a gateway, that already adds friction for many users.

The motivation behind `sshshim` is a simpler model: SSH should be connection-first, not command-first. A user should be able to:

1. Verify a host once
2. Authenticate once
3. Reuse that connection everywhere

That is why `sshshim` is designed as a small foreground program with a single abstraction: a trusted upstream SSH connection exposed as a temporary localhost SSH endpoint that standard tools can reuse immediately.

## Contributing

Contributions are welcome! Please submit pull requests or open issues on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
