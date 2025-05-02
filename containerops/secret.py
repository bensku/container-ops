from abc import ABC, abstractmethod
from dataclasses import dataclass


class SecretStore(ABC):
    @abstractmethod
    def get_secret(self, name: str) -> bytes:
        pass

    @abstractmethod
    def put_secret(self, name: str, value: bytes):
        pass


class SecretNotFoundError(Exception):
    pass


class LocalSecretStore(SecretStore):
    def __init__(self, path: str):
        self.path = path

    def get_secret(self, name: str) -> bytes:
        try:
            with open(f'{self.path}/{name}', 'rb') as f:
                return f.read()
        except FileNotFoundError:
            raise SecretNotFoundError()

    def put_secret(self, name: str, value: bytes):
        with open(f'{self.path}/{name}', 'wb') as f:
            f.write(value)