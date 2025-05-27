# Container operations for Pyinfra
Container-ops is a Python library that makes deploying containerized
applications with [Pyinfra](https://pyinfra.com/) easier. If you can access
it over SSH, you can deploy pods, containers and overlay networks between
them.

When you're not deploying, the created systems do not depend on Container-ops.
In other words, this is not a container *orchestrator*, it is merely a way to
deploy them that is hopefully less cursed than the pile of docker-compose hacks
I'm trying to migrate my homelab away from.

To be more precise, in its current (very early!) version, Container-ops can:
* Deploy OCI containers into pods with Podman Quadlets (recent Podman and Systemd needed)
* Create overlay networks based on [Nebula](https://github.com/slackhq/nebula)
  between your servers and containers
* Serve authorative DNS for your domains with Knot. Not that I'd recommend it, but...

In case it was not abundantly clear yet: **Container-ops is alpha quality software**
This might be fine for your homelab, provided that you don't mind rebuilding it when
the API inevitably breaks in future. However, for more serious usage or those
who don't particularly enjoy spending time inside `journalctl -u`, it would
be best to wait.

## Installation
If the above did not scare your, container-ops is in fact available on
[PyPi](https://pypi.org/project/container-ops/). You can install it with
whatever Python dependency manager you prefer, though I highly recommend
using a virtual environment. With `uv`, just add it to your project:

```
uv add container-ops
```

## Usage
Container-ops is a library that provides additional Pyinfra
[operations](https://docs.pyinfra.com/en/3.x/using-operations.html).
Before using it, make sure you understand what "operation" means in this
context. Pyinfra's own [documentation](https://docs.pyinfra.com/en/3.x/index.html)
is good and highly recommended reading!

Currently, there are unfortunately no good tutorials for container-ops. However,
the [integration tests](./tests/) also serve as passable examples. For getting
the hang of it, go through them in this order:
1. [tests/podman.py](./tests/podman.py) - how to deploy pods and containers
2. All the `nebula_*.py` scripts - how to create overlay networks
3. [tests/knot.py](./tests/knot.py) - if you want to deploy authorative DNS

Beyond this, almost all operations have thorough docstrings *and* types
available. If you're wondering what a particular parameter is for, those
will (hopefully) provide answers.

But then again, alpha quality software! You have been warned.

## License
MIT. It is free (as in freedom), but if it breaks, you can keep the pieces.