from pyinfra.api import operation

from containerops.secret import SecretStore


# TODO port stuff from Pigeon


@operation()
def ca(secrets: SecretStore):
    pass


def _container():
    pass